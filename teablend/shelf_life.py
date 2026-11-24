"""Current shelf-life estimate from lot-average headspace oxygen."""
from .seal_atmosphere import lot_oxygen_pct


def months(samples):
    return 12 if lot_oxygen_pct(samples) <= 3.0 else 3
