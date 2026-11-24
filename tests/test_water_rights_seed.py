import json
from pathlib import Path
from teablend.water_rights import remaining_litres
from teablend.irrigation_board import approve


def test_two_plot_approvals_reuse_one_river_entitlement():
    data = json.loads((Path(__file__).parents[1] / 'fixtures' / 'water-rights.json').read_text())
    assert remaining_litres(data['river_permit']) == 1200
    assert approve(data['plots'], data['river_permit']) == {'G1-P1': 'approved', 'G2-P1': 'approved'}
    assert sum(plot['need_litres'] for plot in data['plots']) > 1200
    smaller = {**data['river_permit'], 'daily_limit_litres': 700}
    assert approve(data['plots'], smaller) == {'G1-P1': 'hold', 'G2-P1': 'hold'}
