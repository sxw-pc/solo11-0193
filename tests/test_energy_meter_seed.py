import json
from pathlib import Path
from teablend.meter_ticks import kwh
from teablend.energy_chargeback import allocate


def test_register_wrap_yields_negative_batch_energy():
    data = json.loads((Path(__file__).parents[1] / 'fixtures' / 'energy-meter.json').read_text())
    assert kwh(data['readings']) == -9960
    assert allocate(data['readings'], data['jobs']) == {'BATCH-A': -5976, 'BATCH-B': -3984}
    later = [data['readings'][0], {**data['readings'][1], 'register_kwh': 40}]
    assert allocate(later, data['jobs'])['BATCH-A'] == -5970
