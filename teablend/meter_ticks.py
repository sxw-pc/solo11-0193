"""Current energy register delta without rollover/replacement evidence."""


def kwh(readings):
    return readings[-1]['register_kwh'] - readings[0]['register_kwh']
