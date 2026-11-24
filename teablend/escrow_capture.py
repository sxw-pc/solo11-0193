"""Idempotent bank-side capture ledger for one auction award.

Hard rule: an authorization or a card hold is *not* cash. Only captured
funds add to the escrow balance; refunds and chargebacks reduce it. Each
bank transaction ID is booked exactly once -- duplicate callbacks for the
same ID are replayed, never double-counted -- and only postings from the
awarded buyer are accepted, so a stranger's money cannot fund the escrow.

The ledger is append-only: every accepted posting grows an immutable,
versioned stream. It stores cents (integers) so reconciliation is exact
to the fen; yuan-facing helpers convert at the boundary.
"""

CENTS_PER_YUAN = 100
AUTHORIZATION = 'authorization'
CAPTURE = 'capture'
REFUND = 'refund'
CHARGEBACK = 'chargeback'
CASH_POSTINGS = (CAPTURE, REFUND, CHARGEBACK)
_ALL_TYPES = (AUTHORIZATION, CAPTURE, REFUND, CHARGEBACK)


def to_cents(yuan):
    """Convert a yuan amount (int/float/string) to whole cents."""
    return int(round(float(yuan) * CENTS_PER_YUAN))


def to_yuan(cents):
    """Convert whole cents back to a yuan amount rounded to two decimals."""
    return round(cents / CENTS_PER_YUAN, 2)


def canonical(posting):
    """Stable JSON text used to reject same-ID callbacks with changed body."""
    import json
    return json.dumps(posting, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'))


class BankLedger:
    """Append-only bank transaction stream for a single awarded lot."""

    def __init__(self, lot_id, awarded_buyer):
        self.lot_id = lot_id
        self.awarded_buyer = awarded_buyer
        self.postings = []          # accepted, order-preserved
        self._by_txn = {}           # txn_id -> posting dict

    @classmethod
    def from_fixture(cls, lot, award_record, bank_events):
        ledger = cls(lot['id'], award_record['buyer'])
        for event in bank_events:
            ledger.book(event)
        return ledger

    # -- ingestion -------------------------------------------------------
    def book(self, event):
        """Book one bank callback; replay the identical one, reject conflicts.

        Returns the stored posting. Raises ``ValueError`` on buyer mismatch,
        unknown posting type, non-positive amounts, lot mismatch, or a
        same-ID callback whose body differs from the first one.
        """
        ptype = event.get('type')
        txn_id = event.get('txn_id')
        if not txn_id:
            raise ValueError('bank posting requires txn_id')
        if ptype not in _ALL_TYPES:
            raise ValueError(f'unknown bank posting type: {ptype!r}')
        if event.get('lot') != self.lot_id:
            raise ValueError('bank posting does not name the awarded lot')
        if event.get('buyer') != self.awarded_buyer:
            raise ValueError('only the awarded buyer can fund this escrow')
        amount_cents = to_cents(event['amount'])
        if amount_cents <= 0:
            raise ValueError('bank posting amount must be positive')
        stored_body = canonical({k: v for k, v in event.items() if k != 'at'})
        existing = self._by_txn.get(txn_id)
        if existing is not None:
            if existing['canonical'] != stored_body:
                raise ValueError(f'bank txn {txn_id} replayed with changed body')
            return existing  # idempotent replay: booked exactly once
        posting = {
            'version': len(self.postings) + 1,
            'txn_id': txn_id,
            'type': ptype,
            'buyer': event['buyer'],
            'lot': event['lot'],
            'amount_cents': amount_cents,
            'at': event.get('at'),
            'canonical': stored_body,
        }
        self.postings.append(posting)
        self._by_txn[txn_id] = posting
        return posting

    def replay(self, events):
        """Book a batch, returning counts of new and replayed postings."""
        inserted = replayed = 0
        for event in events:
            before = len(self.postings)
            self.book(event)
            if len(self.postings) == before:
                replayed += 1
            else:
                inserted += 1
        return {'inserted': inserted, 'replayed': replayed}

    # -- balances --------------------------------------------------------
    def _sum(self, ptype):
        return sum(p['amount_cents'] for p in self.postings if p['type'] == ptype)

    def authorized_cents(self):
        """Authorized/held amount -- display only, never spendable cash."""
        return self._sum(AUTHORIZATION)

    def captured_cents(self):
        return self._sum(CAPTURE)

    def refunded_cents(self):
        return self._sum(REFUND)

    def chargeback_cents(self):
        return self._sum(CHARGEBACK)

    def net_cash_cents(self):
        """Captured minus refunds/chargebacks; the only usable bank balance."""
        return (self.captured_cents()
                - self.refunded_cents()
                - self.chargeback_cents())

    def reversed_cents(self):
        return self.refunded_cents() + self.chargeback_cents()

    def cash_postings(self):
        """Capture/refund/chargeback postings in booking order (no holds)."""
        return [dict(p) for p in self.postings if p['type'] in CASH_POSTINGS]

    def find(self, txn_id):
        p = self._by_txn.get(txn_id)
        return dict(p) if p else None
