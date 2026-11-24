"""Current spray recommendation from mean trap count only."""
from .pest_monitor import pressure


def decide(plot, trigger=5):
    return 'spray' if pressure(plot) >= trigger else 'monitor'
