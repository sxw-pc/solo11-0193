"""Average handheld moisture probe readings for a leaf delivery."""

def percent(readings):
    return sum(r['moisture_pct'] for r in readings) / len(readings)
