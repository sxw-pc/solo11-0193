import json
from pathlib import Path
from teablend.inventory import available
from teablend.fulfillment import accepted

def test_two_offline_branches_oversubscribe_one_lot():
    d = json.loads((Path(__file__).parents[1] / 'fixtures' / 'offline-orders.json').read_text())
    assert [available(b['snapshot'], 'L1') for b in d['branches']] == [10, 10]
    assert all(accepted(b['order'], b['snapshot']) for b in d['branches'])
    assert sum(b['order']['kg'] for b in d['branches']) == 13 > d['stock']['L1']
