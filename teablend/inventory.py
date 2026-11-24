"""Provable multi-site tea inventory: on-hand, in-transit and occupied.

``available(snapshot, label)`` is retained verbatim from the offline seed: it
reads a branch's *local* stock snapshot (each disconnected shop saw ``10``).
That number is local evidence/belief, not proof of central-warehouse stock.

Provable positions are rebuilt only from physical evidence:

* warehouse/shop box codes with weighed ``kg`` and a record ``version``;
* signed/scanned inbound receipts (a replenishment batch counts only after
  physical inbound completion -- a paper number never fills a gap);
* outbound scans and customer returns, hash/event-id de-duplicated so a
  repeated upload or a late return is applied at most once;
* global allocations produced by :mod:`teablend.fulfillment`, which are the
  only legitimate source of the *occupied* quantity.

The view is deterministic and JSON-serialisable; ``inventory_version`` is a
content hash of the physical facts so a merge decision can cite the exact
stock version it was computed against.
"""
from __future__ import annotations

import copy

from .order_capture import digest, first_key

WAREHOUSE = "WH"
EPS = 1e-9


def available(snapshot, label):
    """Read a branch's last synchronized local snapshot value."""
    return snapshot.get(label, 0)


# ---------------------------------------------------------------------------
# numeric / shape helpers
# ---------------------------------------------------------------------------
def num(value, default=0.0):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def clean(value):
    """Collapse integral floats back to int for readable assertions/JSON."""
    value = round(num(value), 6)
    if abs(value - round(value)) < EPS:
        return int(round(value))
    return value


def movement_key(event):
    """Stable de-duplication key for a scan/receipt/return event."""
    eid = first_key(event, "id", "event_id", "scan_id", "receipt_id")
    return str(eid) if eid is not None else digest(event)[:20]


def _list(world, *names):
    if not isinstance(world, dict):
        return []
    for name in names:
        value = world.get(name)
        if isinstance(value, list):
            return value
    return []


def _signed_or_scanned(event):
    if not isinstance(event, dict):
        return False
    if event.get("scan_id") or event.get("scanned_at") or event.get("scan"):
        return True
    if event.get("signature") or event.get("sign") or event.get("received_by_sign") \
            or event.get("receiver_sign") or event.get("customer_sign"):
        return True
    if event.get("received_by") and event.get("at"):
        # Named receiver plus timestamp is treated as a signed receipt line.
        return True
    return bool(event.get("signed") is True)


# ---------------------------------------------------------------------------
# normalization
# ---------------------------------------------------------------------------
def _box_rows(world):
    """Yield normalized physical-box dicts (original + replenishment boxes)."""
    rows = {}

    def put(code, **fields):
        code = str(code)
        if code in rows:
            # Later versioned record revises mutable facts (e.g. version).
            rows[code].update({k: v for k, v in fields.items() if v is not None})
        else:
            rows[code] = {"code": code, "location": WAREHOUSE, "label": None,
                          "kg": 0.0, "version": None, "state": "on_hand",
                          "source": "original", "batch": None,
                          "inbound_id": None, "received_at": None, **fields}

    for b in _list(world, "boxes", "box_codes", "cases"):
        if not isinstance(b, dict):
            continue
        code = first_key(b, "code", "box", "box_code", "id")
        if code is None:
            continue
        put(code, location=b.get("location", WAREHOUSE), label=b.get("label"),
            kg=num(first_key(b, "kg", "weight_kg", "net_kg", "weight")),
            version=b.get("version"), state=b.get("state", "on_hand"),
            source=b.get("source", "original"))

    inbound = _index_inbound(_list(world, "inbound_receipts", "inbound",
                                   "inbound_scans", "goods_receipts"))
    # A withdrawal event (or a record explicitly marked withdrawn) removes
    # all boxes of a replenishment batch from provable stock.
    withdrawn = set()
    for w in _list(world, "replenishment_withdrawals", "withdrawals",
                   "batch_withdrawals"):
        if isinstance(w, dict):
            ref = first_key(w, "replenishment", "batch", "batch_id", "ref")
            if ref is not None:
                withdrawn.add(str(ref))
    rp_rows = _list(world, "replenishments", "replenishment_batches",
                    "restock_batches", "batches")
    withdrawn |= {str(first_key(r, "id", "batch_id", "code"))
                  for r in rp_rows if isinstance(r, dict) and r.get("withdrawn")}
    for rp in rp_rows:
        if not isinstance(rp, dict) or rp.get("withdrawn"):
            continue
        batch_ref = str(first_key(rp, "id", "batch_id", "code"))
        if batch_ref in withdrawn:
            continue
        label = rp.get("label")
        batch_id = first_key(rp, "id", "batch_id", "code")
        rp_boxes = rp.get("boxes")
        if not rp_boxes:
            rp_boxes = [{"code": rp.get("box_code") or f"{batch_id}:B1",
                         "kg": rp.get("kg", rp.get("available_kg", 0))}]
        receipt = inbound.get(str(batch_id)) or inbound.get("*")
        embedded_received = rp.get("received_at") or rp.get("inbound_at")
        embedded_proof = embedded_received and (
            rp.get("received_by") or rp.get("received_sign")
            or rp.get("receiver_sign") or rp.get("signature")
            or rp.get("signed") is True)
        for rb in rp_boxes:
            code = first_key(rb, "code", "box", "box_code", "id") if isinstance(rb, dict) else rb
            kg = num(rb.get("kg", rp.get("kg", 0))) if isinstance(rb, dict) else num(rp.get("kg"))
            matched = receipt
            if isinstance(receipt, dict) and receipt.get("boxes"):
                codes = {str(x) for x in receipt["boxes"]}
                if str(code) not in codes:
                    matched = None
            received = bool(matched and matched.get("_proof")) or bool(embedded_proof)
            put(code, location=(matched or {}).get("location") or rp.get("location") or WAREHOUSE,
                label=(isinstance(rb, dict) and rb.get("label")) or label,
                kg=kg, version=(isinstance(rb, dict) and rb.get("version")) or rp.get("version"),
                state="on_hand" if received else "in_transit",
                source="replenishment", batch=batch_id,
                inbound_id=(matched or {}).get("id") if matched else (
                    rp.get("inbound_id") if embedded_proof else None),
                received_at=((matched or {}).get("at") or embedded_received) if received else None)
    return rows


def _index_inbound(receipts):
    by_ref = {}
    for r in receipts:
        if not isinstance(r, dict):
            continue
        proof = _signed_or_scanned(r)
        rec = {"_proof": proof, "id": movement_key(r),
               "at": first_key(r, "at", "received_at", "scanned_at"),
               "location": r.get("location", WAREHOUSE),
               "boxes": r.get("boxes") or r.get("box_codes")}
        for ref in (r.get("replenishment"), r.get("batch"), r.get("batch_id"),
                    r.get("ref"), r.get("replenishment_id")):
            if ref is not None:
                by_ref[str(ref)] = rec
    return by_ref


def _snapshot_facts(world):
    snaps = []
    for b in _list(world, "branches"):
        branch = b.get("branch") or b.get("location") or b.get("id")
        snap = b.get("snapshot") or {}
        version = b.get("snapshot_version") or snap.get("version") or b.get("version")
        at = b.get("snapshot_at") or b.get("at")
        for label, kg in snap.items():
            if label == "version":
                continue
            snaps.append({"location": branch, "label": label, "kg": num(kg),
                          "version": version, "at": at, "kind": "local_snapshot"})
    for s in _list(world, "snapshots", "stock_snapshots"):
        if not isinstance(s, dict):
            continue
        snaps.append({"location": first_key(s, "location", "branch", "site"),
                      "label": s.get("label"), "kg": num(s.get("kg")),
                      "version": s.get("version"), "at": s.get("at"),
                      "kind": "local_snapshot"})
    return snaps


def _local_commitments(world):
    """Signed kg each shop locally believes it committed (de-duped by order)."""
    out = []
    seen_orders = set()
    if _list(world, "orders"):
        sources = [("orders", o) for o in _list(world, "orders")]
    else:
        sources = []
    for b in _list(world, "branches"):
        o = b.get("order") or {}
        sources.append(("branch", {"order_id": o.get("id"),
                                   "location": b.get("branch"),
                                   "label": o.get("label"), "kg": o.get("kg")}))
    for _kind, o in sources:
        oid = first_key(o, "id", "order_id")
        if oid is not None and oid in seen_orders:
            continue
        if oid is not None:
            seen_orders.add(oid)
        out.append({"order_id": oid,
                    "location": first_key(o, "branch", "location"),
                    "label": o.get("label"), "kg": num(o.get("kg"))})
    return out


# ---------------------------------------------------------------------------
# main view
# ---------------------------------------------------------------------------
def inventory_view(source=None, allocations=None, **overrides):
    """Build the provable inventory view for WH + shops.

    ``source`` is a fixture/world dict (or a list of boxes for convenience).
    ``allocations`` optionally applies a global executable plan.
    """
    if isinstance(source, list):
        world = {"boxes": source}
        world.update(overrides)
    elif source is None:
        world = dict(overrides)
    else:
        world = copy.deepcopy(source)
        if overrides:
            world.update(overrides)

    boxes = _box_rows(world)

    # ---- de-duplicated outbound scans ------------------------------------
    outbound = {}
    for e in _list(world, "outbound_scans", "outbound", "scans"):
        if not isinstance(e, dict):
            continue
        key = movement_key(e)
        if key in outbound:
            outbound[key]["replayed"] += 1
            continue
        outbound[key] = {"id": key, "box": first_key(e, "box", "code", "box_code"),
                         "order_id": first_key(e, "order", "order_id"),
                         "kg": num(first_key(e, "kg", "weight_kg", "net_kg")),
                         "at": first_key(e, "at", "scanned_at"), "replayed": 0}
    for scan in outbound.values():
        code = str(scan["box"]) if scan["box"] is not None else None
        if code in boxes:
            boxes[code]["outbound_kg"] = clean(num(boxes[code].get("outbound_kg")) + scan["kg"])
        scan["matched"] = code in boxes if code else False

    # ---- de-duplicated customer returns ----------------------------------
    returns = {}
    for e in _list(world, "returns", "return_receipts", "customer_returns"):
        if not isinstance(e, dict):
            continue
        key = movement_key(e)
        if key in returns:
            returns[key]["replayed"] += 1
            continue
        kg = num(first_key(e, "kg", "return_kg", "returned_kg", "net_kg"))
        code = first_key(e, "box", "code", "box_code") or f"RETURN:{key}"
        code = str(code)
        returns[key] = {"id": key, "box": code, "kg": kg,
                        "location": e.get("location", WAREHOUSE),
                        "label": e.get("label"), "order_id": first_key(e, "order", "order_id"),
                        "at": first_key(e, "at", "received_at"), "replayed": 0}
        if code in boxes:
            boxes[code]["kg"] = num(boxes[code]["kg"]) + kg
            boxes[code]["restocked_kg"] = clean(num(boxes[code].get("restocked_kg")) + kg)
        else:
            boxes[code] = {"code": code, "location": e.get("location", WAREHOUSE),
                           "label": e.get("label"), "kg": kg, "version": e.get("version"),
                           "state": "on_hand", "source": "return", "batch": None,
                           "inbound_id": None, "received_at": first_key(e, "at", "received_at"),
                           "restocked_kg": clean(kg)}

    # ---- global allocations / occupation ---------------------------------
    alloc_rows = _normalize_allocations(allocations if allocations is not None
                                        else world.get("allocations"))
    for row in alloc_rows:
        code = str(row["box"])
        if code not in boxes:
            boxes[code] = {"code": code, "location": row.get("location", WAREHOUSE),
                           "label": row.get("label"), "kg": 0.0, "version": row.get("version"),
                           "state": "unknown", "source": row.get("source", "plan"),
                           "allocated_kg": 0.0, "unknown": True}
        b = boxes[code]
        b["allocated_kg"] = num(b.get("allocated_kg")) + row["kg"]
        b.setdefault("allocations", []).append(
            {"order_id": row["order_id"], "kg": clean(row["kg"]),
             "version": row.get("version"), "source": row.get("source")})

    # ---- finalize box rows -----------------------------------------------
    for b in boxes.values():
        gross = num(b["kg"])
        out_kg = num(b.get("outbound_kg"))
        alloc_kg = num(b.get("allocated_kg"))
        b["gross_kg"] = clean(gross)
        b["outbound_kg"] = clean(out_kg)
        b["allocated_kg"] = clean(alloc_kg)
        b["on_hand_kg"] = clean(max(0.0, gross - out_kg)) if b["state"] == "on_hand" else 0
        b["in_transit_kg"] = clean(gross) if b["state"] == "in_transit" else 0
        b["remaining_kg"] = clean(gross - out_kg - alloc_kg) if b["state"] == "on_hand" else clean(-alloc_kg)
        b["over_allocated"] = bool(b["state"] == "on_hand"
                                   and alloc_kg - EPS > gross - out_kg)
        b["kg"] = clean(gross)

    snapshots = _snapshot_facts(world)
    commits = _local_commitments(world)

    locations = sorted({WAREHOUSE} | {str(s["location"]) for s in snapshots if s["location"]}
                        | {str(c["location"]) for c in commits if c["location"]}
                        | {b["location"] for b in boxes.values() if b.get("location")})
    by_location = {loc: {} for loc in locations}

    def cell(loc, label):
        return by_location[loc].setdefault(label, {
            "location": loc, "label": label, "on_hand_kg": 0, "in_transit_kg": 0,
            "occupied_kg": 0, "shipped_kg": 0, "available_kg": 0,
            "snapshot_kg": None, "snapshot_version": None, "snapshot_at": None,
            "locally_committed_kg": 0, "box_codes": [], "boxes": []})

    for b in boxes.values():
        if not b.get("label"):
            continue
        c = cell(b["location"], b["label"])
        c["on_hand_kg"] = num(c["on_hand_kg"]) + num(b["on_hand_kg"])
        c["in_transit_kg"] = num(c["in_transit_kg"]) + num(b["in_transit_kg"])
        c["occupied_kg"] = num(c["occupied_kg"]) + num(b["allocated_kg"])
        c["shipped_kg"] = num(c["shipped_kg"]) + num(b.get("outbound_kg"))
        c["box_codes"].append(b["code"])
        c["boxes"].append({"code": b["code"], "kg": b["kg"], "version": b.get("version"),
                           "state": b["state"], "source": b["source"],
                           "allocated_kg": b["allocated_kg"],
                           "remaining_kg": b["remaining_kg"]})

    for s in snapshots:
        c = cell(str(s["location"]), s["label"])
        c["snapshot_kg"] = s["kg"]
        c["snapshot_version"] = s["version"]
        c["snapshot_at"] = s["at"]

    for cm in commits:
        if not cm.get("label") or not cm.get("location"):
            continue
        c = cell(str(cm["location"]), cm["label"])
        c["locally_committed_kg"] = num(c["locally_committed_kg"]) + cm["kg"]

    labels = {}
    for loc, row in by_location.items():
        for label, raw in row.items():
            for key in ("on_hand_kg", "in_transit_kg", "occupied_kg",
                        "shipped_kg", "locally_committed_kg"):
                raw[key] = clean(raw[key])
            raw["available_kg"] = clean(num(raw["on_hand_kg"]) - num(raw["occupied_kg"]))
            raw["box_codes"] = sorted(raw["box_codes"])
            total = labels.setdefault(label, {"label": label, "on_hand_kg": 0,
                                              "in_transit_kg": 0, "occupied_kg": 0,
                                              "shipped_kg": 0, "available_kg": 0})
            for key in ("on_hand_kg", "in_transit_kg", "occupied_kg", "shipped_kg"):
                total[key] = num(total[key]) + num(raw[key])

    for label, t in labels.items():
        for key in ("on_hand_kg", "in_transit_kg", "occupied_kg", "shipped_kg"):
            t[key] = clean(t[key])
        t["available_kg"] = clean(num(t["on_hand_kg"]) - num(t["occupied_kg"]))

    conflicts = [b["code"] for b in boxes.values() if b.get("over_allocated")]
    version = _inventory_version(boxes, outbound, returns, alloc_rows, world)
    explicit_revision = world.get("inventory_revision", world.get("version")) \
        if isinstance(world, dict) else None

    return {
        "inventory_version": version[:12],
        "version": version[:12],
        "version_sha256": version,
        "revision": explicit_revision,
        "locations": locations,
        "by_location": by_location,
        "positions": by_location,
        "labels": labels,
        "totals": labels,
        "boxes": {code: _clean_box(b) for code, b in sorted(boxes.items())},
        "outbound_events": list(outbound.values()),
        "return_events": list(returns.values()),
        "allocation_rows": alloc_rows,
        "over_allocated_boxes": conflicts,
        "conflicts": conflicts,
    }


# Friendly aliases.
positions = inventory_view
build_positions = inventory_view
summarize = inventory_view
ledger = inventory_view


def position(world, location=WAREHOUSE, label="L1", allocations=None):
    """Return one ``(location, label)`` position cell."""
    view = inventory_view(world, allocations=allocations)
    return view["by_location"].get(location, {}).get(label)


def stock_of(world, label="L1", location=WAREHOUSE):
    cell = position(world, location, label)
    return cell["on_hand_kg"] if cell else 0


def occupied_of(world, label="L1", location=WAREHOUSE, allocations=None):
    cell = position(world, location, label, allocations)
    return cell["occupied_kg"] if cell else 0


def in_transit_of(world, label="L1", location=WAREHOUSE):
    cell = position(world, location, label)
    return cell["in_transit_kg"] if cell else 0


# Callers that read the whole multi-site ledger as ``inventory(world)``.
inventory = inventory_view
stock = inventory_view


def _normalize_allocations(raw):
    rows = []
    if not raw:
        return rows
    for a in raw:
        if isinstance(a, dict):
            oid = first_key(a, "order_id", "order", "id")
            code = first_key(a, "box", "code", "box_code")
            if code is None and isinstance(a.get("boxes"), list) and "kg" not in a:
                for sub in a["boxes"]:
                    rows.append({"order_id": oid,
                                 "box": first_key(sub, "code", "box", "box_code"),
                                 "kg": num(first_key(sub, "kg", "allocated_kg")),
                                 "version": sub.get("version"),
                                 "source": sub.get("source")})
                continue
            rows.append({"order_id": oid, "box": code,
                         "kg": num(first_key(a, "kg", "allocated_kg")),
                         "version": a.get("version"), "source": a.get("source")})
        elif isinstance(a, (list, tuple)) and len(a) >= 3:
            rows.append({"order_id": a[0], "box": a[1], "kg": num(a[2]),
                         "version": a[3] if len(a) > 3 else None, "source": None})
    return rows


def _clean_box(b):
    out = dict(b)
    for key in ("kg", "gross_kg", "outbound_kg", "allocated_kg",
                "on_hand_kg", "in_transit_kg", "remaining_kg"):
        if key in out:
            out[key] = clean(out[key])
    if not out.get("unknown"):
        out.pop("unknown", None)
    return out


def _inventory_version(boxes, outbound, returns, alloc_rows, world):
    facts = {
        "boxes": sorted(
            (code, clean(b.get("kg")), b.get("version"), b.get("state"),
             b.get("label"), b.get("location"))
            for code, b in boxes.items()),
        "outbound": sorted((k, clean(v["kg"]), str(v["box"])) for k, v in outbound.items()),
        "returns": sorted((k, clean(v["kg"]), str(v["box"])) for k, v in returns.items()),
        "allocations": sorted((str(r["order_id"]), str(r["box"]), clean(r["kg"]))
                              for r in alloc_rows),
    }
    return digest(facts)


class InventoryBook:
    """Stateful thin wrapper so callers can attach plans and re-read views."""

    def __init__(self, world=None, **kwargs):
        self.world = copy.deepcopy(world) if isinstance(world, dict) else {}
        if kwargs:
            self.world.update(kwargs)
        self._allocations = []

    def add_allocations(self, rows):
        self._allocations.extend(_normalize_allocations(rows if isinstance(rows, list) else [rows]))
        return self

    def set_allocations(self, rows):
        self._allocations = _normalize_allocations(rows)
        return self

    def view(self):
        return inventory_view(self.world, allocations=self._allocations)

    positions = view
    summary = view

    def position(self, location=WAREHOUSE, label="L1"):
        return self.view()["by_location"].get(location, {}).get(label)

    def on_hand(self, location=WAREHOUSE, label="L1"):
        cell = self.position(location, label)
        return cell["on_hand_kg"] if cell else 0

    def in_transit(self, location=WAREHOUSE, label="L1"):
        cell = self.position(location, label)
        return cell["in_transit_kg"] if cell else 0

    def occupied(self, location=WAREHOUSE, label="L1"):
        cell = self.position(location, label)
        return cell["occupied_kg"] if cell else 0

    def snapshot(self, location="A", label="L1"):
        cell = self.position(location, label)
        return cell["snapshot_kg"] if cell else None

    def available_to_promise(self, location=WAREHOUSE, label="L1"):
        cell = self.position(location, label)
        return cell["available_kg"] if cell else 0


# Noun aliases for alternative caller vocabularies.
Inventory = InventoryBook
StockLedger = InventoryBook
MultiSiteInventory = InventoryBook
build_inventory = inventory_view
inventory_summary = inventory_view
inventory_positions = inventory_view
stock_positions = inventory_view
provable_inventory = inventory_view
stock_summary = inventory_view
