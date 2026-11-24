"""Current channel decision based on nominal months only."""
from .shelf_life import months


def channel(samples):
    life = months(samples)
    return 'wholesale' if life >= 6 else 'local'
