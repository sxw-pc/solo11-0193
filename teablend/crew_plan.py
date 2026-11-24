"""Frost-day crew board: one global, non-overlapping harvest/cover ledger.

The old board greedily picked the first whitelisted crew per plot, so two
overlapping plots could seize the same crew and a 07:00 finish survived a
frost brought forward to 06:30. This module instead enumerates every feasible
global assignment under four hard constraints:

1. A crew's jobs (cross-garden travel and tool sanitation included) must never
   overlap; garden whitelists alone never justify an assignment — the picker
   certificate must cover the whole harvest window.
2. Each plot is either fully harvested before the earliest credible frost or
   verifiably covered before it; one optimistic forecast cannot buy time.
3. Promised premium kg only count when the plot can be harvested in time;
   unharvested or frost-hit plots borrow nothing against the customer order.
4. A new forecast or revoked authority creates a new board version, notifies
   crews and revokes the affected promises from the previous version.

Both valid routes (harvest everything / cover one plot for tomorrow) stay
visible; strategy selects between them instead of hard-coding one route.
"""
from datetime import datetime, timedelta

from .frost_window import deadline, allows, windows_changed, all_windows
from .garden import certificate_for, picker_for, decide as garden_decide

STRATEGIES = ('delivery', 'capacity')


def _parse(value):
    return datetime.fromisoformat(value)


def _fmt(value):
    return None if value is None else value.isoformat(timespec='minutes')


def travel_minutes(crew, garden_a, garden_b):
    """Cross-garden deadhead time; same garden needs no travel."""
    if garden_a == garden_b:
        return 0
    table = crew.get('travel_minutes', {})
    key = f'{garden_a}:{garden_b}'
    if key in table:
        return table[key]
    reverse = f'{garden_b}:{garden_a}'
    if reverse in table:
        return table[reverse]
    return table.get('*', 0)


def _sanitation_minutes(ctx, garden_a, garden_b):
    if garden_a == garden_b:
        return int(ctx.get('same_garden_sanitation_minutes', 0))
    return int(ctx.get('sanitation_minutes', 15))


def _gap_minutes(ctx, crew, garden_a, garden_b):
    return travel_minutes(crew, garden_a, garden_b) + \
        _sanitation_minutes(ctx, garden_a, garden_b)


def harvest_authorized(crew, plot, start_at, finish_at, ctx):
    """Whitelist is necessary but never sufficient: certificate + picker must
    cover the entire harvest window, and a revoked authority blocks dispatch."""
    if plot['garden'] not in crew.get('gardens', []):
        return False, 'garden_not_whitelisted'
    certs = ctx.get('certificates', {})
    pickers = ctx.get('pickers', {})
    variety = plot.get('variety', ctx.get('variety', 'Longjing'))
    cert, cert_state = certificate_for(
        certs, plot['garden'], variety, start_at.isoformat(timespec='minutes'))
    if not cert:
        return False, cert_state
    if not (_parse(cert['start']) <= start_at and
            finish_at <= _parse(cert['finish'])):
        return False, 'certificate_window_short'
    picker_id = crew.get('pickers', {}).get(plot['garden'])
    if not picker_id:
        return False, 'picker_unauthorized'
    if not picker_for(pickers, picker_id, plot['garden'],
                      start_at.isoformat(timespec='minutes')):
        return False, 'picker_unauthorized'
    picker = pickers[picker_id]
    if not (_parse(picker['start']) <= start_at and
            finish_at <= _parse(picker['finish'])):
        return False, 'picker_window_short'
    return True, 'authorized'


def plot_candidates(plot, crews, forecasts, ctx):
    """Every crew action that can settle this plot before the frost edge."""
    frost = deadline(plot['garden'], forecasts)
    ready = _parse(plot['ready_at'])
    planned_finish = _parse(plot['finish_at'])
    duration = int(ctx.get('harvest_minutes',
                           (planned_finish - ready).total_seconds() // 60))
    candidates = []
    for crew in crews:
        # Option one: finish the full harvest inside the frost window.
        finish = ready + timedelta(minutes=duration)
        if frost is None or finish < _parse(frost):
            ok, reason = harvest_authorized(crew, plot, ready, finish, ctx)
            if ok:
                candidates.append({
                    'plot': plot['id'], 'garden': plot['garden'],
                    'crew': crew['id'], 'action': 'harvest',
                    'start': ready, 'finish': finish,
                    'kg': plot['kg'], 'grade': plot.get('grade', 'premium'),
                    'frost_at': frost, 'reason': 'authorized',
                })
            else:
                candidates.append({
                    'plot': plot['id'], 'garden': plot['garden'],
                    'crew': crew['id'], 'action': 'harvest',
                    'start': ready, 'finish': finish,
                    'kg': 0, 'grade': plot.get('grade', 'premium'),
                    'frost_at': frost, 'reason': reason, 'feasible': False,
                })
        # Option two: verifiable cover finished before the frost. Covering is
        # a protection record, never a picking receipt.
        cover_minutes = int(ctx.get('cover_minutes', 45))
        cover_finish = ready + timedelta(minutes=cover_minutes)
        if plot['garden'] in crew.get('gardens', []) and (
                frost is None or cover_finish < _parse(frost)):
            candidates.append({
                'plot': plot['id'], 'garden': plot['garden'],
                'crew': crew['id'], 'action': 'cover',
                'start': ready, 'finish': cover_finish,
                'kg': 0, 'grade': None, 'frost_at': frost,
                'cover_minutes': cover_minutes,
                'reason': 'cover_feasible',
            })
    return candidates


def _fits_crew(crew_index, job, assignments, ctx):
    for other in assignments:
        if other['crew'] != job['crew']:
            continue
        a, b = sorted((other, job), key=lambda j: j['start'])
        gap = _gap_minutes(ctx, crew_index[job['crew']],
                           a['garden'], b['garden'])
        if a['finish'] + timedelta(minutes=gap) > b['start']:
            return False, 'crew_overlap'
    return True, None


def _evaluate(combo, plots, forecasts, ctx):
    harvested = sum(j['kg'] for j in combo if j['action'] == 'harvest')
    covered = sorted(j['plot'] for j in combo if j['action'] == 'cover')
    crews_used = sorted({j['crew'] for j in combo})
    order_kg = int(ctx.get('order_kg', 80))
    jobs = sorted(combo, key=lambda j: (j['start'], j['plot']))
    spare = []
    for crew in crews_used:
        crew_jobs = [j for j in jobs if j['crew'] == crew]
        last = max(j['finish'] for j in crew_jobs)
        spare.append({'crew': crew, 'free_after': _fmt(last)})
    return {
        'jobs': [{
            'plot': j['plot'], 'garden': j['garden'], 'crew': j['crew'],
            'action': j['action'], 'start': _fmt(j['start']),
            'finish': _fmt(j['finish']), 'frost_at': j['frost_at'],
            'kg': j['kg'], 'grade': j['grade'],
        } for j in jobs],
        'harvest_kg': harvested,
        'covered_plots': covered,
        'crews_used': crews_used,
        'order_kg': order_kg,
        'order_met': harvested >= order_kg,
        'shortfall_kg': max(0, order_kg - harvested),
        'spare_capacity': spare,
    }


def _enumerate(plots, crews, forecasts, ctx):
    crew_index = {c['id']: c for c in crews}
    per_plot = {p['id']: plot_candidates(p, crews, forecasts, ctx)
                for p in plots}
    feasible, rejected = [], []

    def walk(idx, chosen, reserve_state):
        if idx == len(plots):
            feasible.append(_evaluate(list(chosen), plots, forecasts, ctx))
            return
        plot = plots[idx]
        seen_actions = set()
        for cand in per_plot[plot['id']]:
            if cand.get('feasible') is False:
                rejected.append({'plot': plot['id'], 'crew': cand['crew'],
                                 'action': cand['action'], 'reason': cand['reason']})
                continue
            key = (cand['crew'], cand['action'])
            if key in seen_actions:
                continue
            seen_actions.add(key)
            job = dict(cand)
            fits, reason = _fits_crew(crew_index, job, chosen, ctx)
            if not fits:
                rejected.append({'plot': plot['id'], 'crew': job['crew'],
                                 'action': job['action'], 'reason': reason})
                continue
            if job['action'] == 'cover':
                stock = ctx.get('cover_materials', {}).get(plot['garden'], {})
                used = reserve_state.get(plot['garden'], 0)
                if used + 1 > stock.get('sets', 0):
                    rejected.append({'plot': plot['id'], 'crew': job['crew'],
                                     'action': 'cover',
                                     'reason': 'cover_material_short'})
                    continue
                if stock.get('frost_rating_ok', True) is False:
                    rejected.append({'plot': plot['id'], 'crew': job['crew'],
                                     'action': 'cover',
                                     'reason': 'cover_material_unrated'})
                    continue
            next_reserve = dict(reserve_state)
            if job['action'] == 'cover':
                next_reserve[plot['garden']] = \
                    next_reserve.get(plot['garden'], 0) + 1
            chosen.append(job)
            walk(idx + 1, chosen, next_reserve)
            chosen.pop()

    walk(0, [], {})
    # Deduplicate identical global boards (same plot/crew/action triples).
    unique = {}
    for board in feasible:
        key = tuple(sorted((j['plot'], j['crew'], j['action'])
                           for j in board['jobs']))
        unique[key] = board
    return list(unique.values()), rejected


def _select(boards, strategy):
    if strategy == 'capacity':
        return min(boards, key=lambda b: (b['harvest_kg'],
                                          len(b['crews_used']),
                                          tuple(b['jobs'][i]['plot']
                                                for i in range(len(b['jobs'])))))
    return max(boards, key=lambda b: (b['harvest_kg'],
                                      -len(b['crews_used']),
                                      tuple(b['jobs'][i]['plot']
                                            for i in range(len(b['jobs'])))))


def _shortfall_analysis(plots, forecasts):
    """Why a plot can no longer promise its full premium quantity."""
    rows = []
    for plot in plots:
        frost = deadline(plot['garden'], forecasts)
        ready = _parse(plot['ready_at'])
        planned_finish = _parse(plot['finish_at'])
        if frost is not None and planned_finish >= _parse(frost):
            available = max(0, int((_parse(frost) - ready).total_seconds() // 60))
            full = int((planned_finish - ready).total_seconds() // 60)
            rows.append({'plot': plot['id'], 'garden': plot['garden'],
                         'frost_at': frost, 'ready_at': plot['ready_at'],
                         'usable_minutes': available,
                         'required_minutes': full,
                         'full_harvest_possible': False})
        else:
            rows.append({'plot': plot['id'], 'garden': plot['garden'],
                         'frost_at': frost, 'ready_at': plot['ready_at'],
                         'full_harvest_possible': True})
    return rows


def _normalize_context(context):
    """Accept either raw garden events or the pre-built cert/picker dicts."""
    ctx = dict(context or {})
    if 'garden_events' in ctx and 'certificates' not in ctx:
        certs, pickers, _ = garden_decide(ctx['garden_events'])
        ctx['certificates'], ctx['pickers'] = certs, pickers
    ctx.setdefault('certificates', {})
    ctx.setdefault('pickers', {})
    return ctx


def plan(plots, crews, forecasts, context=None, strategy='delivery',
         prior=None, generated_at=None):
    """Build the versioned global board.

    Returns every feasible route plus the selected route. Infeasibility is a
    first-class result: blocked plots, order shortfall and revoked promises
    are reported instead of silently scheduling work past the frost.
    """
    ctx = _normalize_context(context)
    if strategy not in STRATEGIES:
        raise ValueError(f'strategy must be one of {STRATEGIES}')
    boards, rejected = _enumerate(plots, crews, forecasts, ctx)
    order_kg = int(ctx.get('order_kg', 80))
    feasible = [b for b in boards if b['order_met']]
    windows = all_windows(forecasts,
                          gardens=sorted({p['garden'] for p in plots}))
    if generated_at is None:
        generated_at = min(p['ready_at'] for p in plots)
    version = (prior or {}).get('version', 0) + 1 if prior else 1

    if not feasible:
        selected = None
        feasible_kg = max((b['harvest_kg'] for b in boards), default=0)
        result = {
            'feasible': False, 'version': version,
            'generated_at': generated_at, 'strategy': strategy,
            'windows': windows,
            'frost_analysis': _shortfall_analysis(plots, forecasts),
            'rejections': rejected,
            'order_kg': order_kg,
            'max_harvest_kg': feasible_kg,
            'shortfall_kg': max(0, order_kg - feasible_kg),
            'alternatives': sorted(boards, key=lambda b: -b['harvest_kg']),
            'selected': None,
        }
    else:
        chosen = _select(feasible, strategy)
        result = {
            'feasible': True, 'version': version,
            'generated_at': generated_at, 'strategy': strategy,
            'windows': windows,
            'frost_analysis': _shortfall_analysis(plots, forecasts),
            'rejections': rejected,
            'order_kg': order_kg,
            'max_harvest_kg': max(b['harvest_kg'] for b in feasible),
            'shortfall_kg': 0,
            'alternatives': sorted(feasible, key=lambda b: -b['harvest_kg']),
            'selected': chosen,
        }

    notified, revoked = _diff_promises(prior, result)
    result['notifications'] = notified
    result['revoked_promises'] = revoked
    if result['selected']:
        result['covered_plots'] = result['selected']['covered_plots']
        result['harvest_kg'] = result['selected']['harvest_kg']
    else:
        result['covered_plots'] = sorted(
            j['plot'] for alt in result['alternatives']
            for j in alt['jobs'] if j['action'] == 'cover')
        result['harvest_kg'] = result['max_harvest_kg']
    return result


def _diff_promises(prior, current):
    """A changed window or authority produces a new version: crews get told
    and promises the new board cannot honour are explicitly revoked."""
    if not prior:
        return [], []
    prior_selected = prior.get('selected', prior)
    old_jobs = {j['plot']: j for j in prior_selected.get('jobs', [])}
    new = current['selected']
    new_jobs = {j['plot']: j for j in (new['jobs'] if new else [])}
    notified, revoked = [], []
    for plot, old in sorted(old_jobs.items()):
        new_job = new_jobs.get(plot)
        if new_job is None or (new_job['crew'], new_job['action'],
                               new_job['start'], new_job['finish']) != \
                (old['crew'], old['action'], old['start'], old['finish']):
            notified.append({'crew': old['crew'], 'plot': plot,
                             'version': current['version'],
                             'message': 'assignment_superseded'})
            if old['action'] == 'harvest':
                revoked.append({'plot': plot, 'crew': old['crew'],
                                'kg': old.get('kg', 0),
                                'version': prior.get('version', 0),
                                'reason': 'replanned'})
    if not current['feasible']:
        for plot, old in sorted(old_jobs.items()):
            if old['action'] == 'harvest' and \
                    not any(r['plot'] == plot for r in revoked):
                revoked.append({'plot': plot, 'crew': old['crew'],
                                'kg': old.get('kg', 0),
                                'version': prior.get('version', 0),
                                'reason': 'order_shortfall'})
    return notified, revoked


def replan(plots, crews, forecasts, context, prior, reason, strategy='delivery',
           generated_at=None):
    """Re-issue the board after a late forecast or authority revocation."""
    board = plan(plots, crews, forecasts, context=context, strategy=strategy,
                 prior=prior, generated_at=generated_at)
    before = prior.get('windows', {})
    board['change_reason'] = reason
    board['windows_moved'] = windows_changed(before, board['windows'])
    return board


def legacy_rows(plots, crews, forecasts):
    """Reproduces the old greedy board for seed/regression demonstrations."""
    rows = []
    for plot in plots:
        possible = [c for c in crews if plot['garden'] in c['gardens']]
        chosen = possible[0] if possible else None
        rows.append({'plot': plot['id'],
                     'crew': chosen['id'] if chosen else None,
                     'start': plot['ready_at'], 'finish': plot['finish_at'],
                     'frost_at': deadline(plot['garden'], forecasts)})
    return rows


def board_allows(board, garden, finish_at, forecasts):
    return allows(garden, finish_at, forecasts)
