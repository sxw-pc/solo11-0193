"""Chemical spray execution gate.

Two layers live here deliberately:

* :func:`decide` is the seed rule (mean count versus a threshold). It is kept
  for the regression that proves it recommends ``spray`` while the field
  conditions make spraying unsafe.
* :func:`evaluate_application` is the execution gate. A chemical application
  is executable only when every hard condition holds at the same time:
  approved (and not revoked) product/batch, a *field-measured* wind receipt at
  application time with speed at or below the permit limit, an effective
  buffer of at least the required metres towards the organic neighbour G2,
  and equipment drift reach that stays inside the G1 boundary.

A weather forecast is never accepted as the wind receipt: the nozzle's own
field measurement at execution time is the only acceptable evidence.
"""
from dataclasses import dataclass, field

from .pest_monitor import pressure

WIND_SOURCE_FIELD = 'field'
WIND_SOURCE_FORECAST = 'forecast'


def decide(plot, trigger=5):
    """Legacy threshold rule kept for the seed regression only."""
    return 'spray' if pressure(plot) >= trigger else 'monitor'


@dataclass(frozen=True)
class WindReading:
    """A wind observation.

    ``source`` distinguishes a forecast (never sufficient for execution) from
    a field receipt taken when the sprayer is actually at the nozzle.
    """

    day: int
    speed_mps: float
    toward_organic: bool
    source: str = WIND_SOURCE_FIELD
    note: str = ''


@dataclass(frozen=True)
class SprayRequest:
    plot_id: str
    garden: str
    product_id: str
    batch_id: str
    wind: WindReading
    distance_to_organic_metres: float
    equipment_drift_reach_metres: float = 0.0
    day: int = 0


@dataclass(frozen=True)
class SprayGateDecision:
    permitted: bool
    reasons: tuple = field(default_factory=tuple)
    effective_buffer_metres: float | None = None
    required_buffer_metres: float | None = None
    wind_speed_mps: float | None = None
    wind_limit_mps: float | None = None


def evaluate_application(request, treatment, approved_products, revoked_batches=()):
    """Decide whether one concrete spray application may physically execute.

    ``approved_products`` maps product id to permit details; ``revoked_batches``
    is the set of batch ids whose approval has been withdrawn.
    """
    reasons = []
    permit = approved_products.get(request.product_id)
    if permit is None:
        reasons.append(f"product {request.product_id} is not an approved treatment")
        return SprayGateDecision(False, tuple(reasons))

    if request.batch_id in set(revoked_batches):
        reasons.append(f"batch {request.batch_id} has been revoked")

    wind_limit = permit.get('wind_limit_mps', treatment['approved_spray']['wind_limit_mps'])
    required_buffer = permit.get(
        'organic_buffer_metres', treatment['approved_spray']['organic_buffer_metres']
    )

    wind = request.wind
    if wind.source != WIND_SOURCE_FIELD:
        reasons.append(
            f"wind reading is a {wind.source}, not a field receipt at the nozzle"
        )
    if wind.speed_mps > wind_limit:
        reasons.append(
            f"field wind {wind.speed_mps} m/s exceeds permit limit {wind_limit} m/s"
        )

    effective_buffer = None
    if wind.toward_organic:
        effective_buffer = (
            request.distance_to_organic_metres - request.equipment_drift_reach_metres
        )
        if effective_buffer < required_buffer:
            reasons.append(
                f"effective buffer {effective_buffer} m towards organic garden "
                f"is below required {required_buffer} m (drift reach "
                f"{request.equipment_drift_reach_metres} m would cross the boundary)"
            )

    permitted = not reasons
    if permitted:
        reasons.append(
            "all execution conditions hold: approved product/batch, field wind "
            f"{wind.speed_mps} m/s <= {wind_limit} m/s, effective buffer "
            f"{effective_buffer if wind.toward_organic else 'n/a (wind not towards G2)'} m"
        )

    return SprayGateDecision(
        permitted=permitted,
        reasons=tuple(reasons),
        effective_buffer_metres=effective_buffer,
        required_buffer_metres=required_buffer,
        wind_speed_mps=wind.speed_mps,
        wind_limit_mps=wind_limit,
    )
