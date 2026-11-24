"""Confirm an offline branch order against its local stock snapshot."""
from .inventory import available

def accepted(order, snapshot):
    return order['kg'] <= available(snapshot, order['label'])
