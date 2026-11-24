import json
from pathlib import Path
from teablend.seal_atmosphere import lot_oxygen_pct
from teablend.shelf_life import months
from teablend.ship_decision import channel


def test_average_masks_one_high_oxygen_pouch():
    data = json.loads((Path(__file__).parents[1] / 'fixtures' / 'package-life.json').read_text())
    samples = data['headspace_samples']
    assert round(lot_oxygen_pct(samples), 2) == 2.37
    assert months(samples) == 12
    assert channel(samples) == 'wholesale'
    high = [{**row, 'oxygen_pct': 5.0} for row in samples]
    assert months(high) == 3
    assert channel(high) == 'local'
