"""Auction escrow: real bank money, staged/final release, recourse.

All balances are checked to the fen (cents). The seed scenario:
100 kg lot, winning bid 12000 yuan, 12000 authorized but only 4000
captured, first dual-signed batch of 30 kg.
"""
import copy
import json
from pathlib import Path

import pytest

from teablend.auction_award import winner, award
from teablend.escrow_capture import (
    BankLedger, to_cents, CAPTURE, AUTHORIZATION, REFUND, CHARGEBACK,
)
from teablend.escrow_remit import (
    EscrowAccount, EscrowError, releasable, STAGED, FINAL,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / 'fixtures' / 'auction-escrow.json').read_text())

FEE_PCT = 5
RESERVE = to_cents(300)


def fresh_account(data=FIXTURE):
    return EscrowAccount.from_fixture(copy.deepcopy(data))


def cents(yuan):
    return to_cents(yuan)


# --------------------------------------------------------------------------- #
# Seed: authorization is not cash; reserve above all bids means no winner
# --------------------------------------------------------------------------- #
def test_seed_authorization_is_not_cash_and_reserve_blocks_release():
    data = copy.deepcopy(FIXTURE)
    assert winner(data['lot'], data['bids'])['id'] == 'BID-A'

    # Only the 4000 capture is money; the 12000 hold buys nothing.
    account = fresh_account(data)
    assert account.ledger.authorized_cents() == cents(12000)
    assert account.ledger.captured_cents() == cents(4000)
    assert account.escrow_cash_cents == cents(4000)

    # 30/100 kg signed => 3600 gross entitlement; 400 left frozen,
    # which still covers the 300 return-window reserve.
    plan = account.release_plan(STAGED)
    assert plan['eligible'] is True
    assert plan['gross_yuan'] == 3600
    assert plan['reserve_held_yuan'] == 300
    assert account.earned_gross_cents() == cents(3600)

    # Legacy functional API: the old 12000 answer must never come back.
    assert releasable(data['lot'], data['bids'], data['payment'],
                      data['deliveries'], data['escrow_terms'],
                      data['bank_events']) == 3600

    # Reserve raised above every bid: no award, no release at all.
    no_reserve = {**data['lot'], 'reserve_price': 13000}
    assert award(no_reserve, data['bids']) is None
    assert releasable(no_reserve, data['bids'], data['payment'],
                      data['deliveries'], data['escrow_terms'],
                      data['bank_events']) == 0
    with pytest.raises(EscrowError):
        EscrowAccount.from_fixture({**data, 'lot': no_reserve})


def test_seed_overrelease_12000_receivable_is_blocked():
    account = fresh_account()
    # The un-captured 8000 cannot be paid by writing a bigger remittance.
    with pytest.raises(EscrowError):
        account.disburse(STAGED, amount_yuan=12000)
    with pytest.raises(EscrowError):
        account.disburse(STAGED, amount_yuan=4000)  # cap is 3600 (reserve 300)
    # Final settlement is impossible while 70 kg and 8000 yuan are outstanding.
    with pytest.raises(EscrowError):
        account.disburse(FINAL)
    # Nothing moved.
    assert account.escrow_cash_cents == cents(4000)
    assert account.disbursements == []
    assert account.seller_paid_cents() == 0


# --------------------------------------------------------------------------- #
# Route A: staged remittance for the signed 30 kg batch
# --------------------------------------------------------------------------- #
def test_route_a_staged_release_balances_to_the_fen():
    account = fresh_account()
    record = account.disburse(STAGED, amount_yuan=3600)

    # 3600 gross = 180 platform fee + 3420 seller net, all to the fen.
    assert record['gross_cents'] == cents(3600)
    assert record['fee_cents'] == cents(180)
    assert record['seller_cents'] == cents(3420)
    assert record['seller'] == 'COOP-1'
    assert record['route'] == STAGED

    # 400 remains frozen, still above the 300 risk reserve.
    assert account.escrow_cash_cents == cents(400)
    status = account.status()
    assert status['platform_fee_yuan'] == 180
    assert status['seller_paid_yuan'] == 3420
    assert status['released_gross_yuan'] == 3600
    assert status['frozen_unearned_yuan'] == 8400      # award 12000 - earned 3600
    assert status['open_claims_yuan'] == 0

    # Identity: cash + released gross + claims == net bank cash + equity gap
    assert (account.escrow_cash_cents
            + account.released_gross_cents()
            + account.open_claims_cents()
            == account.ledger.net_cash_cents())


def test_route_a_reserve_blocks_release_when_cash_runs_thin():
    account = fresh_account()
    account.disburse(STAGED, amount_yuan=3600)       # 400 left, reserve 300
    account.record_signed_delivery(
        {'id': 'DEL-2', 'kg': 70, 'buyer_signature': True,
         'seller_signature': True})                  # full equity earned now
    # No new capture: only 100 above the reserve can still move.
    plan = account.release_plan(STAGED)
    assert plan['gross_yuan'] == 100
    account.disburse(STAGED)
    assert account.escrow_cash_cents == cents(300)
    assert account.release_plan(STAGED)['eligible'] is False
    # Outside the return window the reserve is released too.
    assert account.release_plan(STAGED, in_return_window=False)['gross_yuan'] == 300


# --------------------------------------------------------------------------- #
# Route B: hold everything until final delivery and full capture
# --------------------------------------------------------------------------- #
def test_route_b_final_release_pays_nothing_until_complete():
    account = fresh_account()
    plan = account.release_plan(FINAL)
    assert plan['eligible'] is False
    assert plan['reason'] in ('delivery_or_dispute_open', 'captures_not_complete')
    # Current seller income is zero; all 4000 stays protected in escrow.
    assert account.seller_paid_cents() == 0
    assert account.escrow_cash_cents == cents(4000)

    # Complete the contract: remaining 70 kg signed, remaining 8000 captured.
    account.record_signed_delivery(
        {'id': 'DEL-2', 'kg': 70, 'buyer_signature': True,
         'seller_signature': True})
    account.book_bank_event(
        {'txn_id': 'BANK-CAP-2', 'type': CAPTURE, 'lot': 'TEA-LOT-1',
         'buyer': 'BUYER-A', 'amount': 8000, 'at': '2026-11-22T09:00:00'})
    final = account.disburse(FINAL)
    assert final['gross_cents'] == cents(12000)
    assert final['fee_cents'] == cents(600)
    assert final['seller_cents'] == cents(11400)
    assert account.escrow_cash_cents == 0
    assert account.open_claims_cents() == 0


# --------------------------------------------------------------------------- #
# Idempotent bank callbacks; stranger and conflicting callbacks rejected
# --------------------------------------------------------------------------- #
def test_duplicate_bank_callbacks_book_once_and_never_double_fund():
    account = fresh_account()
    cap = {'txn_id': 'BANK-CAP-1', 'type': CAPTURE, 'lot': 'TEA-LOT-1',
           'buyer': 'BUYER-A', 'amount': 4000, 'at': '2026-11-20T09:05:00'}
    first = account.book_bank_event(cap)
    second = account.book_bank_event(copy.deepcopy(cap))
    third = account.book_bank_event(copy.deepcopy(cap))
    assert first['status'] == 'replayed'   # already in the fixture ledger
    assert second['status'] == 'replayed'
    assert third['status'] == 'replayed'
    assert account.ledger.captured_cents() == cents(4000)
    assert account.escrow_cash_cents == cents(4000)
    assert len(account.ledger.postings) == 2   # one auth + one capture

    # Same ID with a different body is a conflict, not a replay.
    tampered = copy.deepcopy(cap)
    tampered['amount'] = 9000
    with pytest.raises(ValueError):
        account.book_bank_event(tampered)

    # A non-awarded buyer cannot inject money into this escrow.
    with pytest.raises(ValueError):
        account.book_bank_event(
            {'txn_id': 'BANK-CAP-X', 'type': CAPTURE, 'lot': 'TEA-LOT-1',
             'buyer': 'BUYER-B', 'amount': 4000})

    # Authorization replays still never become cash.
    auth = {'txn_id': 'BANK-AUTH-1', 'type': AUTHORIZATION, 'lot': 'TEA-LOT-1',
            'buyer': 'BUYER-A', 'amount': 12000, 'at': '2026-11-20T09:00:00'}
    assert account.book_bank_event(auth)['status'] == 'replayed'
    assert account.escrow_cash_cents == cents(4000)


def test_cross_batch_delivery_cannot_reuse_one_capture():
    account = fresh_account()
    account.disburse(STAGED, amount_yuan=3600)
    # A second 30 kg batch arrives with no second capture: the same 4000
    # capture cannot be spent twice, so the release is bounded by cash.
    account.record_signed_delivery(
        {'id': 'DEL-2', 'kg': 30, 'buyer_signature': True,
         'seller_signature': True})
    plan = account.release_plan(STAGED)
    assert plan['earned_gross_yuan'] == 7200
    assert plan['gross_yuan'] == 100       # 400 cash - 300 reserve
    account.disburse(STAGED)
    assert account.escrow_cash_cents == cents(300)
    assert account.released_gross_cents() == cents(3700)


# --------------------------------------------------------------------------- #
# Late chargeback: frozen cash first, then recourse against real beneficiaries
# --------------------------------------------------------------------------- #
def test_late_chargeback_consumes_frozen_cash_then_claims_beneficiaries():
    account = fresh_account()
    account.disburse(STAGED, amount_yuan=3600)   # seller 3420, platform 180; 400 frozen
    chargeback = {'txn_id': 'BANK-CB-1', 'type': CHARGEBACK, 'lot': 'TEA-LOT-1',
                  'buyer': 'BUYER-A', 'amount': 4000, 'at': '2026-11-25T08:00:00'}
    result = account.book_bank_event(chargeback)
    assert result['status'] == 'booked'

    # All frozen cash is consumed; the 3600 already remitted becomes claims.
    assert account.escrow_cash_cents == 0
    assert account.open_claims_cents() == cents(3600)
    by_party = {c['against']: c['amount_cents'] for c in account.claims}
    assert by_party == {'COOP-1': cents(3420), 'PLATFORM': cents(180)}
    # Paid seller money did NOT reappear in escrow.
    assert account.seller_paid_cents() == cents(3420)
    assert account.platform_fee_cents() == cents(180)
    # Bank identity holds: cash + released == net bank cash + claims.
    assert (account.escrow_cash_cents
            + account.released_gross_cents()
            == account.ledger.net_cash_cents() + account.open_claims_cents())

    # The same chargeback callback arriving again changes nothing.
    again = account.book_bank_event(copy.deepcopy(chargeback))
    assert again['status'] == 'replayed'
    assert account.open_claims_cents() == cents(3600)
    assert account.ledger.chargeback_cents() == cents(4000)

    # A reversal beyond total capture is refused.
    with pytest.raises(EscrowError):
        account.book_bank_event(
            {'txn_id': 'BANK-CB-2', 'type': CHARGEBACK, 'lot': 'TEA-LOT-1',
             'buyer': 'BUYER-A', 'amount': 100})


def test_late_refund_before_any_release_raises_no_claims():
    account = fresh_account()
    account.book_bank_event(
        {'txn_id': 'BANK-REF-1', 'type': REFUND, 'lot': 'TEA-LOT-1',
         'buyer': 'BUYER-A', 'amount': 2500, 'at': '2026-11-25T08:00:00'})
    assert account.escrow_cash_cents == cents(1500)
    assert account.claims == []
    # Earned equity (3600) now exceeds remaining cash minus reserve:
    # only 1200 can actually move.
    plan = account.release_plan(STAGED)
    assert plan['gross_yuan'] == 1200


# --------------------------------------------------------------------------- #
# Partial return: outcome follows who actually received funds
# --------------------------------------------------------------------------- #
def test_partial_return_after_staged_release_creates_pro_rata_claims():
    account = fresh_account()
    account.disburse(STAGED, amount_yuan=3600)   # 400 frozen
    # 10 of the 30 delivered kg are rejected => 10% of award = 1200 equity lost.
    correction = account.reject_delivery('DEL-1', kg=10, reason='partial_return')
    assert correction['rejected_kg'] == 10
    assert account.accepted_kg() == 20
    assert account.earned_gross_cents() == cents(2400)

    # Released 3600 vs earned 2400 => 1200 over-released => recourse debt.
    assert account.open_claims_cents() == cents(1200)
    by_party = {c['against']: c['amount_yuan'] for c in account.claims}
    assert by_party == {'COOP-1': 1140.0, 'PLATFORM': 60.0}
    # Frozen cash is untouched; no money is invented back into escrow.
    assert account.escrow_cash_cents == cents(400)
    assert account.seller_paid_yuan() == 3420
    # Further release is now bounded by the reduced equity.
    assert account.release_plan(STAGED)['eligible'] is False


def test_same_partial_return_under_route_b_is_a_freeze_only():
    account = fresh_account()
    # Route B: nothing released yet.
    account.reject_delivery('DEL-1', kg=10, reason='partial_return')
    assert account.escrow_cash_cents == cents(4000)
    assert account.claims == []                          # no beneficiary paid
    assert account.earned_gross_cents() == cents(2400)
    # Final route stays blocked while the correction is open.
    assert account.release_plan(FINAL)['eligible'] is False
    # Staged route can still release only what the surviving equity supports.
    plan = account.release_plan(STAGED)
    assert plan['gross_yuan'] == 2400


# --------------------------------------------------------------------------- #
# Signatures, versions and immutable correction history
# --------------------------------------------------------------------------- #
def test_unsigned_delivery_earns_nothing_and_history_is_versioned():
    data = copy.deepcopy(FIXTURE)
    record = award(data['lot'], data['bids'])
    ledger = BankLedger.from_fixture(data['lot'], record, data['bank_events'])
    account = EscrowAccount(data['lot'], record, data['escrow_terms'], ledger)
    with pytest.raises(EscrowError):
        account.record_signed_delivery(
            {'id': 'DEL-BAD', 'kg': 30, 'buyer_signature': False,
             'seller_signature': True})
    assert account.signed_kg() == 0
    assert account.earned_gross_cents() == 0
    assert account.release_plan(STAGED)['eligible'] is False


def test_all_corrections_keep_original_versions_and_replay_identically():
    def build_and_dispute():
        account = fresh_account()
        account.disburse(STAGED, amount_yuan=3600)
        account.reject_delivery('DEL-1', kg=10, reason='signature_dispute')
        account.book_bank_event(
            {'txn_id': 'BANK-CB-1', 'type': CHARGEBACK, 'lot': 'TEA-LOT-1',
             'buyer': 'BUYER-A', 'amount': 1000, 'at': '2026-11-25T08:00:00'})
        return account

    first = build_and_dispute()
    second = build_and_dispute()
    # Deterministic replay from the same inputs.
    assert first.status() == second.status()
    history = first.history()
    # Original award, signed delivery and bank postings survive corrections.
    assert history['award']['winning_bid'] == 'BID-A'
    assert [d['id'] for d in history['deliveries']] == ['DEL-1']
    assert [p['txn_id'] for p in history['bank_postings']] == \
        ['BANK-AUTH-1', 'BANK-CAP-1', 'BANK-CB-1']
    versions = [c['version'] for c in history['corrections']]
    assert versions == [1]
    assert [d['version'] for d in history['disbursements']] == [1]
    # The rejected delivery record itself is retained, not overwritten.
    assert first.deliveries['DEL-1']['kg'] == 30
    assert first.corrections[0]['reason'] == 'signature_dispute'


def test_captured_fee_split_balances_for_odd_amounts():
    # Non-round award * fraction produces fen-level equity; the fee is
    # floored to whole fen and the seller takes the exact remainder.
    data = {
        'lot': {'id': 'TEA-LOT-X', 'kg': 100, 'reserve_price': 10000,
                'seller': 'COOP-1'},
        'bids': [{'id': 'BID-X', 'lot': 'TEA-LOT-X', 'buyer': 'BUYER-A',
                  'amount': 12001, 'submitted_minute': 540}],
        'payment': {'buyer': 'BUYER-A', 'authorized': 12001, 'captured': 5000,
                    'refunded': 0},
        'bank_events': [
            {'txn_id': 'AUTH-X', 'type': AUTHORIZATION, 'lot': 'TEA-LOT-X',
             'buyer': 'BUYER-A', 'amount': 12001},
            {'txn_id': 'CAP-X', 'type': CAPTURE, 'lot': 'TEA-LOT-X',
             'buyer': 'BUYER-A', 'amount': 5000},
        ],
        'deliveries': [
            {'id': 'DEL-X', 'kg': 33, 'buyer_signature': True,
             'seller_signature': True}],
        'escrow_terms': {'staged_release_allowed': True,
                         'final_release_allowed': True, 'platform_fee_pct': 5},
    }
    account = EscrowAccount.from_fixture(data)
    # 12001 yuan * 33/100 = 3960.33 gross equity.
    assert account.earned_gross_cents() == to_cents(3960.33)
    plan = account.release_plan(STAGED)
    assert plan['gross_yuan'] == 3960.33
    account.disburse(STAGED)
    # fee = floor(396033 * 5 / 100) = 19801 fen; seller gets the rest.
    assert account.platform_fee_cents() == to_cents(198.01)
    assert account.seller_paid_cents() == to_cents(3762.32)
    assert (account.seller_paid_cents() + account.platform_fee_cents()
            == account.released_gross_cents())
    # Cash actually left escrow; no rounding fen was invented or lost.
    assert account.escrow_cash_cents == to_cents(5000) - to_cents(3960.33)
