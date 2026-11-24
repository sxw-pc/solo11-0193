"""Conservative field window from offline frost forecasts."""


def deadline(garden, forecasts):
    relevant = [row['frost_at'] for row in forecasts if row['garden'] == garden]
    return min(relevant) if relevant else None
