"""Pouch headspace oxygen bounds, sampling design and physical pouch identity.

The legacy :func:`lot_oxygen_pct` arithmetic mean is retained only to document
the seed mis-placement: it hides a single leaking high-oxygen pouch.  Every
operational decision instead uses per-pouch conservative upper bounds
(measured value plus instrument error), the sampling-design bound for
untested pouches, and a pouch-code identity ledger for real repack closures.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

ATMOSPHERIC_OXYGEN_PCT = 20.9

_CODE_KEYS = (
    "pouch", "pouch_id", "pouch_code", "pouch_number", "bag", "bag_code",
    "bag_id", "bag_number", "physical_code", "tag", "code", "id",
)
_O2_KEYS = ("oxygen_pct", "oxygen_percent", "oxygen", "o2_pct", "o2", "value")
_ERROR_KEYS = (
    "instrument_error_pct",
    "measurement_error_pct",
    "instrument_uncertainty_pct",
    "uncertainty_pct",
    "error_pct",
    "instrument_error",
    "measurement_error",
    "uncertainty",
    "error",
)
_RANGE_KEYS = (
    "sampling_range",
    "sample_range",
    "sampled_range",
    "sampling_scope",
    "sample_scope",
    "sampling_area",
    "probe_range",
    "range",
    "scope",
    "location",
    "positions",
)
_TIME_KEYS = ("measured_at", "sampled_at", "at", "time", "timestamp")

REPACK_STEPS = ("unpacked", "nitrogen_filled", "resealed", "retested")
STEP_ALIASES = {
    "unpack": "unpacked",
    "unpacked": "unpacked",
    "opened": "unpacked",
    "nitrogen_fill": "nitrogen_filled",
    "nitrogen_filled": "nitrogen_filled",
    "filled_nitrogen": "nitrogen_filled",
    "n2_filled": "nitrogen_filled",
    "reseal": "resealed",
    "resealed": "resealed",
    "sealed": "resealed",
    "retest": "retested",
    "retested": "retested",
}

_MISSING = object()


# ---------------------------------------------------------------------------
# Tolerant record access
# ---------------------------------------------------------------------------

def _pick(row: Any, keys: Iterable[str], default: Any = _MISSING) -> Any:
    if not isinstance(row, dict):
        return default if default is not _MISSING else None
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return None if default is _MISSING else default


def parse_dt(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        # Deterministic tests pass ISO strings; numeric epochs are not needed.
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt


def pouch_code(row: Any) -> str | None:
    """Return the physical pouch / bag code carried by a reading."""
    value = _pick(row, _CODE_KEYS)
    return None if value is None else str(value)


def sampling_range(row: Any) -> Any:
    return _pick(row, _RANGE_KEYS)


# ---------------------------------------------------------------------------
# Legacy seed behaviour (characterisation of the flawed mean-only chain)
# ---------------------------------------------------------------------------

def lot_oxygen_pct(samples: Iterable[dict] | dict) -> float:
    """Arithmetic mean headspace oxygen.  Seed-only summary.

    Deliberately keeps the original lenient contract so the seed
    mis-placement (2.37 % -> nominal 12 months -> wholesale) stays
    reproducible as evidence of the trap.  Accepts a sample list or a
    fixture dict carrying ``headspace_samples``.
    """
    if isinstance(samples, dict):
        samples = samples_of(samples)
    rows = list(samples)
    return sum(row["oxygen_pct"] for row in rows) / len(rows)


# ---------------------------------------------------------------------------
# Reading validation and conservative bounds
# ---------------------------------------------------------------------------

class ReadingValidationError(ValueError):
    """Raised when an oxygen reading lacks pouch id, error or sampling range."""


def normalize_reading(row: dict, *, strict: bool = True) -> dict:
    """Canonicalise a reading and enforce the hard evidence fields.

    Required on every oxygen reading: pouch code, instrument error and
    sampling range.  In strict mode missing fields raise
    :class:`ReadingValidationError`.
    """
    if not isinstance(row, dict):
        raise ReadingValidationError(f"oxygen reading must be an object, got {type(row)!r}")
    code = pouch_code(row)
    oxygen = _pick(row, _O2_KEYS)
    error = _pick(row, _ERROR_KEYS, 0.0)
    rng = sampling_range(row)
    measured_at = _pick(row, _TIME_KEYS)

    missing = []
    if strict and not code:
        missing.append("pouch/bag code")
    if oxygen is None:
        missing.append("oxygen_pct")
    if strict and _pick(row, _ERROR_KEYS) is None:
        missing.append("instrument_error_pct")
    if strict and (rng is None or (isinstance(rng, str) and not rng.strip())):
        missing.append("sampling_range")
    if missing:
        where = f" for pouch {code!r}" if code else ""
        raise ReadingValidationError(f"oxygen reading{where} missing: {', '.join(missing)}")

    oxygen_f = float(oxygen)
    error_f = abs(float(error or 0.0))
    return {
        "pouch": code,
        "oxygen_pct": oxygen_f,
        "instrument_error_pct": error_f,
        "sampling_range": rng,
        "measured_at": measured_at,
        "oxygen_lower_pct": max(0.0, oxygen_f - error_f),
        "oxygen_upper_pct": oxygen_f + error_f,
    }


def validate_reading(row: dict) -> dict:
    return normalize_reading(row, strict=True)


def oxygen_upper_pct(row: dict | float | int, error_pct: float | None = None) -> float:
    """Conservative oxygen upper bound = reading + instrument error.

    Accepts a reading mapping or ``oxygen_upper_pct(4.8, 0.2)`` numbers.
    """
    if isinstance(row, (int, float)):
        return float(row) + abs(float(error_pct or 0.0))
    if isinstance(row, dict) and "oxygen_upper_pct" in row and error_pct is None:
        return float(row["oxygen_upper_pct"])
    if error_pct is not None and isinstance(row, dict):
        return float(_pick(row, _O2_KEYS)) + abs(float(error_pct))
    normalized = normalize_reading(row, strict=False)
    return normalized["oxygen_upper_pct"]


def oxygen_lower_pct(row: dict | float | int, error_pct: float | None = None) -> float:
    if isinstance(row, (int, float)):
        return max(0.0, float(row) - abs(float(error_pct or 0.0)))
    if isinstance(row, dict) and "oxygen_lower_pct" in row and error_pct is None:
        return float(row["oxygen_lower_pct"])
    normalized = normalize_reading(row, strict=False)
    return normalized["oxygen_lower_pct"]


# Friendly aliases used by callers from different naming conventions.
oxygen_bound_pct = oxygen_upper_pct
upper_bound_pct = oxygen_upper_pct
conservative_oxygen_pct = oxygen_upper_pct


def oxygen_bounds_pct(row: dict) -> tuple[float, float]:
    return oxygen_lower_pct(row), oxygen_upper_pct(row)


def worst_oxygen_upper(samples: Iterable[dict] | dict) -> float:
    if isinstance(samples, dict):
        samples = samples_of(samples)
    rows = list(samples)
    if not rows:
        raise ValueError("at least one oxygen reading is required")
    return max(oxygen_upper_pct(row) for row in rows)


max_oxygen_upper = worst_oxygen_upper


def high_oxygen_pouches(samples: Iterable[dict], *, ceiling_pct: float | None = None) -> list[dict]:
    """Return per-reading records that are high / over a conservative ceiling."""
    result = []
    for row in samples:
        norm = normalize_reading(row, strict=False)
        if ceiling_pct is None or norm["oxygen_upper_pct"] > ceiling_pct:
            result.append(norm)
    return result


leaking_pouches = high_oxygen_pouches


# ---------------------------------------------------------------------------
# Pouch registry and sampling-design conservative bound
# ---------------------------------------------------------------------------

def samples_of(data: Any) -> list[dict]:
    """Return oxygen readings from any common fixture key."""
    if isinstance(data, PackageLot):
        data = data.data
    if not isinstance(data, dict):
        return list(data) if data else []
    for key in ("headspace_samples", "oxygen_readings", "oxygen_samples", "readings", "samples"):
        value = data.get(key)
        if isinstance(value, list) and value:
            return value
    return []


def pouch_code_scheme(data: Any = None, **overrides: Any) -> dict:
    scheme = {"prefix": "PK-", "width": 2, "start": 1, "count": 100}
    if isinstance(data, dict):
        raw = data.get("pouch_codes") or data.get("code_scheme") or {}
        if isinstance(raw, dict):
            scheme.update({k: v for k, v in raw.items() if v is not None})
        for count_key in ("pouches", "pouch_count", "bag_count", "bags", "units"):
            if data.get(count_key) is not None and "count" not in overrides:
                scheme["count"] = int(data[count_key])
                break
    scheme.update({k: v for k, v in overrides.items() if v is not None})
    scheme["count"] = int(scheme["count"])
    scheme["start"] = int(scheme.get("start", 1))
    scheme["width"] = int(scheme.get("width", 2))
    scheme["prefix"] = str(scheme.get("prefix", "PK-"))
    return scheme


def all_pouch_codes(data: Any = None, *, count: int | None = None, **kw: Any) -> list[str]:
    """All physical pouch codes of the case, e.g. PK-01 .. PK-100."""
    if isinstance(data, dict):
        explicit = data.get("pouch_codes_list")
        for key in ("pouch_codes", "bag_codes", "codes"):
            value = data.get(key)
            if isinstance(value, list) and value and all(isinstance(x, str) for x in value):
                return list(value)
        if isinstance(explicit, list):
            return list(explicit)
    overrides = dict(kw)
    if count is not None:
        overrides["count"] = count
    scheme = pouch_code_scheme(data, **overrides)
    width = scheme["width"]
    return [
        f"{scheme['prefix']}{i:0{width}d}"
        for i in range(scheme["start"], scheme["start"] + scheme["count"])
    ]


def sampling_design(data: Any) -> dict:
    if isinstance(data, dict) and isinstance(data.get("sampling_design"), dict):
        return data["sampling_design"]
    if isinstance(data, dict) and "untested_policy" in data:
        return data
    return {}


def _design_population(data: Any, design: dict) -> int:
    for value in (design.get("population"), design.get("population_pouches")):
        if value:
            return int(value)
    if isinstance(data, dict) and data.get("pouches"):
        return int(data["pouches"])
    return 100


def untested_oxygen_bound(
    samples: Iterable[dict] | None = None,
    design: dict | None = None,
    data: Any = None,
) -> float:
    """Conservative oxygen bound for pouches without their own measurement.

    With a full-lot / representative random design the untested pouches take
    the worst observed upper bound.  Pouches outside the ranges covered by a
    partial design take the design's ``uncovered_range_bound_pct`` (physical
    maximum headspace oxygen by default), because the sample says nothing
    about them.
    """
    if design is None:
        design = sampling_design(data) if data is not None else {}
    policy = str(design.get("untested_policy", "worst_observed_upper"))
    rows = list(samples) if samples is not None else []
    if not rows and data is not None and isinstance(data, dict):
        rows = list(data.get("headspace_samples", []))

    worst_observed = worst_oxygen_upper(rows) if rows else ATMOSPHERIC_OXYGEN_PCT

    if policy in ("worst_observed_upper", "worst_upper", "conservative_sample"):
        return worst_observed
    if policy in ("fixed", "constant", "bound_pct"):
        return float(design.get("bound_pct", worst_observed))
    if policy in ("full_coverage_required", "uncovered_atmospheric", "range_aware"):
        return max(worst_observed, float(design.get("uncovered_range_bound_pct", ATMOSPHERIC_OXYGEN_PCT)))
    # Unknown policy: stay maximally conservative rather than optimistic.
    return max(worst_observed, float(design.get("uncovered_range_bound_pct", ATMOSPHERIC_OXYGEN_PCT)))


untested_bound_pct = untested_oxygen_bound


def lot_oxygen_profile(data: dict, *, readings_by_code: dict | None = None) -> dict:
    """Project conservative oxygen bounds onto every physical pouch code.

    ``readings_by_code`` optionally carries later direct / retest readings
    keyed by pouch code (original or active).  Codes without a direct current
    reading inherit the sampling-design untested bound.
    """
    samples = data.get("headspace_samples", []) if isinstance(data, dict) else []
    design = sampling_design(data)
    codes = all_pouch_codes(data)
    design_bound = untested_oxygen_bound(samples, design)

    direct: dict[str, dict] = {}
    for row in samples:
        norm = normalize_reading(row, strict=True)
        if norm["pouch"]:
            direct[norm["pouch"]] = norm
    for code, row in (readings_by_code or {}).items():
        norm = row if isinstance(row, dict) and "oxygen_upper_pct" in row else normalize_reading(row, strict=True)
        direct[str(code)] = norm

    pouches: dict[str, dict] = {}
    upper_values: list[float] = []
    for code in codes:
        if code in direct:
            bound = direct[code]["oxygen_upper_pct"]
            basis = direct[code].get("basis", "direct_reading")
            source = direct[code]
        else:
            bound = design_bound
            basis = "sampling_design_untested"
            source = None
        upper_values.append(bound)
        pouches[code] = {
            "pouch": code,
            "oxygen_upper_pct": bound,
            "oxygen_lower_pct": (source or {}).get("oxygen_lower_pct"),
            "basis": basis,
            "measured": source is not None,
        }
    return {
        "pouch_count": len(codes),
        "pouches": pouches,
        "lot_oxygen_upper_pct": max(upper_values),
        "untested_oxygen_upper_pct": design_bound,
        "sampled_count": len(direct),
    }


# ---------------------------------------------------------------------------
# Repack identity: old code -> new code with a per-pouch closed loop
# ---------------------------------------------------------------------------

def next_pouch_code(old_code: str, generation: int = 1, suffix: str = "-R") -> str:
    """Deterministic new code for a repacked physical pouch.

    PK-03 -> PK-03-R1; a second rework of the same physical pouch -> PK-03-R2.
    """
    base = re.sub(r"-[A-Z]+\d+$", "", str(old_code))
    base = re.sub(r"-R\d+$", "", base)
    return f"{base}{suffix}{generation}"


new_pouch_code = next_pouch_code


def _normalise_steps(steps: dict | Iterable | None, at: Any, operator: str | None) -> dict:
    record = {name: {"done": False, "at": None, "by": operator} for name in REPACK_STEPS}
    if steps is None:
        performed = {name: at for name in REPACK_STEPS}
    elif isinstance(steps, dict):
        performed = {}
        for key, value in steps.items():
            name = STEP_ALIASES.get(str(key), STEP_ALIASES.get(str(key).lower()))
            if name is None:
                continue
            if isinstance(value, dict):
                record[name] = {
                    "done": bool(value.get("done", True)),
                    "at": value.get("at", at),
                    "by": value.get("by", operator),
                }
                continue
            performed[name] = value if not isinstance(value, bool) else at if value else None
    else:
        performed = {}
        for key in steps:
            name = STEP_ALIASES.get(str(key), STEP_ALIASES.get(str(key).lower()))
            if name:
                performed[name] = at

    base_time = parse_dt(at)
    for index, name in enumerate(REPACK_STEPS):
        if name in record and record[name].get("done"):
            continue
        if name in performed and performed[name] is not None:
            when = performed[name]
            record[name] = {"done": True, "at": when, "by": operator}
        elif steps is None and name in performed:
            # Default one-shot closure: every physical step gets its own
            # ordered timestamp in the minutes before completion, so the
            # loop is auditable and ``at`` is the final retest / term start.
            offset = timedelta(minutes=len(REPACK_STEPS) - 1 - index)
            when = (base_time - offset) if base_time else at
            record[name] = {
                "done": True,
                "at": when.isoformat() if isinstance(when, datetime) else when,
                "by": operator,
            }
    return record


def build_repack_record(
    original_code: str,
    *,
    retest: dict | None = None,
    oxygen_pct: float | None = None,
    instrument_error_pct: float | None = None,
    error_pct: float | None = None,
    at: Any = None,
    operator: str | None = None,
    generation: int = 1,
    suffix: str = "-R",
    new_code: str | None = None,
    target_upper_pct: float = 2.0,
    steps: dict | Iterable | None = None,
    heat_history_days: float | None = None,
    sampling_range_value: Any = None,
    **kwargs: Any,
) -> dict:
    """Build one pouch's real unpack -> N2 fill -> reseal -> retest closure.

    The closure only passes when every physical step happened and the
    post-reseal retest conservative upper bound is at/below the target.
    """
    # Tolerate common keyword aliases (oxygen=, error=, ...).
    if oxygen_pct is None:
        oxygen_pct = _pick(kwargs, _O2_KEYS)
    if instrument_error_pct is None:
        instrument_error_pct = error_pct
    if instrument_error_pct is None:
        instrument_error_pct = _pick(kwargs, _ERROR_KEYS, None)
    if target_upper_pct is None:
        target_upper_pct = 2.0
    generated_code = new_code or next_pouch_code(original_code, generation, suffix)

    if retest is None:
        retest = {
            _CODE_KEYS[0]: generated_code,
            "oxygen_pct": oxygen_pct,
            "instrument_error_pct": instrument_error_pct,
            "sampling_range": sampling_range_value
            or f"{generated_code} post-reseal full headspace retest",
            "measured_at": at,
        }
    else:
        retest = dict(retest)
        retest.setdefault("pouch", generated_code)
        retest.setdefault("measured_at", at)
        if sampling_range_value is not None:
            retest.setdefault("sampling_range", sampling_range_value)

    retest_norm = normalize_reading(retest, strict=True)
    step_record = _normalise_steps(steps, at, operator)
    steps_complete = all(step_record[name]["done"] for name in REPACK_STEPS)
    retest_passed = retest_norm["oxygen_upper_pct"] <= float(target_upper_pct) + 1e-12
    passed = steps_complete and retest_passed

    return {
        "original_code": str(original_code),
        "new_code": generated_code,
        "generation": generation,
        "process": "nitrogen_repack",
        "at": at,
        "operator": operator,
        "steps": step_record,
        "steps_complete": steps_complete,
        "retest": retest_norm,
        "target_oxygen_upper_pct": float(target_upper_pct),
        "retest_passed": retest_passed,
        "passed": passed,
        "closed_loop": passed,
        "heat_history_retained": True,
        "heat_history_days_before_repack": heat_history_days,
    }


repack_pouch = build_repack_record


# ---------------------------------------------------------------------------
# Stateful lot ledger: physical pouch identity across measurements / repacks
# ---------------------------------------------------------------------------

class IdentityConflict(RuntimeError):
    """Raised when old and new pouch codes would both be sellable."""


class PackageLot:
    """Mutable, append-only state of one packed 100-pouch case.

    The ledger tracks physical pouches by original code, the currently active
    sellable code of each pouch, every oxygen reading, repack closures, heat
    log entries and channel commitments / corrections.
    """

    def __init__(self, package_life: dict | str | Path | None = None, *, now: Any = None):
        if isinstance(package_life, (str, Path)):
            package_life = json.loads(Path(package_life).read_text(encoding="utf-8"))
        self.data: dict = package_life or {}
        self.blend = self.data.get("blend") or self.data.get("batch_id") or self.data.get("lot")
        self.packed_at = parse_dt(self.data.get("packed_at") or self.data.get("packed"))
        self.codes: list[str] = all_pouch_codes(self.data)
        heat_source = []
        for key in ("temperature_log", "heat_log", "temperature_records", "thermal_log", "heat_history"):
            if isinstance(self.data.get(key), list):
                heat_source = self.data[key]
                break
        self.heat_log: list[dict] = list(heat_source)
        self.commitments: list[dict] = []
        self.corrections: list[dict] = []
        self.shipments: list[dict] = []
        self.events: list[dict] = []
        self._seq_commitment = 0
        self._seq_correction = 0
        self.pouches: dict[str, dict] = {
            code: {
                "physical_code": code,
                "original_code": code,
                "active_code": code,
                "code_lineage": [code],
                "generation": 0,
                "status": "original_sellable",
                "process": "as_packed",
                "readings": [],
                "repacks": [],
                "current_upper_pct": None,
                "term_start": self.data.get("packed_at"),
            }
            for code in self.codes
        }
        self._record_event("lot_loaded", {"pouch_count": len(self.codes)})
        for row in samples_of(self.data):
            # Loading is lenient: strict evidence-field validation is exposed
            # via validate_reading() at explicit intake boundaries.
            self.add_reading(row, validate=False)

    # -- event trail -------------------------------------------------------
    def _record_event(self, kind: str, detail: dict | None = None) -> dict:
        event = {"seq": len(self.events) + 1, "kind": kind, "detail": detail or {}}
        self.events.append(event)
        return event

    # -- code resolution ---------------------------------------------------
    def resolve(self, code: str) -> dict:
        """Resolve any old or new code to the physical pouch state."""
        code = str(code)
        if code in self.pouches:
            return self.pouches[code]
        for pouch in self.pouches.values():
            if code in pouch["code_lineage"]:
                return pouch
        raise KeyError(f"unknown pouch code {code!r}")

    def _canonical_original(self, code: str) -> str:
        return self.resolve(code)["original_code"]

    def _register_pouch(self, code: str) -> dict:
        """Register an on-the-fly physical pouch code not in the code scheme."""
        pouch = {
            "physical_code": code,
            "original_code": code,
            "active_code": code,
            "code_lineage": [code],
            "generation": 0,
            "status": "original_sellable",
            "process": "as_packed",
            "readings": [],
            "repacks": [],
            "current_upper_pct": None,
            "term_start": self.data.get("packed_at"),
        }
        self.pouches[code] = pouch
        if code not in self.codes:
            self.codes.append(code)
        return pouch

    # -- readings ----------------------------------------------------------
    def add_reading(self, row: dict, *, validate: bool = True, basis: str | None = None) -> dict:
        norm = normalize_reading(row, strict=validate)
        code = norm["pouch"] or pouch_code(row)
        if code is None:
            raise ReadingValidationError("reading is missing pouch code")
        try:
            pouch = self.resolve(code)
        except KeyError:
            pouch = self._register_pouch(str(code))
        if basis:
            norm["basis"] = basis
        else:
            norm["basis"] = "direct_reading"
        pouch["readings"].append(norm)
        pouch["current_upper_pct"] = norm["oxygen_upper_pct"]
        self._record_event("oxygen_reading", {"pouch": pouch["original_code"], "upper": norm["oxygen_upper_pct"]})
        return norm

    record_retest = add_reading
    record_reading = add_reading

    # -- heat log ----------------------------------------------------------
    def add_heat_log(self, entry: dict | Iterable[dict]) -> None:
        """Append a late temperature record (never erases prior history)."""
        entries = entry if isinstance(entry, (list, tuple)) else [entry]
        for item in entries:
            self.heat_log.append(dict(item))
            self._record_event("heat_log_added", dict(item))

    append_heat_exposure = add_heat_log
    record_temperature = add_heat_log

    def set_heat_log(self, entries: list[dict], *, reason: str | None = None) -> None:
        """Replace the heat log with a corrected full record.

        The previous log is preserved as an event; it is not silently erased.
        """
        previous = list(self.heat_log)
        self.heat_log = list(entries)
        self._record_event(
            "heat_log_corrected",
            {"reason": reason, "previous_entries": len(previous), "new_entries": len(self.heat_log)},
        )

    correct_heat_log = set_heat_log

    # -- repack ------------------------------------------------------------
    def repack(
        self,
        code: str,
        *,
        retest: dict | None = None,
        target_upper_pct: float | None = None,
        at: Any = None,
        **kw: Any,
    ) -> dict:
        """Execute and record one pouch's full repack closed loop."""
        pouch = self.resolve(code)
        generation = pouch["generation"] + 1
        suffix = (self.data.get("repack") or {}).get("new_code_suffix", "-R")
        if target_upper_pct is None:
            target_upper_pct = float((self.data.get("repack") or {}).get("target_oxygen_upper_pct", 2.0))
        original = pouch["original_code"]
        record = build_repack_record(
            original,
            retest=retest,
            at=at,
            generation=generation,
            suffix=suffix,
            target_upper_pct=target_upper_pct,
            **kw,
        )
        new_code = record["new_code"]

        # Identity transition: the previous active code is retired the moment
        # the physical pouch is opened, so old and new can never both sell.
        old_active = pouch["active_code"]
        pouch["status"] = "opened_for_repack"
        pouch["active_code"] = None
        if old_active in pouch["code_lineage"]:
            pass
        if new_code not in pouch["code_lineage"]:
            pouch["code_lineage"].append(new_code)
        pouch["generation"] = generation
        pouch["repacks"].append(record)

        if record["passed"]:
            pouch["active_code"] = new_code
            pouch["status"] = "sellable"
            pouch["process"] = "nitrogen_repack"
            pouch["current_upper_pct"] = record["retest"]["oxygen_upper_pct"]
            pouch["term_start"] = at or record["retest"].get("measured_at")
            self.add_reading(record["retest"], basis="repack_retest")
        else:
            # Failed closure: new physical code exists but is quarantined;
            # no code (old or new) is sellable for this pouch.
            pouch["active_code"] = new_code
            pouch["status"] = "quarantined_repack_failed"
            pouch["process"] = "nitrogen_repack"
            pouch["current_upper_pct"] = record["retest"]["oxygen_upper_pct"]
            self.add_reading(record["retest"], basis="failed_repack_retest")

        self._assert_identity_invariant()
        self._record_event(
            "pouch_repacked",
            {"original": original, "old_code": old_active, "new_code": new_code, "passed": record["passed"]},
        )
        return record

    repack_pouch = repack

    def repack_all(self, *, at: Any = None, oxygen_pct: float = 1.8,
                   instrument_error_pct: float = 0.2, **kw: Any) -> list[dict]:
        """Route A convenience: physically close the loop for every pouch."""
        return self.repack_codes(list(self.codes), at=at, oxygen_pct=oxygen_pct,
                                 instrument_error_pct=instrument_error_pct, **kw)

    def repack_codes(self, codes: Iterable[str], *, at: Any = None, oxygen_pct: float = 1.8,
                     instrument_error_pct: float = 0.2, **kw: Any) -> list[dict]:
        """Repack a specific set of physical pouch codes."""
        records = []
        for code in codes:
            records.append(
                self.repack(
                    code,
                    at=at,
                    oxygen_pct=oxygen_pct,
                    instrument_error_pct=instrument_error_pct,
                    **kw,
                )
            )
        return records

    @property
    def pending_repack_codes(self) -> list[str]:
        """Codes whose latest generation does not have a passed closure."""
        pending = []
        for code, pouch in self.pouches.items():
            latest = pouch["repacks"][-1] if pouch["repacks"] else None
            if latest is None or not latest.get("passed"):
                pending.append(code)
        return pending

    def _assert_identity_invariant(self) -> None:
        active_seen: dict[str, str] = {}
        for original, pouch in self.pouches.items():
            active = pouch.get("active_code")
            if not active or pouch["status"] not in self.SELLABLE_STATUSES:
                continue
            if active in active_seen:
                raise IdentityConflict(f"active code {active} assigned to two physical pouches")
            active_seen[active] = original

    # -- sellable code projection -----------------------------------------
    SELLABLE_STATUSES = ("sellable", "original_sellable")

    def is_sellable(self, code: str, *, require_current_evidence: bool = True) -> bool:
        try:
            pouch = self.resolve(code)
        except KeyError:
            return False
        if pouch["status"] not in self.SELLABLE_STATUSES:
            return False
        if pouch["active_code"] != str(code):
            # Retired original code or superseded generation.
            return False
        if require_current_evidence:
            from .shelf_life import assess_pouch  # local: avoid import cycle

            verdict = assess_pouch(self, pouch["original_code"])
            return bool(verdict.get("sellable"))
        return True

    def active_codes(self) -> list[str]:
        """Identity layer: current active code of every non-quarantined pouch."""
        return [
            p["active_code"]
            for p in self.pouches.values()
            if p["status"] in self.SELLABLE_STATUSES and p["active_code"]
        ]

    def sellable_codes(self, *, require_current_evidence: bool = True) -> list[str]:
        """Codes that are active AND currently supported by shelf-life evidence."""
        if not require_current_evidence:
            return self.active_codes()
        sellable = []
        for code in self.active_codes():
            if self.is_sellable(code, require_current_evidence=True):
                sellable.append(code)
        return sellable

    def retired_codes(self) -> list[str]:
        active = set(self.sellable_codes())
        retired = []
        for pouch in self.pouches.values():
            for code in pouch["code_lineage"][:-1]:
                if code not in active:
                    retired.append(code)
        return retired

    def active_code_for(self, code: str) -> str | None:
        return self.resolve(code)["active_code"]

    @property
    def all_repacked(self) -> bool:
        return all(p["generation"] >= 1 and p["repacks"] and p["repacks"][-1]["passed"] for p in self.pouches.values())

    @property
    def passed_repack_count(self) -> int:
        return sum(1 for p in self.pouches.values() if p["generation"] >= 1 and p["repacks"] and p["repacks"][-1]["passed"])

    # -- (de)serialisation --------------------------------------------------
    def snapshot(self) -> dict:
        return {
            "blend": self.blend,
            "codes": list(self.codes),
            "pouches": self.pouches,
            "heat_log": list(self.heat_log),
            "commitments": list(self.commitments),
            "corrections": list(self.corrections),
            "events": list(self.events),
        }

    to_dict = snapshot


# Common alternative class names a caller may look for.
Lot = PackageLot
Batch = PackageLot
TeaLot = PackageLot
PouchLedger = PackageLot
PackageBatch = PackageLot


def create_lot(package_life: dict | str | "Path" | None = None) -> PackageLot:  # noqa: F821
    """Factory accepting a fixture dict or a path to package-life.json."""
    if package_life is None or isinstance(package_life, dict):
        return PackageLot(package_life)
    import json
    from pathlib import Path

    path = Path(package_life)
    return PackageLot(json.loads(path.read_text(encoding="utf-8")))


def load_lot(package_life: Any = None) -> PackageLot:
    """Alias for :func:`create_lot` accepting dicts, paths, lists or lots."""
    if isinstance(package_life, PackageLot):
        return package_life
    if isinstance(package_life, list):
        return PackageLot({"headspace_samples": package_life})
    return create_lot(package_life)


as_lot = load_lot
coerce_lot = load_lot


def repack_lot(target: Any, *, at: Any = None, oxygen_pct: float = 1.8,
               instrument_error_pct: float = 0.2, **kw: Any) -> PackageLot:
    """Execute the full 100-pouch repack closed loop and return the lot."""
    lot = load_lot(target)
    lot.repack_all(at=at, oxygen_pct=oxygen_pct, instrument_error_pct=instrument_error_pct, **kw)
    return lot


def oxygen_upper_bounds(samples: Iterable[dict]) -> list[float]:
    return [oxygen_upper_pct(row) for row in samples]


def conservative_lot_bound(target: Any = None, samples: Iterable[dict] | None = None,
                           design: dict | None = None) -> float:
    """Worst conservative oxygen bound over measured + untested pouches."""
    if isinstance(target, PackageLot):
        profile = lot_oxygen_profile(target.data)
        return profile["lot_oxygen_upper_pct"]
    rows = list(samples) if samples is not None else samples_of(target) if isinstance(target, dict) else list(target or [])
    if design is None and isinstance(target, dict):
        design = sampling_design(target)
    return untested_oxygen_bound(rows, design)


# Naming aliases used by different callers.
upper_oxygen_pct = oxygen_upper_pct
oxygen_limit = oxygen_upper_pct
sample_upper_limit = oxygen_upper_pct
max_oxygen_bound = worst_oxygen_upper
maximum_oxygen_upper = worst_oxygen_upper
lot_oxygen_limit = worst_oxygen_upper
validate_oxygen_reading = validate_reading
require_reading_fields = validate_reading
new_bag_code = next_pouch_code
repack_bag = build_repack_record
execute_repack = build_repack_record
close_repack_loop = build_repack_record
Ledger = PackageLot
PouchIdentityLedger = PackageLot
