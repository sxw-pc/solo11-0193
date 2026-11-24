"""Time-of-use energy chargeback with physical conservation checks.

Cost beneficiaries are deliberately separate from meter events.  A kWh can be
charged directly to a batch submeter or identified as period-specific loss;
that loss may then be settled either as approved plant overhead or against the
batch lines proven to be running in that exact tariff period.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from .meter_ticks import reconcile_readings

PLANT_ACCOUNT = "PLANT"
CLEARING_ACCOUNT = "PENDING_LOSS_CLEARING"

PLANT_KEY_ALIASES = {
    "PLANT",
    "plant",
    "PLANT_OVERHEAD",
    "plant_overhead",
    "FACTORY",
    "factory",
    "FACTORY_OVERHEAD",
    "PUBLIC",
    "PUBLIC_LOSS",
    "public_loss",
    "厂级",
    "厂级公共费用",
}


class AllocationMap(dict):
    """Dictionary with read-only aliases for the factory/plant account."""

    def __init__(self, *args: Any, plant_account: str = PLANT_ACCOUNT, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.plant_account = plant_account

    def _canonical_alias(self, key: Any) -> Any:
        if dict.__contains__(self, key):
            return key
        if key in PLANT_KEY_ALIASES or key == self.plant_account:
            if dict.__contains__(self, self.plant_account):
                return self.plant_account
            if dict.__contains__(self, PLANT_ACCOUNT):
                return PLANT_ACCOUNT
        raise KeyError(key)

    def __getitem__(self, key: Any) -> Any:
        try:
            return super().__getitem__(key)
        except KeyError:
            return super().__getitem__(self._canonical_alias(key))

    def get(self, key: Any, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def __contains__(self, key: object) -> bool:
        return dict.__contains__(self, key) or (
            isinstance(key, str) and key in PLANT_KEY_ALIASES and self.plant_account in self
        )



class EnergyChargebackError(ValueError):
    """Base class for chargeback reconciliation failures."""


class ConservationError(EnergyChargebackError):
    """Grid, submeters and loss do not conserve energy in a period."""


class TariffBoundaryError(EnergyChargebackError):
    """A time-of-use boundary cannot be reconciled to a meter interval."""


class AuthorizationError(EnergyChargebackError):
    """The selected settlement route lacks contract approval/signature."""


class AllocationBoundaryOpen(EnergyChargebackError):
    """Runtime evidence does not identify a unique authorized beneficiary."""

    def __init__(self, message: str, *, context: dict[str, Any] | None = None):
        super().__init__(message)
        self.context = context or {}


def _D(value: Any) -> Decimal:
    return Decimal(str(value))


def quantity(value: Decimal | int | float) -> int | float:
    value = _D(value)
    integral = value.to_integral_value()
    if value == integral:
        return int(integral)
    return float(value)


def cents(kwh: Any, yuan_per_kwh: Any) -> int:
    value = (_D(kwh) * _D(yuan_per_kwh) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(value)


def yuan_from_cents(value: int) -> int | float:
    if value % 100 == 0:
        return value // 100
    return round(value / 100, 2)


def money(value: Any) -> int | float:
    return yuan_from_cents(int(_D(value).quantize(Decimal("1"), rounding=ROUND_HALF_UP)))


def normalize_route(route: str | None) -> str:
    route = (route or "plant_overhead").lower()
    if route in {"plant", "plant_overhead", "overhead", "factory", "public", "public_loss"}:
        return "plant_overhead"
    if route in {
        "run",
        "actual_run",
        "actual_runtime",
        "runtime",
        "running_batches",
        "batch_runtime",
        "actual_run_borne",
    }:
        return "actual_run"
    raise EnergyChargebackError(f"unknown settlement route: {route!r}")


def nominal_allocate(readings: list[dict[str, Any]], jobs: list[dict[str, Any]]) -> dict[str, float]:
    """The seed defect: scale the raw register delta by nominal weights.

    Kept as an explicit diagnostic/baseline, never used by reconciliation.
    """

    total = readings[-1]["register_kwh"] - readings[0]["register_kwh"]
    weight = sum(job["nominal_weight"] for job in jobs)
    return {
        job["id"]: round(total * job["nominal_weight"] / weight, 2)
        for job in jobs
    }


def _payload_from_args(
    first: Any,
    jobs: list[dict[str, Any]] | None,
    tariff: list[dict[str, Any]] | None,
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    if isinstance(first, dict) and ("readings" in first or "grid_meter" in first):
        if jobs is not None or tariff is not None:
            merged = dict(first)
            if jobs is not None:
                merged["jobs"] = jobs
            if tariff is not None:
                merged["tariff"] = tariff
            merged.update(kwargs)
            return merged
        payload = dict(first)
        payload.update(kwargs)
        return payload

    payload = dict(kwargs.pop("payload", None) or {})
    payload["readings"] = first
    if jobs is not None:
        payload["jobs"] = jobs
    if tariff is not None:
        payload["tariff"] = tariff
    payload.update(kwargs)
    return payload


def _period_overlap(a0: int, a1: int, b0: int, b1: int) -> int:
    return max(0, min(a1, b1) - max(a0, b0))


def _grid_for_period(period: dict[str, Any], meter: dict[str, Any]) -> tuple[Decimal, str, list[dict[str, Any]]]:
    p0 = int(period["start_minute"])
    p1 = int(period["end_minute"])
    exact = [
        item
        for item in meter["intervals"]
        if item.get("start_minute") == p0 and item.get("end_minute") == p1
    ]
    if exact:
        return sum((_D(item["kwh"]) for item in exact), Decimal("0")), "signed_boundary_reading", exact

    pieces: list[Decimal] = []
    used_intervals: list[dict[str, Any]] = []
    prorated = False
    covered_minutes = 0
    for item in meter["intervals"]:
        i0 = item.get("start_minute")
        i1 = item.get("end_minute")
        if i0 is None or i1 is None:
            continue
        overlap = _period_overlap(int(i0), int(i1), p0, p1)
        if overlap <= 0:
            continue
        duration = int(i1) - int(i0)
        if duration <= 0:
            raise TariffBoundaryError(f"zero-duration meter interval {item}")
        piece = _D(item["kwh"]) * overlap / duration
        pieces.append(piece)
        used_intervals.append(item)
        covered_minutes += overlap
        if overlap != duration:
            prorated = True
    if not pieces:
        raise TariffBoundaryError(f"no meter energy covers tariff period {p0}-{p1}")
    if prorated:
        basis = "time_proration_estimated"
    elif covered_minutes == p1 - p0 and len(used_intervals) > 1:
        basis = "signed_meter_intervals"
    else:
        basis = "meter_interval"
    return sum(pieces, Decimal("0")), basis, used_intervals


def _submeters_for_period(
    payload: dict[str, Any], p0: int, p1: int
) -> tuple[dict[str, Decimal], str]:
    result: dict[str, Decimal] = {}
    estimated = False

    explicit = payload.get("submeter_readings") or payload.get("submeters") or []
    for row in explicit:
        batch_id = row.get("batch_id") or row.get("job_id") or row.get("id")
        s0 = int(row.get("start_minute", p0))
        s1 = int(row.get("end_minute", p1))
        overlap = _period_overlap(s0, s1, p0, p1)
        if overlap <= 0:
            continue
        amount = _D(row.get("kwh", row.get("submeter_kwh", row.get("energy_kwh", 0))))
        duration = max(1, s1 - s0)
        if overlap != s1 - s0:
            estimated = True
        result[batch_id] = result.get(batch_id, Decimal("0")) + amount * overlap / duration

    for job in payload.get("jobs", []):
        j0 = int(job.get("start_minute", p0))
        j1 = int(job.get("end_minute", p1))
        overlap = _period_overlap(j0, j1, p0, p1)
        if overlap <= 0:
            continue
        if "submeter_kwh" not in job:
            continue
        amount = _D(job["submeter_kwh"])
        duration = max(1, j1 - j0)
        if overlap != j1 - j0 or j0 != p0 or j1 != p1:
            estimated = True
        shared = amount * overlap / duration
        batch_id = job["id"]
        # Jobs are the canonical seed source.  If an explicit row already
        # supplied the same interval value, do not double count it.
        if batch_id in result and abs(result[batch_id] - shared) < Decimal("0.000001"):
            continue
        result[batch_id] = result.get(batch_id, Decimal("0")) + shared

    return result, "duration_proration_estimated" if estimated else "signed_submeter"


def _contract(payload: dict[str, Any]) -> dict[str, Any]:
    return (
        payload.get("loss_allocation_contract")
        or payload.get("allocation_contract")
        or payload.get("contract")
        or {}
    )


def _authorize_route(payload: dict[str, Any], route: str, require_signed_contract: bool) -> dict[str, Any]:
    contract = _contract(payload)
    if route == "plant_overhead":
        allowed = contract.get("plant_overhead_allowed", True)
        if allowed is False:
            raise AuthorizationError("plant overhead settlement is not contract-approved")
    else:
        allowed = (
            contract.get("actual_run_allocation_allowed")
            if "actual_run_allocation_allowed" in contract
            else contract.get(
                "actual_runtime_allocation_allowed",
                contract.get("batch_proportional_allowed", True),
            )
        )
        if allowed is False:
            raise AuthorizationError("actual-run batch loss settlement is not contract-approved")
    if require_signed_contract and contract and not (contract.get("signed_by") and contract.get("signature")):
        raise AuthorizationError("loss allocation contract is not signed")
    return contract


def _runtime_records(payload: dict[str, Any], extra: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    records = []
    for key in ("runtime_records", "run_records", "equipment_records", "line_run_records"):
        if isinstance(payload.get(key), list):
            records.extend(payload[key])
    records.extend(extra or [])
    return records


def _runtime_for_period(
    payload: dict[str, Any],
    p0: int,
    p1: int,
    extra: list[dict[str, Any]] | None,
    require_signed: bool,
) -> dict[str, Any] | None:
    matches = []
    for record in _runtime_records(payload, extra):
        r0 = int(record.get("start_minute", record.get("from_minute", p0)))
        r1 = int(record.get("end_minute", record.get("to_minute", p1)))
        if _period_overlap(r0, r1, p0, p1) <= 0:
            continue
        if require_signed and not (record.get("signed_by") and record.get("signature")) and record.get("signed") is not True:
            raise AuthorizationError(f"runtime record {record.get('id')!r} is not signed")
        matches.append(record)
    if not matches:
        return None
    # Latest signed/captured record wins as evidence, while capture history
    # still preserves superseded records.
    return sorted(matches, key=lambda r: str(r.get("captured_at", r.get("id", ""))))[-1]


def _active_batch_shares(
    record: dict[str, Any], period_submeters: dict[str, Decimal], contract: dict[str, Any]
) -> tuple[dict[str, Decimal], list[dict[str, Any]]]:
    active: list[dict[str, Any]] = []
    for item in record.get("active_lines", []) or []:
        if isinstance(item, str):
            active.append({"line_id": item, "batch_id": record.get("line_batch", {}).get(item)})
        else:
            active.append(dict(item))
    for batch_id in record.get("active_batches", []) or record.get("batches", []) or []:
        if isinstance(batch_id, str):
            active.append({"batch_id": batch_id})
        else:
            active.append(dict(batch_id))

    if not active:
        raise AllocationBoundaryOpen(
            "runtime record does not identify active lines/batches",
            context={"runtime_record_id": record.get("id")},
        )

    unknown_lines = [item for item in active if not item.get("batch_id")]
    if unknown_lines:
        raise AllocationBoundaryOpen(
            "an active line has no accountable batch; allocation boundary must be reopened",
            context={"unknown_lines": unknown_lines, "runtime_record_id": record.get("id")},
        )

    batch_items: dict[str, list[dict[str, Any]]] = {}
    for item in active:
        batch_items.setdefault(item["batch_id"], []).append(item)

    shares: dict[str, Decimal] = {}
    explicit = False
    for batch_id, items in batch_items.items():
        weight = Decimal("0")
        for item in items:
            if "loss_share" in item or "share" in item:
                explicit = True
                weight += _D(item.get("loss_share", item.get("share")))
            elif "weight" in item:
                explicit = True
                weight += _D(item["weight"])
            elif "runtime_minutes" in item:
                explicit = True
                weight += _D(item["runtime_minutes"])
            else:
                weight += Decimal("1")
        shares[batch_id] = weight

    if explicit:
        total = sum(shares.values(), Decimal("0"))
        if total <= 0:
            raise AllocationBoundaryOpen("runtime allocation weights must be positive")
        return {batch_id: value / total for batch_id, value in shares.items()}, active

    # If there is exactly one accountable batch, all loss belongs to it.
    if len(shares) == 1:
        only = next(iter(shares))
        return {only: Decimal("1")}, active

    # Multiple proven running batches: submeter proportions are an approved,
    # period-specific proxy only when every active batch has a submeter share.
    if contract.get("batch_proportional_allowed", True):
        total_sub = sum(period_submeters.values(), Decimal("0"))
        if total_sub > 0 and all(period_submeters.get(batch_id, Decimal("0")) > 0 for batch_id in shares):
            return {batch_id: period_submeters.get(batch_id, Decimal("0")) / total_sub for batch_id in shares}, active

    raise AllocationBoundaryOpen(
        "multiple running batches require explicit period shares or complete submeter proportions",
        context={"active_batches": sorted(shares), "period_submeters": {k: str(v) for k, v in period_submeters.items()}},
    )


def reconcile_bill(
    payload: dict[str, Any],
    *,
    route: str = "plant_overhead",
    plant_account: str = PLANT_ACCOUNT,
    runtime_evidence: list[dict[str, Any]] | None = None,
    overrides: dict[str, dict[str, Any]] | None = None,
    extra_readings: list[dict[str, Any]] | None = None,
    extra_meter_events: list[dict[str, Any]] | None = None,
    require_signed_readings: bool = False,
    require_signed_contract: bool = False,
    require_signed_runtime: bool = False,
    require_exact_tariff_boundaries: bool = False,
    strict_invoice: bool = True,
    open_loss: str = "raise",
    open_loss_account: str = CLEARING_ACCOUNT,
    meter_kwargs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Reconcile meter energy, submeters, TOU rates and beneficiary costs."""

    route = normalize_route(route)
    contract = _authorize_route(payload, route, require_signed_contract)

    meter_events = list(payload.get("meter_events", []) or []) + list(extra_meter_events or [])
    meter = reconcile_readings(
        payload,
        overrides=overrides,
        extra_readings=extra_readings,
        meter_events=meter_events,
        require_signed_readings=require_signed_readings,
        **(meter_kwargs or {}),
    )

    tariffs = payload.get("tariff") or payload.get("tariffs") or []
    if not tariffs:
        raise TariffBoundaryError("at least one time-of-use tariff period is required")

    batch_ids = [job["id"] for job in payload.get("jobs", [])]
    allocations_cents: dict[str, int] = {batch_id: 0 for batch_id in batch_ids}
    allocations_cents[plant_account] = 0
    loss_kwh_by_account: dict[str, Decimal] = {plant_account: Decimal("0")}
    periods_out: list[dict[str, Any]] = []
    open_boundaries: list[dict[str, Any]] = []
    holding_account = plant_account if open_loss in {"plant", "plant_overhead"} else open_loss_account
    if open_loss != "raise":
        allocations_cents.setdefault(holding_account, 0)
        loss_kwh_by_account.setdefault(holding_account, Decimal("0"))

    for tariff in tariffs:
        p0 = int(tariff["start_minute"])
        p1 = int(tariff["end_minute"])
        rate = tariff.get("yuan_per_kwh", tariff.get("rate_yuan_per_kwh", tariff.get("rate")))
        grid_kwh, grid_basis, meter_intervals = _grid_for_period(tariff, meter)
        if require_exact_tariff_boundaries and grid_basis == "time_proration_estimated":
            raise TariffBoundaryError(
                f"tariff period {p0}-{p1} lacks signed readings at both time-of-use boundaries"
            )
        submeters, sub_basis = _submeters_for_period(payload, p0, p1)
        for batch_id in submeters:
            allocations_cents.setdefault(batch_id, 0)

        sub_total = sum(submeters.values(), Decimal("0"))
        loss_kwh = grid_kwh - sub_total
        tolerance = Decimal(str(payload.get("conservation_tolerance_kwh", "0.000001")))
        if loss_kwh < -tolerance:
            raise ConservationError(
                f"period {p0}-{p1}: submeters {quantity(sub_total)} exceed grid {quantity(grid_kwh)}"
            )
        if loss_kwh < 0:
            loss_kwh = Decimal("0")

        grid_cents = cents(grid_kwh, rate)
        direct_cents = {batch_id: cents(kwh, rate) for batch_id, kwh in submeters.items()}
        loss_cents = cents(loss_kwh, rate)

        # Close the period to the cent.  Independent half-up rounding of
        # several direct lines can otherwise leave a one-cent collection gap.
        direct_pool_cents = grid_cents - loss_cents
        direct_rounding_residual = direct_pool_cents - sum(direct_cents.values())
        if direct_rounding_residual and direct_cents:
            rounding_target = max(direct_cents, key=lambda key: direct_cents[key])
            direct_cents[rounding_target] += direct_rounding_residual

        period_loss_allocations: dict[str, int] = {}
        period_loss_kwh: dict[str, Any] = {}
        beneficiary_basis = route
        boundary_status = "settled"

        if route == "plant_overhead":
            account = plant_account
            period_loss_allocations[account] = loss_cents
            period_loss_kwh[account] = quantity(loss_kwh)
            allocations_cents[account] += loss_cents
            loss_kwh_by_account[account] = loss_kwh_by_account.get(account, Decimal("0")) + loss_kwh
        else:
            runtime = _runtime_for_period(payload, p0, p1, runtime_evidence, require_signed_runtime)
            if runtime is None:
                error = AllocationBoundaryOpen(
                    f"no signed actual-run evidence for period {p0}-{p1}",
                    context={"period": [p0, p1], "loss_kwh": quantity(loss_kwh)},
                )
                if open_loss == "raise":
                    raise error
                boundary_status = "reopened"
                open_boundaries.append(
                    {"reason": str(error), "status": "reopened", "context": error.context}
                )
                account = holding_account
                allocations_cents[account] += loss_cents
                period_loss_allocations[account] = loss_cents
                period_loss_kwh[account] = quantity(loss_kwh)
                loss_kwh_by_account[account] = loss_kwh_by_account.get(account, Decimal("0")) + loss_kwh
            else:
                try:
                    shares, active = _active_batch_shares(runtime, submeters, contract)
                    for batch_id, share in shares.items():
                        amount = int((Decimal(loss_cents) * share).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
                        period_loss_allocations[batch_id] = amount
                        allocations_cents[batch_id] = allocations_cents.get(batch_id, 0) + amount
                        period_loss_kwh[batch_id] = quantity(loss_kwh * share)
                        loss_kwh_by_account[batch_id] = loss_kwh_by_account.get(batch_id, Decimal("0")) + loss_kwh * share
                    # Correct largest rounding residual to the largest share.
                    residual = loss_cents - sum(period_loss_allocations.values())
                    if residual:
                        target = max(shares, key=shares.get)
                        period_loss_allocations[target] += residual
                        allocations_cents[target] += residual
                    beneficiary_basis = f"actual_run:{runtime.get('id', 'unsigned')}"
                except AllocationBoundaryOpen as error:
                    if open_loss == "raise":
                        raise
                    boundary_status = "reopened"
                    context = dict(error.context)
                    context.setdefault("period", [p0, p1])
                    open_boundaries.append(
                        {"reason": str(error), "status": "reopened", "context": context}
                    )
                    account = holding_account
                    allocations_cents[account] += loss_cents
                    period_loss_allocations[account] = loss_cents
                    period_loss_kwh[account] = quantity(loss_kwh)
                    loss_kwh_by_account[account] = loss_kwh_by_account.get(account, Decimal("0")) + loss_kwh

        for batch_id, amount in direct_cents.items():
            allocations_cents[batch_id] += amount

        periods_out.append(
            {
                "start_minute": p0,
                "end_minute": p1,
                "yuan_per_kwh": quantity(_D(rate)),
                "grid_kwh": quantity(grid_kwh),
                "grid_basis": grid_basis,
                "submeter_kwh": {key: quantity(value) for key, value in submeters.items()},
                "submeter_total_kwh": quantity(sub_total),
                "loss_kwh": quantity(loss_kwh),
                "conservation": {
                    "grid_kwh": quantity(grid_kwh),
                    "submeter_total_kwh": quantity(sub_total),
                    "loss_kwh": quantity(loss_kwh),
                    "balanced": abs(grid_kwh - sub_total - loss_kwh) <= tolerance,
                },
                "grid_amount_yuan": yuan_from_cents(grid_cents),
                "grid_amount_cents": grid_cents,
                "direct_amount_yuan": {key: yuan_from_cents(value) for key, value in direct_cents.items()},
                "direct_amount_cents": dict(direct_cents),
                "loss_amount_yuan": yuan_from_cents(loss_cents),
                "loss_amount_cents": loss_cents,
                "loss_allocations_yuan": {
                    key: yuan_from_cents(value) for key, value in period_loss_allocations.items()
                },
                "loss_allocations_cents": dict(period_loss_allocations),
                "loss_kwh_by_account": period_loss_kwh,
                "beneficiary_basis": beneficiary_basis,
                "boundary_status": boundary_status,
                "meter_interval_ids": [
                    [item.get("start_reading_id"), item.get("end_reading_id")] for item in meter_intervals
                ],
            }
        )

    total_cents = sum(allocations_cents.values())
    invoice_yuan = payload.get("utility_invoice_yuan")
    invoice_cents = cents(invoice_yuan, 1) if invoice_yuan is not None else total_cents
    invoice_matches = total_cents == invoice_cents
    if strict_invoice and invoice_yuan is not None and not invoice_matches:
        raise EnergyChargebackError(
            f"chargeback total {yuan_from_cents(total_cents)} != utility invoice {invoice_yuan}"
        )

    batch_allocation = {
        batch_id: yuan_from_cents(allocations_cents.get(batch_id, 0)) for batch_id in batch_ids
    }
    allocation = {key: yuan_from_cents(value) for key, value in allocations_cents.items() if value or key in batch_ids or key == plant_account}
    # Preserve stable ordering: batches first, then plant/clearing.
    ordered_allocation = {batch_id: allocation.get(batch_id, 0) for batch_id in batch_ids}
    for key in (plant_account, CLEARING_ACCOUNT):
        if key in allocation:
            ordered_allocation[key] = allocation[key]
    for key, value in allocation.items():
        if key not in ordered_allocation:
            ordered_allocation[key] = value
    allocation_map = AllocationMap(ordered_allocation, plant_account=plant_account)

    total_grid = sum(_D(p["grid_kwh"]) for p in periods_out)
    total_sub = sum(_D(p["submeter_total_kwh"]) for p in periods_out)
    total_loss = sum(_D(p["loss_kwh"]) for p in periods_out)

    return {
        "route": route,
        "plant_account": plant_account,
        "total_grid_kwh": quantity(total_grid),
        "total_submeter_kwh": quantity(total_sub),
        "total_loss_kwh": quantity(total_loss),
        "total_yuan": yuan_from_cents(total_cents),
        "total_cents": total_cents,
        "utility_invoice_yuan": quantity(_D(invoice_yuan)) if invoice_yuan is not None else None,
        "utility_invoice_cents": invoice_cents,
        "invoice_matches": invoice_matches,
        "balanced": invoice_matches and all(p["conservation"]["balanced"] for p in periods_out),
        "conservation_verified": all(p["conservation"]["balanced"] for p in periods_out),
        "allocations": allocation_map,
        "batch_allocations": batch_allocation,
        "plant_yuan": yuan_from_cents(allocations_cents.get(plant_account, 0)),
        "plant_overhead_yuan": yuan_from_cents(allocations_cents.get(plant_account, 0)),
        "clearing_yuan": yuan_from_cents(allocations_cents.get(CLEARING_ACCOUNT, 0)),
        "loss_kwh_by_account": {key: quantity(value) for key, value in loss_kwh_by_account.items()},
        "periods": periods_out,
        "open_boundaries": open_boundaries,
        "meter": meter,
    }


def allocate_costs(payload: dict[str, Any], route: str = "plant_overhead", **kwargs: Any) -> dict[str, Any]:
    """Rich cost reconciliation entry point."""

    route = kwargs.pop("settlement", kwargs.pop("method", route))
    return reconcile_bill(payload, route=route, **kwargs)


def allocate(
    first: Any = None,
    second: Any = None,
    tariff: list[dict[str, Any]] | None = None,
    route: str = "plant_overhead",
    *,
    payload: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Return beneficiary amounts in yuan.

    Accepts the full fixture/payload as the first argument, optionally with a
    route as the second positional argument, or the historical
    ``(readings, jobs, tariff=...)`` calling convention.
    """

    jobs: list[dict[str, Any]] | None
    if first is None and payload is not None:
        first = payload
    if isinstance(second, str):
        route = second
        jobs = None
    else:
        jobs = second
    route = kwargs.pop("settlement", kwargs.pop("method", kwargs.pop("route_name", route)))
    payload = _payload_from_args(first, jobs, tariff, kwargs)
    result = reconcile_bill(payload, route=route, **kwargs)
    return result["allocations"]


def verify_reconciliation(payload: dict[str, Any], routes: tuple[str, ...] = ("plant_overhead", "actual_run"), **kwargs: Any) -> dict[str, Any]:
    """Verify both contract-authorized routes against the same invoice."""

    results = {route: reconcile_bill(payload, route=route, **kwargs) for route in routes}
    totals = {route: item["total_cents"] for route, item in results.items()}
    if len(set(totals.values())) != 1:
        raise EnergyChargebackError(f"routes produce different totals: {totals}")
    return {
        "ok": all(item["balanced"] for item in results.values()),
        "total_yuan": results[routes[0]]["total_yuan"],
        "routes": {route: item["allocations"] for route, item in results.items()},
        "results": results,
    }


# Domain-language aliases.
reconcile_energy = reconcile_bill
reconcile = reconcile_bill
chargeback = allocate_costs
allocate_energy = allocate_costs
settle_energy = allocate_costs
split_energy_costs = allocate_costs
