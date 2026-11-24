"""Per-point pest diagnosis.

The legacy :func:`pressure` (plain mean of ``trap_counts``) and the legacy
:func:`teablend.spray_gate.decide` threshold rule are kept untouched so the
seed regression can still demonstrate why a whole-plot mean is unsafe: the
12-catch hotspot must survive alongside the quiet 1-catch trap instead of
being averaged away.

:func:`diagnose_plot` keeps every trap point individually (position, count,
counting error, effective working days, offline windows) and returns the
spatial coverage that an intervention must target, not a single plot average.
"""

HOTSPOT_TRIGGER = 5
COVERAGE_RADIUS_METRES = 15.0


def pressure(plot):
    """Legacy whole-plot mean kept for the seed regression only."""
    counts = plot['trap_counts']
    return sum(counts) / len(counts)


def _point_records(plot):
    traps = plot.get('traps')
    if traps is not None:
        return [dict(t) for t in traps]
    # Minimal legacy plots only carry bare counts: synthesize positions and
    # keep every count individually rather than collapsing them to a mean.
    return [
        {
            'id': f'MT-{i + 1:03d}',
            'x_metres': 0.0,
            'y_metres': float(i),
            'count': count,
            'count_error_margin': 0,
            'effective_work_days': None,
            'offline_intervals': [],
        }
        for i, count in enumerate(plot['trap_counts'])
    ]


def _assess_point(point, trigger):
    count = point['count']
    margin = point.get('count_error_margin', 0) or 0
    # A hotspot stays a hotspot unless the whole error band falls below the
    # trigger, so counting error cannot quietly erase the 12-catch hot spot.
    is_hotspot = count - margin >= trigger
    return {
        'id': point['id'],
        'x_metres': point['x_metres'],
        'y_metres': point['y_metres'],
        'count': count,
        'count_error_margin': margin,
        'effective_work_days': point.get('effective_work_days'),
        'offline_intervals': [dict(w) for w in point.get('offline_intervals', [])],
        'is_hotspot': is_hotspot,
        'data_quality': 'offline_window_recorded' if point.get('offline_intervals') else 'complete',
    }


def diagnose_plot(plot, trigger=HOTSPOT_TRIGGER, coverage_radius_metres=COVERAGE_RADIUS_METRES):
    """Diagnose one plot point by point.

    Returns each trap's individual assessment, the hotspot points (the 12-catch
    trap is not diluted by the 1-catch trap), the spatial coverage zones an
    intervention must cover, and a verdict naming the required coverage scope.
    """
    points = [_assess_point(p, trigger) for p in _point_records(plot)]
    hotspots = [p for p in points if p['is_hotspot']]

    coverage_zones = [
        {
            'anchor_trap_id': p['id'],
            'centre': {'x_metres': p['x_metres'], 'y_metres': p['y_metres']},
            'radius_metres': coverage_radius_metres,
            'reason': f"catch {p['count']} (+/-{p['count_error_margin']}) >= trigger {trigger}",
        }
        for p in hotspots
    ]

    if hotspots:
        verdict = 'targeted_intervention_required'
        narrative = (
            f"{len(hotspots)} hotspot trap(s) need targeted coverage; "
            "do not act on the whole-plot mean alone"
        )
    else:
        verdict = 'monitor'
        narrative = 'no point reaches the hotspot trigger; routine monitoring continues'

    return {
        'plot_id': plot['id'],
        'garden': plot.get('garden'),
        'points': points,
        'hotspot_trigger': trigger,
        'hotspot_ids': [p['id'] for p in hotspots],
        'hotspots': hotspots,
        'coverage_zones': coverage_zones,
        'mean_count': pressure(plot),
        'verdict': verdict,
        'narrative': narrative,
    }
