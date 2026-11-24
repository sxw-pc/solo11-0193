"""Append-only capture, posting and responsible revision of energy costs.

The ledger never mutates or deletes a posted row.  A late calibration, dispute
resolution or meter-replacement work order creates a new version and signed
delta rows; the original invoice and originally reported batch costs remain
available for audit.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

from .energy_chargeback import (
    CLEARING_ACCOUNT,
    PLANT_ACCOUNT,
    AllocationBoundaryOpen,
    AllocationMap,
    reconcile_bill,
    yuan_from_cents,
)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class EnergyLedgerError(ValueError):
    """Invalid append-only energy ledger operation."""


class EnergyCapture:
    """Event-sourced energy invoice/cost ledger.

    The class is intentionally storage-light: tests and CLI callers can use an
    in-memory instance.  Every mutation is appended to :attr:`events` and every
    posted version retains its own entry rows.
    """

    def __init__(
        self,
        payload: dict[str, Any] | str | "PathLike[str]" | None = None,
        route: str = "plant_overhead",
        *,
        require_signed: bool = False,
        strict_invoice: bool = True,
        storage_path: str | None = None,
        path: str | None = None,
        db_path: str | None = None,
        **options: Any,
    ) -> None:
        route = options.pop("settlement", options.pop("method", options.pop("route_name", route)))
        require_signed = options.pop("require_signed_evidence", options.pop("signed", require_signed))
        for key in ("require_signed_contract", "require_signed_readings", "require_signed_runtime"):
            require_signed = require_signed or bool(options.get(key, False))
        self.options = options
        self.storage_path = storage_path or path or db_path
        if isinstance(payload, (str, bytes)) or hasattr(payload, "__fspath__"):
            self.storage_path = str(payload)
            payload = None
        self.original_payload: dict[str, Any] | None = deepcopy(payload) if payload is not None else None
        self.route = route
        self.require_signed = require_signed
        self.strict_invoice = strict_invoice
        self.events: list[dict[str, Any]] = []
        self.postings: list[dict[str, Any]] = []
        self.original_invoice: dict[str, Any] | None = None
        if payload is not None:
            self.capture_invoice(payload, route=route)

    # ------------------------------------------------------------------ events
    def _append_event(self, kind: str, body: dict[str, Any]) -> dict[str, Any]:
        seq = len(self.events) + 1
        event_id = body.pop("event_id", None) or f"ENERGY-EVENT-{seq:04d}"
        event = {
            "seq": seq,
            "event_id": event_id,
            "kind": kind,
            "body": deepcopy(body),
        }
        event["sha256"] = _sha256({"seq": seq, "kind": kind, "body": event["body"]})
        if self.events:
            event["prev_sha256"] = self.events[-1]["sha256"]
        self.events.append(event)
        return event

    # ------------------------------------------------------------- projection
    def _mutation_state(self) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        overrides: dict[str, dict[str, Any]] = {}
        extra_readings: list[dict[str, Any]] = []
        runtime_evidence: list[dict[str, Any]] = []
        extra_meter_events: list[dict[str, Any]] = []
        for event in self.events:
            if event["kind"] in {"invoice_captured", "adjustment_posted", "boundary_reopened"}:
                continue
            body = event["body"]
            for reading_id, changes in body.get("reading_overrides", {}).items():
                overrides.setdefault(reading_id, {}).update(changes)
            extra_readings.extend(deepcopy(body.get("extra_readings", [])))
            extra_meter_events.extend(deepcopy(body.get("extra_meter_events", [])))
            if body.get("runtime_record"):
                runtime_evidence.append(deepcopy(body["runtime_record"]))
            runtime_evidence.extend(deepcopy(body.get("runtime_evidence", [])))
        return overrides, extra_readings, runtime_evidence, extra_meter_events  # type: ignore[return-value]

    def effective_payload(self) -> dict[str, Any]:
        if self.original_payload is None:
            raise EnergyLedgerError("no energy payload has been captured")
        payload = deepcopy(self.original_payload)
        overrides, extra_readings, runtime_evidence, extra_meter_events = self._mutation_state()
        if overrides:
            readings = payload.setdefault("readings", [])
            by_id = {row.get("id"): row for row in readings}
            for reading_id, changes in overrides.items():
                if reading_id not in by_id:
                    # An override may target a reading supplied by a later WO.
                    for extra in extra_readings:
                        if extra.get("id") == reading_id:
                            extra.update(changes)
                            break
                    else:
                        raise EnergyLedgerError(f"unknown reading {reading_id!r}")
                else:
                    by_id[reading_id].update(changes)
        existing_extra_ids = {row.get("id") for row in payload.get("readings", [])}
        for row in extra_readings:
            if row.get("id") in existing_extra_ids:
                for existing in payload["readings"]:
                    if existing.get("id") == row.get("id"):
                        existing.update(row)
                        break
            else:
                payload.setdefault("readings", []).append(deepcopy(row))
                existing_extra_ids.add(row.get("id"))
        if runtime_evidence:
            payload.setdefault("runtime_records", []).extend(deepcopy(runtime_evidence))
        if extra_meter_events:
            payload.setdefault("meter_events", []).extend(deepcopy(extra_meter_events))
        return payload

    # -------------------------------------------------------------- entry rows
    def _result_entries(self, result: dict[str, Any], version: int) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for period in result["periods"]:
            p0, p1 = period["start_minute"], period["end_minute"]
            for batch_id, kwh in period["submeter_kwh"].items():
                cents_value = period["direct_amount_cents"][batch_id]
                key = f"P{p0}-{p1}:{batch_id}:direct_energy"
                entries.append(
                    {
                        "line_id": f"V{version}:{key}",
                        "line_key": key,
                        "version": version,
                        "period_start_minute": p0,
                        "period_end_minute": p1,
                        "account": batch_id,
                        "batch_id": batch_id,
                        "kind": "direct_energy",
                        "kwh": kwh,
                        "yuan_per_kwh": period["yuan_per_kwh"],
                        "amount_cents": cents_value,
                        "amount_yuan": yuan_from_cents(cents_value),
                        "status": "posted",
                    }
                )
            for account, cents_value in period["loss_allocations_cents"].items():
                kwh = period.get("loss_kwh_by_account", {}).get(account, period["loss_kwh"])
                key = f"P{p0}-{p1}:{account}:line_loss"
                entries.append(
                    {
                        "line_id": f"V{version}:{key}",
                        "line_key": key,
                        "version": version,
                        "period_start_minute": p0,
                        "period_end_minute": p1,
                        "account": account,
                        "batch_id": account if account in result["batch_allocations"] else None,
                        "kind": "line_loss",
                        "kwh": kwh,
                        "yuan_per_kwh": period["yuan_per_kwh"],
                        "amount_cents": cents_value,
                        "amount_yuan": yuan_from_cents(cents_value),
                        "status": "posted",
                    }
                )
        return entries

    def _line_totals(self) -> dict[str, int]:
        totals: dict[str, int] = {}
        for posting in self.postings:
            for entry in posting["entries"]:
                totals[entry["line_key"]] = totals.get(entry["line_key"], 0) + entry["amount_cents"]
        return totals

    def _current_total_cents(self) -> int:
        return sum(self._line_totals().values())

    # ---------------------------------------------------------------- capture
    def capture_invoice(
        self,
        payload: dict[str, Any] | None = None,
        *,
        route: str | None = None,
        event_id: str | None = None,
        captured_at: str | None = None,
    ) -> dict[str, Any]:
        if payload is not None:
            self.original_payload = deepcopy(payload)
        if self.original_payload is None:
            raise EnergyLedgerError("payload required for first capture")
        route = route or self.route
        self.route = route

        result = reconcile_bill(
            self.original_payload,
            route=route,
            require_signed_readings=self.require_signed,
            require_signed_contract=self.require_signed,
            require_signed_runtime=self.require_signed,
            strict_invoice=self.strict_invoice,
        )
        version = len(self.postings) + 1
        entries = self._result_entries(result, version)
        invoice = self.original_payload.get("utility_invoice") or {}
        self.original_invoice = {
            "invoice_id": invoice.get("id") or self.original_payload.get("invoice_id"),
            "amount_yuan": self.original_payload.get("utility_invoice_yuan", result["total_yuan"]),
            "amount_cents": result["utility_invoice_cents"],
            "payload_sha256": _sha256(self.original_payload),
        }
        posting = {
            "version": version,
            "posting_kind": "original_invoice",
            "status": "posted",
            "route": route,
            "captured_at": captured_at,
            "invoice": deepcopy(self.original_invoice),
            "current_invoice_cents": result["utility_invoice_cents"],
            "current_invoice_yuan": result["utility_invoice_yuan"],
            "entries": entries,
            "allocations": deepcopy(result["allocations"]),
            "total_cents": result["total_cents"],
            "total_yuan": result["total_yuan"],
            "balanced": result["balanced"],
            "open_boundaries": [],
            "responsible_party": None,
            "reason": "original utility invoice and signed readings",
            "work_order_id": None,
            "based_on_version": None,
        }
        self.postings.append(posting)
        event = self._append_event(
            "invoice_captured",
            {
                "event_id": event_id,
                "posting_version": version,
                "invoice": deepcopy(self.original_invoice),
                "allocations": result["allocations"],
                "total_cents": result["total_cents"],
            },
        )
        posting["source_event_id"] = event["event_id"]
        return posting

    # ------------------------------------------------------------- revisions
    def _append_adjustment(
        self,
        *,
        reason: str,
        responsible_party: str,
        route: str | None = None,
        work_order_id: str | None = None,
        utility_adjustment_yuan: int | float | None = None,
        revised_invoice_yuan: int | float | None = None,
        status: str = "adjusted",
        open_loss: str = "raise",
        preserve_invoice: bool = False,
        variance_account: str = "RESPONSIBILITY_VARIANCE",
        event_kind: str = "adjustment_posted",
        event_id: str | None = None,
        captured_at: str | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.postings:
            raise EnergyLedgerError("capture an original invoice before posting adjustments")
        route = route or self.route
        payload = self.effective_payload()
        result = reconcile_bill(
            payload,
            route=route,
            require_signed_readings=self.require_signed,
            require_signed_contract=self.require_signed,
            require_signed_runtime=self.require_signed,
            strict_invoice=False,
            open_loss=open_loss,
            open_loss_account=PLANT_ACCOUNT if open_loss in {"plant", "plant_overhead"} else CLEARING_ACCOUNT,
        )

        target_entries = self._result_entries(result, len(self.postings) + 1)
        original_invoice_cents = self.postings[0]["invoice"]["amount_cents"]
        if preserve_invoice:
            target_variance_cents = original_invoice_cents - result["total_cents"]
            target_entries.append(
                {
                    "line_id": f"TARGET:INVOICE:{variance_account}",
                    "line_key": f"INVOICE:{variance_account}",
                    "version": len(self.postings) + 1,
                    "period_start_minute": None,
                    "period_end_minute": None,
                    "account": variance_account,
                    "batch_id": None,
                    "kind": "responsibility_variance",
                    "kwh": None,
                    "yuan_per_kwh": None,
                    "amount_cents": target_variance_cents,
                    "amount_yuan": yuan_from_cents(target_variance_cents),
                    "status": "responsibility_balance",
                }
            )
        target_by_key = {entry["line_key"]: entry for entry in target_entries}
        current_totals = self._line_totals()
        keys = sorted(set(current_totals) | set(target_by_key))
        version = len(self.postings) + 1
        adjustment_entries: list[dict[str, Any]] = []
        for key in keys:
            new_amount = target_by_key[key]["amount_cents"] if key in target_by_key else 0
            old_amount = current_totals.get(key, 0)
            delta = new_amount - old_amount
            if delta == 0:
                continue
            template = target_by_key.get(key)
            if template is None:
                # The beneficiary/line disappeared under the new result; retain
                # enough dimensions to reverse it in the responsible version.
                prior = next(e for p in self.postings for e in reversed(p["entries"]) if e["line_key"] == key)
                template = prior
            entry = {
                "line_id": f"V{version}:ADJ:{key}",
                "line_key": key,
                "version": version,
                "period_start_minute": template.get("period_start_minute"),
                "period_end_minute": template.get("period_end_minute"),
                "account": template.get("account"),
                "batch_id": template.get("batch_id"),
                "kind": f"adjustment:{template.get('kind')}",
                "kwh": template.get("kwh"),
                "amount_cents": delta,
                "amount_yuan": yuan_from_cents(delta),
                "previous_effective_amount_cents": old_amount,
                "new_effective_amount_cents": new_amount,
                "new_effective_amount_yuan": yuan_from_cents(new_amount),
                "status": status,
                "reason": reason,
                "responsible_party": responsible_party,
                "work_order_id": work_order_id,
            }
            adjustment_entries.append(entry)

        current_metered_total = sum(
            amount for key, amount in self._line_totals().items() if not key.startswith("INVOICE:")
        )
        metered_delta = result["total_cents"] - current_metered_total
        previous_invoice_cents = self.postings[-1]["current_invoice_cents"]

        if preserve_invoice:
            if utility_adjustment_yuan is not None or revised_invoice_yuan is not None:
                raise EnergyLedgerError(
                    "preserve_invoice cannot be combined with a replacement utility bill/adjustment"
                )
            utility_delta = 0
            effective_invoice_cents = original_invoice_cents
        elif revised_invoice_yuan is not None:
            effective_invoice_cents = int(round(float(revised_invoice_yuan) * 100))
            utility_delta = effective_invoice_cents - previous_invoice_cents
            if utility_delta != metered_delta:
                raise EnergyLedgerError(
                    f"revised bill incremental delta {utility_delta} cents does not equal metered/cost "
                    f"delta {metered_delta} cents"
                )
        elif utility_adjustment_yuan is not None:
            utility_delta = int(round(float(utility_adjustment_yuan) * 100))
            effective_invoice_cents = previous_invoice_cents + utility_delta
            if utility_delta != metered_delta:
                raise EnergyLedgerError(
                    f"utility adjustment {utility_delta} cents does not equal metered/cost delta "
                    f"{metered_delta} cents"
                )
        else:
            utility_delta = metered_delta
            effective_invoice_cents = previous_invoice_cents + utility_delta

        explicit_delta = utility_delta
        effective_allocations = self.current_allocations(extra_entries=adjustment_entries)
        balanced = sum(
            int(round(float(value) * 100)) for value in effective_allocations.values()
        ) == effective_invoice_cents

        posting = {
            "version": version,
            "posting_kind": event_kind,
            "status": status,
            "route": route,
            "captured_at": captured_at,
            "reason": reason,
            "responsible_party": responsible_party,
            "work_order_id": work_order_id,
            "based_on_version": version - 1,
            "original_invoice": deepcopy(self.original_invoice),
            "original_invoice_yuan": self.postings[0]["invoice"]["amount_yuan"],
            "utility_adjustment_cents": explicit_delta,
            "utility_adjustment_yuan": yuan_from_cents(explicit_delta),
            "cumulative_utility_adjustment_cents": effective_invoice_cents
            - self.postings[0]["invoice"]["amount_cents"],
            "cumulative_utility_adjustment_yuan": yuan_from_cents(
                effective_invoice_cents - self.postings[0]["invoice"]["amount_cents"]
            ),
            "invoice_preserved": preserve_invoice,
            "metered_cost_delta_cents": metered_delta,
            "metered_cost_delta_yuan": yuan_from_cents(metered_delta),
            "current_invoice_cents": effective_invoice_cents,
            "current_invoice_yuan": yuan_from_cents(effective_invoice_cents),
            "entries": adjustment_entries,
            "allocations": effective_allocations,
            "total_cents": effective_invoice_cents,
            "total_yuan": yuan_from_cents(effective_invoice_cents),
            "metered_total_cents": result["total_cents"],
            "balanced": balanced and result["conservation_verified"],
            "open_boundaries": result.get("open_boundaries", []),
            "target_result_summary": {
                "allocations": result["allocations"],
                "total_yuan": result["total_yuan"],
                "total_grid_kwh": result["total_grid_kwh"],
                "total_loss_kwh": result["total_loss_kwh"],
            },
        }
        self.postings.append(posting)
        event_body = {
            "event_id": event_id,
            "posting_version": version,
            "reason": reason,
            "responsible_party": responsible_party,
            "work_order_id": work_order_id,
            "utility_adjustment_cents": explicit_delta,
            "entry_ids": [entry["line_id"] for entry in adjustment_entries],
        }
        if extra_body:
            event_body.update(extra_body)
        event = self._append_event(event_kind, event_body)
        posting["source_event_id"] = event["event_id"]
        return posting

    def revise_reading(
        self,
        reading_id: str | dict[str, Any] | None = None,
        changes: dict[str, Any] | None = None,
        *,
        register_kwh: int | float | None = None,
        corrected_register_kwh: int | float | None = None,
        correction_kwh: int | float | None = None,
        reason: str = "late_reading_revision",
        responsible_party: str = "meter_owner",
        work_order_id: str | None = None,
        utility_adjustment_yuan: int | float | None = None,
        revised_invoice_yuan: int | float | None = None,
        preserve_invoice: bool = False,
        route: str | None = None,
        signed_by: str | None = None,
        signature: str | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """Append a calibration/reading correction and post signed deltas."""

        if isinstance(reading_id, dict):
            source = deepcopy(reading_id)
            reading_id = source.pop("reading_id", source.pop("id"))
            changes = source.pop("changes", changes)
            register_kwh = source.pop("register_kwh", register_kwh)
            corrected_register_kwh = source.pop("correction_register_kwh", corrected_register_kwh)
            correction_kwh = source.pop("correction_delta_kwh", correction_kwh)
            reason = source.pop("reason", reason)
            responsible_party = source.pop("responsible_party", responsible_party)
            work_order_id = source.pop("work_order_id", work_order_id)
            utility_adjustment_yuan = source.pop("utility_adjustment_yuan", utility_adjustment_yuan)
            revised_invoice_yuan = source.pop("revised_invoice_yuan", revised_invoice_yuan)
            preserve_invoice = source.pop("preserve_invoice", preserve_invoice)
            signed_by = source.pop("signed_by", signed_by)
            signature = source.pop("signature", signature)

        if reading_id is None:
            raise EnergyLedgerError("reading_id is required")
        new_value = register_kwh if register_kwh is not None else corrected_register_kwh
        if new_value is None and correction_kwh is not None:
            current_payload = self.effective_payload() if self.original_payload is not None else {}
            current_reading = next(
                (row for row in current_payload.get("readings", []) if row.get("id") == reading_id),
                None,
            )
            if current_reading is None:
                raise EnergyLedgerError(f"cannot apply correction delta to unknown reading {reading_id!r}")
            new_value = current_reading["register_kwh"] + correction_kwh
        if new_value is None:
            raise EnergyLedgerError("a corrected register value is required")

        override = dict(changes or {})
        override["register_kwh"] = new_value
        if signed_by:
            override["revision_signed_by"] = signed_by
        if signature:
            override["revision_signature"] = signature
        self._append_event(
            "reading_revision",
            {
                "event_id": event_id,
                "reading_overrides": {reading_id: override},
                "reason": reason,
                "responsible_party": responsible_party,
                "work_order_id": work_order_id,
                "signed_by": signed_by,
                "signature": signature,
            },
        )
        return self._append_adjustment(
            reason=reason,
            responsible_party=responsible_party,
            route=route,
            work_order_id=work_order_id,
            utility_adjustment_yuan=utility_adjustment_yuan,
            revised_invoice_yuan=revised_invoice_yuan,
            preserve_invoice=preserve_invoice,
            status="adjusted",
        )

    adjust_reading = revise_reading
    apply_reading_revision = revise_reading

    # Friendly domain aliases.
    capture = capture_invoice
    post = capture_invoice
    post_invoice = capture_invoice
    post_costs = capture_invoice
    adjust = revise_reading
    revise = revise_reading

    def apply_calibration(self, calibration: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        if calibration is not None:
            return self.revise_reading(calibration, **kwargs)
        kwargs.setdefault("reason", "late_meter_calibration")
        return self.revise_reading(**kwargs)

    apply_late_calibration = apply_calibration
    record_calibration = apply_calibration

    def record_replacement(
        self,
        work_order: dict[str, Any] | None = None,
        *,
        additional_readings: list[dict[str, Any]] | None = None,
        meter_event: dict[str, Any] | None = None,
        reason: str = "late_meter_replacement_work_order",
        responsible_party: str = "meter_maintenance",
        work_order_id: str | None = None,
        utility_adjustment_yuan: int | float | None = None,
        revised_invoice_yuan: int | float | None = None,
        preserve_invoice: bool = False,
        route: str | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """Append a replacement/reset work order and its boundary readings."""

        if work_order is not None:
            source = deepcopy(work_order)
            additional_readings = source.pop("additional_readings", additional_readings)
            meter_event = source.pop("meter_event", meter_event)
            reason = source.pop("reason", reason)
            responsible_party = source.pop("responsible_party", responsible_party)
            work_order_id = source.pop("work_order_id", source.pop("id", work_order_id))
            utility_adjustment_yuan = source.pop("utility_adjustment_yuan", utility_adjustment_yuan)
            revised_invoice_yuan = source.pop("revised_invoice_yuan", revised_invoice_yuan)
            preserve_invoice = source.pop("preserve_invoice", preserve_invoice)
            event_id = source.pop("event_id", event_id)
            if meter_event is None and source.get("kind") in {"replacement", "reset"}:
                meter_event = source

        if not meter_event or meter_event.get("kind") not in {"replacement", "reset"}:
            raise EnergyLedgerError("a signed replacement/reset meter_event is required")
        if not additional_readings:
            raise EnergyLedgerError("replacement requires old-final/new-initial boundary readings")

        self._append_event(
            "meter_work_order",
            {
                "event_id": event_id,
                "extra_readings": additional_readings,
                "extra_meter_events": [meter_event],
                "reason": reason,
                "responsible_party": responsible_party,
                "work_order_id": work_order_id or meter_event.get("work_order_id"),
            },
        )
        return self._append_adjustment(
            reason=reason,
            responsible_party=responsible_party,
            route=route,
            work_order_id=work_order_id or meter_event.get("work_order_id"),
            utility_adjustment_yuan=utility_adjustment_yuan,
            revised_invoice_yuan=revised_invoice_yuan,
            preserve_invoice=preserve_invoice,
            status="adjusted",
        )

    apply_late_replacement = record_replacement
    apply_replacement = record_replacement
    add_meter_work_order = record_replacement

    def add_runtime_evidence(self, record: dict[str, Any]) -> dict[str, Any]:
        event = self._append_event(
            "runtime_evidence",
            {"runtime_record": deepcopy(record), "reason": record.get("reason", "runtime_evidence")},
        )
        return event

    def register_dispute(
        self,
        dispute: dict[str, Any] | str | None = None,
        *,
        reason: str = "invoice_payment_dispute",
        responsible_party: str = "utility",
        dispute_id: str | None = None,
        amount_yuan: int | float | None = None,
    ) -> dict[str, Any]:
        """Record a refusal/dispute without rewriting posted costs.

        A dispute is not itself a reading correction.  If it later changes the
        valid register or invoice, call :meth:`revise_reading` or
        :meth:`record_replacement` to create the responsible adjustment version.
        """

        body: dict[str, Any]
        if isinstance(dispute, dict):
            body = deepcopy(dispute)
        else:
            body = {"dispute_id": dispute} if dispute else {}
        body.setdefault("reason", reason)
        body.setdefault("responsible_party", responsible_party)
        if dispute_id is not None:
            body["dispute_id"] = dispute_id
        if amount_yuan is not None:
            body["amount_yuan"] = amount_yuan
        if self.original_invoice is not None:
            body.setdefault("invoice_id", self.original_invoice.get("invoice_id"))
        event = self._append_event("invoice_disputed", body)
        return event

    def disputes(self) -> list[dict[str, Any]]:
        return [deepcopy(event["body"]) for event in self.events if event["kind"] == "invoice_disputed"]

    dispute_invoice = register_dispute

    def reopen_loss_boundary(
        self,
        late_runtime_record: dict[str, Any] | None = None,
        *,
        reason: str = "late_runtime_evidence_reopens_allocation_boundary",
        responsible_party: str = "production_planning",
        work_order_id: str | None = None,
        fallback: str = "plant_overhead",
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """Reopen an actual-run loss boundary without deleting old postings.

        The original version remains exactly as reported.  The new version
        reverses the no-longer-proven batch loss and, by default, parks it in
        the contract-approved plant account pending final run adjudication.
        Direct submeter charges are untouched.
        """

        if late_runtime_record is not None:
            self.add_runtime_evidence(late_runtime_record)
        return self._append_adjustment(
            reason=reason,
            responsible_party=responsible_party,
            route="actual_run",
            work_order_id=work_order_id,
            status="boundary_reopened",
            open_loss=fallback,
            event_kind="boundary_reopened",
            event_id=event_id,
        )

    def reallocate_loss(
        self,
        target_route: str | None = None,
        *,
        route: str | None = None,
        new_route: str | None = None,
        settlement: str | None = None,
        runtime_record: dict[str, Any] | None = None,
        reason: str = "contract_authorized_loss_route_reclassification",
        responsible_party: str = "finance",
        work_order_id: str | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """Switch between the two contract routes in a new immutable version."""

        target = target_route or route or new_route or settlement
        if target is None:
            raise EnergyLedgerError("target settlement route is required")
        if runtime_record is not None:
            self.add_runtime_evidence(runtime_record)
        posting = self._append_adjustment(
            reason=reason,
            responsible_party=responsible_party,
            route=target,
            work_order_id=work_order_id,
            utility_adjustment_yuan=0,
            status="route_reclassified",
            event_kind="route_reclassified",
            event_id=event_id,
        )
        self.route = target
        return posting

    change_settlement_route = reallocate_loss

    # ---------------------------------------------------------------- queries
    def original_allocations(self) -> dict[str, int | float]:
        if not self.postings:
            return {}
        return deepcopy(self.postings[0]["allocations"])

    def current_allocations(self, *, extra_entries: list[dict[str, Any]] | None = None) -> dict[str, int | float]:
        totals = self._line_totals()
        for entry in extra_entries or []:
            totals[entry["line_key"]] = totals.get(entry["line_key"], 0) + entry["amount_cents"]
        result: dict[str, int] = {}
        for key, value in totals.items():
            # line_key period:account:kind; account is also stored on entries,
            # but rebuilding from keys is sufficient for the canonical format.
            account = key.split(":")[1]
            result[account] = result.get(account, 0) + value
        return AllocationMap({key: yuan_from_cents(value) for key, value in result.items()}, plant_account=PLANT_ACCOUNT)

    def entries(self, *, include_adjustments: bool = True) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for posting in self.postings:
            if posting["posting_kind"] == "original_invoice" or include_adjustments:
                rows.extend(deepcopy(posting["entries"]))
        return rows

    @property
    def rows(self) -> list[dict[str, Any]]:
        return self.entries()

    @property
    def adjustment_postings(self) -> list[dict[str, Any]]:
        return [deepcopy(posting) for posting in self.postings if posting["version"] > 1]

    @property
    def adjustment_entries(self) -> list[dict[str, Any]]:
        return [entry for entry in self.entries() if str(entry.get("kind", "")).startswith("adjustment:")]

    @property
    def adjustments(self) -> list[dict[str, Any]]:
        return self.adjustment_postings

    def versions(self) -> list[dict[str, Any]]:
        return [
            {
                "version": p["version"],
                "kind": p["posting_kind"],
                "status": p["status"],
                "route": p["route"],
                "total_yuan": p["total_yuan"],
                "allocations": deepcopy(p["allocations"]),
                "reason": p["reason"],
                "responsible_party": p["responsible_party"],
                "work_order_id": p["work_order_id"],
                "balanced": p["balanced"],
                "open_boundaries": p.get("open_boundaries", []),
            }
            for p in self.postings
        ]

    def history(self) -> list[dict[str, Any]]:
        return self.versions()

    def audit(self) -> dict[str, Any]:
        problems: list[str] = []
        if not self.postings or self.original_invoice is None:
            return {"ok": False, "problems": ["no posted invoice"]}

        # Hash-chain check.
        previous_hash = None
        for event in self.events:
            expected = _sha256({"seq": event["seq"], "kind": event["kind"], "body": event["body"]})
            if event["sha256"] != expected:
                problems.append(f"event {event['event_id']} hash mismatch")
            if previous_hash is not None and event.get("prev_sha256") != previous_hash:
                problems.append(f"event {event['event_id']} chain mismatch")
            previous_hash = event["sha256"]

        # Original rows must still exist and sum to the immutable invoice.
        original = self.postings[0]
        original_sum = sum(entry["amount_cents"] for entry in original["entries"])
        if original_sum != self.original_invoice["amount_cents"]:
            problems.append("original posted rows no longer sum to original invoice")
        if original["invoice"]["payload_sha256"] != self.original_invoice["payload_sha256"]:
            problems.append("original invoice identity changed")

        # Every adjustment version must preserve debit/credit closure.
        line_totals: dict[str, int] = {}
        for posting in self.postings:
            for entry in posting["entries"]:
                line_totals[entry["line_key"]] = line_totals.get(entry["line_key"], 0) + entry["amount_cents"]
            if posting["posting_kind"] == "original_invoice":
                if posting["total_cents"] != posting["invoice"]["amount_cents"]:
                    problems.append(f"version {posting['version']} does not match invoice")
            elif sum(e["amount_cents"] for e in posting["entries"]) != posting.get("utility_adjustment_cents"):
                problems.append(f"version {posting['version']} adjustment does not close")
            if not posting.get("balanced", False):
                # A reopened boundary may be held in plant and still balance;
                # any genuinely open result is reported explicitly.
                if not posting.get("open_boundaries"):
                    problems.append(f"version {posting['version']} is not balanced")

        current_sum = sum(line_totals.values())
        latest = self.postings[-1]
        if current_sum != latest["current_invoice_cents"]:
            problems.append("current batch+plant totals do not equal current invoice")

        return {
            "ok": not problems,
            "problems": problems,
            "event_count": len(self.events),
            "version_count": len(self.postings),
            "original_invoice": deepcopy(self.original_invoice),
            "original_allocations": self.original_allocations(),
            "current_allocations": self.current_allocations(),
            "current_invoice_yuan": latest["current_invoice_yuan"],
        }


# Functional aliases for callers that prefer not to instantiate the class.
EnergyLedger = EnergyCapture
EnergyCaptureLedger = EnergyCapture
Energy = EnergyCapture


def capture_energy(payload: dict[str, Any], route: str = "plant_overhead", **kwargs: Any) -> EnergyCapture:
    return EnergyCapture(payload, route=route, **kwargs)


capture = capture_energy


def post_costs(payload: dict[str, Any], route: str = "plant_overhead", **kwargs: Any) -> EnergyCapture:
    return EnergyCapture(payload, route=route, **kwargs)


def apply_late_reading(payload_or_ledger: Any, revision: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    """Functional helper accepting either an existing ledger or a payload."""

    if isinstance(payload_or_ledger, EnergyCapture):
        ledger = payload_or_ledger
    else:
        ledger = EnergyCapture(payload_or_ledger)
    if revision is not None:
        return ledger.revise_reading(revision, **kwargs)
    return ledger.revise_reading(**kwargs)


def build_posted_ledger(payload: dict[str, Any], route: str = "plant_overhead", **kwargs: Any) -> EnergyCapture:
    return EnergyCapture(payload, route=route, **kwargs)


def open_energy_ledger(payload: dict[str, Any] | None = None, **kwargs: Any) -> EnergyCapture:
    return EnergyCapture(payload, **kwargs)
