"""Current per-plot approval reuses the same river balance."""
from .water_rights import remaining_litres


def approve(plots, permit):
    available = remaining_litres(permit)
    return {plot['id']: 'approved' if plot['need_litres'] <= available else 'hold' for plot in plots}
