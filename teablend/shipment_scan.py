"""Per-box scan summaries and verifiable custody / cold-chain chains.

The legacy :func:`arrived_parcels` summary only lists boxes that ever carried
an ``arrived`` scan.  Such a scan is an *arrival announcement* (often a
unilateral driver/gate scan), not proof that custody moved to the customer.

Two additional, independently verifiable chains are modelled here:

* custody -- a box moves only along dual-signed handovers between real nodes;
* temperature -- 4-12 C compliance needs a gap-free time-stamped record that
  brackets outbound, transit, gate arrival and signature.  Legacy min/max
  summary rows can corroborate the range but can never prove continuity.
"""

from datetime import datetime, timedelta

TEMP_MIN_CELSIUS = 4.0
TEMP_MAX_CELSIUS = 12.0
# A cold-chain logger missing for longer than this is a monitoring break.
MAX_READING_GAP_MINUTES = 45


# ---------------------------------------------------------------------------
# time helpers
# ---------------------------------------------------------------------------

def parse_ts(value):
    """Parse the ISO timestamps used across the shipment ledger."""
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _minutes_between(a, b):
    return (parse_ts(b) - parse_ts(a)).total_seconds() / 60.0


# ---------------------------------------------------------------------------
# raw carrier scans (the original seed behaviour)
# ---------------------------------------------------------------------------

def arrived_parcels(events):
    """Set of parcel ids that have at least one raw ``arrived`` scan."""
    return {row['parcel'] for row in events if row.get('stage') == 'arrived'}


def parcel_scans(events, parcel):
    rows = [row for row in events if row.get('parcel') == parcel]
    return sorted(rows, key=lambda row: row.get('at', ''))


def arrival_announcements(events, parcel=None):
    """Every raw ``arrived`` scan, preserved even when later evidence arrives."""
    rows = [row for row in events if row.get('stage') == 'arrived']
    if parcel is not None:
        rows = [row for row in rows if row.get('parcel') == parcel]
    return sorted(rows, key=lambda row: row.get('at', ''))


def last_scan(events, parcel):
    rows = parcel_scans(events, parcel)
    return rows[-1] if rows else None


# ---------------------------------------------------------------------------
# dual signatures and custody chain
# ---------------------------------------------------------------------------

def handover_signatures(row):
    """Return the two signature strings of a handover/receipt row.

    Accepts the canonical ``from_sign``/``to_sign`` pair, a nested
    ``signatures`` mapping and the carrier/customer naming used by receipts.
    """
    signs = row.get('signatures')
    if isinstance(signs, dict):
        first = signs.get('from', signs.get('from_sign', signs.get('carrier')))
        second = signs.get('to', signs.get('to_sign', signs.get('customer')))
    else:
        first = row.get('from_sign')
        second = row.get('to_sign')
        if first is None and row.get('carrier_sign') is not None:
            first = row.get('carrier_sign')
        if second is None and row.get('customer_sign') is not None:
            second = row.get('customer_sign')
    first = str(first).strip() if first not in (None, '') else ''
    second = str(second).strip() if second not in (None, '') else ''
    return first, second


def dual_signed(row):
    """A handover needs two present and distinct signatures."""
    first, second = handover_signatures(row)
    return bool(first and second and first != second)


def _is_customer_receipt(row, customer):
    if row.get('kind') in ('receipt', 'customer_receipt', 'sign_receipt'):
        return True
    if row.get('stage') in ('received', 'signed', 'receipt'):
        return True
    return bool(customer and row.get('to_party') == customer
                and row.get('receipt') is True)


def custody_verdict(parcel, handovers=None, events=None, *, nodes=None,
                    customer=None, shipper=None):
    """Reconstruct one parcel's provable custody chain.

    Returns a plain dict with ``state`` one of ``unproven``,
    ``in_custody`` and ``customer_received`` plus every issue found.  Raw
    scans only contribute announcements / last-known location; they never
    move custody.
    """
    handovers = [h for h in (handovers or []) if h.get('parcel') == parcel]
    handovers = sorted(handovers, key=lambda h: h.get('at', ''))
    issues = []
    chain_ids = []
    valid = []

    if not handovers:
        issues.append({'code': 'no_signed_handover', 'parcel': parcel})

    for row in handovers:
        hid = row.get('id')
        if not dual_signed(row):
            issues.append({'code': 'missing_dual_signature',
                           'handover': hid, 'at': row.get('at')})
            continue
        if nodes and row.get('node') and row['node'] not in nodes:
            issues.append({'code': 'unknown_node', 'handover': hid,
                           'node': row.get('node')})
        valid.append(row)

    expected_from = shipper
    for row in valid:
        rid = row.get('id')
        if row.get('kind') == 'outbound' and expected_from is None:
            expected_from = row.get('from_party')
        if expected_from is not None and row.get('from_party') not in (None, expected_from):
            issues.append({'code': 'custody_gap', 'handover': rid,
                           'expected_from': expected_from,
                           'actual_from': row.get('from_party')})
        chain_ids.append(rid)
        expected_from = row.get('to_party')

    if valid and not any(h.get('kind') == 'outbound' for h in valid):
        issues.append({'code': 'missing_outbound_origin', 'parcel': parcel})

    receipts = [h for h in valid if _is_customer_receipt(h, customer)]
    receipt = receipts[0] if receipts else None
    received_at = receipt.get('at') if receipt else None

    scans = parcel_scans(events or [], parcel)
    gate = [s for s in arrival_announcements(events or [], parcel)]
    gate_at = gate[0]['at'] if gate else None
    last = scans[-1] if scans else None

    if receipt is not None:
        state = 'customer_received'
        custodian = customer or receipt.get('to_party')
    elif valid:
        state = 'in_custody'
        custodian = valid[-1].get('to_party')
    elif last is not None:
        state = 'unproven'
        custodian = last.get('holder')
    else:
        state = 'unproven'
        custodian = None

    if gate and receipt is None:
        issues.append({'code': 'unilateral_arrival_scan_not_receipt',
                       'parcel': parcel, 'scan': gate[0].get('id'),
                       'at': gate_at,
                       'location': gate[0].get('location')})

    severe = {'custody_gap', 'missing_dual_signature', 'unknown_node',
              'missing_outbound_origin'}
    chain_complete = not any(i['code'] in severe for i in issues) and bool(valid)

    return {
        'parcel': parcel,
        'state': state,
        'custodian': custodian,
        'chain_complete': chain_complete,
        'handover_ids': chain_ids,
        'received_at': received_at,
        'gate_announced_at': gate_at,
        'arrival_scan_ids': [s.get('id') for s in gate],
        'last_scan': last,
        'issues': issues,
    }


def issue_codes(verdict):
    """Convenience helper: issue code list from a verdict dict."""
    return [item['code'] for item in verdict.get('issues', [])]


# ---------------------------------------------------------------------------
# temperature chain
# ---------------------------------------------------------------------------

def _reading_at(row):
    return row.get('at') or row.get('recorded_at') or row.get('ts')


def _reading_celsius(row):
    value = row.get('celsius', row.get('value', row.get('c')))
    return float(value)


def temperature_audit(parcel, readings=None, summary=None, *,
                       minimum=TEMP_MIN_CELSIUS, maximum=TEMP_MAX_CELSIUS,
                       max_gap_minutes=MAX_READING_GAP_MINUTES, phases=None):
    """Audit one box's cold chain.

    ``state`` is:

    * ``qualified`` -- time-stamped readings exist, stay in 4-12 C, have no
      gap longer than ``max_gap_minutes`` and cover every required phase;
    * ``abuse`` -- at least one reading left the legal band (the box is
      definitively not sound fresh leaf);
    * ``pending`` -- missing / summary-only / gappy / phase coverage unknown,
      i.e. the box must wait for verification.
    """
    readings = list(readings or [])
    readings = [r for r in readings if _reading_at(r) is not None]
    readings.sort(key=_reading_at)

    issues = []
    out_of_range = []
    gaps = []
    uncovered = []

    if not readings:
        if summary and (summary.get('min_celsius') is not None
                        or summary.get('max_celsius') is not None):
            issues.append({
                'code': 'summary_minmax_cannot_prove_continuity',
                'parcel': parcel,
                'summary': {k: summary.get(k) for k in
                            ('min_celsius', 'max_celsius')},
            })
        else:
            issues.append({'code': 'no_temperature_records', 'parcel': parcel})
        state = 'pending'
        observed_min = observed_max = None
    else:
        values = [_reading_celsius(r) for r in readings]
        observed_min, observed_max = min(values), max(values)
        for row, value in zip(readings, values):
            if value < minimum or value > maximum:
                out_of_range.append({'at': _reading_at(row), 'celsius': value})
                issues.append({'code': 'temperature_out_of_range',
                               'parcel': parcel, 'at': _reading_at(row),
                               'celsius': value})

        for prev, nxt in zip(readings, readings[1:]):
            minutes = _minutes_between(_reading_at(prev), _reading_at(nxt))
            if minutes > max_gap_minutes:
                gaps.append({'after': _reading_at(prev),
                             'before': _reading_at(nxt),
                             'minutes': minutes})
                issues.append({'code': 'continuity_gap', 'parcel': parcel,
                               'after': _reading_at(prev),
                               'before': _reading_at(nxt),
                               'minutes': minutes})

        first_at = parse_ts(_reading_at(readings[0]))
        last_at = parse_ts(_reading_at(readings[-1]))
        bad_gap_spans = [(parse_ts(g['after']), parse_ts(g['before']))
                         for g in gaps]

        phase_items = _normalize_phases(phases)
        for name, at in phase_items:
            if at is None:
                continue
            at_dt = parse_ts(at)
            if at_dt < first_at or at_dt > last_at:
                uncovered.append(name)
                issues.append({'code': 'phase_not_covered', 'parcel': parcel,
                               'phase': name, 'at': at})
                continue
            for after, before in bad_gap_spans:
                if after < at_dt < before:
                    uncovered.append(name)
                    issues.append({'code': 'phase_in_monitoring_gap',
                                   'parcel': parcel, 'phase': name, 'at': at,
                                   'after': after.isoformat(),
                                   'before': before.isoformat()})
                    break

        state = 'abuse' if out_of_range else (
            'qualified' if not issues else 'pending')

    return {
        'parcel': parcel,
        'state': state,
        'reading_count': len(readings),
        'observed_min_celsius': observed_min,
        'observed_max_celsius': observed_max,
        'required_min_celsius': minimum,
        'required_max_celsius': maximum,
        'max_gap_minutes': max_gap_minutes,
        'out_of_range': out_of_range,
        'gaps': gaps,
        'uncovered_phases': uncovered,
        'summary_only': not readings and bool(summary),
        'issues': issues,
    }


def _normalize_phases(phases):
    if not phases:
        return []
    if isinstance(phases, dict):
        return sorted(phases.items(), key=lambda kv: kv[1] or '')
    return list(phases)


def continuous_series(parcel, start, end, *, step_minutes=30, celsius=7.0,
                       logger_id='LOGGER-1'):
    """Generate a gap-free qualified reading series (test/evidence helper)."""
    start_dt = parse_ts(start)
    end_dt = parse_ts(end)
    step = timedelta(minutes=step_minutes)
    rows = []
    current = start_dt
    index = 0
    while current <= end_dt:
        rows.append({
            'id': f'TEMP-{parcel}-{index:03d}',
            'parcel': parcel,
            'at': current.isoformat(timespec='minutes'),
            'celsius': celsius,
            'logger_id': logger_id,
        })
        current += step
        index += 1
    if not rows or parse_ts(rows[-1]['at']) < end_dt:
        rows.append({
            'id': f'TEMP-{parcel}-{index:03d}',
            'parcel': parcel,
            'at': end if isinstance(end, str) else end.isoformat(timespec='minutes'),
            'celsius': celsius,
            'logger_id': logger_id,
        })
    return rows


# Descriptive aliases for the three verifiable summaries.
scan_summary = arrived_parcels
custody_chain = custody_verdict
verify_temperature = temperature_audit
temperature_chain = temperature_audit
