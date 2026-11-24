"""End-to-end tests for the traceable per-plot pest intervention.

Covers the five required evidence points:
1. the seed rule still emits an unsafe spray recommendation while the hard
   execution gate refuses at 5 m/s towards organic G2;
2. both legal routes (A: targeted traps + day-3 recheck; B: next-day field
   spray once wind, buffer and drift conditions hold);
3. a field wind change invalidates route B — forecasts are never permits;
4. a failed trap recheck must not be reported as pest control;
5. G2 keeps an independent state and never inherits G1's picking block.
"""
import json
from pathlib import Path

import pytest

from teablend.pest_capture import PestIntervention
from teablend.pest_monitor import diagnose_plot, pressure
from teablend.spray_gate import (
    WIND_SOURCE_FIELD,
    WIND_SOURCE_FORECAST,
    WindReading,
    decide,
)

FIXTURE = Path(__file__).parents[1] / 'fixtures' / 'pest-intervention.json'


def load_fixture():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


def quiet_plot(data):
    plot = dict(data['plot'])
    plot['trap_counts'] = [1, 1]
    plot['traps'] = [
        {**t, 'count': 1, 'count_error_margin': 0, 'offline_intervals': []}
        for t in plot['traps']
    ]
    quiet = dict(data)
    quiet['plot'] = plot
    return quiet


# ------------------------------------------------------------- 1. seed is unsafe
def test_seed_mean_still_recommends_spray_but_execution_gate_refuses():
    data = load_fixture()
    # Legacy seed behavior is preserved for the regression.
    assert pressure(data['plot']) == 6.5
    assert decide(data['plot']) == 'spray'
    wind_limit = data['treatments']['approved_spray']['wind_limit_mps']
    assert data['weather'][0]['wind_toward_G2_mps'] > wind_limit

    # The same situation is not executable under the hard constraints.
    intervention = PestIntervention(data)
    field_wind_day0 = WindReading(day=0, speed_mps=5, toward_organic=True,
                                  source=WIND_SOURCE_FIELD, note='nozzle anemometer')
    result = intervention.request_spray(0, field_wind_day0)
    assert result['executed'] is False
    assert any('exceeds permit limit' in r for r in result['decision'].reasons)
    assert intervention.g1_harvest_block_active is False
    assert intervention.g2_status['exposure'] == 'none'

    # Lowering both catches removes the hotspot and the seed recommendation.
    quiet = quiet_plot(data)
    assert decide(quiet['plot']) == 'monitor'
    assert diagnose_plot(quiet['plot'])['hotspot_ids'] == []


def test_diagnosis_keeps_every_trap_point_and_spatial_hotspot():
    data = load_fixture()
    diagnosis = diagnose_plot(data['plot'])
    # The 12-catch trap must not be averaged away by the 1-catch trap.
    assert [p['count'] for p in diagnosis['points']] == [12, 1]
    hotspot = diagnosis['points'][0]
    assert hotspot['is_hotspot'] is True
    assert hotspot['offline_intervals'] and hotspot['effective_work_days'] == 7
    assert diagnosis['points'][1]['is_hotspot'] is False
    assert diagnosis['hotspot_ids'] == ['MT-001']
    zone = diagnosis['coverage_zones'][0]
    assert zone['anchor_trap_id'] == 'MT-001'
    assert zone['radius_metres'] == 15.0


def test_counting_error_cannot_erase_hotspot():
    data = load_fixture()
    plot = dict(data['plot'])
    plot['traps'] = [{**plot['traps'][0], 'count': 6, 'count_error_margin': 1},
                     plot['traps'][1]]
    plot['trap_counts'] = [6, 1]
    diagnosis = diagnose_plot(plot)
    assert diagnosis['hotspot_ids'] == ['MT-001']


# ------------------------------------------------------------- 2a. route A: traps
def test_route_a_deploys_20_traps_and_recheck_confirms_control():
    data = load_fixture()
    intervention = PestIntervention(data)

    deployment = intervention.deploy_traps(day=0)
    assert deployment['deployed_units'] == 20
    assert intervention.trap_inventory_remaining == 0
    serials = [t['serial'] for t in deployment['traps']]
    assert serials[0] == 'PT-001' and serials[-1] == 'PT-020'
    assert all(t['anchor_hotspot_id'] == 'MT-001' for t in deployment['traps'])
    # Every placement point is stored with its own coordinates and serial.
    assert len({(t['x_metres'], t['y_metres']) for t in deployment['traps']}) == 20

    # No chemical block on G1, no drift exposure for G2, control unverified.
    assert intervention.g1_harvest_block_active is False
    assert intervention.g2_status['exposure'] == 'none'
    assert intervention.control_status == 'unverified'

    # Recheck before the 3-day interval is rejected.
    early = {'MT-001': 1, **{s: 0 for s in serials}}
    with pytest.raises(ValueError):
        intervention.recheck_traps(day=2, per_point_counts=early)

    counts = {'MT-001': 2, **{s: 0 for s in serials}}
    result = intervention.recheck_traps(day=3, per_point_counts=counts)
    assert result.controlled is True
    assert result.anchor_baseline == 12 and result.anchor_count == 2
    assert intervention.control_status == 'controlled'
    assert intervention.g1_harvest_block_active is False
    assert intervention.g2_status['picking_blocked'] is False

    # Retrieval returns the units but leaves the physical history on the ledger.
    intervention.retrieve_traps(day=4, serials=serials)
    assert intervention.trap_inventory_remaining == 20
    assert any(e.type == 'trap_deployment' for e in intervention.events)
    assert any(e.type == 'trap_retrieval' for e in intervention.events)


# ------------------------------------------------------------- 4. failed recheck
def test_route_a_failed_recheck_does_not_count_as_control():
    data = load_fixture()
    intervention = PestIntervention(data)
    deployment = intervention.deploy_traps(day=0)
    serials = [t['serial'] for t in deployment['traps']]

    # Counts did not fall at the anchor: deploying traps is not control.
    failed = {'MT-001': 12, **{s: 1 for s in serials}}
    result = intervention.recheck_traps(day=3, per_point_counts=failed)
    assert result.controlled is False
    assert intervention.control_status == 'not_controlled_escalation_required'
    assert any('did not fall below baseline' in r for r in result.reasons)

    # A point still above the trigger also fails, even if the anchor dropped.
    other = PestIntervention(data)
    other.deploy_traps(day=0)
    still_hot = {'MT-001': 2, **{s: 0 for s in serials}}
    still_hot['PT-010'] = 8
    result_b = other.recheck_traps(day=3, per_point_counts=still_hot)
    assert result_b.controlled is False
    assert any('PT-010' in r for r in result_b.reasons)

    # A missing point makes the recheck impossible rather than assumed good.
    third = PestIntervention(data)
    third.deploy_traps(day=0)
    incomplete = {'MT-001': 2, **{s: 0 for s in serials if s != 'PT-007'}}
    with pytest.raises(ValueError):
        third.recheck_traps(day=3, per_point_counts=incomplete)


# ------------------------------------------------------------- 2b. route B: spray
def test_route_b_waits_for_day1_field_conditions_and_sets_g1_block():
    data = load_fixture()
    intervention = PestIntervention(data)

    # Day 0: forecast alone never authorizes, and the real 5 m/s field wind
    # is refused even though the seed rule says spray.
    forecast = WindReading(day=0, speed_mps=1, toward_organic=True,
                           source=WIND_SOURCE_FORECAST)
    assert intervention.request_spray(0, forecast)['executed'] is False
    windy = WindReading(day=0, speed_mps=5, toward_organic=True, source=WIND_SOURCE_FIELD)
    assert intervention.request_spray(0, windy)['executed'] is False
    assert intervention.g1_spray_day is None

    # Day 1: field receipt at 1 m/s, 18 m distance and 2 m drift reach leave
    # an effective 16 m buffer >= the required 15 m.
    calm = WindReading(day=1, speed_mps=1, toward_organic=True, source=WIND_SOURCE_FIELD,
                       note='next-day nozzle anemometer receipt')
    result = intervention.request_spray(1, calm, equipment_drift_reach_metres=2.0)
    assert result['executed'] is True
    assert result['decision'].effective_buffer_metres == 16.0
    assert intervention.g1_spray_day == 1
    # Picking no earlier than spray day + 14.
    assert intervention.g1_no_pick_before_day == 15
    assert intervention.g1_pickable_on(14) is False
    assert intervention.g1_pickable_on(15) is True

    # G2 gets an independent drift task, never a picking block.
    g2 = intervention.g2_status
    assert g2['exposure'] == 'downwind_observation'
    assert g2['drift_task']['status'] == 'pending_verification'
    assert g2['picking_blocked'] is False
    outcome = intervention.verify_g2_drift(day=2, detected=False, note='leaf swabs clean')
    assert outcome['status'] == 'no_drift_detected'
    assert intervention.g2_status['picking_blocked'] is False


def test_buffer_and_drift_boundary_are_enforced_point_by_point():
    data = load_fixture()
    # 18 m distance minus 4 m drift reach = 14 m effective buffer < 15 m.
    intervention = PestIntervention(data)
    calm = WindReading(day=1, speed_mps=1, toward_organic=True, source=WIND_SOURCE_FIELD)
    result = intervention.request_spray(1, calm, equipment_drift_reach_metres=4.0)
    assert result['executed'] is False
    assert any('below required 15 m' in r for r in result['decision'].reasons)

    # Wind not blowing towards G2: no G2 buffer constraint applies at all.
    other = PestIntervention(data)
    away = WindReading(day=1, speed_mps=1.5, toward_organic=False, source=WIND_SOURCE_FIELD)
    ok = other.request_spray(1, away, equipment_drift_reach_metres=4.0)
    assert ok['executed'] is True
    assert ok['decision'].effective_buffer_metres is None
    assert other.g2_status['drift_task']['wind_toward_g2'] is False


# ------------------------------------------------------------- 3. field wind change
def test_forecast_is_not_a_permit_when_field_wind_changes():
    data = load_fixture()
    intervention = PestIntervention(data)

    # The fixture forecasts 1 m/s on day 1, but the nozzle reads 3 m/s.
    forecast_events = [e for e in intervention.events
                       if e.type == 'wind_recorded'
                       and e.payload['source'] == WIND_SOURCE_FORECAST]
    assert len(forecast_events) == 2

    changed = WindReading(day=1, speed_mps=3, toward_organic=True, source=WIND_SOURCE_FIELD,
                          note='field gust above forecast')
    intervention.record_wind(changed)
    result = intervention.request_spray(1, changed, equipment_drift_reach_metres=2.0)
    assert result['executed'] is False
    assert any('exceeds permit limit' in r for r in result['decision'].reasons)
    assert intervention.g1_harvest_block_active is False

    # A late field receipt arriving afterwards is appended, not overwritten.
    intervention.record_wind(
        WindReading(day=1, speed_mps=1, toward_organic=True, source=WIND_SOURCE_FIELD,
                    note='late corrected receipt after gust subsided')
    )
    latest = intervention.latest_field_wind(1)
    assert latest.speed_mps == 1
    assert [e.payload['speed_mps'] for e in intervention.events
            if e.type == 'wind_recorded' and e.day == 1] == [1, 3, 1]


# ------------------------------------------------------------- 5. G2 independence
def test_g2_never_inherits_g1_picking_block():
    data = load_fixture()
    route_a = PestIntervention(data)
    route_a.deploy_traps(day=0)
    assert route_a.g1_harvest_block_active is False
    assert route_a.g2_status == {
        'garden': 'G2', 'exposure': 'none', 'drift_task': None, 'picking_blocked': False
    }

    route_b = PestIntervention(data)
    calm = WindReading(day=1, speed_mps=1, toward_organic=True, source=WIND_SOURCE_FIELD)
    route_b.request_spray(1, calm, equipment_drift_reach_metres=2.0)
    assert route_b.g1_no_pick_before_day == 15
    g2 = route_b.g2_status
    assert g2['garden'] == 'G2'
    assert g2['picking_blocked'] is False
    # Even confirmed drift is recorded as G2's own finding, not a G1-style block.
    route_b.verify_g2_drift(day=2, detected=True, note='edge residue found')
    assert route_b.g2_status['drift_task']['status'] == 'drift_detected'
    assert route_b.g2_status['picking_blocked'] is False


# ------------------------------------------------------------- hard constraint 4
def test_revocation_and_late_records_never_erase_physical_history():
    data = load_fixture()
    intervention = PestIntervention(data)
    calm = WindReading(day=1, speed_mps=1, toward_organic=True, source=WIND_SOURCE_FIELD)
    intervention.request_spray(1, calm, equipment_drift_reach_metres=2.0)
    executed_seq = [e.seq for e in intervention.events if e.type == 'spray_executed']
    assert executed_seq

    # Batch revoked afterwards: the spray that happened stays, later spray blocked.
    intervention.revoke_batch(day=3, batch_id='BATCH-001', reason='quality withdrawal')
    again = intervention.request_spray(
        4, WindReading(day=4, speed_mps=1, toward_organic=True, source=WIND_SOURCE_FIELD)
    )
    assert again['executed'] is False
    assert any('revoked' in r for r in again['decision'].reasons)
    assert intervention.g1_spray_day == 1
    assert intervention.g1_no_pick_before_day == 15
    types = [e.type for e in intervention.events]
    assert types.count('spray_executed') == 1
    assert 'batch_revoked' in types and 'g2_drift_observation_opened' in types
    # The ledger is strictly append-only: sequence numbers never repeat.
    assert [e.seq for e in intervention.events] == list(range(1, len(intervention.events) + 1))


# ------------------------------------------------------------- routes differ
def test_two_routes_differ_in_materials_workdays_and_state():
    data = load_fixture()

    route_a = PestIntervention(data)
    deployment = route_a.deploy_traps(day=0)
    serials = [t['serial'] for t in deployment['traps']]
    route_a.recheck_traps(day=3, per_point_counts={'MT-001': 2, **{s: 0 for s in serials}})

    route_b = PestIntervention(data)
    route_b.request_spray(
        1,
        WindReading(day=1, speed_mps=1, toward_organic=True, source=WIND_SOURCE_FIELD),
        equipment_drift_reach_metres=2.0,
    )

    assert route_a.trap_inventory_remaining == 0
    assert route_b.trap_inventory_remaining == 20
    assert route_a.work_days == (0, 3)
    assert route_b.work_days == (1,)
    assert route_a.g1_no_pick_before_day is None
    assert route_b.g1_no_pick_before_day == 15
    assert route_a.g2_status['exposure'] == 'none'
    assert route_b.g2_status['exposure'] == 'downwind_observation'
    assert route_a.control_status == 'controlled'
    assert route_b.control_status == 'unverified'
