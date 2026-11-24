"""Per-box receipt decision, shipment close and claim/ownership aggregation.

Unlike the seed ``decide`` (which closed the whole shipment on any unilateral
``arrived`` scan), closing SHIP-1 requires the physical chain to be proven
for *every* box:

1. dual-signed custody handovers from outbound through the customer receipt;
2. a continuous 4-12 C record covering outbound/transit/gate/signature;
3. the customer's per-box dual signature (or a written partial receipt plus
   a settled non-delivery claim for the missing box).

The function is pure: callers (see :class:`teablend.delivery_capture.
ShipmentCapture`) keep append-only evidence and versioned snapshots.
"""

from datetime import datetime

from .shipment_scan import (
    TEMP_MAX_CELSIUS,
    TEMP_MIN_CELSIUS,
    arrival_announcements,
    arrived_parcels,
    custody_verdict,
    last_scan,
    temperature_audit,
)

# Shipment-level status tokens.
DELIVERED = 'delivered'
PARTIAL_WITH_CLAIM = 'partially_received_with_claim'
PARTIAL = 'partially_received'
AWAITING_RECEIPT = 'awaiting_receipt'
REJECTED = 'rejected'
IN_TRANSIT = 'in_transit'

_SOUND_DISPOSITION = 'customer_received_sound'


def _index_rows(rows, key='parcel'):
    grouped = {}
    for row in rows or []:
        grouped.setdefault(row.get(key), []).append(row)
    return grouped


def _claim_state(claim):
    return claim.get('state') or claim.get('status') or 'opened'


def _claim_amount(claim):
    value = claim.get('amount_cny', claim.get('amount', 0))
    return float(value or 0)


def _normalize_claim(claim):
    return {
        'id': claim.get('id'),
        'parcel': claim.get('parcel'),
        'reason': claim.get('reason', 'carrier_non_delivery'),
        'state': _claim_state(claim),
        'amount_cny': _claim_amount(claim),
        'paid_at': claim.get('paid_at'),
        'payment_ref': claim.get('payment_ref'),
        'awarded_at': claim.get('awarded_at'),
        'opened_at': claim.get('opened_at') or claim.get('at'),
    }


def _latest_claim(claims):
    if not claims:
        return None
    order = {'opened': 0, 'awarded': 1, 'paid': 2}
    return sorted(claims, key=lambda c: (order.get(_claim_state(c), 0),
                                         c.get('paid_at') or c.get('awarded_at')
                                         or c.get('opened_at') or ''))[-1]


def _ownership_for(parcel, *, shipment, claim, recoveries, confirmations,
                   explicit=None, sound_delivered=False):
    """Derive current ownership / disposition lock for one parcel."""
    shipper = shipment.get('shipper', 'TEA-FACTORY')
    carrier = shipment.get('carrier', 'CARRIER-X')
    customer = shipment.get('customer')

    mine = [c for c in (confirmations or []) if c.get('parcel') == parcel]
    if mine:
        latest = sorted(mine, key=lambda c: c.get('at', ''))[-1]
        return {
            'owner': latest.get('owner') or latest.get('assigned_owner'),
            'basis': 'triparty_reconfirmation',
            'disposition': latest.get('disposition'),
            'locked': bool(latest.get('locked', False)),
            'confirmation_id': latest.get('id'),
            'confirmed_at': latest.get('at'),
            'parties': latest.get('parties'),
            'new_order_id': latest.get('new_order_id'),
            # A paid claim can never be undone into an original-order delivery.
            'original_order_redelivery_barred': True,
        }

    recovered = [r for r in (recoveries or []) if r.get('parcel') == parcel]
    if recovered and claim and _claim_state(claim) == 'paid':
        return {
            'owner': carrier,
            'basis': 'paid_claim_subrogation',
            'disposition': 'recovery_review_pending',
            'locked': True,
            'recovered_at': recovered[-1].get('at'),
            'recovery_location': recovered[-1].get('location'),
            'original_order_redelivery_barred': True,
        }

    if claim and _claim_state(claim) == 'paid':
        return {
            'owner': carrier,
            'basis': 'paid_claim_subrogation_salvage',
            'disposition': claim.get('disposition', 'carrier_salvage_locked'),
            'locked': True,
            'original_order_redelivery_barred': True,
        }

    if claim and _claim_state(claim) == 'awarded':
        return {
            'owner': shipper,
            'basis': 'awarded_claim_disposition_hold',
            'disposition': 'claim_awarded_disposition_locked',
            'locked': True,
            'pending_payment_to': customer,
            'original_order_redelivery_barred': True,
        }

    if explicit and parcel in explicit:
        return explicit[parcel]

    if sound_delivered:
        return {'owner': customer,
                'basis': 'dual_signed_sound_delivery',
                'locked': False}

    return {'owner': shipper, 'basis': 'original_title',
            'locked': False, 'customer': customer}


def close_case(case):
    """Adjudicate a whole shipment from a plain evidence mapping.

    ``case`` may contain ``shipment``, ``events``, ``handovers``,
    ``temperature`` (legacy min/max rows), ``temperature_readings``,
    ``inspections``, ``receipts`` (merged into handovers), ``rejections``,
    ``claims``, ``recoveries`` and ``ownership_confirmations``.
    """
    shipment = case.get('shipment', {}) or {}
    shipment_id = shipment.get('id')
    parcels = list(shipment.get('parcels') or case.get('parcels') or [])
    customer = shipment.get('customer')
    shipper = shipment.get('shipper')
    nodes = case.get('nodes') or shipment.get('nodes')

    minimum = float(shipment.get('minimum_celsius',
                                 case.get('minimum_celsius', TEMP_MIN_CELSIUS)))
    maximum = float(shipment.get('maximum_celsius',
                                 case.get('maximum_celsius', TEMP_MAX_CELSIUS)))
    max_gap = float(shipment.get('temp_max_gap_minutes',
                                 case.get('temp_max_gap_minutes', 45)))

    events = case.get('events', []) or []
    handovers = list(case.get('handovers', []) or [])
    # Explicit receipt rows are normalized into custody handovers so the
    # carrier->customer dual signature closes the chain exactly once.
    for receipt in case.get('receipts', []) or []:
        handovers.append(dict(
            receipt,
            kind=receipt.get('kind', 'receipt'),
            from_party=(receipt.get('from_party')
                        or receipt.get('carrier_party')
                        or receipt.get('carrier')
                        or shipment.get('carrier')),
            to_party=receipt.get('to_party') or customer,
        ))

    summaries = _index_rows(case.get('temperature'), 'parcel')
    readings = _index_rows(case.get('temperature_readings'), 'parcel')
    claims_by_parcel = _index_rows(case.get('claims'), 'parcel')
    rejections_by_parcel = _index_rows(case.get('rejections'), 'parcel')
    inspections_by_parcel = _index_rows(case.get('inspections'), 'parcel')
    recoveries = case.get('recoveries', []) or []
    confirmations = case.get('ownership_confirmations', []) or []

    arrived = arrived_parcels(events)
    parcel_results = {}
    received_sound = []
    signed_parcels = []
    claim_parcels = []
    holds = []
    carrier_debt = 0.0
    paid_total = 0.0
    obligation_total = 0.0

    for parcel in parcels:
        custody = custody_verdict(
            parcel, handovers, events, nodes=nodes,
            customer=customer, shipper=shipper)

        outbound = next((h.get('at') for h in
                         sorted([h for h in handovers
                                 if h.get('parcel') == parcel
                                 and h.get('kind') == 'outbound'],
                                key=lambda h: h.get('at', ''))), None)
        phases = {}
        if outbound:
            phases['outbound'] = outbound
        if custody['gate_announced_at']:
            phases['gate'] = custody['gate_announced_at']
        if custody['received_at']:
            phases['receipt'] = custody['received_at']
        # Transit is the span between outbound and gate/receipt; probe its
        # midpoint so a hole in the middle cannot masquerade as coverage.
        span_end = custody['gate_announced_at'] or custody['received_at']
        if outbound and span_end:
            start_dt = datetime.fromisoformat(outbound)
            end_dt = datetime.fromisoformat(span_end)
            phases['transit'] = (start_dt + (end_dt - start_dt) / 2).isoformat(
                timespec='minutes')

        summary = (summaries.get(parcel) or [None])[0]
        temp = temperature_audit(
            parcel, readings.get(parcel), summary,
            minimum=minimum, maximum=maximum,
            max_gap_minutes=max_gap, phases=phases)

        parcel_claims = [_normalize_claim(c) for c in
                         claims_by_parcel.get(parcel, [])]
        claim = _latest_claim(parcel_claims)
        if claim:
            claim_parcels.append(parcel)
            if claim['state'] == 'awarded':
                carrier_debt += claim['amount_cny']
                obligation_total += claim['amount_cny']
            elif claim['state'] == 'paid':
                paid_total += claim['amount_cny']
                obligation_total += claim['amount_cny']

        rejection_rows = rejections_by_parcel.get(parcel, [])
        rejection = sorted(rejection_rows, key=lambda r: r.get('at', ''))[-1] \
            if rejection_rows else None

        inspection_rows = inspections_by_parcel.get(parcel, [])
        inspection = sorted(inspection_rows, key=lambda r: r.get('at', ''))[-1] \
            if inspection_rows else None

        signed = custody['state'] == 'customer_received'
        if signed:
            signed_parcels.append(parcel)

        temp_state = temp['state']
        chain_ok = custody['chain_complete']
        sound_delivered = signed and temp_state == 'qualified' and chain_ok

        ownership = _ownership_for(
            parcel, shipment=shipment, claim=claim,
            recoveries=recoveries, confirmations=confirmations,
            explicit=case.get('ownership'),
            sound_delivered=sound_delivered)

        # ---- current disposition / responsibility for this box -----------
        if rejection is not None:
            disposition = 'rejected_by_customer'
            responsible = 'carrier'
            basis = rejection.get('reason', 'customer_rejection')
        elif claim and claim['state'] in ('opened', 'awarded', 'paid') \
                and not signed:
            disposition = {
                'paid': 'claim_paid_undelivered',
                'awarded': 'claim_awarded_undelivered',
                'opened': 'claim_opened_undelivered',
            }[claim['state']]
            responsible = 'carrier'
            basis = 'carrier_custody_non_delivery'
        elif sound_delivered:
            disposition = _SOUND_DISPOSITION
            responsible = 'customer'
            basis = 'dual_signed_receipt_qualified_cold_chain'
            received_sound.append(parcel)
        elif signed and temp_state == 'abuse':
            disposition = 'received_but_temperature_failed'
            responsible = 'carrier'
            basis = 'temperature_excursion'
        elif signed:
            disposition = 'received_pending_verification'
            responsible = 'pending'
            basis = 'cold_chain_or_custody_unproven'
        elif temp_state == 'abuse':
            disposition = 'temperature_failed_not_deliverable'
            responsible = 'carrier'
            basis = 'temperature_excursion'
        elif custody['gate_announced_at']:
            disposition = 'at_gate_awaiting_dual_signature'
            responsible = 'carrier'
            basis = 'unilateral_arrival_scan'
        elif (last_scan(events, parcel) or custody['custodian']):
            location = (last_scan(events, parcel) or {}).get('location')
            disposition = 'in_transit' if location != 'HUB-2' else 'held_at_hub'
            if location == 'HUB-2':
                disposition = 'held_at_hub'
            responsible = 'carrier'
            basis = 'pre_receipt_custody'
        else:
            disposition = 'no_custody_evidence'
            responsible = 'factory'
            basis = 'before_carrier_outbound'

        parcel_holds = [i['code'] for i in temp['issues']]
        if not chain_ok:
            parcel_holds.extend(i['code'] for i in custody['issues']
                                if i['code'] != 'unilateral_arrival_scan_not_receipt')
        for code in parcel_holds:
            holds.append(f'{code}:{parcel}')

        parcel_results[parcel] = {
            'parcel': parcel,
            'disposition': disposition,
            'responsible_party': responsible,
            'liability_basis': basis,
            'signed': signed,
            'received_at': custody['received_at'],
            'gate_announced_at': custody['gate_announced_at'],
            'last_scan': custody['last_scan'],
            'custody_state': custody['state'],
            'custodian': custody['custodian'],
            'custody_chain_complete': chain_ok,
            'custody_issue_codes': [i['code'] for i in custody['issues']],
            'temperature_state': temp_state,
            'temperature': temp,
            'claim': claim,
            'inspection': inspection,
            'rejection': rejection,
            'ownership': ownership,
            'holds': parcel_holds,
        }

    # ---- shipment-level aggregation --------------------------------------
    total = len(parcels)
    all_sound = total > 0 and len(received_sound) == total

    partial_consent = case.get('partial_consent') or shipment.get(
        'partial_consent')
    if not partial_consent:
        consent_rows = case.get('receipts', []) or []
        partial_consent = next(({
            'allowed': shipment.get('partial_credit_allowed', False),
            'ref': r.get('consent_ref'),
            'at': r.get('at'),
            'parcels': r.get('parcels'),
        } for r in consent_rows if r.get('partial') and r.get('consent_ref')),
            None)

    claim_closed = set(claim_parcels)
    sound_set = set(received_sound)
    partial_covers = sound_set | claim_closed

    settled = all(
        (p['claim'] and p['claim']['state'] == 'paid')
        for pid, p in parcel_results.items()
        if pid in claim_closed and pid not in sound_set
    ) if claim_closed - sound_set else False

    if all_sound:
        status = DELIVERED
    elif received_sound and partial_covers == set(parcels) and claim_closed:
        status = PARTIAL_WITH_CLAIM
    elif any(p['rejection'] for p in parcel_results.values()) \
            and not received_sound:
        status = REJECTED
    elif received_sound:
        status = PARTIAL
    elif any(p['temperature_state'] == 'abuse'
             for p in parcel_results.values()):
        status = 'temperature_excursion_blocked'
    elif any(p['gate_announced_at'] for p in parcel_results.values()):
        status = AWAITING_RECEIPT
    else:
        status = IN_TRANSIT
    closed = status == DELIVERED or (
        status == PARTIAL_WITH_CLAIM and settled)

    return {
        'shipment': shipment_id,
        'status': status,
        'closed': closed,
        'arrived_parcels': sorted(arrived),
        'parcels_total': total,
        'parcel_order': parcels,
        'received_parcels': sorted(signed_parcels),
        'received_count': len(signed_parcels),
        'received_sound_parcels': sorted(received_sound),
        'sound_count': len(received_sound),
        'usable_boxes': len(received_sound),
        'claim_parcels': sorted(claim_closed),
        'partial_receipt': bool(received_sound and not all_sound),
        'partial_consent': partial_consent,
        'settled': settled,
        'carrier_debt_cny': carrier_debt,
        'paid_claims_cny': paid_total,
        'claim_obligation_cny': obligation_total,
        'holds': holds,
        'deliverable': all_sound,
        'parcels_detail': parcel_results,
        # short alias kept for ergonomic callers/tests
        'parcels': parcel_results,
    }


def decide(shipment=None, events=None, **evidence):
    """Backward-compatible entry point with enriched evidence keywords.

    The seed two-argument call ``decide(shipment, events)`` keeps working; it
    can no longer return ``delivered`` on a lone gate scan because signatures,
    custody and temperature coverage are then unproven.

    A whole fixture/case mapping (containing a ``shipment`` key) may also be
    passed as the only positional argument.
    """
    if (events is None and not evidence and isinstance(shipment, dict)
            and isinstance(shipment.get('shipment'), dict)):
        return close_case(shipment)
    case = dict(evidence)
    case['shipment'] = shipment
    if events is not None:
        case['events'] = events
    return close_case(case)


# Descriptive aliases for callers that prefer the domain wording.
close_shipment = close_case
adjudicate = decide
