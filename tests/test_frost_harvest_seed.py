import json
from pathlib import Path
from teablend.frost_window import deadline
from teablend.crew_plan import plan


def test_current_board_reuses_one_crew_during_overlap():
    data = json.loads((Path(__file__).parents[1] / 'fixtures' / 'frost-harvest.json').read_text())
    assert deadline('G1', data['forecasts']) == '2026-11-25T07:20:00'
    rows = plan(data['plots'], data['crews'], data['forecasts'])
    assert [row['crew'] for row in rows] == ['C1', 'C1']
    assert rows[0]['finish'] > rows[1]['start']
