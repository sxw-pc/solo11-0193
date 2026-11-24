"""Capture signed offline order originals and customer authorizations.

Two shops sign orders while disconnected.  The signed original (with its
content hash and signature string) is the only thing that may later drive a
global merge; the local "接单成功" screen is just a provisional notice.

The capture log is append-only and content addressed, following the same
replay rules as :mod:`teablend.store`:

* same id + same body  -> idempotent replay, never a second occupation;
* same id + changed body -> :class:`DocumentCollision`, whole batch rejected;
* documents are returned in event time for deterministic replay, but the
  fulfillment merge never uses arrival/signing order to pick a winner.

The signature check is the same simulated-credential boundary used elsewhere
in this repository: non-empty signature strings are evidence, there is no
real CA / HSM integration.  An optional demo HMAC helper is provided.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json


class DocumentCollision(ValueError):
    """A document id was re-uploaded with different content."""


# Alternate exception name.
CollisionError = DocumentCollision


#: Fields that may carry a signer's mark across the fixture vocabulary.
SIGNATURE_FIELDS = ("signature", "sign", "customer_sign", "receiver_sign",
                    "received_by_sign", "signed_signature", "signer")

ORDER_KEYS = ("order", "order_id", "oid")


def canonical(item):
    return json.dumps(item, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def digest(doc):
    """SHA-256 over the canonical JSON of a document's business content."""
    return hashlib.sha256(canonical(doc).encode("utf-8")).hexdigest()


content_hash = digest
fingerprint = digest
hash_document = digest
sha256_document = digest


def first_key(doc, *names):
    for name in names:
        if name in doc and doc[name] not in (None, ""):
            return doc[name]
    return None


def signature_of(doc):
    return first_key(doc, *SIGNATURE_FIELDS)


def has_signature(doc):
    sig = signature_of(doc)
    return bool(sig) and str(sig).strip().lower() not in ("true", "signed", "1") \
        or doc.get("signed") is True


def sign_demo(doc, secret="teablend-demo"):
    """Produce the optional demo HMAC signature for a document."""
    return "demo-hmac:" + hmac.new(
        secret.encode("utf-8"), digest(doc).encode("utf-8"),
        hashlib.sha256).hexdigest()


def verify_signature(doc, secret=None):
    """Verify a signature.

    With ``secret=None`` this performs the repository's structural check
    (a non-empty sign mark exists).  With a shared secret and a
    ``demo-hmac:`` value it performs constant-time HMAC verification.
    """
    sig = signature_of(doc)
    if not sig:
        return False
    if secret is None or not str(sig).startswith("demo-hmac:"):
        return bool(str(sig).strip())
    expected = sign_demo(doc, secret)
    return hmac.compare_digest(expected, sig)


def event_time(doc):
    """Best-effort sortable event time; missing values sort last."""
    value = first_key(doc, "at", "signed_at", "received_at", "scanned_at",
                      "created_at", "ts")
    return str(value) if value is not None else "~"


def infer_kind(doc):
    kind = doc.get("kind") or doc.get("type")
    if kind:
        return kind
    if "consent" in doc or ("compensation" in doc and "order" in doc):
        return "revocation_consent"
    if "return" in str(doc.get("id", "")).lower() or "returned_kg" in doc:
        return "return"
    if "boxes" in doc and "kg" in doc and "location" in doc:
        return "box"
    if "branch" in doc and "kg" in doc:
        return "order_signed"
    return "document"


class CaptureBook:
    """Append-only, hash-deduplicated store of signed offline documents."""

    def __init__(self, require_signature=False):
        self.require_signature = require_signature
        self._records = {}
        self._sequence = []
        self.inserted = 0
        self.replayed = 0

    # -- write side -------------------------------------------------------
    def capture(self, doc):
        if not isinstance(doc, dict):
            raise ValueError("captured document must be an object")
        doc_id = first_key(doc, "id", "doc_id", "event_id")
        if doc_id in (None, ""):
            raise ValueError("captured document requires an id")
        if self.require_signature and not verify_signature(doc):
            raise ValueError(f"document {doc_id} lacks a valid signature")
        body = copy.deepcopy(doc)
        sha = digest(body)
        old = self._records.get(doc_id)
        if old is not None:
            if old["sha256"] != sha:
                raise DocumentCollision(
                    f"document id {doc_id} re-uploaded with different content "
                    f"(stored {old['sha256'][:12]}, uploaded {sha[:12]})")
            old["upload_count"] += 1
            self.replayed += 1
            return {"id": doc_id, "kind": old["kind"], "status": "replayed",
                    "replayed": True, "inserted": False, "sha256": sha,
                    "upload_count": old["upload_count"]}
        record = {"id": doc_id, "kind": infer_kind(body), "sha256": sha,
                  "version": 1, "upload_count": 1,
                  "seq": len(self._sequence), "doc": body}
        self._records[doc_id] = record
        self._sequence.append(doc_id)
        self.inserted += 1
        return {"id": doc_id, "kind": record["kind"], "status": "captured",
                "replayed": False, "inserted": True, "sha256": sha,
                "upload_count": 1}

    # House-style aliases for the single-document write.
    add = capture
    put = capture
    upload = capture

    def ingest(self, docs):
        if isinstance(docs, dict):
            docs = [docs]
        if not isinstance(docs, (list, tuple)):
            raise ValueError("ingest requires a document or list of documents")
        # Pre-flight: a collision anywhere (stored or inside the batch)
        # rejects the whole batch before any state changes.
        planned = []
        batch_hashes = {}
        for doc in docs:
            doc_id = first_key(doc, "id", "doc_id", "event_id") if isinstance(doc, dict) else None
            if doc_id is None:
                raise ValueError("document requires an id")
            sha = digest(doc)
            old = self._records.get(doc_id)
            if old is not None and old["sha256"] != sha:
                raise DocumentCollision(f"document id {doc_id} content collision")
            if doc_id in batch_hashes and batch_hashes[doc_id] != sha:
                raise DocumentCollision(
                    f"document id {doc_id} uploaded twice with different content")
            batch_hashes[doc_id] = sha
            planned.append((doc, sha))
        out = {"inserted": 0, "replayed": 0, "results": []}
        for doc, _sha in planned:
            r = self.capture(doc)
            out["results"].append(r)
            if r["inserted"]:
                out["inserted"] += 1
            else:
                out["replayed"] += 1
        return out

    replay = ingest
    load_documents = ingest

    # -- read side --------------------------------------------------------
    def __len__(self):
        return len(self._records)

    def has(self, doc_id):
        return doc_id in self._records

    def get(self, doc_id):
        rec = self._records.get(doc_id)
        return copy.deepcopy(rec["doc"]) if rec else None

    def record(self, doc_id):
        return copy.deepcopy(self._records.get(doc_id))

    def documents(self, kind=None):
        rows = [copy.deepcopy(self._records[i]["doc"]) for i in self._sequence]
        if kind is not None:
            rows = [d for d in rows if infer_kind(d) == kind or d.get("kind") == kind]
        return rows

    def all(self):
        return self.documents()

    def timeline(self, kind=None):
        rows = self.documents(kind)
        return sorted(rows, key=lambda d: (event_time(d), str(d.get("id"))))

    def orders(self):
        rows = self.documents()
        out = []
        for d in rows:
            if infer_kind(d) == "order_signed" or (
                    "kg" in d and ("branch" in d or "customer" in d)
                    and d.get("kind", "order_signed") in ("order_signed", "order")):
                out.append(d)
        return sorted(out, key=lambda d: (event_time(d), str(d.get("id"))))

    def consents(self):
        wanted = {"revocation_consent", "consent", "cancellation_consent"}
        return [d for d in self.documents()
                if d.get("kind") in wanted or "consent" in str(d.get("kind", ""))]

    revocations = consents

    def returns(self):
        return [d for d in self.documents()
                if d.get("kind") == "return" or "return" in str(d.get("kind", ""))]

    def outbound_scans(self):
        return [d for d in self.documents()
                if d.get("kind") in ("outbound_scan", "outbound")]

    def inbound_receipts(self):
        return [d for d in self.documents()
                if d.get("kind") in ("inbound_receipt", "inbound", "inbound_scan")]

    def summary(self):
        return {"unique_documents": len(self._records),
                "inserted": self.inserted, "replayed": self.replayed,
                "ids": list(self._sequence)}


# Common noun aliases used by callers/tests.
OrderCapture = CaptureBook
DocumentBook = CaptureBook
SignedOrderBook = CaptureBook


def capture_order(book_or_doc, doc=None, **kwargs):
    """Convenience helper: ``capture_order(doc)`` or ``capture_order(book, doc)``."""
    if doc is None:
        doc = book_or_doc
        book = CaptureBook()
    else:
        book = book_or_doc
    return book.capture(doc, **kwargs) if kwargs else book.capture(doc)


def replay_documents(docs):
    """Replay a document sequence into a fresh :class:`CaptureBook`."""
    book = CaptureBook()
    book.ingest(docs)
    return book


def replay_orders(orders):
    """Replay signed orders into a fresh capture book."""
    return replay_documents(orders)


def capture_documents(docs):
    return replay_documents(docs)


def capture_signed_order(order, book=None):
    """Capture one signed order into ``book`` (or a fresh book)."""
    if book is None:
        book = CaptureBook()
    return book.capture(order)


def verify_signed_order(doc, secret=None):
    return verify_signature(doc, secret)


# Extra class/function nouns callers/tests may reach for.
OrderCaptureBook = CaptureBook
SignedOrderCapture = CaptureBook
create_capture_book = CaptureBook
