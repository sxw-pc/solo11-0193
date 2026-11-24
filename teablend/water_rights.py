"""Current remaining river diversion entitlement."""


def remaining_litres(permit):
    return permit['daily_limit_litres'] - permit['recorded_litres']
