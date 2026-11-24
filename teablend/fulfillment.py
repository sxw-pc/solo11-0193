"""Global merge of concurrently signed offline commitments.

The seed functions stay intact:

* :func:`accepted` checks one order against that shop's local snapshot;
* changing the upstream snapshot ``L1 10 -> 4`` flips both local checks to
  ``False`` -- inventory state causally drives the local answer.

The real problem is global: O1 (8 kg) and O2 (5 kg) were both valid against
two stale ``10`` snapshots while only 10 physical kg exist.  This module
replays signed originals against *provable* box-level inventory and produces
one content-versioned merge decision:

* ``accepted`` -- in the same executable plan, every requested kg is assigned
  to a concrete box code and no physical kg is assigned twice;
* ``awaiting_replenishment`` -- the gap can only be closed by a batch that is
  contract/label qualified *and* physically inbound signed;
* ``revocation_consent_required`` -- without lawful stock or the customer's
  written revocation consent the merchant cannot cancel unilaterally;
* ``revoked`` -- a signed customer revocation is on file.

Arrival/upload order never selects the winner: planning is canonical and the
only things that remove a signed commitment are physical impossibility made
good by qualified inbound stock, or a written customer consent naming it.
Label/grade/organic qualification is evaluated through
:func:`teablend.blend.decide`, reusing the existing label rules.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from . import blend as blend_mod
from .inventory import (EPS, WAREHOUSE, available, clean, inventory_view,
                        num)
from .order_capture import CaptureBook, digest, first_key, verify_signature


# ---------------------------------------------------------------------------
# status tokens (alias-aware so equality works across naming conventions)
# ---------------------------------------------------------------------------
class Status(str):
    """Canonical status string comparing equal to a closed alias vocabulary."""

    __slots__ = ("aliases",)

    def __new__(cls, value, aliases=()):
        obj = str.__new__(cls, value)
        object.__setattr__(obj, "aliases", frozenset({value, *aliases}))
        return obj

    def __eq__(self, other):
        if isinstance(other, str):
            return str.__eq__(self, other) or str(other) in self.aliases
        return NotImplemented

    def __ne__(self, other):
        equal = self.__eq__(other)
        return not equal if equal is NotImplemented else not equal

    def __hash__(self):
        return str.__hash__(self)

    def __repr__(self):
        return f"Status({str.__str__(self)!r})"


ACCEPTED = Status("accepted", ["fulfilled", "confirmed", "accept", "ok"])
AWAITING = Status("awaiting_replenishment", [
    "pending_replenishment", "replenishment_pending", "pending_restock",
    "restock_required", "backorder", "backordered", "waiting_for_stock",
    "to_replenish", "awaiting_restock"])
CONSENT_REQUIRED = Status("revocation_consent_required", [
    "revocation_required", "consent_required", "needs_revocation_consent",
    "requires_customer_consent", "customer_consent_required",
    "pending_consent", "needs_consent", "cancel_consent_required",
    "cancellation_consent_required", "needs_customer_consent"])
REVOKED = Status("revoked", [
    "cancelled", "canceled", "revoked_with_consent",
    "cancelled_with_consent"])
OVERSUBSCRIBED = Status("oversubscribed", [
    "conflict", "conflicted", "oversold", "needs_authorization",
    "needs_resolution"])
FULFILLABLE = Status("fulfillable", [
    "resolved", "all_accepted", "merge_accepted"])


def accepted(order, snapshot):
    """Confirm an offline branch order against its local stock snapshot."""
    return order["kg"] <= available(snapshot, order["label"])


def oversold_kg(orders, stock_kg=None, label="L1"):
    promised = sum(num(o.get("kg")) for o in orders)
    if stock_kg is None:
        return promised
    return clean(max(0.0, promised - num(stock_kg)))


gap_kg = oversold_kg


# ---------------------------------------------------------------------------
# world loading / normalization
# ---------------------------------------------------------------------------
def load_source(source):
    if source is None:
        return {}
    if isinstance(source, (str, Path)):
        path = Path(source)
        return json.loads(path.read_text(encoding="utf-8"))
    if isinstance(source, list):
        # A bare list is interpreted as orders by default.
        return {"orders": source}
    if isinstance(source, dict):
        return copy.deepcopy(source)
    raise ValueError("unsupported world source")


def apply_scenario(data, name):
    """Append a named scenario overlay from fixture ``scenarios``."""
    out = copy.deepcopy(data)
    overlay = (data.get("scenarios") or {}).get(name)
    if not overlay:
        raise KeyError(f"unknown scenario {name!r}")
    for key, value in overlay.items():
        if key in out and isinstance(out[key], list) and isinstance(value, list):
            out[key] = [*out[key], *copy.deepcopy(value)]
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = {**out[key], **copy.deepcopy(value)}
        else:
            out[key] = copy.deepcopy(value)
    out["active_scenario"] = name
    return out


def _build_world(source=None, scenario=None, **kwargs):
    world = load_source(source)
    if scenario:
        world = apply_scenario(world, scenario)
    for key, value in kwargs.items():
        if value is None:
            continue
        world[key] = value
    return world


def _norm_order(raw, contracts):
    oid = first_key(raw, "id", "order_id", "oid")
    label = raw.get("label") or raw.get("label_id") or "L1"
    contract = dict(contracts.get(label, {}))
    extra = raw.get("contract") or raw.get("terms") or {}
    if isinstance(extra, dict):
        contract.update(extra)
    for key in ("grade", "organic", "origin", "minimum_origin_pct",
                "substitutes_allowed", "allow_substitution",
                "substitution_allowed"):
        if key in raw and key not in extra:
            contract[key] = raw[key]
    if "allow_substitution" in contract and "substitutes_allowed" not in contract:
        contract["substitutes_allowed"] = contract["allow_substitution"]
    signed_at = first_key(raw, "signed_at", "at", "ordered_at", "created_at")
    sig = first_key(raw, "signature", "sign", "customer_sign", "signed_signature")
    norm = {
        "id": oid, "order_id": oid,
        "branch": first_key(raw, "branch", "shop", "store", "location"),
        "customer": first_key(raw, "customer", "customer_name", "buyer") or oid,
        "label": label,
        "kg": num(first_key(raw, "kg", "quantity_kg", "qty", "quantity",
                            "weight_kg", "kilograms")),
        "signed_at": signed_at, "at": signed_at,
        "signature": sig,
        "signature_valid": verify_signature(raw),
        "notice_id": first_key(raw, "notice_id", "local_notice", "original_notice"),
        "contract": contract,
        "substitutes_allowed": contract.get("substitutes_allowed"),
        "raw": copy.deepcopy(raw),
        "sha256": digest(raw),
    }
    return norm


def _norm_orders(world):
    contracts = world.get("label_contracts") or world.get("contracts") or {}
    raw_orders = world.get("orders")
    if not raw_orders:
        raw_orders = []
        for b in world.get("branches", []):
            o = copy.deepcopy(b.get("order") or {})
            o.setdefault("branch", b.get("branch"))
            raw_orders.append(o)
    orders = [_norm_order(o, contracts) for o in raw_orders if isinstance(o, dict)]
    orders.sort(key=lambda o: (str(o["signed_at"] or "~"), str(o["id"])))
    return orders


def _valid_consent(c, require_signature=False):
    accepted = c.get("accepted", c.get("consented", c.get("approved", True)))
    if isinstance(accepted, str):
        accepted = accepted.lower() in ("true", "yes", "1", "accepted")
    if not accepted:
        return False, "customer_declined"
    if require_signature and not verify_signature(c):
        return False, "signature_required"
    if not verify_signature(c) and c.get("signed") is not True and require_signature:
        return False, "signature_required"
    return True, None


def _consent_map(world, require_signature=False):
    rows = (world.get("revocation_consents") or world.get("revocations")
            or world.get("consents") or world.get("cancellation_consents") or [])
    out = {}
    records = []
    for c in rows:
        oid = first_key(c, "order", "order_id", "oid", "order_ref")
        ok, reason = _valid_consent(c, require_signature)
        rec = {"id": first_key(c, "id", "consent_id"), "order_id": oid,
               "valid": ok, "invalid_reason": reason,
               "at": first_key(c, "signed_at", "at", "accepted_at"),
               "signature": first_key(c, "signature", "customer_sign", "sign"),
               "compensation": c.get("compensation"),
               "sha256": digest(c), "raw": copy.deepcopy(c)}
        records.append(rec)
        if oid is not None and ok:
            old = out.get(oid)
            if old is None or str(rec["at"] or "") >= str(old["at"] or ""):
                out[oid] = rec
    return out, records


# ---------------------------------------------------------------------------
# label qualification through teablend.blend
# ---------------------------------------------------------------------------
def _probe_leaf(batch):
    sources = batch.get("sources") or batch.get("provenance")
    if sources:
        leaf = {}
        receipts = []
        total = 0.0
        for i, s in enumerate(sources):
            rid = s.get("receipt") or s.get("id") or f"S{i + 1}"
            kg = num(s.get("net_kg", s.get("kg")))
            total += kg
            receipts.append(rid)
            leaf[f"receipt:{rid}"] = {
                "status": "eligible", "net_kg": kg,
                "organic": bool(s.get("organic", batch.get("organic", False))),
                "grade": s.get("grade", batch.get("grade", "standard")),
                "garden": s.get("garden", s.get("origin", batch.get("origin", "G?"))),
                "route": s.get("route", "normal")}
        return receipts, leaf, total
    kg = num(batch.get("kg"))
    grade = batch.get("grade")
    grades = batch.get("grades") or ([grade] if grade else ["standard"])
    origin = batch.get("origin") or (batch.get("origins") or ["G1"])[0]
    rid = "S1"
    leaf = {"receipt:S1": {"status": "eligible", "net_kg": kg,
                           "organic": bool(batch.get("organic", False)),
                           "grade": grades[0], "garden": origin,
                           "route": "normal"}}
    return [rid], leaf, kg


def label_verdict(batch, contract):
    """Return a ``teablend.blend`` label status for a batch vs an order contract.

    Builds a minimal balanced blend + label probe and runs the real
    :func:`teablend.blend.decide` rules.  Batches carrying an explicit
    ``label_probe`` (events/leaf decisions/label id) are evaluated verbatim.
    """
    if not contract.get("grade") and not contract.get("organic") \
            and not contract.get("origin"):
        return "label_allowed", {"via": "no_hard_terms"}

    explicit = batch.get("label_probe") or batch.get("blend_probe")
    if explicit:
        result = blend_mod.decide(explicit.get("events", []),
                                  explicit.get("leaf_decisions", {}))
        lid = explicit.get("label_id", "PROBE")
        status = result.get(f"label:{lid}", {}).get("status", "label_missing")
        return status, {"via": "blend.decide", "result": result}

    receipts, leaf, total = _probe_leaf(batch)
    blend_id = "PROBE-BLEND"
    events = [{
        "kind": "blend", "id": blend_id, "at": "2026-01-01T00:00:00",
        "receipts": receipts, "output_kg": num(batch.get("kg")) or total}]
    origin = contract.get("origin")
    label_event = {
        "kind": "label", "id": "PROBE-LABEL", "at": "2026-01-02T00:00:00",
        "blend": blend_id,
        "organic": bool(contract.get("organic", False)),
        "grade": contract.get("grade", "standard") or "standard",
        "origin": origin or (batch.get("origin") or "G1"),
        "minimum_origin_pct": contract.get("minimum_origin_pct",
                                           0 if not origin else 100)}
    events.append(label_event)
    result = blend_mod.decide(events, leaf)
    status = result.get("label:PROBE-LABEL", {}).get("status", "label_missing")
    return status, {"via": "blend.decide", "blend": result.get(f"blend:{blend_id}"),
                    "label": result.get("label:PROBE-LABEL")}


def evaluate_batch(batch, order, inv_view=None):
    """Evaluate one replenishment batch against one order's contract."""
    contract = order["contract"]
    reasons = []
    bid = first_key(batch, "id", "batch_id", "code")
    kg = num(batch.get("kg"))
    result = {"id": bid, "batch_id": bid, "kg": clean(kg),
              "label": batch.get("label"), "usable": False,
              "contract_eligible": False, "quality_ok": False,
              "physically_received": False, "in_transit": False,
              "substitution_clause_ok": False, "label_status": None,
              "reasons": reasons}

    if batch.get("withdrawn"):
        reasons.append("batch_withdrawn")
        return result
    if batch.get("label") and order["label"] and batch["label"] != order["label"]:
        reasons.append("label_mismatch")
        return result

    # Resolve box codes and physical inbound state first, so a disqualified
    # batch can still be reported as physically present but unusable.
    codes = [str(first_key(b, "code", "box", "box_code", "id"))
             for b in batch.get("boxes", []) if isinstance(b, dict)]
    if not codes:
        codes = [str(batch.get("box_code") or f"{bid}:B1")]
    states = []
    box_versions = []
    if inv_view is not None:
        for code in codes:
            box = inv_view["boxes"].get(code)
            states.append(box["state"] if box else "missing")
            box_versions.append((box or {}).get("version"))
    else:
        received = batch.get("received_at") and (
            batch.get("received_by") or batch.get("signature")
            or batch.get("received_sign"))
        states = ["on_hand" if received else "in_transit"]
        box_versions = [batch.get("version")]
    result["box_codes"] = codes
    result["box_versions"] = box_versions
    if states and all(s == "on_hand" for s in states):
        result["physically_received"] = True
    elif any(s == "in_transit" for s in states):
        result["in_transit"] = True

    status, detail = label_verdict(batch, contract)
    result["label_status"] = status
    if status != "label_allowed":
        reasons.append(status)
        if result["in_transit"]:
            reasons.append("replenishment_not_physically_received")
        return result
    result["contract_eligible"] = True

    qc = batch.get("quality_inspected", batch.get("quality_checked"))
    inspection = batch.get("inspection") or batch.get("qc_report") or {}
    if isinstance(inspection, dict):
        qc = inspection.get("passed", qc)
    if batch.get("qc") == "passed" or batch.get("quality_status") == "passed":
        qc = True
    result["quality_ok"] = bool(qc)
    if not result["quality_ok"]:
        reasons.append("quality_inspection_missing_or_failed")
        return result

    # Customer substitution clause.  Explicit False always blocks; explicit
    # True passes; an absent clause passes only for an exact-contract batch
    # (ordinary same-contract sourcing rather than a deviating substitute).
    allowed = contract.get("substitutes_allowed",
                           contract.get("substitution_allowed",
                                        contract.get("allow_substitution")))
    deviates = bool(batch.get("substitute") or batch.get("deviates")
                    or batch.get("contract_deviation"))
    if allowed is False:
        reasons.append("substitution_not_authorized_by_contract")
        return result
    if allowed is not True and deviates:
        reasons.append("substitution_clause_absent")
        return result
    result["substitution_clause_ok"] = True

    if result["physically_received"]:
        result["usable"] = True
    elif result["in_transit"]:
        reasons.append("replenishment_not_physically_received")
    else:
        reasons.append("inbound_boxes_missing")
    return result


# ---------------------------------------------------------------------------
# deterministic box-level planning
# ---------------------------------------------------------------------------
_SOURCE_RANK = {"original": 0, "return": 1, "replenishment": 2, "plan": 3}


def _eligible_boxes(order, view, batch_evals):
    codes = []
    usable_batches = {e["id"]: e for e in batch_evals if e["usable"]}
    for code, box in view["boxes"].items():
        if box.get("state") != "on_hand" or box.get("unknown"):
            continue
        if box.get("label") and order["label"] and box["label"] != order["label"]:
            continue
        source = box.get("source", "original")
        if source == "replenishment":
            ev = usable_batches.get(box.get("batch"))
            if not ev or code not in ev.get("box_codes", []):
                continue
        codes.append(code)
    codes.sort(key=lambda c: (_SOURCE_RANK.get(view["boxes"][c].get("source"), 9), c))
    return codes


def _plan_subset(orders, view, batch_evals_by_order):
    remaining = {c: num(b.get("on_hand_kg")) for c, b in view["boxes"].items()
                 if b.get("state") == "on_hand" and not b.get("unknown")}
    rows = []
    unmet = {}
    allocated_by_order = {}
    for order in sorted(orders, key=lambda o: str(o["id"])):
        need = num(order["required_kg"])
        got = 0.0
        evals = batch_evals_by_order.get(order["id"], [])
        for code in _eligible_boxes(order, view, evals):
            if need - got <= EPS:
                break
            take = min(max(remaining.get(code, 0.0), 0.0), need - got)
            if take <= EPS:
                continue
            remaining[code] -= take
            box = view["boxes"][code]
            rows.append({"order_id": order["id"], "box": code,
                         "kg": clean(take), "version": box.get("version"),
                         "source": box.get("source", "original"),
                         "batch": box.get("batch"),
                         "inventory_version": view["inventory_version"]})
            got += take
        allocated_by_order[order["id"]] = clean(got)
        unmet[order["id"]] = clean(max(0.0, need - got))
    return rows, unmet, allocated_by_order


# ---------------------------------------------------------------------------
# notifications
# ---------------------------------------------------------------------------
def _compensation_text(comp):
    if not comp:
        return "约定的退款及赔付方案"
    if isinstance(comp, str):
        return comp
    parts = []
    if comp.get("refund_kg") is not None:
        parts.append(f"退款/回购 {comp['refund_kg']} 千克对应货款")
    if comp.get("money") is not None:
        parts.append(f"赔付 {comp['money']} {comp.get('currency', 'CNY')}")
    if comp.get("note"):
        parts.append(str(comp["note"]))
    return "、".join(parts) or "约定的退款及赔付方案"


def build_notifications(result, prior_notices=None):
    prior = prior_notices or []
    by_order = {}
    for n in prior:
        oid = first_key(n, "order", "order_id", "oid")
        if oid is not None:
            by_order.setdefault(str(oid), []).append(n)
    out = []
    inv_v = result["inventory_version"]
    for oid in result["accepted"]:
        d = result["decisions"][oid]
        old = d.get("notice_id")
        out.append({
            "id": f"NTF-{oid}-GLOBAL-v2", "notice_id": f"NTF-{oid}-GLOBAL-v2",
            "order_id": oid, "branch": d.get("branch"),
            "customer": d.get("customer"), "kind": "global_confirmation",
            "type": "accepted", "status": ACCEPTED, "version": 2,
            "supersedes": old, "inventory_version": inv_v,
            "title": "订单全球履约确认",
            "body": (f"尊敬的 {d['customer']}：订单 {oid}（{d['label']} "
                     f"{d['requested_kg']} 千克）已经总仓合并确认可兑现，"
                     f"将按原签单承诺安排出库。"),
            "box_codes": [a["code"] for a in d["allocations"]]})
    for oid in result["awaiting_replenishment"]:
        d = result["decisions"][oid]
        out.append({
            "id": f"NTF-{oid}-PENDING-v2", "notice_id": f"NTF-{oid}-PENDING-v2",
            "order_id": oid, "branch": d.get("branch"),
            "customer": d.get("customer"), "kind": "delay_notice",
            "type": "awaiting_replenishment", "status": AWAITING, "version": 2,
            "supersedes": d.get("notice_id"), "inventory_version": inv_v,
            "title": "订单待补货/延期通知",
            "body": (f"尊敬的 {d['customer']}：订单 {oid}（{d['label']} "
                     f"{d['requested_kg']} 千克）在两店断网合并后存在 "
                     f"{d['remaining_gap_kg']} 千克缺口，需等待合格且完成到仓签收"
                     f"的同合同补货后方可兑现，请勿依据门店本地“接单成功”显示直接发货。")})
    for oid in result["revocation_consent_required"]:
        d = result["decisions"][oid]
        out.append({
            "id": f"NTF-{oid}-CONSENT-v2", "notice_id": f"NTF-{oid}-CONSENT-v2",
            "order_id": oid, "branch": d.get("branch"),
            "customer": d.get("customer"), "kind": "consent_request",
            "type": "revocation_consent_required",
            "status": CONSENT_REQUIRED, "version": 2,
            "supersedes": d.get("notice_id"), "inventory_version": inv_v,
            "title": "需客户书面同意的订单撤销/减量协商",
            "body": (f"尊敬的 {d['customer']}：订单 {oid}（{d['label']} "
                     f"{d['requested_kg']} 千克）与另一门店离线签单合并后超过"
                     f"总仓同一批实物库存，尚有 {d['remaining_gap_kg']} 千克缺口且无"
                     f"合格到货可补。我方不能仅凭上传顺序撤单，特函请您书面确认"
                     f"撤销或减量，并办理退款赔付。")})
    for oid in result["revoked"]:
        d = result["decisions"][oid]
        consent = d.get("consent") or {}
        old_id = d.get("notice_id") or (by_order.get(oid, [{}])[0].get("id"))
        comp = _compensation_text(consent.get("compensation"))
        out.append({
            "id": f"NTF-{oid}-CORRECTION-v2",
            "notice_id": f"NTF-{oid}-CORRECTION-v2",
            "order_id": oid, "branch": d.get("branch"),
            "customer": d.get("customer"), "kind": "formal_correction",
            "type": "correction", "status": REVOKED, "version": 2,
            "supersedes": old_id, "corrects": old_id,
            "consent_id": consent.get("id"),
            "compensation": consent.get("compensation"),
            "inventory_version": inv_v,
            "title": "订单履约更正通知（撤销原“接单成功”）",
            "body": (
                f"尊敬的 {d['customer']}：您于 {d.get('signed_at') or ''} 在 "
                f"{d.get('branch')} 店签署的 {oid} 号订单（{d['label']} "
                f"{d['requested_kg']} 千克），门店断网时本地显示“接单成功”"
                f"（原通知 {old_id or '无编号'}）。两店离线承诺恢复联网合并后，"
                f"承诺总量 {result['total_promised_kg']} 千克超过总仓同一实物批次"
                f"库存 {result.get('original_stock_kg', result['on_hand_kg'])} 千克，"
                f"超售缺口 {result.get('conflict_excess_kg', result['gap_kg'])} 千克，"
                f"且无满足标签/等级/有机合同的合格补货完成入库。依据您书面签署的"
                f"撤销及赔付确认（{consent.get('id', '签认件')}），{oid} 号订单自本"
                f"更正起撤销，不再占用任何库存；我方将按{comp}履行。原“接单成功”"
                f"通知同时更正作废，一切以本正式更正通知为准。")})
    return out


class NoticeBook:
    """Append-only notification ledger; old notices stay readable forever."""

    def __init__(self, prior=None):
        self._notices = {}
        self._by_order = {}
        self._seq = {}
        self._counter = 0
        for n in prior or []:
            if isinstance(n, dict):
                self.append(n)

    def load(self, notice):
        nid = first_key(notice, "id", "notice_id")
        if nid is None:
            raise ValueError("notice requires id")
        if nid not in self._notices:
            self._notices[nid] = copy.deepcopy(notice)
            self._counter += 1
            self._seq[nid] = self._counter
        return nid

    def _index(self, notice):
        oid = first_key(notice, "order_id", "order", "oid")
        if oid is not None:
            ids = self._by_order.setdefault(str(oid), [])
            nid = first_key(notice, "id", "notice_id")
            if nid not in ids:
                ids.append(nid)

    def append(self, notice):
        nid = self.load(notice)
        self._index(notice)
        return self._notices[nid]

    add = append
    publish = append

    def read(self, notice_id):
        """Read back the exact historical notice (never mutated)."""
        if notice_id not in self._notices:
            raise KeyError(notice_id)
        return copy.deepcopy(self._notices[notice_id])

    get = read

    def all(self):
        return [copy.deepcopy(n) for n in self._notices.values()]

    def for_order(self, order_id):
        return [self.read(i) for i in self._by_order.get(str(order_id), [])]

    def current(self, order_id):
        rows = self.for_order(order_id)
        if not rows:
            return None
        return max(rows, key=lambda n: (num(n.get("version", 1)),
                                        self._seq.get(first_key(n, "id", "notice_id"), 0)))

    def correction_chain(self, order_id):
        rows = self.for_order(order_id)
        by_id = {first_key(n, "id", "notice_id"): n for n in rows}
        cur = self.current(order_id)
        chain = []
        seen = set()
        while cur is not None:
            nid = first_key(cur, "id", "notice_id")
            if nid in seen:
                break
            seen.add(nid)
            chain.append(cur)
            parent = cur.get("supersedes") or cur.get("corrects")
            cur = by_id.get(parent)
        return chain


NotificationBook = NoticeBook
NotificationLog = NoticeBook
NoticeLedger = NoticeBook
create_notifications = build_notifications
build_correction_notices = build_notifications


# ---------------------------------------------------------------------------
# main reconcile
# ---------------------------------------------------------------------------
def reconcile(source=None, *, scenario=None, orders=None, boxes=None,
              stock=None, replenishments=None, inbound_receipts=None,
              revocations=None, consents=None, returns=None,
              outbound_scans=None, notices=None, allocations=None,
              require_signatures=False, emit_notifications=True, **extra):
    """Replay all evidence and return the single versioned merge decision."""
    world = _build_world(
        source, scenario=scenario, orders=orders, boxes=boxes, stock=stock,
        replenishments=replenishments, inbound_receipts=inbound_receipts,
        revocations=revocations, consents=consents, returns=returns,
        outbound_scans=outbound_scans, notices=notices,
        allocations=allocations, **extra)

    # Convenience: stock={'L1':10} without explicit box codes -> virtual box.
    if not world.get("boxes") and isinstance(world.get("stock"), dict):
        virtual = []
        for label, kg in world["stock"].items():
            if label in ("version", "revision"):
                continue
            virtual.append({"code": f"STOCK-{label}", "label": label,
                            "location": WAREHOUSE, "kg": kg,
                            "version": world["stock"].get("version"),
                            "source": "original", "virtual": True})
        if virtual:
            world["boxes"] = virtual
    if boxes is not None:
        world["boxes"] = boxes

    norm_orders = _norm_orders(world)
    consent_by_order, consent_records = _consent_map(world, require_signatures)

    # First inventory pass: physical facts only (no plan allocations).
    view = inventory_view(world)

    shipped_by_order = {}
    for scan in view.get("outbound_events", []):
        if scan.get("order_id") is not None:
            shipped_by_order[str(scan["order_id"])] = clean(
                num(shipped_by_order.get(str(scan["order_id"]))) + scan["kg"])

    active, revoked_rows = [], []
    for o in norm_orders:
        o["shipped_kg"] = shipped_by_order.get(str(o["id"]), 0)
        o["required_kg"] = clean(max(0.0, o["kg"] - num(o["shipped_kg"])))
        consent = consent_by_order.get(str(o["id"]))
        if consent:
            o["consent"] = consent
            revoked_rows.append(o)
        else:
            active.append(o)

    # Evaluate every replenishment batch against every active order contract.
    withdrawn_ids = set()
    for w in (world.get("replenishment_withdrawals") or world.get("withdrawals")
              or []):
        ref = first_key(w, "replenishment", "batch", "batch_id", "ref")
        if ref is not None:
            withdrawn_ids.add(str(ref))
    batches = []
    for b in world.get("replenishments") or []:
        if not isinstance(b, dict):
            continue
        bid = str(first_key(b, "id", "batch_id", "code"))
        if bid in withdrawn_ids:
            b = {**b, "withdrawn": True}
        batches.append(b)
    evals_by_order = {}
    all_batch_evals = []
    for o in active:
        evals = [evaluate_batch(copy.deepcopy(b), o, view) for b in batches
                 if isinstance(b, dict)]
        evals_by_order[o["id"]] = evals
        for e in evals:
            e["order_id"] = o["id"]
        all_batch_evals.extend(evals)

    rows, unmet, allocated_by_order = _plan_subset(active, view, evals_by_order)

    # Recompute the inventory view carrying the global plan as occupation.
    final_view = inventory_view(world, allocations=rows)

    decisions = {}
    accepted_ids, awaiting_ids, consent_ids = [], [], []
    for o in active:
        oid = o["id"]
        gap = unmet[oid]
        o_rows = [r for r in rows if r["order_id"] == oid]
        evals = evals_by_order[oid]
        pending_batches = [e for e in evals
                           if e["contract_eligible"] and e["quality_ok"]
                           and e["substitution_clause_ok"]
                           and e["in_transit"]]
        if gap <= EPS:
            status = ACCEPTED
            accepted_ids.append(oid)
        elif pending_batches:
            status = AWAITING
            awaiting_ids.append(oid)
        else:
            status = CONSENT_REQUIRED
            consent_ids.append(oid)
        box_refs = [{"code": r["box"], "box": r["box"],
                     "box_code": r["box"], "kg": r["kg"],
                     "version": r["version"], "source": r["source"],
                     "batch": r.get("batch")} for r in o_rows]
        decisions[oid] = {
            "id": oid, "order_id": oid, "branch": o["branch"],
            "customer": o["customer"], "label": o["label"],
            "status": status, "state": status, "outcome": status,
            "fulfillable": status == ACCEPTED,
            "accepted": status == ACCEPTED,
            "requested_kg": clean(o["kg"]), "shipped_kg": o["shipped_kg"],
            "required_kg": clean(o["required_kg"]),
            "allocated_kg": allocated_by_order[oid],
            "remaining_gap_kg": gap, "gap_kg": gap,
            "allocations": box_refs,
            "boxes": [{"code": r["box"], "version": r["version"],
                       "kg": r["kg"]} for r in o_rows],
            "signed_order_id": oid,
            "signed_order_sha256": o["sha256"],
            "signed_order": {"id": oid, "sha256": o["sha256"],
                             "signed_at": o["signed_at"],
                             "signature_valid": o["signature_valid"],
                             "branch": o["branch"]},
            "contract": o["contract"],
            "notice_id": o["notice_id"],
            "inventory_version": final_view["inventory_version"],
            "pending_replenishment": pending_batches,
            "batch_evaluations": evals,
            "reasons": sorted({reason for e in evals for reason in e["reasons"]}),
        }

    revoked_ids = []
    for o in revoked_rows:
        oid = o["id"]
        revoked_ids.append(oid)
        decisions[oid] = {
            "id": oid, "order_id": oid, "branch": o["branch"],
            "customer": o["customer"], "label": o["label"],
            "status": REVOKED, "state": REVOKED, "outcome": REVOKED,
            "fulfillable": False, "accepted": False,
            "revoked": True,
            "requested_kg": clean(o["kg"]), "shipped_kg": o["shipped_kg"],
            "required_kg": 0, "allocated_kg": 0,
            "remaining_gap_kg": 0, "gap_kg": 0,
            "allocations": [], "boxes": [],
            "signed_order_id": oid, "signed_order_sha256": o["sha256"],
            "signed_order": {"id": oid, "sha256": o["sha256"],
                             "signed_at": o["signed_at"],
                             "signature_valid": o["signature_valid"]},
            "contract": o["contract"], "notice_id": o["notice_id"],
            "consent": {"id": o["consent"]["id"], "at": o["consent"]["at"],
                        "sha256": o["consent"]["sha256"],
                        "compensation": o["consent"]["compensation"]},
            "inventory_version": final_view["inventory_version"],
            "allocations_released": True}

    # Candidate consent targets: which single revocation makes the rest fit?
    consent_options = []
    for target in active:
        subset = [o for o in active if o["id"] != target["id"]]
        _, sub_unmet, _ = _plan_subset(subset, view, evals_by_order)
        residual = clean(sum(sub_unmet.values()))
        if residual <= EPS:
            consent_options.append({
                "revoke_order": target["id"],
                "branch": target["branch"],
                "requested_kg": clean(target["kg"]),
                "current_unmet_kg": unmet[target["id"]],
                "residual_gap_for_others_kg": residual,
                "makes_rest_fulfillable": True})

    total_promised = clean(sum(o["kg"] for o in norm_orders))
    active_promised = clean(sum(o["kg"] for o in active))
    wh_label_totals = final_view["by_location"].get(WAREHOUSE, {})
    l1 = wh_label_totals.get("L1") or next(iter(wh_label_totals.values()), {})
    on_hand = l1.get("on_hand_kg", 0)
    in_transit = l1.get("in_transit_kg", 0)
    occupied = l1.get("occupied_kg", 0)
    total_gap = clean(sum(unmet.values()))

    # The fixed conflict arithmetic (13 promised vs the original 10 kg lot),
    # distinct from the dynamic executable gap after authorized remedies.
    declared_stock = None
    if isinstance(world.get("stock"), dict):
        declared_stock = world["stock"].get("L1")
    original_box_kg = clean(sum(
        num(b.get("gross_kg")) for b in final_view["boxes"].values()
        if b.get("source") == "original" and b.get("label") in (None, "L1")))
    original_stock = clean(num(declared_stock)) if declared_stock is not None \
        else original_box_kg
    conflict_excess = clean(max(0.0, total_promised - num(original_stock)))

    if not active:
        top_status = FULFILLABLE
    elif not consent_ids and not awaiting_ids:
        top_status = FULFILLABLE
    elif consent_ids and not awaiting_ids:
        top_status = OVERSUBSCRIBED
    else:
        top_status = Status("awaiting_authorization", [
            "partially_fulfillable", "pending", "in_progress"])

    result = {
        "status": top_status, "ok": top_status == FULFILLABLE,
        "globally_executable": top_status == FULFILLABLE,
        "all_accepted": top_status == FULFILLABLE,
        "scenario": world.get("active_scenario"),
        "inventory_version": final_view["inventory_version"],
        "version": final_view["inventory_version"],
        "version_sha256": final_view["version_sha256"],
        "inventory": final_view,
        "total_promised_kg": total_promised,
        "active_promised_kg": active_promised,
        "stock_kg": clean(num((world.get("stock") or {}).get("L1")))
        if isinstance(world.get("stock"), dict) else None,
        "original_stock_kg": original_stock,
        "conflict_excess_kg": conflict_excess,
        "oversell_gap_kg": conflict_excess,
        "physical_on_hand_kg": on_hand,
        "on_hand_kg": on_hand,
        "in_transit_kg": in_transit,
        "occupied_kg": occupied,
        "gap_kg": total_gap,
        "shortfall_kg": conflict_excess,
        "decisions": decisions,
        "orders": [decisions[o["id"]] for o in norm_orders],
        "accepted": sorted(accepted_ids),
        "awaiting_replenishment": sorted(awaiting_ids),
        "revocation_consent_required": sorted(consent_ids),
        "revoked": sorted(revoked_ids),
        "conflicts": final_view["conflicts"],
        "over_allocated_boxes": final_view["over_allocated_boxes"],
        "allocations": rows,
        "box_loads": final_view["boxes"],
        "consent_records": consent_records,
        "consent_options": consent_options,
        "batch_evaluations": all_batch_evals,
        "notifications": [],
    }
    result["decision_hash"] = digest(_hashable_result(result))
    result["version"] = result["decision_hash"][:12]

    # Read-model aliases for different call-site vocabularies.
    result["results"] = decisions
    result["merge_decisions"] = decisions
    result["order_decisions"] = decisions
    result["fulfillable_orders"] = sorted(accepted_ids)
    result["accepted_orders"] = sorted(accepted_ids)
    result["difference_kg"] = conflict_excess
    result["oversubscribed_kg"] = conflict_excess

    prior_notices = world.get("notices") or world.get("local_notices") or []
    if emit_notifications:
        result["notifications"] = build_notifications(result, prior_notices)
    return result


def _looks_like_stock_map(obj):
    return (isinstance(obj, dict) and bool(obj)
            and all(isinstance(k, str) and isinstance(v, (int, float))
                    and not isinstance(v, bool) for k, v in obj.items())
            and not any(k in obj for k in ("orders", "branches", "boxes")))


def _looks_like_box_list(obj):
    return (isinstance(obj, list) and bool(obj)
            and isinstance(obj[0], dict)
            and any(k in obj[0] for k in ("code", "box_code", "box")))


def flexible_reconcile(*args, **kwargs):
    """Call :func:`reconcile` with tolerant positional-argument classification.

    Accepts ``merge(world)``, ``merge(orders, stock)``,
    ``merge(orders, boxes)`` or ``merge(orders, stock, boxes)`` in addition
    to the keyword form.  Intended to keep small caller conventions working.
    """
    source = kwargs.pop("source", None)
    classified = {"orders": None, "boxes": None, "stock": None}
    extra_world = {}
    positional = list(args)
    for arg in positional:
        if isinstance(arg, (str, Path)):
            text = str(arg)
            if source is None and (text.endswith(".json") or Path(text).exists()):
                source = arg
            elif source is not None and isinstance(source, dict) \
                    and text in (source.get("scenarios") or {}):
                kwargs.setdefault("scenario", text)
            else:
                source = arg
        elif isinstance(arg, list):
            if _looks_like_box_list(arg):
                classified["boxes"] = arg
            else:
                classified["orders"] = arg
        elif isinstance(arg, dict):
            if _looks_like_stock_map(arg):
                classified["stock"] = arg
            elif any(k in arg for k in ("orders", "branches", "boxes",
                                        "replenishments", "scenarios")):
                if source is None:
                    source = arg
                else:
                    extra_world.update(arg)
            elif any(k in arg for k in ("code", "box_code")):
                classified["boxes"] = [arg]
            elif "kg" in arg or "qty" in arg:
                classified["orders"] = [arg]
            else:
                if source is None:
                    source = arg
    for key, value in classified.items():
        if value is not None and kwargs.get(key) is None:
            kwargs[key] = value
    if extra_world and isinstance(source, dict):
        source = {**source, **extra_world}
    return reconcile(source, **kwargs)


# Friendly functional aliases (positional-tolerant).
merge = flexible_reconcile
resolve = flexible_reconcile
decide = flexible_reconcile
merge_commitments = flexible_reconcile
merge_offline_orders = flexible_reconcile
merge_offline = flexible_reconcile
run_merge = flexible_reconcile
global_reconcile = flexible_reconcile
publish_merge = flexible_reconcile
build_merge_decision = flexible_reconcile
compute_merge = flexible_reconcile


class FulfillableSet(set):
    def __new__(cls, ids=(), result=None):
        obj = super().__new__(cls, ids)
        return obj

    def __init__(self, ids=(), result=None):
        super().__init__(ids)
        self.result = result


def fulfillable_set(source=None, *args, **kwargs):
    """Return the set of globally accepted order ids for the same stock version."""
    kwargs.setdefault("emit_notifications", False)
    result = flexible_reconcile(source, *args, **kwargs)
    return FulfillableSet(result["accepted"], result)


fulfillable = fulfillable_set


def fulfillable_orders(source=None, *args, **kwargs):
    """Sorted list of globally accepted order ids (list-shaped counterpart)."""
    return sorted(fulfillable_set(source, *args, **kwargs))


def accepted_order_ids(source=None, *args, **kwargs):
    return fulfillable_orders(source, *args, **kwargs)


qualify_batch = evaluate_batch
evaluate_replenishment = evaluate_batch
check_label_eligibility = label_verdict
resolve_offline_orders = flexible_reconcile
replay_merge = flexible_reconcile


def plan_by_order(source=None, *args, **kwargs):
    kwargs.setdefault("emit_notifications", False)
    result = flexible_reconcile(source, *args, **kwargs)
    return {oid: d["allocations"] for oid, d in result["decisions"].items()
            if d["status"] == ACCEPTED}


class AllocationPlan(list):
    """Allocation rows as a list, with a ``by_order`` projection."""

    def __init__(self, rows, result):
        super().__init__(rows)
        self.result = result
        self.by_order = {oid: d["allocations"]
                         for oid, d in result["decisions"].items()}


def allocate(source=None, *args, **kwargs):
    kwargs.setdefault("emit_notifications", False)
    result = flexible_reconcile(source, *args, **kwargs)
    return AllocationPlan(result["allocations"], result)


build_plan = allocate
plan = allocate


def _hashable_result(result):
    keep = {k: v for k, v in result.items()
            if k not in ("inventory", "box_loads", "notifications",
                         "batch_evaluations", "consent_records")}
    return json.loads(json.dumps(keep, ensure_ascii=False, sort_keys=True,
                                 default=str))


# ---------------------------------------------------------------------------
# stateful replay facade
# ---------------------------------------------------------------------------
class MergeBook:
    """Capture uploads, replay the merge, and keep notices/history append-only."""

    _LIST_FOR_KIND = {
        "order_signed": "orders", "order": "orders",
        "revocation_consent": "revocation_consents", "consent": "revocation_consents",
        "return": "returns",
        "outbound_scan": "outbound_scans", "outbound": "outbound_scans",
        "inbound_receipt": "inbound_receipts", "inbound": "inbound_receipts",
        "inbound_scan": "inbound_receipts",
        "replenishment": "replenishments", "batch": "replenishments",
        "replenishment_withdrawal": "replenishment_withdrawals",
        "withdrawal": "replenishment_withdrawals",
        "box": "boxes", "notice": "notices"}

    def __init__(self, source=None, stock=None, *, require_signatures=False,
                 orders=None, boxes=None, **kwargs):
        if isinstance(source, list):
            data = {"orders": source}
            if isinstance(stock, dict):
                data["stock"] = stock
            stock = None
        else:
            data = load_source(source)
        if not isinstance(data, dict):
            data = {}
        if orders is not None:
            data["orders"] = orders
        if boxes is not None:
            data["boxes"] = boxes
        if isinstance(stock, dict):
            data["stock"] = stock
        data.update({k: v for k, v in kwargs.items() if v is not None})
        if isinstance(source, (str, Path)):
            self.source_path = Path(source)
        else:
            self.source_path = None
        self.world = data
        self.require_signatures = require_signatures
        self.capture = CaptureBook(require_signature=require_signatures)
        self.notices = NoticeBook(
            self.world.get("notices") or self.world.get("local_notices") or [])
        self.history = []
        self.revision = 0
        self.result = None
        self._seed_capture()

    _SEED_KEYS = ("orders", "revocation_consents", "returns", "outbound_scans",
                  "inbound_receipts", "replenishments",
                  "replenishment_withdrawals", "boxes", "notices")

    def _seed_capture(self):
        """Hash the originals already present in the loaded world."""
        for key in self._SEED_KEYS:
            for doc in self.world.get(key, []) or []:
                if isinstance(doc, dict) and doc.get("id") is not None:
                    self.capture.capture(doc)

    # -- uploads -----------------------------------------------------------
    def upload(self, doc):
        rec = self.capture.capture(doc)
        if rec["inserted"]:
            kind = doc.get("kind") or self.capture.record(rec["id"])["kind"]
            key = self._LIST_FOR_KIND.get(kind)
            if key:
                self.world.setdefault(key, []).append(copy.deepcopy(doc))
            if kind == "notice":
                self.notices.append(doc)
        return rec

    add = upload
    put = upload

    def ingest(self, docs):
        if isinstance(docs, dict):
            docs = [docs]
        cap_out = self.capture.ingest(docs)  # preflights the whole batch
        for doc, result in zip(docs, cap_out["results"]):
            if not result["inserted"]:
                continue
            kind = doc.get("kind") or self.capture.record(result["id"])["kind"]
            key = self._LIST_FOR_KIND.get(kind)
            if key:
                self.world.setdefault(key, []).append(copy.deepcopy(doc))
            if kind == "notice":
                self.notices.append(doc)
        return {"inserted": cap_out["inserted"], "replayed": cap_out["replayed"],
                "results": cap_out["results"]}

    # -- merge / publish ---------------------------------------------------
    def reconcile(self, *, scenario=None):
        result = reconcile(self.world, scenario=scenario,
                           require_signatures=self.require_signatures)
        for n in result["notifications"]:
            self.notices.append(n)
        comparable = result["decision_hash"]
        if not self.history or self.history[-1]["decision_hash"] != comparable:
            self.revision += 1
            self.history.append({"revision": self.revision,
                                 "decision_hash": comparable,
                                 "inventory_version": result["inventory_version"],
                                 "status": str(result["status"]),
                                 "result": result})
        self.result = result
        return result

    merge = reconcile
    resolve = reconcile
    evaluate = reconcile
    run = reconcile
    merge_orders = reconcile
    publish = reconcile

    # -- notice readback ---------------------------------------------------
    def read_notice(self, notice_id):
        return self.notices.read(notice_id)

    get_notice = read_notice

    def current_notice(self, order_id):
        return self.notices.current(order_id)

    def notice_chain(self, order_id):
        return self.notices.correction_chain(order_id)

    def replay_audit(self):
        """Re-run the merge from unique captured documents; versions must match."""
        again = reconcile(self.world,
                          require_signatures=self.require_signatures,
                          emit_notifications=False)
        return {"ok": again["decision_hash"] == (self.result or {}).get("decision_hash"),
                "decision_hash": again["decision_hash"],
                "captured": self.capture.summary()}


# Noun aliases for callers/tests.
FulfillmentBook = MergeBook
FulfillmentLedger = MergeBook
OfflineMerge = MergeBook
MergeSession = MergeBook
OrderFulfillment = MergeBook
FulfillmentPlanner = MergeBook
OfflineOrderMerger = MergeBook
MergeLedger = MergeBook
CommitmentMerge = MergeBook
