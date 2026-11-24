"""Arrival shelf-life channel checks, legal routes and commitment correction.

Wholesale needs >= 6 months remaining *at customer arrival*; local needs
>= 1 month.  The seed :func:`channel` mean/nominal rule is retained for the
seed mis-placement characterisation; real decisions go through
:func:`evaluate` / :func:`route_a` / :func:`route_b` and the commitment
ledger (:func:`commit_wholesale`, :func:`reconcile_commitments`).
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any, Iterable

from .seal_atmosphere import PackageLot, load_lot, parse_dt
from .shelf_life import assess_lot, months as _seed_months

DEFAULT_CHANNELS = [
    {"id": "wholesale", "minimum_remaining_months": 6},
    {"id": "local", "minimum_remaining_months": 1},
]
CHANNEL_MIN_ALIASES = {
    "wholesale": ("wholesale", "wholesale_minimum_months", "min_wholesale_months"),
    "local": ("local", "local_minimum_months", "min_local_months"),
}


class ChannelCommitmentError(RuntimeError):
    """Raised when a channel commitment cannot be made on current evidence."""

    def __init__(self, message: str, evaluation: dict | None = None):
        super().__init__(message)
        self.evaluation = evaluation


class ChannelDecision(dict):
    """Full evaluation mapping that also compares equal to its channel id."""

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, str):
            return self.get("channel") == other
        if other is None:
            return self.get("channel") is None
        return dict.__eq__(self, other)

    def __hash__(self):
        return id(self)

    def __str__(self):
        return str(self.get("channel"))


# ---------------------------------------------------------------------------
# Seed legacy channel rule (documents the mis-placement into wholesale)
# ---------------------------------------------------------------------------

def channel(target, **kw) -> "str | None":
    """Seed nominal channel for a sample list; corrected channel for a lot.

    List input keeps the exact seed behaviour (nominal months >= 6 ->
    ``'wholesale'`` else ``'local'``).  Fixture / :class:`PackageLot` input
    runs the corrected arrival-life assessment and returns the recommended
    channel id (``'wholesale'`` / ``'local'`` / ``None``); use
    :func:`evaluate` for the full per-pouch decision.
    """
    if isinstance(target, (list, tuple)):
        life = _seed_months(target)
        return "wholesale" if life >= 6 else "local"
    if isinstance(target, dict) and "oxygen_pct" in target and "stability_evidence" not in target:
        life = _seed_months([target])
        return "wholesale" if life >= 6 else "local"
    return evaluate(target, **kw)


def channel_decision(target, **kw) -> dict:
    """Full channel evaluation (mapping form)."""
    return evaluate(target, **kw)


def naive_channel(samples) -> str:
    """Explicit name for the flawed seed mean/nominal decision."""
    life = _seed_months(samples)
    return "wholesale" if life >= 6 else "local"


# ---------------------------------------------------------------------------
# Channel requirements / arrival checks
# ---------------------------------------------------------------------------

def channel_requirements(data: Any) -> dict[str, float]:
    rows = DEFAULT_CHANNELS
    if isinstance(data, PackageLot):
        data = data.data
    if isinstance(data, dict) and isinstance(data.get("channels"), list) and data["channels"]:
        rows = data["channels"]
    requirements: dict[str, float] = {}
    for row in rows:
        cid = row.get("id") or row.get("channel")
        minimum = row.get("minimum_remaining_months", row.get("minimum_months", row.get("min_months")))
        if cid is not None and minimum is not None:
            requirements[str(cid)] = float(minimum)
    requirements.setdefault("wholesale", 6.0)
    requirements.setdefault("local", 1.0)
    return requirements


def check_arrival(remaining_months: float, channel_id: str, data: Any = None,
                  minimum: float | None = None) -> dict:
    """Check one remaining-life number against a channel's arrival minimum."""
    if minimum is None:
        minimum = channel_requirements(data).get(str(channel_id), 1.0 if channel_id == "local" else 6.0)
    remaining = max(0.0, float(remaining_months))
    return {
        "channel": str(channel_id),
        "minimum_remaining_months": float(minimum),
        "remaining_months": remaining,
        "allowed": remaining + 1e-12 >= float(minimum),
        "shortfall_months": max(0.0, float(minimum) - remaining),
    }


arrival_check = check_arrival


def check_channel(*args: Any, **kw: Any) -> dict:
    """Flexible channel check accepting either positional argument order.

    ``check_channel(6.2, 'wholesale')`` and
    ``check_channel('wholesale', 6.2)`` both return the arrival-check mapping.
    """
    remaining = kw.pop("remaining_months", kw.pop("remaining", None))
    channel_id = kw.pop("channel_id", kw.pop("channel", None))
    positional = list(args)
    data = kw.pop("data", None)
    if len(positional) >= 2:
        first, second = positional[0], positional[1]
        if isinstance(first, str):
            channel_id = channel_id or first
            remaining = remaining if remaining is not None else second
        else:
            remaining = remaining if remaining is not None else first
            channel_id = channel_id or second
        positional = positional[2:]
    if positional and data is None:
        data = positional[0]
    if remaining is None or channel_id is None:
        raise TypeError("check_channel requires remaining months and a channel id")
    return check_arrival(float(remaining), str(channel_id), data, **kw)


def channel_allows(*args: Any, **kw: Any) -> bool:
    return bool(check_channel(*args, **kw)["allowed"])


def _coerce_lot(target: Any) -> PackageLot:
    if isinstance(target, PackageLot):
        return target
    if isinstance(target, (dict, list, str, Path)):
        return load_lot(target)
    raise TypeError(f"expected PackageLot or package-life data, got {type(target)!r}")


def _channel_blocks(life: dict, requirements: dict[str, float], required_units: int | None = None) -> dict:
    pouches = life["pouches"]
    total = life["pouch_count"]
    blocks: dict[str, dict] = {}
    for cid, minimum in requirements.items():
        eligible: list[str] = []
        limiting: list[tuple[str, float, list]] = []
        for code, verdict in pouches.items():
            rem = verdict["remaining_months"]
            if not verdict["blocked"] and rem + 1e-12 >= minimum:
                eligible.append(code)
            else:
                limiting.append((code, rem, verdict.get("gate_reasons", [])))
        limiting.sort(key=lambda item: item[1])
        need = total if required_units is None else int(required_units)
        worst_remaining = min((v["remaining_months"] for v in pouches.values()), default=0.0)
        whole_lot_ok = len(eligible) >= total
        order_ok = len(eligible) >= need
        blocks[cid] = {
            "id": cid,
            "channel": cid,
            "minimum_remaining_months": minimum,
            "allowed": whole_lot_ok if required_units is None else order_ok,
            "whole_lot_allowed": whole_lot_ok,
            "order_allowed": order_ok,
            "required_units": need,
            "eligible_units": len(eligible),
            "available_units": len(eligible),
            "remaining_months": worst_remaining,
            "shortfall_months": max(0.0, minimum - worst_remaining),
            "eligible_codes": eligible,
            "limiting_pouches": [
                {"pouch": code, "remaining_months": rem, "gate_reasons": reasons}
                for code, rem, reasons in limiting[:10]
            ],
        }
    return blocks


# ---------------------------------------------------------------------------
# Corrected evaluation and the two legal routes
# ---------------------------------------------------------------------------

def evaluate(
    target: Any,
    *,
    arrival_at: Any = None,
    arrival: Any = None,
    as_of: Any = None,
    at: Any = None,
    now: Any = None,
    elapsed_days: float | None = None,
    storage_days: float | None = None,
    warehouse_days: float | None = None,
    days_in_storage: float | None = None,
    transport_days: float = 0.0,
    shipping_days: float | None = None,
    required_units: int | None = None,
) -> dict:
    """Full corrected package-life / channel evaluation for the physical lot."""
    lot = _coerce_lot(target)
    arrival_at = arrival_at or arrival
    as_of = as_of or at or now
    if elapsed_days is None:
        storage = storage_days if storage_days is not None else warehouse_days
        if storage is None:
            storage = days_in_storage
        if storage is not None:
            elapsed_days = float(storage)
    if shipping_days is not None:
        transport_days = shipping_days
    life = assess_lot(
        lot,
        arrival_at=arrival_at,
        as_of=as_of,
        elapsed_days=elapsed_days,
        transport_days=transport_days,
    )
    requirements = channel_requirements(lot.data)
    channels = _channel_blocks(life, requirements, required_units)

    if channels.get("wholesale", {}).get("allowed"):
        recommended = "wholesale"
    elif channels.get("local", {}).get("allowed"):
        recommended = "local"
    else:
        recommended = None

    if life["all_repacked"]:
        route = "A"
        route_name = "nitrogen_repack"
    else:
        route = "B"
        route_name = "as_packed"

    ws_allowed = bool(channels.get("wholesale", {}).get("allowed"))
    local_ok = bool(channels.get("local", {}).get("allowed"))
    return ChannelDecision({
        "blend": life["blend"],
        "route": route,
        "legal_route": route,
        "process": life["process"],
        "route_name": route_name,
        "channel": recommended,
        "recommended_channel": recommended,
        "sellable": recommended is not None,
        "wholesale_allowed": ws_allowed,
        "local_allowed": local_ok,
        "can_wholesale": ws_allowed,
        "can_local": local_ok,
        "oxygen_upper_pct": life["oxygen_upper_pct"],
        "heat_days_at_28c": life["heat_days_at_28c"],
        "granted_months": life["granted_months"],
        "max_months": life["max_months"],
        "shelf_life_months": life["granted_months"],
        "seed_nominal_months": life.get("seed_nominal_months"),
        "nominal_months": life.get("nominal_months"),
        "remaining_months": life["remaining_months"],
        "arrival_remaining_months": life["remaining_months"],
        "remaining_shelf_life_months": life["remaining_months"],
        "remaining_days": life["remaining_days"],
        "expires_at": life.get("expires_at"),
        "repacked_count": life["repacked_count"],
        "all_repacked": life["all_repacked"],
        "gate_reasons": life["gate_reasons"],
        "channels": channels,
        "wholesale": channels.get("wholesale"),
        "local": channels.get("local"),
        "pouches": life["pouches"],
        "lot": lot,
        "arrival_at": arrival_at or as_of,
    })


decide = evaluate
decide_channel = evaluate
shipping_decision = evaluate
choose_channel = evaluate
assess_channels = evaluate


def route_b(
    target: Any,
    *,
    arrival_at: Any = None,
    as_of: Any = None,
    at: Any = None,
    now: Any = None,
    elapsed_days: float | None = None,
    transport_days: float = 0.0,
) -> dict:
    """Legal route B: no repack, original codes, <= 2 months, local only."""
    lot = _coerce_lot(target)
    as_of = as_of or at or now
    life = assess_lot(
        lot,
        process="as_packed",
        arrival_at=arrival_at,
        as_of=as_of,
        elapsed_days=elapsed_days,
        transport_days=transport_days,
    )
    requirements = channel_requirements(lot.data)
    channels = _channel_blocks(life, requirements)
    local = channels["local"]
    wholesale = channels["wholesale"]
    legal = bool(local["allowed"])
    return {
        "route": "B",
        "name": "no_repack_short_life_local",
        "repack": False,
        "codes_preserved": True,
        "original_codes": list(lot.codes),
        "new_codes": [],
        "max_months": life["granted_months"],
        "granted_months": life["granted_months"],
        "remaining_months": life["remaining_months"],
        "oxygen_upper_pct": life["oxygen_upper_pct"],
        "heat_days_at_28c": life["heat_days_at_28c"],
        "channels": channels,
        "local": local,
        "wholesale": wholesale,
        "local_allowed": bool(local["allowed"]),
        "wholesale_allowed": False,
        "channel": "local" if legal else None,
        "legal": legal,
        "sellable": legal,
        "expires_at": life.get("expires_at"),
        "gate_reasons": life["gate_reasons"],
        "pouches": life["pouches"],
    }


def route_a(
    target: Any,
    *,
    at: Any = None,
    repack_at: Any = None,
    arrival_at: Any = None,
    as_of: Any = None,
    elapsed_days: float | None = None,
    transport_days: float = 0.0,
    oxygen_pct: float = 1.8,
    instrument_error_pct: float = 0.2,
    execute: bool = True,
) -> dict:
    """Legal route A: all 100 pouches physically nitrogen-repacked.

    Every pouch must complete unpack -> N2 fill -> reseal -> passing retest
    and receive a new code.  The nitrogen evidence grants <= 10 months;
    wholesale re-commitment only when >= 6 months remain at arrival.
    """
    lot = _coerce_lot(target)
    when = repack_at or at
    if execute:
        pending = list(lot.pending_repack_codes)
        if pending:
            lot.repack_codes(
                pending,
                at=when,
                oxygen_pct=oxygen_pct,
                instrument_error_pct=instrument_error_pct,
            )
    life = assess_lot(
        lot,
        process="nitrogen_repack",
        arrival_at=arrival_at,
        as_of=as_of,
        elapsed_days=elapsed_days,
        transport_days=transport_days,
    )
    requirements = channel_requirements(lot.data)
    channels = _channel_blocks(life, requirements)
    wholesale = channels["wholesale"]
    local = channels["local"]
    all_closed = lot.all_repacked
    wholesale_legal = all_closed and bool(wholesale["allowed"])
    return {
        "route": "A",
        "name": "full_nitrogen_repack_new_codes",
        "repack": True,
        "all_pouches_repacked": all_closed,
        "repacked_count": lot.passed_repack_count,
        "repack_capacity_consumed": len(lot.codes),
        "old_codes_retired": lot.retired_codes(),
        "new_codes": lot.sellable_codes(),
        "max_months": life["granted_months"],
        "granted_months": life["granted_months"],
        "remaining_months": life["remaining_months"],
        "oxygen_upper_pct": life["oxygen_upper_pct"],
        "heat_days_at_28c": life["heat_days_at_28c"],
        "heat_history_retained": True,
        "channels": channels,
        "wholesale": wholesale,
        "local": local,
        "wholesale_allowed": wholesale_legal,
        "local_allowed": bool(local["allowed"]),
        "channel": "wholesale" if wholesale_legal else ("local" if local["allowed"] else None),
        "wholesale_recommit_allowed": wholesale_legal,
        "legal": all_closed and (wholesale_legal or local["allowed"]),
        "sellable": all_closed and (wholesale_legal or local["allowed"]),
        "expires_at": life.get("expires_at"),
        "gate_reasons": life["gate_reasons"],
        "pouches": life["pouches"],
    }


plan_route_a = route_a
plan_route_b = route_b


# ---------------------------------------------------------------------------
# Order fulfilment by physical codes
# ---------------------------------------------------------------------------

def fulfill_order(
    target: Any,
    channel_id: str = "local",
    *,
    units: int = 100,
    arrival_at: Any = None,
    at: Any = None,
    order_id: str | None = None,
) -> dict:
    """Select concrete eligible physical codes for a channel order."""
    lot = _coerce_lot(target)
    decision = evaluate(lot, arrival_at=arrival_at)
    block = decision["channels"][channel_id]
    codes = list(block["eligible_codes"][: int(units)])
    shipment = {
        "order_id": order_id or f"ORD-{len(getattr(lot, 'shipments', [])) + 1 if hasattr(lot, 'shipments') else 1}",
        "channel": channel_id,
        "units_requested": int(units),
        "units_fulfilled": len(codes),
        "pouch_codes": [lot.resolve(c)["active_code"] for c in codes],
        "original_codes": codes,
        "arrival_at": arrival_at,
        "shipped_at": at,
        "complete": len(codes) >= int(units),
    }
    if not hasattr(lot, "shipments"):
        lot.shipments = []
    lot.shipments.append(shipment)
    return shipment


ship_order = fulfill_order
ship = fulfill_order


# ---------------------------------------------------------------------------
# Wholesale commitments, revocation and customer correction evidence
# ---------------------------------------------------------------------------

def _expires_at(term_start: Any, granted_months: float) -> str | None:
    start = parse_dt(term_start)
    if start is None:
        return term_start if isinstance(term_start, str) else None
    return (start + timedelta(days=granted_months * 30.0)).isoformat()


def commit_wholesale(
    target: Any,
    customer: str | None = None,
    *,
    customer_name: str | None = None,
    name: str | None = None,
    customer_order: str | None = None,
    order_id: str | None = None,
    at: Any = None,
    sent_at: Any = None,
    arrival_at: Any = None,
    units: int = 100,
    force: bool = False,
    **extra: Any,
) -> dict:
    """Issue a wholesale commitment only when arrival life clears 6 months.

    Use :func:`record_commitment` with ``force=True`` to model an already-sent
    erroneous seed promise; such a promise must then be reconciled and
    corrected rather than silently edited.
    """
    customer = customer or customer_name or name
    if not customer:
        raise ValueError("commit_wholesale requires a customer name")
    lot = _coerce_lot(target)
    issued_at = sent_at or at
    decision = evaluate(lot, arrival_at=arrival_at, required_units=units)
    ws = decision["channels"]["wholesale"]
    if not ws["allowed"] and not force:
        raise ChannelCommitmentError(
            f"wholesale commitment to {customer!r} blocked: only "
            f"{ws['eligible_units']}/{units} units hold >= "
            f"{ws['minimum_remaining_months']:g} months at arrival",
            decision,
        )

    lot._seq_commitment += 1
    commitment = {
        "id": f"WSC-{lot._seq_commitment}",
        "kind": "wholesale",
        "channel": "wholesale",
        "customer": customer,
        "customer_order": customer_order or order_id,
        "units": int(units),
        "sent_at": issued_at,
        "committed_at": issued_at,
        "contractual_arrival_at": arrival_at,
        "minimum_remaining_months": ws["minimum_remaining_months"],
        "promised_remaining_months": ws["remaining_months"],
        "promised_channel": "wholesale",
        "expires_at": decision.get("expires_at") or _expires_at(lot.data.get("packed_at"), decision["granted_months"]),
        "status": "sent",
        "forced": bool(force) and not ws["allowed"],
        "basis": {
            "route": decision["route"],
            "oxygen_upper_pct": decision["oxygen_upper_pct"],
            "heat_days_at_28c": decision["heat_days_at_28c"],
            "granted_months": decision["granted_months"],
            "remaining_months_at_commit_check": ws["remaining_months"],
            "allowed_at_issue": bool(ws["allowed"]),
        },
        "correction_ids": [],
        **extra,
    }
    lot.commitments.append(commitment)
    lot._record_event("wholesale_commitment_sent", {"id": commitment["id"], "customer": customer})
    return commitment


def record_commitment(target: Any, customer: str | None = None, *, force: bool = True, **kw: Any) -> dict:
    """Record an already-dispatched promise (default: even if unverified)."""
    if customer is not None:
        kw["customer"] = customer
    return commit_wholesale(target, force=force, **kw)


send_wholesale_commitment = commit_wholesale


def _infer_reason(lot: PackageLot) -> str:
    for event in reversed(lot.events[-6:]):
        kind = event.get("kind", "")
        if kind == "heat_log_added":
            return "late_heat_log"
        if kind == "oxygen_reading":
            return "high_oxygen_retest"
        if kind == "pouch_repacked" and not event.get("detail", {}).get("passed", True):
            return "high_oxygen_retest"
    return "shelf_life_reassessment"


def _issue_correction(lot: PackageLot, commitment: dict, decision: dict, *, at: Any,
                      reason: str | None) -> dict:
    lot._seq_correction += 1
    reason = reason or _infer_reason(lot)
    recommended = decision["channel"]
    revised_ws = decision["channels"].get("wholesale", {})
    revised_local = decision["channels"].get("local", {})
    correction = {
        "correction_id": f"CORR-{lot._seq_correction}",
        "id": f"CORR-{lot._seq_correction}",
        "kind": "wholesale_revocation_and_customer_correction",
        "commitment_id": commitment["id"],
        "customer": commitment["customer"],
        "customer_order": commitment.get("customer_order"),
        "issued_at": at,
        "reason": reason,
        "previous": {
            "channel": "wholesale",
            "promised_minimum_remaining_months": commitment["minimum_remaining_months"],
            "promised_remaining_months": commitment.get("promised_remaining_months"),
            "expires_at": commitment.get("expires_at"),
            "status": "sent",
        },
        "revised": {
            "channel": recommended,
            "sellable": decision["sellable"],
            "remaining_months": decision["remaining_months"],
            "oxygen_upper_pct": decision["oxygen_upper_pct"],
            "heat_days_at_28c": decision["heat_days_at_28c"],
            "granted_months": decision["granted_months"],
            "wholesale_allowed": revised_ws.get("allowed", False),
            "local_allowed": revised_local.get("allowed", False),
            "eligible_units": {
                "wholesale": revised_ws.get("eligible_units", 0),
                "local": revised_local.get("eligible_units", 0),
            },
            "gate_reasons": decision.get("gate_reasons", []),
        },
        "actions": ["commitment_revoked", "customer_correction_notice_issued"],
        "notice": (
            f"{commitment['customer']} 您好：编号 {commitment['id']}（订单 "
            f"{commitment.get('customer_order') or '-'}）的批发到货保质期承诺现予撤销并更正。"
            f"触发原因：{reason}。按最新物理袋码证据重算，到货剩余保质期为 "
            f"{decision['remaining_months']:.2f} 个月（氧气保守上界 "
            f"{decision['oxygen_upper_pct']:.2f}%，28°C 实际暴露 "
            f"{decision['heat_days_at_28c']:g} 天）。批发渠道要求到货至少 6 个月，"
            f"原承诺不再成立；当前合法渠道为：{recommended or '无（整批隔离/不得销售）'}。"
            f"请注意：本更正为追加证据，原承诺记录保留备查。"
        ),
        "status": "issued",
        "delivery": {"status": "pending", "method": "offline_correction_notice", "delivered_at": None},
    }
    lot.corrections.append(correction)
    commitment["status"] = "revoked"
    commitment["revoked_at"] = at
    commitment["correction_ids"].append(correction["correction_id"])
    lot._record_event(
        "wholesale_commitment_revoked",
        {"commitment": commitment["id"], "correction": correction["correction_id"], "reason": reason},
    )
    return correction


def reconcile_commitments(
    target: Any,
    *,
    at: Any = None,
    arrival_at: Any = None,
    reason: str | None = None,
) -> list[dict]:
    """Recheck every live commitment against the latest physical evidence.

    Any wholesale promise overturned by a late heat log / high-oxygen
    retest / transport delay is revoked and receives a customer correction
    record.  Historical commitment rows are never overwritten in place.
    Idempotent: already-revoked commitments are not corrected twice.
    """
    lot = _coerce_lot(target)
    corrections: list[dict] = []
    for commitment in lot.commitments:
        if commitment.get("status") == "revoked":
            continue
        check_arrival_at = arrival_at or commitment.get("contractual_arrival_at")
        decision = evaluate(lot, arrival_at=check_arrival_at, required_units=commitment.get("units"))
        block = decision["channels"].get(commitment["channel"], decision["channels"]["wholesale"])
        units = int(commitment.get("units", 100))
        still_good = bool(block.get("allowed")) and block.get("eligible_units", 0) >= units
        if not still_good:
            corrections.append(_issue_correction(lot, commitment, decision, at=at, reason=reason))
    return corrections


reconcile = reconcile_commitments
reassess_commitments = reconcile_commitments


def deliver_correction(target: Any, correction_id: str, *, at: Any = None, via: str = "registered_notice") -> dict:
    """Record delivery evidence for a customer correction notice."""
    lot = _coerce_lot(target)
    for correction in lot.corrections:
        if correction["correction_id"] == correction_id or correction.get("id") == correction_id:
            correction["delivery"] = {"status": "delivered", "method": via, "delivered_at": at}
            return correction
    raise KeyError(f"unknown correction {correction_id!r}")


def revocation_evidence(target: Any) -> list[dict]:
    lot = _coerce_lot(target)
    return list(lot.corrections)


correction_evidence = revocation_evidence
customer_corrections = revocation_evidence


# ---------------------------------------------------------------------------
# Convenience predicates and route enumeration
# ---------------------------------------------------------------------------

def wholesale_allowed(target: Any, **kw: Any) -> bool:
    return bool(evaluate(target, **kw)["channels"]["wholesale"]["allowed"])


def local_allowed(target: Any, **kw: Any) -> bool:
    return bool(evaluate(target, **kw)["channels"]["local"]["allowed"])


can_ship_wholesale = wholesale_allowed
can_ship_local = local_allowed


def can_ship(target: Any, channel_id: str = "local", **kw: Any) -> bool:
    block = evaluate(target, **kw)["channels"].get(channel_id)
    return bool(block and block["allowed"])


def legal_routes(target: Any, *, at: Any = None, arrival_at: Any = None, **kw: Any) -> dict:
    """Evaluate both legal business choices for the same physical case.

    Route A is executed on an isolated copy so its repacks / new codes do not
    mutate the route-B state.
    """
    import copy

    base = _coerce_lot(target)
    lot_b = copy.deepcopy(base)
    lot_a = copy.deepcopy(base)
    plan_b = route_b(lot_b, arrival_at=arrival_at, **kw)
    plan_a = route_a(lot_a, at=at, arrival_at=arrival_at, **kw)
    return {"A": plan_a, "B": plan_b, "route_a": plan_a, "route_b": plan_b}


evaluate_routes = legal_routes
route_options = legal_routes


def revoke_commitment(target: Any, commitment_id: str, *, at: Any = None,
                      arrival_at: Any = None, reason: str = "manual_revocation") -> dict:
    """Directly revoke one commitment and issue its customer correction."""
    lot = _coerce_lot(target)
    for commitment in lot.commitments:
        if commitment["id"] != commitment_id:
            continue
        check_at = arrival_at or commitment.get("contractual_arrival_at")
        decision = evaluate(lot, arrival_at=check_at)
        return _issue_correction(lot, commitment, decision, at=at, reason=reason)
    raise KeyError(f"unknown commitment {commitment_id!r}")


def issue_correction(target: Any, commitment_id: str, **kw: Any) -> dict:
    return revoke_commitment(target, commitment_id, **kw)


# Extra high-level aliases.
assess_package = evaluate
package_decision = evaluate
plan_shipping = evaluate
decide_shipping = evaluate
shipping_channel = evaluate
channel_for = evaluate
resolve_channel = channel
commit = commit_wholesale
revoke = reconcile_commitments
