"""Current carrier scan summary for a tea shipment."""


def arrived_parcels(events):
    return {row['parcel'] for row in events if row['stage'] == 'arrived'}
