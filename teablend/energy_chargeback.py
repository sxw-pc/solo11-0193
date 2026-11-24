"""Current batch allocation simply scales the raw grid register delta."""
from .meter_ticks import kwh


def allocate(readings, jobs):
    total = kwh(readings)
    weight = sum(job['nominal_weight'] for job in jobs)
    return {job['id']: round(total * job['nominal_weight'] / weight, 2) for job in jobs}
