import copy
import json
from pathlib import Path

import pytest

from teablend.frost_window import (
    deadline, window, allows, all_windows, windows_changed, is_credible)
from teablend.crew_plan import (
    plan, replan, legacy_rows, harvest_authorized, travel_minutes)
from teablend.harvest_capture import capture_day, capture_covered_plot

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'fixtures' / 'frost-harvest.json'


@pytest.fixture
def data():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


@pytest.fixture
def ctx(data):
    return {**data['context'], 'garden_events': data['garden_events']}


def _jobs(board):
    return {job['plot']: job for job in board['selected']['jobs']}


def _weight(wid, at, net, garden_sign='farmer1', factory_sign='factory'):
    return {'id': wid, 'at': at, 'net_kg': net,
            'garden_sign': garden_sign, 'factory_sign': factory_sign}


def _capture(cid, plot, crew, picker, ticket, picked, delivered, gross,
             weights, grade='premium'):
    return {'id': cid, 'plot': plot, 'crew': crew, 'picker': picker,
            'pick_ticket': ticket, 'picked_at': picked,
            'delivered_at': delivered, 'gross_kg': gross, 'grade': grade,
            'weights': weights}


# ---------------------------------------------------------------------------
# Seed: the old greedy board double-books C1; the new global board must not.
# ---------------------------------------------------------------------------

def test_seed_legacy_board_still_double_books_one_crew(data):
    rows = legacy_rows(data['plots'], data['crews'], data['forecasts'])
    assert [row['crew'] for row in rows] == ['C1', 'C1']
    assert rows[0]['finish'] > rows[1]['start']
    assert deadline('G1', data['forecasts']) == '2026-11-25T07:20:00'


def test_new_board_eliminates_seed_double_booking(data, ctx):
    board = plan(data['plots'], data['crews'], data['forecasts'], ctx)
    assert board['feasible']
    crews = [job['crew'] for job in board['selected']['jobs']]
    assert len(crews) == len(set(crews))
    for rejection in board['rejections']:
        if rejection['reason'] == 'crew_overlap':
            return
    raise AssertionError('overlap rejection should explain why C1 cannot take both')


def test_conservative_frost_window_ignores_optimistic_and_unverified(data):
    assert deadline('G1', data['forecasts']) == '2026-11-25T07:20:00'
    g2 = window('G2', data['forecasts'])
    # earliest credible bound wins; the later optimistic forecast does not
    # move the deadline and the unverified 07:10 warning is excluded entirely.
    assert g2['deadline'] == '2026-11-25T07:45:00'
    assert g2['bounds'] == ['2026-11-25T07:45:00', '2026-11-25T08:05:00']
    assert 'crowdsourced' in g2['ignored_sources']
    assert allows('G2', '2026-11-25T07:40:00', data['forecasts'])
    assert not allows('G2', '2026-11-25T07:45:00', data['forecasts'])
    noisy = next(f for f in data['forecasts'] if f['id'] == 'F-G2-NOISY')
    assert not is_credible(noisy)


def test_whitelist_alone_cannot_dispatch_when_authority_missing(data, ctx):
    from datetime import datetime
    broken = copy.deepcopy(ctx)
    broken['garden_events'] = [
        e for e in broken['garden_events'] if e['kind'] != 'certificate']
    plot = data['plots'][0]
    crew = data['crews'][0]
    ok, reason = harvest_authorized(
        crew, plot, datetime.fromisoformat(plot['ready_at']),
        datetime.fromisoformat(plot['finish_at']), broken)
    assert not ok and reason == 'certificate_missing'


# ---------------------------------------------------------------------------
# Both A (harvest both) and B (harvest P1, cover P2) are valid routes.
# ---------------------------------------------------------------------------

def test_route_a_and_route_b_are_both_feasible(data, ctx):
    board_a = plan(data['plots'], data['crews'], data['forecasts'],
                   ctx, strategy='delivery')
    board_b = plan(data['plots'], data['crews'], data['forecasts'],
                   ctx, strategy='capacity')

    jobs_a, jobs_b = _jobs(board_a), _jobs(board_b)
    assert jobs_a['P1']['action'] == 'harvest'
    assert jobs_a['P2']['action'] == 'harvest'
    assert board_a['selected']['harvest_kg'] == 120
    assert board_a['selected']['crews_used'] == ['C1', 'C2']

    assert jobs_b['P1']['action'] == 'harvest'
    assert jobs_b['P2']['action'] == 'cover'
    assert board_b['selected']['harvest_kg'] == 80
    assert board_b['selected']['covered_plots'] == ['P2']
    # nothing pre-borrowed: covered plot contributes zero kg today
    assert all(j['kg'] == 0 for j in jobs_b.values() if j['action'] == 'cover')

    for board in (board_a, board_b):
        assert board['selected']['order_met']
        for job in board['selected']['jobs']:
            assert allows(job['garden'], job['finish'], data['forecasts'])
    # the board does not fix one route: both appear as explicit alternatives
    harvest_totals = {alt['harvest_kg'] for alt in board_a['alternatives']}
    assert harvest_totals == {80, 120}


def test_every_assignment_beats_each_credible_bound_with_travel_and_sanitation(data, ctx):
    board = plan(data['plots'], data['crews'], data['forecasts'], ctx)
    for job in board['selected']['jobs']:
        for bound in window(job['garden'], data['forecasts'])['bounds']:
            assert job['finish'] < bound
    # cross-garden deadhead plus sanitation must separate two jobs of one crew
    c2 = next(c for c in data['crews'] if c['id'] == 'C2')
    assert travel_minutes(c2, 'G1', 'G2') == 45
    assert ctx['sanitation_minutes'] == 15


# ---------------------------------------------------------------------------
# A frost brought forward to 06:30 changes committable kg, not just a label.
# ---------------------------------------------------------------------------

def test_forecast_brought_forward_replans_and_revokes_promises(data, ctx):
    prior = plan(data['plots'], data['crews'], data['forecasts'],
                 ctx, strategy='delivery')
    early = copy.deepcopy(data['forecasts'])
    early.append({'id': 'F-G1-EARLY', 'source': 'met-bureau', 'garden': 'G1',
                  'frost_at': '2026-11-25T06:30:00',
                  'status': 'credible', 'trusted': True})

    board = replan(data['plots'], data['crews'], early, ctx, prior=prior,
                   reason='frost_forecast_brought_forward',
                   generated_at='2026-11-25T04:40:00')

    assert deadline('G1', early) == '2026-11-25T06:30:00'
    assert board['version'] == prior['version'] + 1
    assert board['windows_moved'] == ['G1']
    # P1 needs 120 minutes but only 90 remain: the full 80 kg cannot be
    # promised, and the best board still falls short of the customer order.
    p1 = next(row for row in board['frost_analysis'] if row['plot'] == 'P1')
    assert p1['usable_minutes'] == 90 and not p1['full_harvest_possible']
    assert not board['feasible']
    assert board['max_harvest_kg'] == 40
    assert board['shortfall_kg'] == 40
    best = board['alternatives'][0]
    assert next(j for j in best['jobs'] if j['plot'] == 'P1')['action'] == 'cover'
    # crews are notified and the old promises are explicitly revoked
    notified_plots = {n['plot'] for n in board['notifications']}
    assert {'P1', 'P2'} <= notified_plots
    revoked = {r['plot']: r for r in board['revoked_promises']}
    assert revoked['P1']['kg'] == 80 and revoked['P1']['reason'] == 'replanned'

    before = all_windows(data['forecasts'], ['G1', 'G2'])
    after = all_windows(early, ['G1', 'G2'])
    assert windows_changed(before, after) == ['G1']


# ---------------------------------------------------------------------------
# Revoked picking authority: harvest blocked even though the whitelist allows.
# ---------------------------------------------------------------------------

def test_authority_revocation_forces_cover_and_issues_new_version(data, ctx):
    prior = plan(data['plots'], data['crews'], data['forecasts'],
                 ctx, strategy='delivery')
    events = copy.deepcopy(data['garden_events'])
    events.append({
        'id': 'CERT-G2-V2', 'kind': 'certificate',
        'at': '2026-11-25T04:30:00', 'garden': 'G2', 'variety': 'Longjing',
        'version': 2, 'start': '2026-11-25T04:30:00',
        'finish': '2026-12-01T00:00:00', 'organic': False,
        'status': 'revoked'})
    revoked_ctx = {**ctx, 'garden_events': events}

    board = replan(data['plots'], data['crews'], data['forecasts'],
                   revoked_ctx, prior=prior,
                   reason='picking_authority_revoked',
                   generated_at='2026-11-25T04:45:00')

    assert board['feasible'] and board['version'] == prior['version'] + 1
    jobs = _jobs(board)
    assert jobs['P1']['action'] == 'harvest'
    assert jobs['P2']['action'] == 'cover'
    assert board['selected']['harvest_kg'] == 80
    g2_harvest_blocks = [r for r in board['rejections']
                         if r['plot'] == 'P2' and r['action'] == 'harvest']
    assert g2_harvest_blocks and all(
        r['reason'] == 'certificate_missing' for r in g2_harvest_blocks)
    assert any(n['plot'] == 'P2' for n in board['notifications'])
    revoked_promise = next(r for r in board['revoked_promises']
                           if r['plot'] == 'P2')
    assert revoked_promise['kg'] == 40


# ---------------------------------------------------------------------------
# Actual leaf capture feeds the leaf.py dual-sign / downgrade adjudication.
# ---------------------------------------------------------------------------

def _route_a_captures():
    return [
        _capture('CAP-P1', 'P1', 'C1', 'PK-C1-G1', 'T-P1',
                 '2026-11-25T06:30:00', '2026-11-25T08:30:00', 80,
                 [_weight('W-P1', '2026-11-25T08:40:00', 78)]),
        _capture('CAP-P2', 'P2', 'C2', 'PK-C2-G2', 'T-P2',
                 '2026-11-25T07:00:00', '2026-11-25T09:00:00', 40,
                 [_weight('W-P2', '2026-11-25T09:10:00', 39,
                          garden_sign='farmer2')]),
    ]


def test_dual_signed_timely_net_weight_counts_toward_80kg(data, ctx):
    board = plan(data['plots'], data['crews'], data['forecasts'],
                 ctx, strategy='delivery')
    result = capture_day(board, _route_a_captures(), data['garden_events'])
    assert result['feasible']
    assert result['premium_kg'] == 117
    assert {line['status'] for line in result['accepted']} == {'eligible'}
    assert result['shortfall_kg'] == 0


def test_missing_factory_signature_does_not_count(data, ctx):
    board = plan(data['plots'], data['crews'], data['forecasts'],
                 ctx, strategy='delivery')
    captures = _route_a_captures()
    captures[1]['weights'][0]['factory_sign'] = ''
    result = capture_day(board, captures, data['garden_events'])
    assert not result['feasible']
    assert result['premium_kg'] == 78
    pending = next(p for p in result['pending'] if p['plot'] == 'P2')
    assert pending['status'] == 'weight_unverified'


def test_frost_damaged_and_unpicked_plots_never_prebook_premium(data, ctx):
    board = plan(data['plots'], data['crews'], data['forecasts'],
                 ctx, strategy='capacity')
    # same-day attempt against the covered plot is refused, not signed off
    sneak = [_capture('CAP-P2', 'P2', 'C2', 'PK-C2-G2', 'T-P2-X',
                      '2026-11-25T07:00:00', '2026-11-25T09:00:00', 40,
                      [_weight('W-P2-X', '2026-11-25T09:10:00', 39,
                               garden_sign='farmer2')])]
    refused = capture_day(board, sneak, data['garden_events'])
    assert refused['accepted'] == []
    assert refused['rejected'][0]['reason'] == 'plot_covered_not_harvested'
    assert refused['premium_kg'] == 0

    # frost-damaged leaf from a harvest plot is likewise excluded
    damaged = [_capture('CAP-P1-D', 'P1', 'C1', 'PK-C1-G1', 'T-P1-D',
                        '2026-11-25T06:30:00', '2026-11-25T08:30:00', 80,
                        [_weight('W-P1-D', '2026-11-25T08:40:00', 78)])]
    damaged[0]['frost_damaged'] = True
    hit = capture_day(board, damaged, data['garden_events'])
    assert hit['rejected'][0]['reason'] == 'frost_damaged_leaf'
    assert hit['premium_kg'] == 0


def test_plan_to_handoff_is_one_traceable_joint_commitment(data, ctx):
    board = plan(data['plots'], data['crews'], data['forecasts'],
                 ctx, strategy='delivery')
    captures = _route_a_captures()
    result = capture_day(board, captures, data['garden_events'])

    # every accepted receipt traces back to a board job through its plot
    jobs = _jobs(board)
    for line in result['accepted']:
        assert jobs[line['plot']]['action'] == 'harvest'
        assert line['garden'] == jobs[line['plot']]['garden']
    # leaf.py adjudication is embedded, not a parallel shadow ledger
    assert result['decisions']['receipt:R:CAP-P1']['status'] == 'eligible'
    assert result['decisions']['weight:W:W-P1']['status'] == 'dual_signed'
    # the day's promise is settled by net weight actually inside the rules
    assert result['premium_kg'] >= board['order_kg']
    assert result['next_day_todo'] == []


def test_covered_plot_is_reassessed_next_day_and_delay_regrades(data, ctx):
    board = plan(data['plots'], data['crews'], data['forecasts'],
                 ctx, strategy='capacity')
    assert board['selected']['harvest_kg'] == 80

    # same-day picking of a covered plot is blocked by the capture guard
    same_day = [_capture('CAP-P2-EARLY', 'P2', 'C2', 'PK-C2-G2', 'T-EARLY',
                         '2026-11-25T22:00:00', '2026-11-25T23:00:00', 38,
                         [_weight('W-EARLY', '2026-11-25T23:10:00', 37,
                                  garden_sign='farmer2')])]
    blocked = capture_covered_plot(board, 'P2', same_day,
                                   data['garden_events'])
    assert blocked['rejected'][0]['reason'] == 'cover_holds_until_next_day'

    next_day = [_capture('CAP-P2-ND', 'P2', 'C2', 'PK-C2-G2', 'T-P2-ND',
                         '2026-11-26T05:30:00', '2026-11-26T14:00:00', 38,
                         [_weight('W-P2-ND', '2026-11-26T14:10:00', 37,
                                  garden_sign='farmer2')])]
    slow = capture_covered_plot(board, 'P2', next_day,
                                data['garden_events'])
    line = slow['lines'][0]
    assert line['status'] == 'eligible'
    assert line['grade'] == 'standard'  # 8.5 h handoff delay regrades premium
    assert line['net_kg'] == 37

    fast_payload = copy.deepcopy(next_day)
    fast_payload[0].update(id='CAP-P2-ND2', pick_ticket='T-P2-ND2',
                           delivered_at='2026-11-26T07:30:00')
    fast_payload[0]['weights'] = [
        dict(next_day[0]['weights'][0], id='W-P2-ND2',
             at='2026-11-26T07:40:00')]
    fast = capture_covered_plot(board, 'P2', fast_payload,
                                data['garden_events'])
    assert fast['lines'][0]['grade'] == 'premium'
    assert fast['lines'][0]['delay_hours'] == 2.0
