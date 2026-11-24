"""Staged escrow accounting and actual bank remittance after auction award.

Money model (all internal math in integer cents):

* **Only captured, non-reversed bank funds are cash.** Authorization holds
  live in :class:`~teablend.escrow_capture.BankLedger` but never enter the
  spendable escrow balance. The ledger itself is idempotent per bank
  transaction ID, so a capture cannot fund two batches.
* **Signed delivery earns release equity.** Each dual-signed, non-rejected
  kilogram earns ``award_price * kg / lot_kg`` of *gross* releasable
  entitlement. Rejected or unsigned tea earns nothing.
* **A disbursement cannot exceed cash.** Staged releases additionally keep
  at least the 300 yuan risk reserve inside the return window; no
  receivable/IOU can bypass the bank balance.
* **Released money is irreversible.** A late refund, chargeback, return or
  signature dispute first consumes still-frozen escrow cash; anything
  beyond that becomes recourse claims against the beneficiaries who
  actually received the funds (seller and platform fee account). Paid
  seller money never silently reappears in escrow.
* Every award, delivery, correction, bank posting, disbursement and claim
  is retained as an append-only versioned record.
"""

from .auction_award import award
from .escrow_capture import (
    AUTHORIZATION, CAPTURE, REFUND, CHARGEBACK,
    BankLedger, to_cents, to_yuan,
)

STAGED = 'staged'
FINAL = 'final'
RISK_RESERVE_YUAN = 300
DEFAULT_PLATFORM_PARTY = 'PLATFORM'


class EscrowError(ValueError):
    """Raised when an escrow operation would break a hard money rule."""


class EscrowAccount:
    """Event-sourced escrow ledger for one auction lot."""

    def __init__(self, lot, award_record, terms, ledger,
                 platform_party=DEFAULT_PLATFORM_PARTY, reserve_yuan=RISK_RESERVE_YUAN):
        if award_record is None or award_record['lot'] != lot['id']:
            raise EscrowError('escrow requires a valid award for the lot')
        self.lot = lot
        self.award_record = award_record
        self.terms = terms
        self.ledger = ledger
        self.platform_party = platform_party
        self.seller = award_record['seller']
        self.buyer = award_record['buyer']
        self.fee_pct = int(terms.get('platform_fee_pct', 0))
        self.reserve_cents = to_cents(reserve_yuan)
        self.award_cents = to_cents(award_record['award_amount'])
        if self.award_cents <= 0 or lot['kg'] <= 0:
            raise EscrowError('award amount and lot weight must be positive')

        self.deliveries = {}          # delivery_id -> record
        self.corrections = []         # append-only delivery rejections/disputes
        self.disbursements = []       # append-only actual bank remittances
        self.claims = []              # append-only recourse claims
        self.escrow_cash_cents = 0    # spendable captured cash currently frozen/held

        # Absorb any ledger postings supplied at construction (e.g. fixtures).
        for posting in ledger.postings:
            self._absorb(posting)

    # ------------------------------------------------------------------ #
    # Construction helpers
    # ------------------------------------------------------------------ #
    @classmethod
    def from_fixture(cls, data, bank_events=None, platform_party=DEFAULT_PLATFORM_PARTY):
        record = award(data['lot'], data['bids'])
        if record is None:
            raise EscrowError('no eligible winner for lot')
        events = bank_events if bank_events is not None else data.get('bank_events', [])
        ledger = BankLedger.from_fixture(data['lot'], record, events)
        account = cls(data['lot'], record, data['escrow_terms'], ledger,
                      platform_party=platform_party)
        for delivery in data.get('deliveries', []):
            account.record_signed_delivery(delivery)
        return account

    # ------------------------------------------------------------------ #
    # Bank side: captures, refunds, chargebacks (idempotent via ledger)
    # ------------------------------------------------------------------ #
    def book_bank_event(self, event):
        """Book a bank callback and move only real cash in/out of escrow.

        Returns ``{'posting': ..., 'status': 'booked'|'replayed'}``.
        Authorization/hold postings change no balance; duplicate callbacks
        are replayed through the ledger without touching cash twice.
        """
        ptype = event.get('type')
        # Idempotent replay must win over every other balance rule: a
        # duplicate callback of an already-booked reversal is not a new
        # reversal and therefore cannot trip the capture ceiling.
        existing = self.ledger.find(event.get('txn_id')) if event.get('txn_id') else None
        if existing is not None:
            posting = self.ledger.book(event)  # replays, or raises on body conflict
            return {'posting': posting, 'status': 'replayed'}
        if ptype in (REFUND, CHARGEBACK):
            # A reversal cannot exceed what was ever captured for the lot.
            projected = (self.ledger.reversed_cents() + to_cents(event['amount']))
            if projected > self.ledger.captured_cents():
                raise EscrowError(
                    'refund/chargeback cannot exceed total captured funds')
        posting = self.ledger.book(event)
        self._absorb(posting)
        return {'posting': posting, 'status': 'booked'}

    def _absorb(self, posting):
        ptype = posting['type']
        amount = posting['amount_cents']
        if ptype == AUTHORIZATION:
            return  # a hold is not cash
        if ptype == CAPTURE:
            self.escrow_cash_cents += amount
            return
        if ptype in (REFUND, CHARGEBACK):
            self._apply_bank_reversal(amount, posting)
            return
        raise EscrowError(f'cannot absorb posting type {ptype!r}')

    def _apply_bank_reversal(self, amount_cents, posting):
        """Frozen cash first; the remainder is recourse against beneficiaries."""
        from_cash = min(self.escrow_cash_cents, amount_cents)
        self.escrow_cash_cents -= from_cash
        remaining = amount_cents - from_cash
        if remaining:
            self._raise_recourse(remaining, f'late_{posting["type"]}',
                                 posting['txn_id'])

    # ------------------------------------------------------------------ #
    # Delivery side: dual signatures earn release equity
    # ------------------------------------------------------------------ #
    def record_signed_delivery(self, delivery):
        """Record a batch; only dual-signed batches can later earn equity."""
        did = delivery['id']
        if did in self.deliveries:
            raise EscrowError(f'delivery {did} already recorded')
        if not delivery.get('buyer_signature') or not delivery.get('seller_signature'):
            raise EscrowError(f'delivery {did} lacks dual signatures')
        if delivery['kg'] <= 0:
            raise EscrowError('delivery weight must be positive')
        record = {
            'version': len(self.deliveries) + 1,
            'id': did,
            'kg': delivery['kg'],
            'buyer_signature': True,
            'seller_signature': True,
            'at': delivery.get('at'),
        }
        self.deliveries[did] = record
        return record

    def reject_delivery(self, delivery_id, kg=None, reason='buyer_rejection', at=None):
        """Mark (part of) a signed delivery rejected/disputed.

        Rejected kilograms stop earning equity. Any equity already released
        for them turns into recourse claims against the actual beneficiaries;
        if it was never released, the money simply stays frozen and no claim
        is raised.
        """
        delivery = self.deliveries.get(delivery_id)
        if delivery is None:
            raise EscrowError(f'unknown delivery {delivery_id}')
        already = sum(c['rejected_kg'] for c in self.corrections
                      if c['delivery_id'] == delivery_id)
        reject_kg = delivery['kg'] if kg is None else kg
        if reject_kg <= 0 or already + reject_kg > delivery['kg']:
            raise EscrowError('rejected weight exceeds delivered weight')
        before_earned = self.earned_gross_cents()
        correction = {
            'id': f'CORR-{len(self.corrections) + 1}',
            'version': len(self.corrections) + 1,
            'delivery_id': delivery_id,
            'rejected_kg': reject_kg,
            'reason': reason,
            'at': at,
        }
        self.corrections.append(correction)
        shortfall = before_earned - self.earned_gross_cents()
        if shortfall > 0:
            self._raise_recourse(shortfall, reason, correction['id'])
        return correction

    def rejected_kg(self):
        return sum(c['rejected_kg'] for c in self.corrections)

    def signed_kg(self):
        return sum(d['kg'] for d in self.deliveries.values())

    def accepted_kg(self):
        return max(0, self.signed_kg() - self.rejected_kg())

    def earned_gross_cents(self):
        """Gross release equity earned by accepted dual-signed kilograms."""
        kg = min(self.accepted_kg(), self.lot['kg'])
        return self.award_cents * kg // self.lot['kg']

    def earned_gross_yuan(self):
        return to_yuan(self.earned_gross_cents())

    def released_gross_cents(self):
        return sum(d['gross_cents'] for d in self.disbursements)

    def seller_paid_cents(self):
        return sum(d['seller_cents'] for d in self.disbursements)

    def seller_paid_yuan(self):
        return to_yuan(self.seller_paid_cents())

    def platform_fee_cents(self):
        return sum(d['fee_cents'] for d in self.disbursements)

    # ------------------------------------------------------------------ #
    # Release planning and actual, irreversible disbursement
    # ------------------------------------------------------------------ #
    def release_plan(self, route=STAGED, in_return_window=True):
        """What may be remitted now under route ``staged`` or ``final``.

        The plan is a proposal; no money moves until :meth:`disburse`.
        """
        earned = self.earned_gross_cents()
        already = self.released_gross_cents()
        cash = self.escrow_cash_cents
        plan = {
            'route': route,
            'eligible': False,
            'reason': None,
            'earned_gross_cents': earned,
            'already_released_cents': already,
            'escrow_cash_cents': cash,
            'reserve_held_cents': 0,
            'gross_cents': 0,
            'fee_cents': 0,
            'seller_cents': 0,
        }
        if route == STAGED:
            if not self.terms.get('staged_release_allowed'):
                plan['reason'] = 'staged_release_disallowed'
                return self._with_yuan(plan)
            cap = earned - already
            if in_return_window:
                plan['reserve_held_cents'] = min(self.reserve_cents, cash)
                cap = min(cap, cash - plan['reserve_held_cents'])
            else:
                cap = min(cap, cash)
            plan['gross_cents'] = max(0, cap)
            plan['eligible'] = plan['gross_cents'] > 0
            if not plan['eligible']:
                plan['reason'] = 'no_earned_cash_available'
        elif route == FINAL:
            if not self.terms.get('final_release_allowed'):
                plan['reason'] = 'final_release_disallowed'
                return self._with_yuan(plan)
            complete = self.accepted_kg() == self.lot['kg'] and not self.corrections
            if not complete:
                plan['reason'] = 'delivery_or_dispute_open'
                return self._with_yuan(plan)
            if cash < self.award_cents - already:
                plan['reason'] = 'captures_not_complete'
                return self._with_yuan(plan)
            plan['gross_cents'] = max(0, min(self.award_cents - already, cash))
            plan['eligible'] = plan['gross_cents'] > 0
            if not plan['eligible']:
                plan['reason'] = 'nothing_left_to_release'
        else:
            raise EscrowError(f'unknown release route {route!r}')
        gross = plan['gross_cents']
        plan['fee_cents'] = gross * self.fee_pct // 100
        plan['seller_cents'] = gross - plan['fee_cents']
        return self._with_yuan(plan)

    def disburse(self, route=STAGED, amount_yuan=None, at=None, in_return_window=True):
        """Execute an actual bank remittance; immutable once booked.

        The remittance can never exceed earned equity, available captured
        cash (and reserve for staged releases), so an un-captured 12000 yuan
        receivable can never be paid out.
        """
        plan = self.release_plan(route, in_return_window)
        if not plan['eligible']:
            raise EscrowError(f'release not eligible: {plan["reason"]}')
        gross = plan['gross_cents'] if amount_yuan is None else to_cents(amount_yuan)
        if gross <= 0:
            raise EscrowError('disbursement amount must be positive')
        if gross > plan['gross_cents']:
            raise EscrowError(
                f'disbursement {to_yuan(gross)} exceeds releasable cap '
                f'{to_yuan(plan["gross_cents"])}')
        fee = gross * self.fee_pct // 100
        seller = gross - fee
        self.escrow_cash_cents -= gross
        record = {
            'id': f'DISB-{len(self.disbursements) + 1}',
            'version': len(self.disbursements) + 1,
            'route': route,
            'at': at,
            'gross_cents': gross,
            'fee_cents': fee,
            'seller_cents': seller,
            'seller': self.seller,
            'platform_party': self.platform_party,
            'escrow_cash_after_cents': self.escrow_cash_cents,
        }
        self.disbursements.append(record)
        return dict(record)

    # ------------------------------------------------------------------ #
    # Recourse: claims against the beneficiaries who actually got funds
    # ------------------------------------------------------------------ #
    def _raise_recourse(self, amount_cents, reason, source_event):
        """Split a shortfall pro rata over seller receipts and platform fees.

        Money already remitted through the bank cannot be pulled back into
        escrow locally; instead the true beneficiaries owe recourse debt.
        """
        seller_held = self.seller_paid_cents()
        platform_held = self.platform_fee_cents()
        held_total = seller_held + platform_held
        if held_total == 0:
            return []  # nothing was released: the cash stays frozen, no debt
        seller_share = min(seller_held, amount_cents * seller_held // held_total)
        platform_share = min(platform_held, amount_cents - seller_share)
        # Rounding remainder goes to the platform fee beneficiary.
        platform_share = min(platform_held,
                             platform_share + max(0, amount_cents
                                                  - seller_share - platform_share))
        raised = []
        for against, held, share in (
                (self.seller, seller_held, seller_share),
                (self.platform_party, platform_held, platform_share)):
            if share <= 0:
                continue
            claim = {
                'id': f'CLAIM-{len(self.claims) + 1}',
                'version': len(self.claims) + 1,
                'reason': reason,
                'source_event': source_event,
                'against': against,
                'beneficiary_role': 'seller' if against == self.seller else 'platform',
                'amount_cents': share,
                'amount_yuan': to_yuan(share),
            }
            self.claims.append(claim)
            raised.append(dict(claim))
        return raised

    # ------------------------------------------------------------------ #
    # Views / reconciliation
    # ------------------------------------------------------------------ #
    def open_claims_cents(self):
        return sum(c['amount_cents'] for c in self.claims)

    def status(self):
        earned = self.earned_gross_cents()
        return {
            'lot': self.lot['id'],
            'winning_bid': self.award_record['winning_bid'],
            'buyer': self.buyer,
            'seller': self.seller,
            'award_amount_cents': self.award_cents,
            'award_amount_yuan': to_yuan(self.award_cents),
            'authorized_cents': self.ledger.authorized_cents(),
            'authorized_yuan': to_yuan(self.ledger.authorized_cents()),
            'captured_cents': self.ledger.captured_cents(),
            'captured_yuan': to_yuan(self.ledger.captured_cents()),
            'refunded_cents': self.ledger.refunded_cents(),
            'chargeback_cents': self.ledger.chargeback_cents(),
            'escrow_cash_cents': self.escrow_cash_cents,
            'escrow_cash_yuan': to_yuan(self.escrow_cash_cents),
            'signed_kg': self.signed_kg(),
            'accepted_kg': self.accepted_kg(),
            'rejected_kg': self.rejected_kg(),
            'earned_gross_cents': earned,
            'earned_gross_yuan': to_yuan(earned),
            'released_gross_cents': self.released_gross_cents(),
            'released_gross_yuan': to_yuan(self.released_gross_cents()),
            'seller_paid_cents': self.seller_paid_cents(),
            'seller_paid_yuan': to_yuan(self.seller_paid_cents()),
            'platform_fee_cents': self.platform_fee_cents(),
            'platform_fee_yuan': to_yuan(self.platform_fee_cents()),
            'frozen_unearned_cents': max(0, self.award_cents - earned),
            'frozen_unearned_yuan': to_yuan(max(0, self.award_cents - earned)),
            'open_claims_cents': self.open_claims_cents(),
            'open_claims_yuan': to_yuan(self.open_claims_cents()),
            'deliveries': [dict(d) for d in self.deliveries.values()],
            'corrections': [dict(c) for c in self.corrections],
            'disbursements': [dict(d) for d in self.disbursements],
            'claims': [dict(c) for c in self.claims],
            'bank_postings': self.ledger.cash_postings(),
        }

    def history(self):
        """Immutable, versioned stream of every fact that drove the account."""
        return {
            'award': dict(self.award_record),
            'bank_postings': [dict(p) for p in self.ledger.postings],
            'deliveries': [dict(d) for d in self.deliveries.values()],
            'corrections': [dict(c) for c in self.corrections],
            'disbursements': [dict(d) for d in self.disbursements],
            'claims': [dict(c) for c in self.claims],
        }

    @staticmethod
    def _with_yuan(plan):
        for key in ('earned_gross_cents', 'already_released_cents',
                    'escrow_cash_cents', 'reserve_held_cents',
                    'gross_cents', 'fee_cents', 'seller_cents'):
            plan[key.replace('_cents', '_yuan')] = to_yuan(plan[key])
        return plan


def _ledger_from_payment(lot_id, awarded_buyer, payment):
    """Backward-compatible ledger synthesis for payment-dict style callers."""
    ledger = BankLedger(lot_id, awarded_buyer)
    if payment.get('authorized'):
        ledger.book({'txn_id': 'TXN-AUTH-1', 'type': AUTHORIZATION,
                     'lot': lot_id, 'buyer': awarded_buyer,
                     'amount': payment['authorized']})
    if payment.get('captured'):
        ledger.book({'txn_id': 'TXN-CAP-1', 'type': CAPTURE,
                     'lot': lot_id, 'buyer': awarded_buyer,
                     'amount': payment['captured']})
    if payment.get('refunded'):
        ledger.book({'txn_id': 'TXN-REF-1', 'type': REFUND,
                     'lot': lot_id, 'buyer': awarded_buyer,
                     'amount': payment['refunded']})
    return ledger


def releasable(lot, bids, payment, deliveries=None, terms=None, bank_events=None):
    """Maximum gross yuan releasable now on the staged route.

    Returns 0 when no bid clears reserve, nothing is dual-signed, or no
    captured cash remains after the risk reserve. Authorization amounts
    never count -- the legacy behavior that returned the 12000 yuan hold as
    cash is rejected on purpose.
    """
    record = award(lot, bids)
    if record is None:
        return 0
    if bank_events is not None:
        ledger = BankLedger.from_fixture(lot, record, bank_events)
    else:
        ledger = _ledger_from_payment(lot['id'], record['buyer'], payment)
    terms = terms or {'staged_release_allowed': True,
                      'final_release_allowed': True, 'platform_fee_pct': 5}
    account = EscrowAccount(lot, record, terms, ledger)
    for delivery in deliveries or []:
        account.record_signed_delivery(delivery)
    return account.release_plan(STAGED)['gross_yuan']
