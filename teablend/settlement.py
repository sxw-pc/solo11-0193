"""Estimate payable dry leaf mass from a signed net weight."""
from .moisture import percent

def payable_dry_kg(net_kg, readings):
    return round(net_kg * (1 - percent(readings) / 100), 2)
