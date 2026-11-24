"""Actual leaf capture reconciled against the frost-day board.

A board promise is not leaf in the factory. This module turns field capture
events into the receipt/weight evidence already adjudicated by
:mod:`teablend.leaf`, enforces the same picking authority as
:mod:`teablend.garden`, and counts only dual-signed, timely, in-rule net
weight toward the 80 kg customer commitment.

Guardrails:
* covered or frost-hit plots cannot book premium kg on the frost day;
* a cover record can never be presented as a picking receipt;
* a plot picked the next day after covering is graded fresh from its actual
  handoff delay — yesterday's promise cannot be pre-borrowed;
* unsigned / disputed / late weight never counts toward the commitment.
"""
from .garden import decide as garden_decide
from .leaf import decide as leaf_decide

PREMIUM = 'premium'


def _board_jobs(board):
    selected = board.get('selected')
    if not selected:
        return {}
    return {job['plot']: job for job in selected.get('jobs', [])}


def _capture_ref(capture):
    return capture.get('id') or f"cap:{capture['plot']}:{capture['picked_at']}"


def build_evidence(board, captures, garden_events):
    """Project field captures into receipt/weight events for leaf.decide.

    Captures against covered plots are rejected outright rather than emitted
    as receipts, so a protection record can never become a picking sign-off.
    """
    certs, pickers, _ = garden_decide(garden_events)
    jobs = _board_jobs(board)
    board_day = board['generated_at'][:10]
    receipts, weights, rejected = [], [], []
    receipt_plots = {}

    for capture in captures:
        ref = _capture_ref(capture)
        plot_id = capture['plot']
        job = jobs.get(plot_id)
        if job is None:
            rejected.append({'capture': ref, 'plot': plot_id,
                             'reason': 'plot_not_on_board'})
            continue
        if job['action'] != 'harvest':
            rejected.append({'capture': ref, 'plot': plot_id,
                             'reason': 'plot_covered_not_harvested'})
            continue
        if capture.get('frost_damaged'):
            rejected.append({'capture': ref, 'plot': plot_id,
                             'reason': 'frost_damaged_leaf'})
            continue
        picked_at = capture['picked_at']
        if picked_at[:10] != board_day:
            rejected.append({'capture': ref, 'plot': plot_id,
                             'reason': 'capture_not_on_board_day'})
            continue
        # The crew that actually picks must hold the plot's harvest job.
        if capture.get('crew') and capture['crew'] != job['crew']:
            rejected.append({'capture': ref, 'plot': plot_id,
                             'reason': 'crew_not_assigned'})
            continue
        picker = capture['picker']
        variety = capture.get('variety', 'Longjing')
        receipt = {
            'id': f"R:{ref}", 'kind': 'receipt', 'at': capture['delivered_at'],
            'garden': job['garden'], 'variety': variety, 'picker': picker,
            'pick_ticket': capture['pick_ticket'], 'picked_at': picked_at,
            'delivered_at': capture['delivered_at'],
            'gross_kg': capture['gross_kg'],
            'grade': capture.get('grade') or job.get('grade') or PREMIUM,
        }
        if capture.get('leaf_age_days') is not None:
            receipt['leaf_age_days'] = capture['leaf_age_days']
        receipts.append(receipt)
        receipt_plots[receipt['id']] = plot_id
        for w in capture.get('weights', []):
            weights.append({
                'id': f"W:{w['id']}", 'kind': 'weight', 'at': w['at'],
                'receipt': receipt['id'], 'net_kg': w['net_kg'],
                'garden_sign': w['garden_sign'],
                'factory_sign': w['factory_sign'],
            })
    return receipts, weights, rejected, certs, pickers, receipt_plots


def capture_day(board, captures, garden_events):
    """Adjudicate the day's captures and reconcile against the order promise."""
    receipts, weights, rejected, certs, pickers, receipt_plots = build_evidence(
        board, captures, garden_events)
    decisions = leaf_decide([*receipts, *weights], certs, pickers)
    order_kg = int(board.get('order_kg', 80))
    accepted, pending = [], []
    for receipt in receipts:
        verdict = decisions[f"receipt:{receipt['id']}"]
        line = {'receipt': receipt['id'], 'plot': receipt_plots[receipt['id']],
                'garden': receipt['garden'], 'status': verdict['status'],
                'grade': verdict['grade'], 'net_kg': verdict['net_kg'],
                'delay_hours': verdict['delay_hours'],
                'route': verdict['route'],
                'pick_ticket': receipt['pick_ticket']}
        if verdict['status'] == 'eligible':
            accepted.append(line)
        else:
            pending.append(line)

    premium_kg = sum(x['net_kg'] for x in accepted
                     if x['grade'] == PREMIUM and x['route'] == 'normal')
    total_net = sum(x['net_kg'] for x in accepted)
    jobs = _board_jobs(board)
    covered = sorted(pid for pid, job in jobs.items()
                     if job['action'] == 'cover')
    return {
        'feasible': premium_kg >= order_kg,
        'order_kg': order_kg,
        'premium_kg': premium_kg,
        'accepted_net_kg': total_net,
        'shortfall_kg': max(0, order_kg - premium_kg),
        'accepted': accepted,
        'pending': pending,
        'rejected': rejected,
        'covered_plots': covered,
        'next_day_todo': [
            {'plot': pid, 'action': 'reassess_after_cover',
             'note': 'grade and yield recomputed at next-day handoff; '
                     'no premium kg pre-booked today'}
            for pid in covered],
        'decisions': decisions,
    }


def capture_covered_plot(board, plot_id, captures, garden_events):
    """Next-day capture of a covered plot: authority re-checked and the grade
    is decided entirely from the fresh handoff (delay / overnight / age)."""
    jobs = _board_jobs(board)
    job = jobs.get(plot_id)
    if not job or job['action'] != 'cover':
        raise ValueError(f'{plot_id} was not placed under cover')
    board_day = board['generated_at'][:10]
    certs, pickers, _ = garden_decide(garden_events)
    receipts, weights, rejected = [], [], []
    for capture in captures:
        ref = _capture_ref(capture)
        if capture['plot'] != plot_id:
            continue
        picked_at = capture['picked_at']
        if picked_at[:10] == board_day:
            rejected.append({'capture': ref, 'plot': plot_id,
                             'reason': 'cover_holds_until_next_day'})
            continue
        receipt = {
            'id': f"R:{ref}", 'kind': 'receipt', 'at': capture['delivered_at'],
            'garden': job['garden'],
            'variety': capture.get('variety', 'Longjing'),
            'picker': capture['picker'],
            'pick_ticket': capture['pick_ticket'], 'picked_at': picked_at,
            'delivered_at': capture['delivered_at'],
            'gross_kg': capture['gross_kg'],
            'grade': capture.get('grade', PREMIUM),
        }
        if capture.get('leaf_age_days') is not None:
            receipt['leaf_age_days'] = capture['leaf_age_days']
        receipts.append(receipt)
        for w in capture.get('weights', []):
            weights.append({
                'id': f"W:{w['id']}", 'kind': 'weight', 'at': w['at'],
                'receipt': receipt['id'], 'net_kg': w['net_kg'],
                'garden_sign': w['garden_sign'],
                'factory_sign': w['factory_sign'],
            })
    decisions = leaf_decide([*receipts, *weights], certs, pickers)
    lines = []
    for receipt in receipts:
        verdict = decisions[f"receipt:{receipt['id']}"]
        lines.append({'receipt': receipt['id'], 'plot': plot_id,
                      'garden': receipt['garden'], 'status': verdict['status'],
                      'grade': verdict['grade'], 'net_kg': verdict['net_kg'],
                      'delay_hours': verdict['delay_hours'],
                      'route': verdict['route']})
    return {'plot': plot_id, 'reassessed': True,
            'covered_on': board_day, 'lines': lines,
            'rejected': rejected, 'decisions': decisions}
