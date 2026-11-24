import json
from pathlib import Path
from teablend.shipment_scan import arrived_parcels
from teablend.delivery_close import decide


def test_one_gate_scan_closes_two_box_shipment_without_receipt():
    data = json.loads((Path(__file__).parents[1] / 'fixtures' / 'leaf-shipment.json').read_text())
    assert arrived_parcels(data['events']) == {'BOX-1'}
    assert decide(data['shipment'], data['events']) == {
        'shipment': 'SHIP-1', 'status': 'delivered', 'arrived_parcels': ['BOX-1']}
    transfer_only = [row for row in data['events'] if row['stage'] != 'arrived']
    assert decide(data['shipment'], transfer_only)['status'] == 'in_transit'
