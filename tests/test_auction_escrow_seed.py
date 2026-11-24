import json
from pathlib import Path
from teablend.auction_award import winner
from teablend.escrow_remit import releasable


def test_authorization_is_not_capture_or_signed_delivery():
    data = json.loads((Path(__file__).parents[1] / 'fixtures' / 'auction-escrow.json').read_text())
    assert winner(data['lot'], data['bids'])['id'] == 'BID-A'
    assert releasable(data['lot'], data['bids'], data['payment']) == 12000
    assert data['payment']['captured'] == 4000
    assert sum(d['kg'] for d in data['deliveries']) == 30
    no_reserve = {**data['lot'], 'reserve_price': 13000}
    assert releasable(no_reserve, data['bids'], data['payment']) == 0
