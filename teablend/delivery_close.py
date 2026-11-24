"""Current order close from any arrival scan, without recipient proof."""
from .shipment_scan import arrived_parcels


def decide(shipment, events):
    arrived = arrived_parcels(events)
    return {'shipment': shipment['id'], 'status': 'delivered' if arrived else 'in_transit',
            'arrived_parcels': sorted(arrived)}
