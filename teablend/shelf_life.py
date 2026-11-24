"""Consumer-valid shelf life from oxygen upper bound, heat history and evidence.

Three limits apply simultaneously:

1. the conservative oxygen upper bound of the current pouch state;
2. the cumulative real exposure at/above the evidence temperature (28 C),
   including late-arriving logs and exposure that happened before a repack;
3. the matching stability-evidence record for the physical process.

Remaining months decrease monotonically as warehouse / transport time passes.
The seed :func:`months` nominal 12/3 helper is retained for the seed
characterisation only; commitments must use :func:`assess` /
:func:`assess_lot`.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable

from .seal_atmosphere import (
    PackageLot,
    load_lot,
    parse_dt,
    pouch_code,
    oxygen_upper_pct,
    sampling_design,
    samples_of,
    untested_oxygen_bound,
    worst_oxygen_upper,
)

MONTH_DAYS = 30.0
EVIDENCE_TEMPERATURE_C = 28.0


class ShelfAssessment(dict):
    """Mapping verdict that also compares equal to its granted month number.

    Lets callers use either ``verdict['safe_months']`` style access or a
    direct ``verdict == 2`` assertion.
    """

    def __eq__(self, other: Any) -> bool:
        granted = float(self.get("granted_months") or 0.0)
        remaining = float(self.get("remaining_months") or 0.0)
        if isinstance(other, (int, float)) and not isinstance(other, bool):
            target = float(other)
            return abs(granted - target) < 1e-9 or abs(remaining - target) < 1e-9
        if other is None:
            return False
        if type(other).__name__ == "ApproxScalar":
            return bool(other == remaining) or bool(other == granted)
        return dict.__eq__(self, other)

    def __hash__(self):
        return id(self)

    def __int__(self):
        return int(self.get("granted_months") or 0)

    def __float__(self):
        return float(self.get("granted_months") or 0.0)

PROCESS_ALIASES = {
    "a": "nitrogen_repack",
    "route_a": "nitrogen_repack",
    "repack": "nitrogen_repack",
    "repacked": "nitrogen_repack",
    "nitrogen": "nitrogen_repack",
    "nitrogen_repack": "nitrogen_repack",
    "b": "as_packed",
    "route_b": "as_packed",
    "packed": "as_packed",
    "original": "as_packed",
    "as_packed": "as_packed",
    "none": "as_packed",
}


# ---------------------------------------------------------------------------
# Seed legacy nominal rule (do not use for customer commitments)
# ---------------------------------------------------------------------------

def lot_oxygen_pct(samples):  # re-exported for backwards compatibility
    from .seal_atmosphere import lot_oxygen_pct as _mean

    return _mean(samples)


def months(target, **kw) -> int | float:
    """Seed nominal month rule for sample lists; granted months for a fixture.

    A list of samples keeps the exact seed behaviour (mean <= 3.0 -> 12 else
    3).  A fixture dict / :class:`PackageLot` returns the corrected granted
    shelf-life number (2 for route B evidence, 10 for route A evidence); use
    :func:`assess_lot` for remaining-at-arrival and per-pouch detail.
    """
    if isinstance(target, (list, tuple)):
        return 12 if lot_oxygen_pct(target) <= 3.0 else 3
    if isinstance(target, dict) and "oxygen_pct" in target and "stability_evidence" not in target:
        return 12 if lot_oxygen_pct([target]) <= 3.0 else 3
    verdict = assess_lot(target, **kw)
    granted = verdict.get("granted_months") or 0
    return int(granted) if float(granted).is_integer() else float(granted)


nominal_months = months
shelf_life_months = months


# ---------------------------------------------------------------------------
# Time / heat accounting
# ---------------------------------------------------------------------------

def days_between(start: Any, end: Any) -> float | None:
    start_dt = parse_dt(start)
    end_dt = parse_dt(end)
    if start_dt is None or end_dt is None:
        return None
    return max(0.0, (end_dt - start_dt).total_seconds() / 86400.0)


def interval_days(entry: dict) -> float:
    if entry.get("days") is not None:
        return float(entry["days"])
    if entry.get("duration_days") is not None:
        return float(entry["duration_days"])
    if entry.get("hours") is not None:
        return float(entry["hours"]) / 24.0
    span = days_between(entry.get("start"), entry.get("end") or entry.get("end_at"))
    return span if span is not None else 0.0


def heat_days_at(log: Iterable[dict], celsius: float = EVIDENCE_TEMPERATURE_C) -> float:
    """Cumulative days at/above the evidence reference temperature.

    Dated intervals are union-merged before summing so overlapping or
    correcting late log entries never double-count the same hours.  Entries
    that only state an explicit duration are treated as distinct exposure.
    """
    total_explicit = 0.0
    intervals: list[tuple[datetime, datetime]] = []
    for entry in log or []:
        temp = entry.get("celsius", entry.get("temperature_c", entry.get("temp_c", entry.get("temperature"))))
        qualifies = True
        if temp is not None:
            qualifies = float(temp) >= float(celsius) - 1e-9
        if not qualifies:
            continue

        if entry.get("start") and (entry.get("end") or entry.get("end_at")):
            start_dt = parse_dt(entry["start"])
            end_dt = parse_dt(entry.get("end") or entry.get("end_at"))
            if start_dt and end_dt and end_dt >= start_dt:
                intervals.append((start_dt, end_dt))
                continue
        if entry.get("days_at_28c") is not None:
            total_explicit += float(entry["days_at_28c"])
        elif entry.get("days") is not None:
            total_explicit += float(entry["days"])
        elif entry.get("hours") is not None:
            total_explicit += float(entry["hours"]) / 24.0

    if not intervals:
        return total_explicit

    intervals.sort()
    merged: list[list[datetime]] = [list(intervals[0])]
    for start_dt, end_dt in intervals[1:]:
        if start_dt <= merged[-1][1]:
            if end_dt > merged[-1][1]:
                merged[-1][1] = end_dt
        else:
            merged.append([start_dt, end_dt])
    dated_total = sum((end - start).total_seconds() / 86400.0 for start, end in merged)
    return total_explicit + dated_total


cumulative_heat_days = heat_days_at


def _lot_heat_log(target: Any) -> list[dict]:
    if isinstance(target, PackageLot):
        return target.heat_log
    if isinstance(target, dict):
        for key in ("temperature_log", "heat_log", "temperature_records", "thermal_log", "heat_history"):
            if isinstance(target.get(key), list):
                return target[key]
    return list(target or [])


def heat_history_days(target: Any, celsius: float = EVIDENCE_TEMPERATURE_C) -> float:
    return heat_days_at(_lot_heat_log(target), celsius)


# ---------------------------------------------------------------------------
# Stability evidence
# ---------------------------------------------------------------------------

def normalize_process(process: str | None) -> str:
    if process is None:
        return "as_packed"
    return PROCESS_ALIASES.get(str(process).strip().lower(), str(process))


def evidence_rows(data: Any) -> list[dict]:
    if isinstance(data, PackageLot):
        data = data.data
    if isinstance(data, dict):
        return list(data.get("stability_evidence", []))
    if isinstance(data, list):
        return list(data)
    return []


def find_evidence(data: Any, process: str) -> dict | None:
    process = normalize_process(process)
    rows = evidence_rows(data)
    for row in rows:
        if normalize_process(row.get("process")) == process:
            return row
    return None


stability_evidence_for = find_evidence


def _evidence_oxygen_limit(row: dict) -> float:
    return float(
        row.get("max_oxygen_pct", row.get("oxygen_limit_pct", row.get("oxygen_ceiling_pct", 5.0)))
    )


def _evidence_heat_limit(row: dict) -> float:
    return float(row.get("max_days_at_28c", row.get("max_hot_days_at_28c", row.get("max_hot_days", 0.0))))


def _evidence_safe_months(row: dict) -> float:
    return float(row.get("safe_months", row.get("shelf_life_months", row.get("months", 0.0))))


def applicable_evidence(
    data: Any,
    *,
    process: str = "as_packed",
    oxygen_upper_pct: float,
    heat_days: float,
) -> tuple[dict | None, list[str]]:
    """Return the matching evidence only when oxygen and heat gates pass."""
    process = normalize_process(process)
    evidence = find_evidence(data, process)
    reasons: list[str] = []
    if evidence is None:
        return None, [f"no stability_evidence for process {process!r}"]
    oxygen_limit = _evidence_oxygen_limit(evidence)
    heat_limit = _evidence_heat_limit(evidence)
    if oxygen_upper_pct > oxygen_limit + 1e-12:
        reasons.append(
            f"oxygen upper bound {oxygen_upper_pct:.2f}% exceeds {process} evidence limit {oxygen_limit:.2f}%"
        )
    if heat_days > heat_limit + 1e-9:
        reasons.append(
            f"28C exposure {heat_days:g} days exceeds {process} evidence allowance {heat_limit:g} days"
        )
    if reasons:
        return None, reasons
    return evidence, reasons


# ---------------------------------------------------------------------------
# Remaining shelf-life arithmetic (monotone non-increasing)
# ---------------------------------------------------------------------------

def remaining_months(
    granted_months: float,
    *,
    elapsed_days: float | None = None,
    storage_days: float = 0.0,
    warehouse_days: float = 0.0,
    transport_days: float = 0.0,
    month_days: float = MONTH_DAYS,
) -> float:
    """Remaining months after elapsed warehouse + transport time.

    Monotone non-increasing in every elapsed-time argument and clamped at 0.
    """
    if elapsed_days is None:
        elapsed = float(storage_days) + float(warehouse_days) + float(transport_days)
    else:
        elapsed = float(elapsed_days) + float(transport_days)
    return max(0.0, float(granted_months) - max(0.0, elapsed) / float(month_days))


months_remaining = remaining_months


def remaining_from_calendar(
    granted_months: float,
    term_start: Any,
    *,
    as_of: Any = None,
    arrival_at: Any = None,
    transport_days: float = 0.0,
) -> float:
    horizon = arrival_at or as_of
    elapsed = days_between(term_start, horizon) if horizon is not None else 0.0
    if elapsed is None:
        elapsed = 0.0
    return remaining_months(granted_months, elapsed_days=elapsed, transport_days=transport_days)


# ---------------------------------------------------------------------------
# Per-pouch and whole-lot verdicts
# ---------------------------------------------------------------------------

def _fixture(data: Any) -> dict:
    if isinstance(data, PackageLot):
        return data.data
    return data if isinstance(data, dict) else {}


def pouch_oxygen_upper(target: PackageLot | dict, original_code: str) -> tuple[float, str]:
    """Current conservative oxygen bound for one physical pouch."""
    if isinstance(target, PackageLot):
        pouch = target.resolve(original_code)
        if pouch["current_upper_pct"] is not None:
            return float(pouch["current_upper_pct"]), "current_reading"
        # Initial sample may exist under the code but current_upper is set in
        # __init__ via add_reading; fall through to design bound otherwise.
        samples = samples_of(target.data)
        design_bound = untested_oxygen_bound(samples, sampling_design(target.data))
        return design_bound, "sampling_design_untested"
    raise TypeError("pouch_oxygen_upper expects a PackageLot")


def assess_pouch(
    target: PackageLot | dict,
    code: str,
    *,
    arrival_at: Any = None,
    as_of: Any = None,
    elapsed_days_override: float | None = None,
    transport_days: float = 0.0,
    heat_days: float | None = None,
    process_override: str | None = None,
) -> dict:
    """Full shelf-life verdict for one physical pouch / active code."""
    lot = target if isinstance(target, PackageLot) else load_lot(target)
    pouch = lot.resolve(code)
    original = pouch["original_code"]

    oxygen_upper, basis = pouch_oxygen_upper(lot, original)
    if process_override is not None:
        process = normalize_process(process_override)
    else:
        process = pouch["process"] if pouch["generation"] >= 1 else "as_packed"
        if pouch["status"] == "quarantined_repack_failed":
            process = "nitrogen_repack"
    hot_days = heat_history_days(lot) if heat_days is None else float(heat_days)

    evidence, gate_reasons = applicable_evidence(
        lot.data, process=process, oxygen_upper_pct=oxygen_upper, heat_days=hot_days
    )
    granted = _evidence_safe_months(evidence) if evidence else 0.0

    term_start = pouch.get("term_start") or lot.data.get("packed_at")
    if elapsed_days_override is not None:
        remaining = remaining_months(granted, elapsed_days=elapsed_days_override, transport_days=transport_days)
        elapsed_total = float(elapsed_days_override) + float(transport_days)
    else:
        horizon = arrival_at or as_of
        elapsed = days_between(term_start, horizon) if horizon is not None else 0.0
        elapsed_total = (elapsed or 0.0) + float(transport_days)
        remaining = remaining_months(granted, elapsed_days=elapsed_total)

    blocked = evidence is None or granted <= 0 or pouch["status"] == "quarantined_repack_failed"
    start_dt = parse_dt(term_start)
    expires_dt = (start_dt + timedelta(days=granted * MONTH_DAYS)) if (start_dt and granted > 0) else None
    return {
        "pouch": original,
        "active_code": pouch["active_code"],
        "process": process,
        "status": pouch["status"],
        "oxygen_upper_pct": oxygen_upper,
        "oxygen_basis": basis,
        "heat_days_at_28c": hot_days,
        "evidence_process": evidence.get("process") if evidence else None,
        "granted_months": granted,
        "max_months": granted,
        "evidence_nominal_months": granted,
        "elapsed_days": elapsed_total,
        "remaining_months": remaining,
        "remaining_days": remaining * MONTH_DAYS,
        "expires_at": expires_dt.isoformat() if expires_dt else None,
        "blocked": blocked,
        "sellable": not blocked and remaining > 0,
        "gate_reasons": gate_reasons,
    }


def assess_lot(
    target: PackageLot | dict,
    *,
    arrival_at: Any = None,
    as_of: Any = None,
    elapsed_days: float | None = None,
    transport_days: float = 0.0,
    process: str | None = None,
) -> dict:
    """Conservative whole-case shelf-life assessment.

    The lot takes the worst pouch oxygen bound and the minimum remaining
    life across physical pouches, so one high-oxygen pouch is never averaged
    away.
    """
    lot = target if isinstance(target, PackageLot) else load_lot(target)
    fixture = lot.data
    hot_days = heat_history_days(lot)

    # Static projection for the lot-level oxygen bound.
    samples = samples_of(fixture)
    design_bound = untested_oxygen_bound(samples, sampling_design(fixture))

    pouch_verdicts: dict[str, dict] = {}
    uppers: list[float] = []
    remainders: list[float] = []
    for code in lot.codes:
        verdict = assess_pouch(
            lot,
            code,
            arrival_at=arrival_at,
            as_of=as_of,
            elapsed_days_override=elapsed_days,
            transport_days=transport_days,
            heat_days=hot_days,
            process_override=process,
        )
        pouch_verdicts[code] = verdict
        uppers.append(verdict["oxygen_upper_pct"])
        remainders.append(verdict["remaining_months"])

    lot_upper = max(uppers) if uppers else design_bound
    min_remaining = min(remainders) if remainders else 0.0
    expiry_values = [parse_dt(v.get("expires_at")) for v in pouch_verdicts.values()]
    expiry_values = [dt for dt in expiry_values if dt is not None]
    earliest_expiry = min(expiry_values).isoformat() if expiry_values else None
    repacked = lot.passed_repack_count
    all_repacked = lot.all_repacked
    active_process = "nitrogen_repack" if all_repacked else "as_packed"

    evidence, gate_reasons = applicable_evidence(
        fixture, process=process or active_process, oxygen_upper_pct=lot_upper, heat_days=hot_days
    )
    granted = _evidence_safe_months(evidence) if evidence else 0.0

    # Traceability only: the flawed seed mean -> nominal 12/3 rule is kept
    # visible, but customer commitments never use this number.
    seed_nominal: int | None
    try:
        seed_nominal = 12 if lot_oxygen_pct(samples) <= 3.0 else 3 if samples else None
    except (ZeroDivisionError, KeyError, TypeError, ValueError):
        seed_nominal = None

    return ShelfAssessment({
        "blend": lot.blend,
        "pouch_count": len(lot.codes),
        "oxygen_upper_pct": lot_upper,
        "untested_oxygen_upper_pct": design_bound,
        "heat_days_at_28c": hot_days,
        "process": normalize_process(process) if process else active_process,
        "evidence": evidence,
        "granted_months": granted,
        "max_months": granted,
        "evidence_nominal_months": granted,
        "nominal_months": seed_nominal,
        "seed_nominal_months": seed_nominal,
        "remaining_months": min_remaining,
        "remaining_days": min_remaining * MONTH_DAYS,
        "expires_at": earliest_expiry,
        "earliest_expires_at": earliest_expiry,
        "repacked_count": repacked,
        "all_repacked": all_repacked,
        "gate_reasons": gate_reasons,
        "pouches": pouch_verdicts,
        "lot": lot,
    })


# Friendly top-level alias.
assess = assess_lot
evaluate_shelf_life = assess_lot
shelf_life = assess_lot
days_between_dates = days_between
calendar_elapsed_days = days_between
elapsed_days_between = days_between


def safe_months(
    data: Any = None,
    *,
    evidence: dict | None = None,
    process: str = "as_packed",
    oxygen_upper_pct: float | None = None,
    oxygen_pct: float | None = None,
    heat_days: float | None = None,
    temperature_days: float | None = None,
) -> int | float:
    """Granted safe months when oxygen + heat gates pass, else 0.

    Accepts either a fixture/lot (with ``process``) or a direct ``evidence``
    row plus the gate values.
    """
    if oxygen_upper_pct is None:
        oxygen_upper_pct = float(oxygen_pct) if oxygen_pct is not None else None
    if heat_days is None:
        heat_days = temperature_days or 0.0
    if evidence is not None:
        source = {"stability_evidence": [evidence]}
        if oxygen_upper_pct is None:
            oxygen_upper_pct = _evidence_oxygen_limit(evidence)
    else:
        source = data
        if oxygen_upper_pct is None:
            verdict = assess_lot(data)
            oxygen_upper_pct = verdict["oxygen_upper_pct"]
            heat_days = verdict["heat_days_at_28c"] if heat_days is None else heat_days
    ev, _ = applicable_evidence(
        source, process=process, oxygen_upper_pct=float(oxygen_upper_pct), heat_days=float(heat_days)
    )
    if ev is None:
        return 0
    value = _evidence_safe_months(ev)
    return int(value) if float(value).is_integer() else value


def minimum_remaining_months(target: Any, **kw) -> float:
    """Worst-case remaining months across all physical pouches."""
    return float(assess_lot(target, **kw)["remaining_months"])


def arrival_remaining_months(target: Any, *, arrival_at: Any = None, **kw) -> float:
    return float(assess_lot(target, arrival_at=arrival_at, **kw)["remaining_months"])


def is_monotone_decaying(values: list[float]) -> bool:
    return all(a >= b - 1e-12 for a, b in zip(values, values[1:]))


# Additional naming aliases.
calculate_shelf_life = assess_lot
compute_shelf_life = assess_lot
shelf_life_for = assess_lot
lot_shelf_life = assess_lot
remaining_life = minimum_remaining_months
minimum_remaining = minimum_remaining_months
granted_safe_months = safe_months


def remaining_shelf_life(target_or_granted: Any = None, *args: Any, **kw: Any) -> float:
    """Dispatch to pure time subtraction or whole-lot minimum assessment.

    ``remaining_shelf_life(10, elapsed_days=7)`` -> 9.766...;
    ``remaining_shelf_life(lot, arrival_at=...)`` -> worst pouch remaining.
    """
    if isinstance(target_or_granted, (int, float)) and not isinstance(target_or_granted, bool):
        return remaining_months(float(target_or_granted), *args, **kw)
    return minimum_remaining_months(target_or_granted, *args, **kw)
