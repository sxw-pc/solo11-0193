"""Seed false-close, two legal closures, cold-chain breaks, late revisions
and recovery of a compensated box.

Run from the repository root:

    python -B -m pytest -q -p no:cacheprovider
"""

import copy
import json
from pathlib import Path

import pytest

from teablend.delivery_capture import (
    CaptureError,
    ClaimSettledLock,
    ConfirmationRequired,
    DeliveryBlocked,
    DoublePresentment,
    EvidenceConflict,
    ShipmentCapture,
)
from teablend.delivery_close import decide
from teablend.shipment_scan import (
    arrived_parcels,
    continuous_series,
    issue_codes,
    temperature_audit,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'fixtures' / 'leaf-shipment.json'

CUSTOMER_SIGN = 'FACTORY-B-DUAL-SIGN'
CARRIER_SIGN = 'CARRIER-X-DUAL-SIGN'


@pytest.fixture()
def seed():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


# ---------------------------------------------------------------------------
# evidence builders shared by routes A and B (same initial two boxes)
# ---------------------------------------------------------------------------

def box1_outbound():
    return {
        'id': 'HO-B1-OUT', 'parcel': 'BOX-1',
        'at': '2026-11-25T06:10:00', 'node': 'TEA-COLD-OUT',
        'kind': 'outbound', 'from_party': 'TEA-FACTORY',
        'to_party': 'COURIER-A',
        'from_sign': 'TEA-OUT-SIGN', 'to_sign': 'COURIER-A-SIGN',
    }


def box1_readings():
    return continuous_series(
        'BOX-1', '2026-11-25T06:10:00', '2026-11-25T10:30:00',
        step_minutes=25, celsius=7.0, logger_id='LOGGER-B1')


def add_box1_proven(cap):
    cap.add_handover(box1_outbound())
    rows = box1_readings()
    cap.add_temperature_readings(rows)
    return rows


def box2_outbound():
    return {
        'id': 'HO-B2-OUT', 'parcel': 'BOX-2',
        'at': '2026-11-25T06:20:00', 'node': 'TEA-COLD-OUT',
        'kind': 'outbound', 'from_party': 'TEA-FACTORY',
        'to_party': 'CARRIER-X',
        'from_sign': 'TEA-OUT-SIGN-2', 'to_sign': 'CARRIER-X-OUT-SIGN',
    }


def box2_hub_inbound():
    return {
        'id': 'HO-B2-HUB-IN', 'parcel': 'BOX-2',
        'at': '2026-11-25T09:00:00', 'node': 'HUB-2',
        'kind': 'carrier_transfer', 'from_party': 'CARRIER-X',
        'to_party': 'HUB-2',
        'from_sign': 'CARRIER-X-LH-SIGN', 'to_sign': 'HUB-2-IN-SIGN',
    }


def box2_hub_recovery():
    return {
        'id': 'HO-B2-HUB-OUT', 'parcel': 'BOX-2',
        'at': '2026-11-25T11:30:00', 'node': 'HUB-2',
        'kind': 'carrier_transfer', 'from_party': 'HUB-2',
        'to_party': 'COURIER-B',
        'from_sign': 'HUB-2-OUT-SIGN', 'to_sign': 'COURIER-B-SIGN',
    }


def box2_inspection(result='present_sound', at='2026-11-25T11:00:00'):
    return {
        'id': 'INSP-B2-HUB', 'parcel': 'BOX-2', 'at': at,
        'location': 'HUB-2', 'method': 'physical_recheck',
        'physical_verified': True, 'result': result,
        'inspected_by': 'HUB-2-QC', 'seal_intact': True,
    }


def build_route_a(seed):
    """Recover BOX-2 at HUB-2, complete both chains, dual-sign both boxes."""
    cap = ShipmentCapture(copy.deepcopy(seed))
    cap.add_handover(box1_outbound())
    cap.add_handover(box2_outbound())
    cap.add_handover(box2_hub_inbound())

    # Physical re-inspection is what unlocks choosing route A.
    cap.inspect_at_hub(box2_inspection())
    assessment = cap.assess_recovery(
        'BOX-2', at='2026-11-25T11:05:00',
        required_transport_minutes=240)
    assert assessment['route_a_available'] is True

    cap.add_handover(box2_hub_recovery())
    cap.add_event({
        'id': 'SCAN-B2-GATE', 'parcel': 'BOX-2', 'stage': 'arrived',
        'at': '2026-11-25T14:30:00', 'holder': 'COURIER-B',
        'location': 'CUSTOMER_GATE'})

    cap.add_temperature_readings(continuous_series(
        'BOX-1', '2026-11-25T06:10:00', '2026-11-25T15:00:00',
        step_minutes=30, celsius=7.0, logger_id='LOGGER-B1'))
    cap.add_temperature_readings(continuous_series(
        'BOX-2', '2026-11-25T06:20:00', '2026-11-25T15:00:00',
        step_minutes=30, celsius=6.5, logger_id='LOGGER-B2'))

    cap.accept_full(
        at='2026-11-25T15:00:00',
        customer_sign=CUSTOMER_SIGN, carrier_sign=CARRIER_SIGN,
        carrier_party={'BOX-1': 'COURIER-A', 'BOX-2': 'COURIER-B'})
    return cap


def build_route_b(seed):
    """Partial receipt for qualified BOX-1 + paid non-delivery claim on B2."""
    cap = ShipmentCapture(copy.deepcopy(seed))
    add_box1_proven(cap)
    cap.accept_partial(
        ['BOX-1'], at='2026-11-25T10:30:00',
        customer_sign=CUSTOMER_SIGN, carrier_sign=CARRIER_SIGN,
        consent_ref='WRITTEN-PARTIAL-CONSENT-20261125-01',
        carrier_party='COURIER-A')
    cap.settle_non_delivery(
        'BOX-2', amount_cny=800, at='2026-11-25T11:20:00',
        payment_ref='PAY-CLM-BOX2-0001')
    return cap


# ---------------------------------------------------------------------------
# 1. seed: one gate arrival scan must not close the two-box shipment
# ---------------------------------------------------------------------------

def test_seed_gate_scan_is_announcement_not_delivery(seed):
    # Preserved seed fact: BOX-1 is the only parcel with an arrived scan.
    assert arrived_parcels(seed['events']) == {'BOX-1'}

    d = decide(seed['shipment'], seed['events'],
               temperature=seed['temperature'], nodes=seed['nodes'])

    # The historical output keys stay stable ...
    assert d['shipment'] == 'SHIP-1'
    assert d['arrived_parcels'] == ['BOX-1']
    # ... but the false whole-ticket closure is gone.
    assert d['status'] == 'awaiting_receipt'
    assert d['status'] != 'delivered'
    assert d['received_count'] == 0
    assert d['usable_boxes'] == 0
    assert d['parcels_total'] == 2
    assert d['closed'] is False

    box1 = d['parcels_detail']['BOX-1']
    assert box1['gate_announced_at'] == '2026-11-25T10:00:00'
    assert box1['received_at'] is None
    assert 'unilateral_arrival_scan_not_receipt' in box1['custody_issue_codes']

    box2 = d['parcels_detail']['BOX-2']
    assert box2['disposition'] == 'held_at_hub'
    assert (box2['last_scan'] or {}).get('location') == 'HUB-2'
    assert box2['custody_state'] == 'unproven'

    # Min/max rows alone leave both cold chains pending verification.
    assert box1['temperature_state'] == 'pending'
    assert box2['temperature_state'] == 'pending'
    assert 'summary_minmax_cannot_prove_continuity' in box1['holds']

    # Removing the arrival scan leaves a plain in-transit shipment.
    transfer_only = [e for e in seed['events'] if e['stage'] != 'arrived']
    assert decide(seed['shipment'], transfer_only)['status'] == 'in_transit'


def test_counting_arrived_scans_for_every_box_still_is_not_delivery(seed):
    # The explicit anti-pattern: arrived count == parcels must not close
    # anything while dual signatures / custody / cold chain are missing.
    events = seed['events'] + [{
        'id': 'SCAN-B2-GATE-RAW', 'parcel': 'BOX-2', 'stage': 'arrived',
        'at': '2026-11-25T13:00:00', 'holder': 'COURIER-B',
        'location': 'CUSTOMER_GATE'}]
    assert arrived_parcels(events) == {'BOX-1', 'BOX-2'}
    d = decide(seed['shipment'], events, temperature=seed['temperature'])
    assert d['arrived_parcels'] == ['BOX-1', 'BOX-2']
    assert d['status'] != 'delivered'
    assert d['received_count'] == 0
    assert all(not p['signed'] for p in d['parcels_detail'].values())


def test_seed_capture_opens_with_version_and_not_delivered(seed):
    cap = ShipmentCapture(copy.deepcopy(seed))
    d = cap.close()
    assert d['status'] == 'awaiting_receipt'
    assert len(cap.versions) == 1
    assert cap.history()[0]['reason'] == 'capture_opened'


# ---------------------------------------------------------------------------
# 2a. route A: recover BOX-2 at HUB-2 and deliver 2/2 sound
# ---------------------------------------------------------------------------

def test_route_a_full_recovery_is_two_of_two_delivered(seed):
    cap = build_route_a(seed)
    d = cap.close()

    assert d['status'] == 'delivered'
    assert d['closed'] is True
    assert d['received_sound_parcels'] == ['BOX-1', 'BOX-2']
    assert d['sound_count'] == 2
    assert d['usable_boxes'] == 2
    assert d['claim_parcels'] == []
    assert d['claim_obligation_cny'] == 0
    assert d['carrier_debt_cny'] == 0
    assert d['paid_claims_cny'] == 0

    for parcel in ('BOX-1', 'BOX-2'):
        detail = d['parcels_detail'][parcel]
        assert detail['disposition'] == 'customer_received_sound'
        assert detail['responsible_party'] == 'customer'
        assert detail['temperature_state'] == 'qualified'
        assert detail['custody_chain_complete'] is True
        assert detail['ownership']['owner'] == 'FACTORY-B'

    assert cap.notices[-1]['kind'] == 'full_receipt'
    assert cap.notices[-1]['received_sound_parcels'] == ['BOX-1', 'BOX-2']


def test_transfer_scan_alone_does_not_make_box2_recoverable(seed):
    cap = ShipmentCapture(copy.deepcopy(seed))  # already has SCAN-2 transfer
    assessment = cap.assess_recovery(
        'BOX-2', at='2026-11-25T10:30:00',
        required_transport_minutes=240)
    assert assessment['transfer_scan_seen'] is True
    assert assessment['route_a_available'] is False
    assert 'missing_physical_reinspection' in assessment['reasons']
    assert cap.close()['parcels_detail']['BOX-2']['disposition'] == 'held_at_hub'


def test_route_a_choice_requires_physical_check_and_time_window(seed):
    cap = ShipmentCapture(copy.deepcopy(seed))
    cap.inspect_at_hub(box2_inspection())

    feasible = cap.assess_recovery(
        'BOX-2', at='2026-11-25T11:05:00',
        required_transport_minutes=240)
    assert feasible['route_a_available'] is True
    assert feasible['eta'] == '2026-11-25T15:05'

    too_late = cap.assess_recovery(
        'BOX-2', at='2026-11-25T11:05:00',
        required_transport_minutes=600)
    assert too_late['route_a_available'] is False
    assert 'remaining_transport_time_insufficient' in too_late['reasons']

    damaged = ShipmentCapture(copy.deepcopy(seed))
    damaged.inspect_at_hub(box2_inspection(result='present_damaged'))
    verdict = damaged.assess_recovery(
        'BOX-2', at='2026-11-25T11:05:00',
        required_transport_minutes=120)
    assert verdict['route_a_available'] is False
    assert 'inspection_present_damaged' in verdict['reasons']


def test_accept_full_refuses_when_evidence_incomplete(seed):
    cap = ShipmentCapture(copy.deepcopy(seed))
    with pytest.raises(DeliveryBlocked):
        cap.accept_full(at='2026-11-25T15:00:00',
                        customer_sign=CUSTOMER_SIGN,
                        carrier_sign=CARRIER_SIGN)


# ---------------------------------------------------------------------------
# 2b. route B: partial receipt BOX-1 + paid claim on BOX-2
# ---------------------------------------------------------------------------

def test_route_b_partial_receipt_and_unpaid_then_paid_claim(seed):
    cap = ShipmentCapture(copy.deepcopy(seed))

    # No cold/custody proof yet: even BOX-1 cannot be accepted as sound.
    with pytest.raises(DeliveryBlocked):
        cap.accept_partial(
            ['BOX-1'], at='2026-11-25T10:30:00',
            customer_sign=CUSTOMER_SIGN, carrier_sign=CARRIER_SIGN,
            consent_ref='CONSENT-1', carrier_party='COURIER-A')

    add_box1_proven(cap)

    # BOX-2 cannot be smuggled into the partial receipt.
    with pytest.raises(DeliveryBlocked):
        cap.accept_partial(
            ['BOX-1', 'BOX-2'], at='2026-11-25T10:30:00',
            customer_sign=CUSTOMER_SIGN, carrier_sign=CARRIER_SIGN,
            consent_ref='CONSENT-1',
            carrier_party={'BOX-1': 'COURIER-A', 'BOX-2': 'COURIER-B'})

    cap.accept_partial(
        ['BOX-1'], at='2026-11-25T10:30:00',
        customer_sign=CUSTOMER_SIGN, carrier_sign=CARRIER_SIGN,
        consent_ref='WRITTEN-PARTIAL-CONSENT-20261125-01',
        carrier_party='COURIER-A')
    d = cap.close()
    assert d['status'] == 'partially_received'
    assert d['usable_boxes'] == 1
    assert d['partial_consent']['ref'] == \
        'WRITTEN-PARTIAL-CONSENT-20261125-01'

    # Awarded but unpaid claim is real carrier debt.
    cap.open_claim('BOX-2', amount_cny=800, at='2026-11-25T10:45:00')
    cap.award_claim('BOX-2', at='2026-11-25T11:00:00')
    d = cap.close()
    assert d['status'] == 'partially_received_with_claim'
    assert d['carrier_debt_cny'] == 800
    assert d['closed'] is False
    # Award itself locks BOX-2 disposition against original-order sign-off.
    assert d['parcels_detail']['BOX-2']['ownership']['locked'] is True
    with pytest.raises(ClaimSettledLock):
        cap.sign_receipt(
            ['BOX-2'], at='2026-11-25T11:05:00',
            customer_sign=CUSTOMER_SIGN, carrier_sign=CARRIER_SIGN,
            carrier_party='COURIER-B')

    cap.pay_claim('BOX-2', at='2026-11-25T11:20:00',
                  payment_ref='PAY-CLM-BOX2-0001')
    d = cap.close()

    # 1/2 received + 1/2 claim -- never "delivered".
    assert d['status'] == 'partially_received_with_claim'
    assert d['status'] != 'delivered'
    assert d['closed'] is True
    assert d['settled'] is True
    assert d['received_sound_parcels'] == ['BOX-1']
    assert d['claim_parcels'] == ['BOX-2']
    assert d['usable_boxes'] == 1
    assert d['carrier_debt_cny'] == 0
    assert d['paid_claims_cny'] == 800
    assert d['claim_obligation_cny'] == 800

    box2 = d['parcels_detail']['BOX-2']
    assert box2['disposition'] == 'claim_paid_undelivered'
    assert box2['ownership']['owner'] == 'CARRIER-X'
    assert box2['ownership']['locked'] is True
    assert box2['ownership']['original_order_redelivery_barred'] is True

    kinds = [n['kind'] for n in cap.notices]
    assert 'partial_receipt' in kinds
    assert 'claim_paid' in kinds


def test_routes_a_and_b_differ_in_boxes_debt_and_box2_ownership(seed):
    a = build_route_a(seed).close()
    b = build_route_b(seed).close()

    assert a['usable_boxes'] == 2 and b['usable_boxes'] == 1
    assert a['claim_obligation_cny'] == 0
    assert b['claim_obligation_cny'] == 800
    assert a['parcels_detail']['BOX-2']['ownership']['owner'] == 'FACTORY-B'
    assert b['parcels_detail']['BOX-2']['ownership']['owner'] == 'CARRIER-X'
    assert a['status'] == 'delivered'
    assert b['status'] == 'partially_received_with_claim'


# ---------------------------------------------------------------------------
# 3. temperature breaks: summary-only pending, gaps pending, abuse blocked
# ---------------------------------------------------------------------------

def test_minmax_summary_cannot_prove_continuity():
    verdict = temperature_audit(
        'BOX-X',
        summary={'min_celsius': 5, 'max_celsius': 11},
        phases={'outbound': '2026-11-25T06:00:00',
                'receipt': '2026-11-25T12:00:00'})
    assert verdict['state'] == 'pending'
    assert verdict['summary_only'] is True
    assert 'summary_minmax_cannot_prove_continuity' in issue_codes(verdict)


def test_missing_temperature_records_are_pending_not_qualified():
    verdict = temperature_audit('BOX-NONE')
    assert verdict['state'] == 'pending'
    assert 'no_temperature_records' in issue_codes(verdict)


def test_in_band_boundary_and_continuous_series_qualify():
    phases = {'outbound': '2026-11-25T06:00:00',
              'gate': '2026-11-25T09:00:00',
              'receipt': '2026-11-25T10:00:00'}
    cold4 = continuous_series(
        'BOX', '2026-11-25T06:00:00', '2026-11-25T10:00:00',
        step_minutes=20, celsius=4.0)
    cold12 = continuous_series(
        'BOX', '2026-11-25T06:00:00', '2026-11-25T10:00:00',
        step_minutes=20, celsius=12.0)
    assert temperature_audit('BOX', cold4, phases=phases)['state'] == \
        'qualified'
    assert temperature_audit('BOX', cold12, phases=phases)['state'] == \
        'qualified'


def test_monitoring_gap_makes_chain_pending_and_phase_uncovered():
    rows = [
        {'at': '2026-11-25T06:00:00', 'celsius': 7},
        {'at': '2026-11-25T06:30:00', 'celsius': 7},
        {'at': '2026-11-25T09:00:00', 'celsius': 7},
        {'at': '2026-11-25T09:30:00', 'celsius': 7},
    ]
    verdict = temperature_audit('BOX-GAP', rows, phases={
        'outbound': '2026-11-25T06:00:00',
        'transit': '2026-11-25T07:30:00',
        'gate': '2026-11-25T09:00:00'})
    assert verdict['state'] == 'pending'
    codes = issue_codes(verdict)
    assert 'continuity_gap' in codes
    assert 'transit' in verdict['uncovered_phases']


def test_out_of_range_reading_is_abuse_not_pending():
    rows = continuous_series(
        'BOX-HOT', '2026-11-25T06:00:00', '2026-11-25T09:00:00',
        step_minutes=20, celsius=7.0)
    rows[4]['celsius'] = 15.0
    verdict = temperature_audit('BOX-HOT', rows)
    assert verdict['state'] == 'abuse'
    assert verdict['out_of_range'][0]['celsius'] == 15.0


def test_gap_or_abuse_blocks_sound_partial_receipt(seed):
    cap = ShipmentCapture(copy.deepcopy(seed))
    cap.add_handover(box1_outbound())
    rows = box1_readings()
    cap.add_temperature_readings(rows)
    cap.accept_partial(
        ['BOX-1'], at='2026-11-25T10:30:00',
        customer_sign=CUSTOMER_SIGN, carrier_sign=CARRIER_SIGN,
        consent_ref='CONSENT-OK', carrier_party='COURIER-A')

    # A later correction to an abusive value blocks any subsequent sound use
    # (exercised through sound_check directly here; versioning below).
    target = rows[3]['id']
    cap.correct_temperature({
        'corrects': target, 'parcel': 'BOX-1', 'celsius': 14.5,
        'at': '2026-11-25T16:40:00', 'by': 'COLD-AUDIT'})
    ok, reasons = cap.sound_check('BOX-1')
    assert ok is False
    assert any('temperature_not_qualified' in r for r in reasons)


# ---------------------------------------------------------------------------
# 4. late scans / temperature corrections / rejection keep old versions
# ---------------------------------------------------------------------------

def test_late_scan_correction_and_rejection_version_without_backdate(seed):
    cap = build_route_b(seed)
    assert [v['version'] for v in cap.versions] == [1, 2, 3]
    signed_at = cap.close()['parcels_detail']['BOX-1']['received_at']
    assert signed_at == '2026-11-25T10:30:00'

    # v4: a late中转 scan for BOX-2 (business time 08:10, filed at 16:00).
    late = cap.append_late_event({
        'id': 'SCAN-B2-LATE', 'parcel': 'BOX-2', 'stage': 'transfer',
        'at': '2026-11-25T08:10:00', 'holder': 'COURIER-B',
        'location': 'HUB-2'}, recorded_at='2026-11-25T16:00:00')
    assert late['version'] == 4
    assert late['notice']['kind'] == 'evidence_revision'

    # Old v3 decision is frozen as the partial + paid-claim closure.
    old = cap.version(3)['decision']
    assert old['status'] == 'partially_received_with_claim'
    assert old['paid_claims_cny'] == 800

    latest = cap.close()
    # Late scan must not rewrite the actual signature timestamp.
    assert latest['parcels_detail']['BOX-1']['received_at'] == \
        '2026-11-25T10:30:00'
    # Old arrival announcement SCAN-1 is still present.
    from teablend.shipment_scan import arrival_announcements
    assert [s['id'] for s in arrival_announcements(cap.events)] == ['SCAN-1']
    assert any(e['id'] == 'SCAN-B2-LATE' and e['late'] for e in cap.events)

    # v5: temperature correction turns the qualified BOX-1 chain abusive.
    target_reading = next(
        r['id'] for r in cap.temperature_readings
        if r['parcel'] == 'BOX-1')
    original_value = next(
        r['celsius'] for r in cap.temperature_readings
        if r['id'] == target_reading)
    cap.correct_temperature({
        'corrects': target_reading, 'parcel': 'BOX-1',
        'celsius': 14.0, 'at': '2026-11-25T16:40:00',
        'by': 'COLD-AUDIT'})

    assert cap.version(4)['decision']['parcels_detail']['BOX-1'][
        'temperature_state'] == 'qualified'
    assert cap.close()['parcels_detail']['BOX-1'][
        'temperature_state'] == 'abuse'
    # Original reading and correction trail are both preserved.
    assert next(r for r in cap.temperature_readings
                if r['id'] == target_reading)['celsius'] == original_value
    assert cap.temperature_corrections[0]['corrects'] == target_reading

    # v6: customer formal rejection after the correction.
    cap.reject('BOX-1', at='2026-11-25T17:10:00',
               reason='temperature_excursion_confirmed')
    d = cap.close()
    box1 = d['parcels_detail']['BOX-1']
    assert box1['disposition'] == 'rejected_by_customer'
    assert box1['responsible_party'] == 'carrier'

    # Actual signature evidence/time survives the rejection ...
    assert cap.receipts[0]['at'] == '2026-11-25T10:30:00'
    assert box1['received_at'] == '2026-11-25T10:30:00'
    # ... and so does the paid claim.
    assert d['paid_claims_cny'] == 800
    assert d['carrier_debt_cny'] == 0

    # Every stage kept its own notice; newest is the rejection notice.
    kinds = [n['kind'] for n in cap.notices]
    assert kinds == [
        'partial_receipt', 'claim_paid', 'evidence_revision',
        'temperature_revision', 'rejection']
    assert cap.notices[-1]['detail']['parcel'] == 'BOX-1'

    # Full ordered responsibility history is append-only.
    assert [row['version'] for row in cap.history()] == [1, 2, 3, 4, 5, 6]


def test_evidence_idempotent_replay_and_conflict(seed):
    cap = ShipmentCapture(copy.deepcopy(seed))
    event = {'id': 'EVT-DUP', 'parcel': 'BOX-1', 'stage': 'transfer',
             'at': '2026-11-25T08:00:00', 'location': 'HUB-1'}
    cap.add_event(event)
    cap.add_event(copy.deepcopy(event))  # same id+content: no-op replay
    assert [e['id'] for e in cap.events].count('EVT-DUP') == 1
    with pytest.raises(EvidenceConflict):
        cap.add_event(dict(event, location='HUB-9'))


# ---------------------------------------------------------------------------
# 5. compensated BOX-2 recovered: reconfirm ownership, no double delivery
# ---------------------------------------------------------------------------

def test_recovered_paid_box_needs_triparty_reconfirmation(seed):
    cap = build_route_b(seed)

    # Paid claim locks BOX-2 against any original-order receipt action.
    with pytest.raises(ClaimSettledLock):
        cap.sign_receipt(
            ['BOX-2'], at='2026-11-25T16:00:00',
            customer_sign=CUSTOMER_SIGN, carrier_sign=CARRIER_SIGN,
            carrier_party='COURIER-B')
    with pytest.raises(ClaimSettledLock):
        cap.deliver_recovered_against_original('BOX-2')

    cap.recover_found_parcel(
        'BOX-2', at='2026-11-26T08:30:00', location='HUB-2',
        condition='seal_intact_leaf_recheck_pending',
        recovered_by='HUB-2-OPENING-CHECK')

    d = cap.close()
    # Recovery does not reopen SHIP-1 as delivered.
    assert d['status'] == 'partially_received_with_claim'
    ownership = d['parcels_detail']['BOX-2']['ownership']
    assert ownership['locked'] is True
    assert ownership['disposition'] == 'recovery_review_pending'

    # One signature short: no reconfirmation.
    with pytest.raises(ConfirmationRequired):
        cap.confirm_recovered_ownership(
            'BOX-2', at='2026-11-26T10:00:00',
            disposition='carrier_salvage',
            carrier_confirm='CARRIER-X-RECONF',
            factory_confirm='TEA-FACTORY-RECONF')

    snap = cap.confirm_recovered_ownership(
        'BOX-2', at='2026-11-26T10:00:00',
        disposition='carrier_salvage',
        carrier_confirm='CARRIER-X-RECONF',
        customer_confirm='FACTORY-B-RECONF',
        factory_confirm='TEA-FACTORY-RECONF')
    assert snap['notice']['kind'] == 'ownership_reconfirmed'

    d = cap.close()
    ownership = d['parcels_detail']['BOX-2']['ownership']
    assert ownership['owner'] == 'CARRIER-X'
    assert ownership['basis'] == 'triparty_reconfirmation'
    assert ownership['original_order_redelivery_barred'] is True
    assert d['status'] != 'delivered'
    assert d['usable_boxes'] == 1  # SHIP-1 still only delivered BOX-1


def test_recovered_box_cannot_be_double_charged_and_repurchase_is_new_order(
        seed):
    cap = build_route_b(seed)

    # Original SHIP-1 BOX-2 line was invoiced once before the loss.
    first = cap.present_charge(
        'SHIP-1', 'BOX-2', 500, at='2026-11-24T15:00:00',
        kind='original_invoice', idempotency_key='INV-SHIP1-L2')
    replay = cap.present_charge(
        'SHIP-1', 'BOX-2', 500, at='2026-11-24T15:00:00',
        kind='original_invoice', idempotency_key='INV-SHIP1-L2')
    assert replay['id'] == first['id']
    assert len(cap.charges) == 1

    # A second presentment to bill it again as original delivery is barred.
    with pytest.raises(DoublePresentment):
        cap.present_charge(
            'SHIP-1', 'BOX-2', 500, at='2026-11-26T11:00:00',
            kind='redelivery_after_recovery')

    cap.recover_found_parcel(
        'BOX-2', at='2026-11-26T08:30:00', location='HUB-2',
        condition='recovered_sound', recovered_by='HUB-2-QC')

    # Repurchase under the original order id is rejected.
    with pytest.raises(CaptureError):
        cap.confirm_recovered_ownership(
            'BOX-2', at='2026-11-26T10:00:00',
            disposition='customer_repurchase',
            new_order_id='SHIP-1', salvage_amount_cny=300,
            carrier_confirm='C', customer_confirm='F', factory_confirm='T')

    cap.confirm_recovered_ownership(
        'BOX-2', at='2026-11-26T10:00:00',
        disposition='customer_repurchase',
        new_order_id='ORD-NEW-BOX2-01', salvage_amount_cny=300,
        carrier_confirm='CARRIER-X-RECONF',
        customer_confirm='FACTORY-B-RECONF',
        factory_confirm='TEA-FACTORY-RECONF')

    new_charges = [c for c in cap.charges
                   if c['order_id'] == 'ORD-NEW-BOX2-01']
    assert len(new_charges) == 1
    assert new_charges[0]['amount_cny'] == 300

    # The new order line itself also cannot be double collected.
    with pytest.raises(DoublePresentment):
        cap.present_charge('ORD-NEW-BOX2-01', 'BOX-2', 300,
                           at='2026-11-26T10:05:00')

    # Original shipment economics are unchanged by the recovery/new sale.
    d = cap.close()
    assert d['status'] == 'partially_received_with_claim'
    assert d['paid_claims_cny'] == 800
    assert d['usable_boxes'] == 1
    assert len([c for c in cap.charges if c['order_id'] == 'SHIP-1']) == 1
