"""Traceable per-plot pest intervention ledger.

This module turns the point-level diagnosis and the spray execution gate into
one auditable intervention process:

* the event ledger is append-only — a late wind record, a batch revocation or
  a trap retrieval appends new facts and recomputes downstream state, but a
  physical spray or trap deployment that already happened (and the G2
  notification it caused) can never be erased by editing a latest status;
* pheromone traps are physically placed by serial around a named hotspot and
  re-checked point by point after the required follow-up interval — having
  deployed traps is never reported as pest control until counts actually fall;
* an executed spray blocks G1 picking for at least the permit's pre-harvest
  interval, while G2 never inherits that block — G2 only carries an
  independent downwind drift-exposure observation task.
"""
import math
from dataclasses import dataclass, replace

from .pest_monitor import HOTSPOT_TRIGGER, diagnose_plot
from .spray_gate import (
    WIND_SOURCE_FIELD,
    WIND_SOURCE_FORECAST,
    SprayRequest,
    WindReading,
    evaluate_application,
)

DEFAULT_PRODUCT_ID = 'PX-APPROVED'
DEFAULT_BATCH_ID = 'BATCH-001'


@dataclass(frozen=True)
class InterventionEvent:
    seq: int
    day: int
    type: str
    payload: dict


@dataclass(frozen=True)
class DeployedTrap:
    serial: str
    anchor_hotspot_id: str
    x_metres: float
    y_metres: float
    deployed_day: int
    status: str = 'active'  # active | retrieved
    retrieved_day: int | None = None


@dataclass(frozen=True)
class RecheckResult:
    day: int
    controlled: bool
    anchor_trap_id: str
    anchor_baseline: int
    anchor_count: int
    hotspot_threshold: int
    reasons: tuple
    per_point: dict


class PestIntervention:
    """Append-only intervention state machine for one G1 plot next to G2."""

    def __init__(self, fixture, product_id=DEFAULT_PRODUCT_ID, batch_id=DEFAULT_BATCH_ID):
        self._fixture = fixture
        plot = fixture['plot']
        self._plot_id = plot['id']
        self._garden = plot.get('garden')
        self._organic_garden = plot['adjacent_organic_garden']
        self._distance = plot['distance_to_G2_metres']
        self._treatment = fixture['treatments']
        self._product_id = product_id
        self._batch_id = batch_id
        self._approved_products = {
            product_id: {
                'wind_limit_mps': self._treatment['approved_spray']['wind_limit_mps'],
                'organic_buffer_metres': self._treatment['approved_spray']['organic_buffer_metres'],
                'preharvest_days': self._treatment['approved_spray']['preharvest_days'],
            }
        }
        self._events = []
        self._seq = 0
        self._revoked_batches = set()
        self._traps_inventory = self._treatment['pheromone_traps']['units']
        self._deployed = {}          # serial -> DeployedTrap
        self._deployment_day = None
        self._anchor = None
        self._recheck = None
        self._spray_day = None
        self._g1_block_days = None
        self._g2_drift_task = None
        self._work_days = set()
        # Forecasts are recorded as facts but never authorize execution.
        for w in fixture.get('weather', []):
            self.record_wind(
                WindReading(
                    day=w['day'],
                    speed_mps=w['wind_toward_G2_mps'],
                    toward_organic=True,
                    source=WIND_SOURCE_FORECAST,
                    note='weather forecast loaded from fixture',
                ),
                log_work=False,
            )

    # ------------------------------------------------------------------ ledger
    def _append(self, day, event_type, payload):
        self._seq += 1
        event = InterventionEvent(self._seq, day, event_type, payload)
        self._events.append(event)
        return event

    @property
    def events(self):
        return tuple(self._events)

    @property
    def diagnosis(self):
        return diagnose_plot(self._fixture['plot'])

    # ------------------------------------------------------------- route A: traps
    def deploy_traps(self, day, units=None):
        """Place the available pheromone traps around the diagnosed hotspot."""
        if self._deployed:
            raise ValueError('a trap deployment is already active for this plot')
        diagnosis = self.diagnosis
        if not diagnosis['hotspot_ids']:
            raise ValueError('no diagnosed hotspot to target; deployment is not justified')
        available = self._treatment['pheromone_traps']['units']
        units = available if units is None else units
        if units > available:
            raise ValueError(f'requested {units} traps but only {available} are available')

        anchor_id = diagnosis['hotspot_ids'][0]
        anchor = next(p for p in diagnosis['points'] if p['id'] == anchor_id)
        radius = diagnosis['coverage_zones'][0]['radius_metres']
        placed = []
        for i in range(units):
            serial = f'PT-{i + 1:03d}'
            angle = round(360.0 * i / units, 1)
            trap = DeployedTrap(
                serial=serial,
                anchor_hotspot_id=anchor_id,
                x_metres=round(anchor['x_metres'] + radius * math.cos(math.radians(angle)), 2),
                y_metres=round(anchor['y_metres'] + radius * math.sin(math.radians(angle)), 2),
                deployed_day=day,
            )
            self._deployed[serial] = trap
            placed.append(
                {
                    'serial': serial,
                    'anchor_hotspot_id': anchor_id,
                    'x_metres': trap.x_metres,
                    'y_metres': trap.y_metres,
                    'deployed_day': day,
                }
            )

        self._traps_inventory -= units
        self._deployment_day = day
        self._anchor = anchor
        self._work_days.add(day)
        self._append(
            day,
            'trap_deployment',
            {
                'units': units,
                'anchor_hotspot_id': anchor_id,
                'plot_id': self._plot_id,
                'traps': placed,
            },
        )
        return {
            'deployed_units': units,
            'inventory_remaining': self._traps_inventory,
            'anchor_hotspot_id': anchor_id,
            'traps': placed,
        }

    def recheck_traps(self, day, per_point_counts):
        """Point-by-point follow-up after the required interval.

        Control is claimed only when every deployed point is below the hotspot
        trigger and the anchor trap actually falls versus its baseline count.
        """
        if not self._deployed:
            raise ValueError('no active trap deployment to re-check')
        followup = self._treatment['pheromone_traps']['followup_days']
        if day < self._deployment_day + followup:
            raise ValueError(
                f'recheck on day {day} is before the required {followup}-day follow-up'
            )
        missing = sorted(set(self._deployed) - set(per_point_counts))
        if missing:
            raise ValueError(f'recheck misses per-point counts for: {missing}')

        reasons = []
        threshold = HOTSPOT_TRIGGER
        anchor_id = self._anchor['id']
        if anchor_id not in per_point_counts:
            raise ValueError(f'recheck must include the anchor hotspot trap {anchor_id}')
        anchor_count = per_point_counts[anchor_id]
        if anchor_count >= self._anchor['count']:
            reasons.append(
                f"anchor {anchor_id} count {anchor_count} did not fall below baseline "
                f"{self._anchor['count']}"
            )
        elif anchor_count >= threshold:
            reasons.append(
                f"anchor {anchor_id} count {anchor_count} still reaches hotspot trigger {threshold}"
            )
        for serial, count in per_point_counts.items():
            if serial in self._deployed and count >= threshold:
                reasons.append(f'deployed trap {serial} still counts {count} >= {threshold}')

        controlled = not reasons
        if controlled:
            reasons = (
                f'all {len(per_point_counts)} points below trigger {threshold}; '
                f'anchor fell from {self._anchor["count"]} to {anchor_count}',
            )
        result = RecheckResult(
            day=day,
            controlled=controlled,
            anchor_trap_id=anchor_id,
            anchor_baseline=self._anchor['count'],
            anchor_count=anchor_count,
            hotspot_threshold=threshold,
            reasons=tuple(reasons),
            per_point=dict(per_point_counts),
        )
        self._recheck = result
        self._work_days.add(day)
        self._append(
            day,
            'trap_recheck',
            {
                'controlled': controlled,
                'anchor_trap_id': anchor_id,
                'anchor_baseline': self._anchor['count'],
                'anchor_count': anchor_count,
                'per_point_counts': dict(per_point_counts),
                'reasons': list(reasons),
            },
        )
        return result

    def retrieve_traps(self, day, serials):
        """Physically retrieve deployed traps; the deployment history remains."""
        serials = list(serials)
        unknown = [s for s in serials if s not in self._deployed]
        if unknown:
            raise ValueError(f'not deployed on this plot: {unknown}')
        for serial in serials:
            self._deployed[serial] = replace(
                self._deployed[serial], status='retrieved', retrieved_day=day
            )
        self._traps_inventory += len(serials)
        self._work_days.add(day)
        self._append(
            day,
            'trap_retrieval',
            {'serials': serials, 'returned_to_inventory': len(serials)},
        )
        if all(t.status == 'retrieved' for t in self._deployed.values()):
            # The recheck evidence stays on the ledger; only the active session ends.
            self._deployment_day = None
        return {'retrieved': serials, 'inventory_remaining': self._traps_inventory}

    # ------------------------------------------------------------- wind evidence
    def record_wind(self, wind, log_work=True):
        """Append a wind observation (forecast or late field receipt)."""
        event = self._append(
            wind.day,
            'wind_recorded',
            {
                'speed_mps': wind.speed_mps,
                'toward_organic': wind.toward_organic,
                'source': wind.source,
                'note': wind.note,
            },
        )
        if log_work:
            self._work_days.add(wind.day)
        return event

    def latest_field_wind(self, day):
        field_winds = [
            e for e in self._events
            if e.type == 'wind_recorded'
            and e.payload['source'] == WIND_SOURCE_FIELD
            and e.day == day
        ]
        if not field_winds:
            return None
        payload = field_winds[-1].payload
        return WindReading(
            day=day,
            speed_mps=payload['speed_mps'],
            toward_organic=payload['toward_organic'],
            source=WIND_SOURCE_FIELD,
            note=payload.get('note', ''),
        )

    # ------------------------------------------------------------- route B: spray
    def request_spray(self, day, wind, equipment_drift_reach_metres=0.0):
        """Run the execution gate and, only if permitted, record a real spray."""
        request = SprayRequest(
            plot_id=self._plot_id,
            garden=self._garden,
            product_id=self._product_id,
            batch_id=self._batch_id,
            wind=wind,
            distance_to_organic_metres=self._distance,
            equipment_drift_reach_metres=equipment_drift_reach_metres,
            day=day,
        )
        decision = evaluate_application(
            request, self._treatment, self._approved_products, self._revoked_batches
        )
        self._append(
            day,
            'spray_evaluation',
            {
                'permitted': decision.permitted,
                'reasons': list(decision.reasons),
                'wind_source': wind.source,
                'wind_speed_mps': wind.speed_mps,
                'wind_toward_organic': wind.toward_organic,
                'effective_buffer_metres': decision.effective_buffer_metres,
                'equipment_drift_reach_metres': equipment_drift_reach_metres,
                'batch_id': self._batch_id,
            },
        )
        if not decision.permitted:
            return {'executed': False, 'decision': decision}

        preharvest = self._treatment['approved_spray']['preharvest_days']
        self._spray_day = day
        self._g1_block_days = day + preharvest
        self._work_days.add(day)
        self._append(
            day,
            'spray_executed',
            {
                'plot_id': self._plot_id,
                'garden': self._garden,
                'product_id': self._product_id,
                'batch_id': self._batch_id,
                'wind_speed_mps': wind.speed_mps,
                'wind_toward_organic': wind.toward_organic,
                'effective_buffer_metres': decision.effective_buffer_metres,
                'preharvest_days': preharvest,
                'g1_no_pick_before_day': self._g1_block_days,
            },
        )
        # The organic neighbour is notified and given its own observation task.
        # G2 is neither declared contaminated nor declared safe by this plan.
        self._g2_drift_task = {
            'garden': self._organic_garden,
            'source_plot': self._plot_id,
            'spray_day': day,
            'status': 'pending_verification',
            'wind_toward_g2': wind.toward_organic,
        }
        self._append(
            day,
            'g2_drift_observation_opened',
            dict(self._g2_drift_task),
        )
        return {
            'executed': True,
            'decision': decision,
            'g1_no_pick_before_day': self._g1_block_days,
            'g2_drift_task': dict(self._g2_drift_task),
        }

    def revoke_batch(self, day, batch_id, reason):
        """Withdraw a batch after the fact; past physical sprays stay on record."""
        self._revoked_batches.add(batch_id)
        self._append(
            day,
            'batch_revoked',
            {'batch_id': batch_id, 'reason': reason, 'past_sprays_retained': True},
        )

    def verify_g2_drift(self, day, detected, note=''):
        """Close G2's independent drift observation with a field finding."""
        if self._g2_drift_task is None:
            raise ValueError('no G2 drift observation task is open')
        self._g2_drift_task = {
            **self._g2_drift_task,
            'status': 'drift_detected' if detected else 'no_drift_detected',
            'verification_day': day,
            'note': note,
        }
        self._append(day, 'g2_drift_verified', dict(self._g2_drift_task))
        return dict(self._g2_drift_task)

    # ------------------------------------------------------------- derived state
    @property
    def deployed_traps(self):
        return tuple(self._deployed.values())

    @property
    def trap_inventory_remaining(self):
        return self._traps_inventory

    @property
    def work_days(self):
        return tuple(sorted(self._work_days))

    @property
    def g1_spray_day(self):
        return self._spray_day

    @property
    def g1_no_pick_before_day(self):
        """Earliest legal picking day for G1; ``None`` means no chemical block."""
        return self._g1_block_days

    @property
    def g1_harvest_block_active(self):
        return self._g1_block_days is not None

    @property
    def g2_status(self):
        """G2 state is independent: no inherited picking block, ever."""
        if self._g2_drift_task is None:
            return {
                'garden': self._organic_garden,
                'exposure': 'none',
                'drift_task': None,
                'picking_blocked': False,
            }
        return {
            'garden': self._organic_garden,
            'exposure': 'downwind_observation',
            'drift_task': dict(self._g2_drift_task),
            'picking_blocked': False,
        }

    @property
    def control_status(self):
        if self._recheck is None:
            return 'unverified'
        return 'controlled' if self._recheck.controlled else 'not_controlled_escalation_required'

    def g1_pickable_on(self, day):
        return self._g1_block_days is None or day >= self._g1_block_days
