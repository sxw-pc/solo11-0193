"""Physical meter register reconciliation.

This module is intentionally concerned with *energy*, not money or cost
beneficiaries.  A dial wrap, a meter replacement and a reset are different
physical events and must not be collapsed into one raw last-minus-first delta.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable

DEFAULT_DIAL_MAX_KWH = 10_000


class MeterReconciliationError(ValueError):
    """Base class for meter reconciliation failures."""


class NegativeConsumptionError(MeterReconciliationError):
    """A signed interval would imply negative energy consumption."""


class RolloverEvidenceError(MeterReconciliationError):
    """A wrap is missing or conflicts with the signed register history."""


class MeterIdentityError(MeterReconciliationError):
    """The meter/epoch changed without an attested replacement/reset."""


def _payload_and_readings(first: Any) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if isinstance(first, dict) and "readings" in first:
        return first, list(first["readings"] or [])
    if isinstance(first, list):
        return None, list(first)
    return None, []


def raw_delta(readings: list[dict[str, Any]] | dict[str, Any]) -> int | float:
    """Return the unsafe last-register minus first-register value.

    It is retained as a diagnostic for the seed defect.  Callers that need
    billable energy should use :func:`kwh` or :func:`reconcile_readings`.
    """

    _, rows = _payload_and_readings(readings)
    if len(rows) < 2:
        return 0
    return rows[-1]["register_kwh"] - rows[0]["register_kwh"]


def _dial_max(payload: dict[str, Any] | None, explicit: Any = None) -> int | float:
    if explicit is not None:
        return explicit
    if payload:
        meter = payload.get("grid_meter") or payload.get("meter") or {}
        for key in ("dial_max_kwh", "rollover_kwh", "register_max_kwh", "max_kwh"):
            if key in meter:
                return meter[key]
        for key in ("dial_max_kwh", "rollover_kwh"):
            if key in payload:
                return payload[key]
    return DEFAULT_DIAL_MAX_KWH


def _event_kind(event: dict[str, Any]) -> str:
    kind = str(event.get("kind") or event.get("type") or "").lower()
    aliases = {
        "wrap": "rollover",
        "dial_rollover": "rollover",
        "register_rollover": "rollover",
        "meter_rollover": "rollover",
        "change": "replacement",
        "meter_change": "replacement",
        "meter_replacement": "replacement",
        "replace": "replacement",
        "meter_reset": "reset",
        "reset_register": "reset",
    }
    return aliases.get(kind, kind)


def _events_from(payload: dict[str, Any] | None, explicit: Any = None) -> list[dict[str, Any]]:
    if explicit is not None:
        return list(explicit)
    if not payload:
        return []
    events: list[dict[str, Any]] = []
    for key in ("meter_events", "register_events", "events"):
        if isinstance(payload.get(key), list):
            events.extend(payload[key])
    for key in ("replacements", "resets", "rollover_events"):
        value = payload.get(key)
        if isinstance(value, list):
            for event in value:
                event = dict(event)
                event.setdefault("kind", key[:-1] if key.endswith("s") else key)
                events.append(event)
    return events


def _signed(record: dict[str, Any]) -> bool:
    if record.get("signed") is True or record.get("attested") is True:
        return True
    return bool(record.get("signed_by") and record.get("signature") not in (None, "", False))


def _meter_id(reading: dict[str, Any], payload: dict[str, Any] | None) -> str:
    meter = payload.get("grid_meter") if payload else None
    default_id = (meter or {}).get("id", "GRID") if isinstance(meter, dict) else "GRID"
    return str(reading.get("meter_id") or reading.get("meter") or default_id)


def _epoch(reading: dict[str, Any]) -> str:
    return str(reading.get("epoch", reading.get("register_epoch", reading.get("reset_id", "0"))))


def _event_matches_interval(
    event: dict[str, Any],
    previous: dict[str, Any],
    current: dict[str, Any],
) -> bool:
    if event.get("meter_id") and event["meter_id"] not in (
        previous.get("meter_id"),
        current.get("meter_id"),
    ):
        return False

    if event.get("between_reading_ids"):
        pair = list(event["between_reading_ids"])
        return pair == [previous.get("id"), current.get("id")] or pair == [
            current.get("id"),
            previous.get("id"),
        ]

    if event.get("after_reading_id"):
        return event["after_reading_id"] == previous.get("id")

    start = event.get("start_minute", event.get("from_minute"))
    end = event.get("end_minute", event.get("to_minute", event.get("at_minute")))
    at = event.get("at_minute")
    p_start = previous.get("at_minute", previous.get("minute"))
    c_end = current.get("at_minute", current.get("minute"))
    if at is not None and start is None and end is None:
        # An event stamped at the closing reading belongs to the interval
        # immediately before it; an event at the opening reading belongs to
        # the interval immediately after it.
        if at == c_end:
            return True
        if at == p_start:
            return False
        return p_start is not None and c_end is not None and p_start < at < c_end
    if start is not None or end is not None:
        return (start is None or start >= p_start) and (end is None or end <= c_end)
    return False


def _transition_event(
    events: Iterable[dict[str, Any]],
    previous: dict[str, Any],
    current: dict[str, Any],
    kind: str,
) -> dict[str, Any] | None:
    for event in events:
        if _event_kind(event) != kind:
            continue
        if _event_matches_interval(event, previous, current):
            return event
    return None


def _number(value: Any) -> int | float:
    if isinstance(value, bool):
        return int(value)
    return value


def interval_kwh(
    previous: dict[str, Any],
    current: dict[str, Any],
    *,
    dial_max_kwh: int | float = DEFAULT_DIAL_MAX_KWH,
    events: list[dict[str, Any]] | None = None,
    require_signed_rollover: bool = False,
    allow_inferred_rollover: bool = True,
) -> dict[str, Any]:
    """Compute one same-register interval without returning negative energy."""

    events = events or []
    before = _number(previous["register_kwh"])
    after = _number(current["register_kwh"])
    delta = after - before
    rollover_event = _transition_event(events, previous, current, "rollover")
    rollover_count = 0
    basis = "monotonic_register"

    if rollover_event is not None:
        rollover_count = int(rollover_event.get("count", rollover_event.get("rollover_count", 1)))
        if rollover_count < 0:
            raise RolloverEvidenceError("rollover count cannot be negative")
        if require_signed_rollover and not _signed(rollover_event):
            raise RolloverEvidenceError("rollover event is not signed/attested")
        basis = "signed_rollover_record" if _signed(rollover_event) else "rollover_record"
    elif delta < 0:
        if not allow_inferred_rollover:
            raise RolloverEvidenceError(
                f"negative register delta {delta} requires an attested rollover/replacement record"
            )
        if require_signed_rollover:
            raise RolloverEvidenceError("inferred rollover requires a signed rollover record")
        # The modular minimum is exactly one revolution.  Additional full
        # revolutions are never silently invented or zeroed: they must be
        # supplied by a signed event with an explicit count.
        rollover_count = 1
        basis = "inferred_single_rollover"

    used = delta + rollover_count * dial_max_kwh
    if used < 0:
        raise NegativeConsumptionError(
            f"interval {previous.get('id')}->{current.get('id')} gives negative use {used}"
        )

    return {
        "start_reading_id": previous.get("id"),
        "end_reading_id": current.get("id"),
        "meter_id": current.get("meter_id") or previous.get("meter_id"),
        "start_minute": current.get("period_start_minute", previous.get("at_minute")),
        "end_minute": current.get("at_minute"),
        "start_register_kwh": before,
        "end_register_kwh": after,
        "raw_delta_kwh": delta,
        "rollover_count": rollover_count,
        "dial_max_kwh": dial_max_kwh,
        "kwh": used,
        "basis": basis,
    }


def _effective_readings(
    readings: list[dict[str, Any]],
    overrides: dict[str, dict[str, Any]] | None = None,
    extra_readings: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    rows = [deepcopy(row) for row in readings]
    by_id = {row.get("id"): row for row in rows}
    for reading_id, changes in (overrides or {}).items():
        if reading_id not in by_id:
            raise MeterReconciliationError(f"unknown reading id {reading_id!r} in override")
        by_id[reading_id].update(deepcopy(changes))
    for extra in extra_readings or []:
        extra = deepcopy(extra)
        if extra.get("id") in by_id:
            by_id[extra["id"]].update(extra)
        else:
            rows.append(extra)
            by_id[extra["id"]] = extra
    return rows


def reconcile_readings(
    readings: list[dict[str, Any]] | dict[str, Any],
    *,
    dial_max_kwh: int | float | None = None,
    events: list[dict[str, Any]] | None = None,
    meter_events: list[dict[str, Any]] | None = None,
    overrides: dict[str, dict[str, Any]] | None = None,
    extra_readings: list[dict[str, Any]] | None = None,
    require_signed_readings: bool = False,
    require_signed_rollover: bool = False,
    allow_inferred_rollover: bool = True,
) -> dict[str, Any]:
    """Reconcile a signed, ordered register history.

    Readings on different meter ids/epochs are not subtracted from each other.
    A replacement or reset record must attest to the identity transition.
    Energy is summed only from adjacent readings within the same epoch.
    """

    payload, rows = _payload_and_readings(readings)
    if isinstance(readings, dict):
        payload = readings
    cap = _dial_max(payload, dial_max_kwh)
    all_events = list(meter_events or [])
    if events is not None:
        all_events.extend(events)
    else:
        all_events.extend(_events_from(payload))

    rows = _effective_readings(rows, overrides, extra_readings)

    # A work order may carry its boundary register values directly instead of
    # supplying separate signed reading rows.  Materialize them so the normal
    # same-epoch interval logic remains the only energy calculation path.
    virtual_rows: list[dict[str, Any]] = []
    for event in all_events:
        kind = _event_kind(event)
        if kind not in {"replacement", "reset"}:
            continue
        at_minute = event.get("at_minute", event.get("transition_minute"))
        old_meter_id = event.get("old_meter_id", event.get("meter_id", _meter_id({}, payload)))
        new_meter_id = event.get("new_meter_id", event.get("meter_id", _meter_id({}, payload)))
        old_epoch = str(event.get("old_epoch", "0"))
        new_epoch = str(event.get("new_epoch", "1" if kind == "reset" else "0"))

        old_reading = event.get("old_final_reading")
        if isinstance(old_reading, dict):
            row = deepcopy(old_reading)
            row.setdefault("at_minute", at_minute)
            row.setdefault("meter_id", old_meter_id)
            row.setdefault("epoch", old_epoch)
            virtual_rows.append(row)
        elif event.get("old_final_register_kwh") is not None and at_minute is not None:
            virtual_rows.append(
                {
                    "id": f"{event.get('id', 'EVENT')}:OLD-FINAL",
                    "at_minute": at_minute,
                    "sequence": 0,
                    "meter_id": old_meter_id,
                    "epoch": old_epoch,
                    "register_kwh": event["old_final_register_kwh"],
                    "signed_by": event.get("signed_by"),
                    "signature": event.get("signature"),
                }
            )

        new_reading = event.get("new_initial_reading")
        if isinstance(new_reading, dict):
            row = deepcopy(new_reading)
            row.setdefault("at_minute", at_minute)
            row.setdefault("meter_id", new_meter_id)
            row.setdefault("epoch", new_epoch)
            row.setdefault("sequence", 1)
            virtual_rows.append(row)
        elif event.get("new_initial_register_kwh") is not None and at_minute is not None:
            virtual_rows.append(
                {
                    "id": f"{event.get('id', 'EVENT')}:NEW-INIT",
                    "at_minute": at_minute,
                    "sequence": 1,
                    "meter_id": new_meter_id,
                    "epoch": new_epoch,
                    "register_kwh": event["new_initial_register_kwh"],
                    "signed_by": event.get("signed_by"),
                    "signature": event.get("signature"),
                }
            )
    rows.extend(virtual_rows)

    for row in rows:
        row.setdefault("meter_id", _meter_id(row, payload))
        row.setdefault("epoch", _epoch(row))
        if require_signed_readings and not _signed(row):
            raise MeterReconciliationError(f"reading {row.get('id')!r} is not signed/attested")

    # Preserve source order for equal-timestamp boundary reads unless an
    # explicit sequence says otherwise.
    ordered = sorted(
        enumerate(rows),
        key=lambda item: (
            item[1].get("at_minute", item[1].get("minute", 0)),
            item[1].get("sequence", 0),
            item[0],
        ),
    )
    ordered = [row for _, row in ordered]

    # Verify every identity/epoch transition has the physical work record and
    # usable old-final/new-initial boundary readings.
    for previous, current in zip(ordered, ordered[1:]):
        if previous["meter_id"] == current["meter_id"] and previous["epoch"] == current["epoch"]:
            continue
        kind = "replacement" if previous["meter_id"] != current["meter_id"] else "reset"
        event = _transition_event(all_events, previous, current, kind)
        if event is None:
            raise MeterIdentityError(
                f"{kind} between {previous.get('id')} and {current.get('id')} lacks an attested record"
            )
        if require_signed_readings and not _signed(event):
            raise MeterIdentityError(f"{kind} record {event.get('id')!r} is not signed")
        transition_minute = event.get("at_minute", event.get("transition_minute"))
        has_inline_registers = (
            event.get("old_final_register_kwh") is not None
            and event.get("new_initial_register_kwh") is not None
        ) or (
            isinstance(event.get("old_final_reading"), dict)
            and isinstance(event.get("new_initial_reading"), dict)
        )
        if (
            transition_minute is not None
            and not (
                previous.get("at_minute") == transition_minute
                and current.get("at_minute") == transition_minute
            )
            and not has_inline_registers
        ):
            raise MeterIdentityError(
                f"{kind} {event.get('id')!r} requires signed old-final and new-initial boundary readings"
            )

    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in ordered:
        groups.setdefault((row["meter_id"], row["epoch"]), []).append(row)

    intervals: list[dict[str, Any]] = []
    for group_rows in groups.values():
        group_rows = sorted(
            group_rows,
            key=lambda r: (r.get("at_minute", r.get("minute", 0)), r.get("sequence", 0)),
        )
        for previous, current in zip(group_rows, group_rows[1:]):
            intervals.append(
                interval_kwh(
                    previous,
                    current,
                    dial_max_kwh=cap,
                    events=all_events,
                    require_signed_rollover=require_signed_rollover,
                    allow_inferred_rollover=allow_inferred_rollover,
                )
            )

    intervals.sort(key=lambda item: (item["start_minute"] is None, item["start_minute"], str(item["start_reading_id"])))
    total = sum(item["kwh"] for item in intervals)
    return {
        "meter_id": (payload or {}).get("grid_meter", {}).get("id") if payload else None,
        "dial_max_kwh": cap,
        "intervals": intervals,
        "kwh": total,
        "total_kwh": total,
        "raw_delta_kwh": raw_delta(rows),
    }


def kwh(
    first: list[dict[str, Any]] | dict[str, Any] | int | float | None = None,
    second: dict[str, Any] | int | float | None = None,
    *,
    readings: list[dict[str, Any]] | dict[str, Any] | None = None,
    dial_max_kwh: int | float | None = None,
    rollover_kwh: int | float | None = None,
    events: list[dict[str, Any]] | None = None,
    **kwargs: Any,
) -> int | float:
    """Billable energy for a wrapped register history or one adjacent pair.

    The old seed-compatible unsafe diagnostic is available as
    :func:`raw_delta`.  With the seed's 10,000 dial this returns 40 rather
    than -9,960.
    """

    target = readings if readings is not None else first
    if target is None:
        raise MeterReconciliationError("readings are required")
    if dial_max_kwh is None:
        dial_max_kwh = rollover_kwh

    # Convenience forms: kwh(old_reading, new_reading) or kwh(9990, 16).
    if second is not None:
        cap = DEFAULT_DIAL_MAX_KWH if dial_max_kwh is None else dial_max_kwh
        if isinstance(target, dict) and isinstance(second, dict):
            return interval_kwh(
                target,
                second,
                dial_max_kwh=cap,
                events=events or _events_from(target if "readings" in target else None),
                **{key: value for key, value in kwargs.items() if key in {"require_signed_rollover", "allow_inferred_rollover"}},
            )["kwh"]
        before = _number(target)  # type: ignore[arg-type]
        after = _number(second)  # type: ignore[arg-type]
        delta = after - before
        if delta < 0:
            event_count = 1
            if events:
                event_count = sum(int(e.get("count", e.get("rollover_count", 1))) for e in events if _event_kind(e) == "rollover")
            delta += event_count * cap
        if delta < 0:
            raise NegativeConsumptionError(f"register pair implies negative use {delta}")
        return delta

    return reconcile_readings(target, dial_max_kwh=dial_max_kwh, events=events, **kwargs)["kwh"]


def reconcile_meter(payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Convenience wrapper used by chargeback and capture layers."""

    return reconcile_readings(payload, **kwargs)


# Descriptive aliases for callers/tests using the domain language.
billable_kwh = kwh
verified_kwh = kwh
meter_use = kwh
consumption = kwh


def period_consumption(readings: list[dict[str, Any]] | dict[str, Any], **kwargs: Any) -> list[dict[str, Any]]:
    return reconcile_readings(readings, **kwargs)["intervals"]


def reconcile_meters(payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    return reconcile_readings(payload, **kwargs)
