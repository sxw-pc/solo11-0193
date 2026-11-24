import json
from pathlib import Path
from teablend.pest_monitor import pressure
from teablend.spray_gate import decide


def test_average_recommends_spray_despite_current_downwind_neighbor():
    data = json.loads((Path(__file__).parents[1] / 'fixtures' / 'pest-intervention.json').read_text())
    assert pressure(data['plot']) == 6.5
    assert decide(data['plot']) == 'spray'
    assert data['weather'][0]['wind_toward_G2_mps'] > data['treatments']['approved_spray']['wind_limit_mps']
    quiet = {**data['plot'], 'trap_counts': [1, 1]}
    assert decide(quiet) == 'monitor'
