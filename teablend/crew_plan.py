"""Current harvest board allocates a crew per plot without a global ledger."""
from .frost_window import deadline


def plan(plots, crews, forecasts):
    assignments = []
    for plot in plots:
        possible = [c for c in crews if plot['garden'] in c['gardens']]
        chosen = possible[0] if possible else None
        assignments.append({'plot': plot['id'], 'crew': chosen['id'] if chosen else None,
                            'start': plot['ready_at'], 'finish': plot['finish_at'],
                            'frost_at': deadline(plot['garden'], forecasts)})
    return assignments
