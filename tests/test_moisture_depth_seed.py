import json
from pathlib import Path
from teablend.moisture import percent
from teablend.settlement import payable_dry_kg

def test_surface_only_probes_overstate_payable_mass():
    d = json.loads((Path(__file__).parents[1] / 'fixtures' / 'moisture-depth.json').read_text())
    assert percent(d['readings']) == 20
    assert payable_dry_kg(d['signed_net_kg'], d['readings']) == 80
    assert {r['depth'] for r in d['readings']} == {'surface'}
    assert d['drying_output_kg'] < 80
