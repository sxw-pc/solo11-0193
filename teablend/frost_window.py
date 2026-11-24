"""Conservative field window from offline frost forecasts.

Each credible forecast is an independent feasible upper bound on field work:
a committed job is safe only when it finishes before the *earliest* credible
frost. A later, more optimistic forecast can never cancel an earlier credible
warning, and unverified/retracted forecasts cannot move the window in either
direction. Rows without an explicit trust marker are treated as credible so
legacy single-source fixtures keep working.
"""
from datetime import datetime

NON_CREDIBLE_STATUSES = ('unverified', 'retracted', 'disputed', 'rejected')


def _parse(value):
    return datetime.fromisoformat(value)


def is_credible(row):
    """A forecast counts only when trusted and not explicitly disqualified."""
    if row.get('trusted', True) is False:
        return False
    return row.get('status', 'credible') not in NON_CREDIBLE_STATUSES


def credible_rows(garden, forecasts):
    return [row for row in forecasts
            if row.get('garden') == garden and is_credible(row)]


def _source(row):
    return row.get('source') or row.get('id') or 'unknown'


def deadline(garden, forecasts):
    """Earliest credible frost time for a garden, or None when no bound exists.

    This is the single conservative upper bound every committed job must beat;
    taking the max would let one optimistic forecast hide a credible warning.
    """
    rows = credible_rows(garden, forecasts)
    if not rows:
        return None
    return min(rows, key=lambda r: _parse(r['frost_at']))['frost_at']


def bounds(garden, forecasts):
    """All credible frost times, earliest first (the feasible-bound stack)."""
    rows = sorted(credible_rows(garden, forecasts),
                  key=lambda r: _parse(r['frost_at']))
    return [row['frost_at'] for row in rows]


def window(garden, forecasts):
    rows = sorted(credible_rows(garden, forecasts),
                  key=lambda r: _parse(r['frost_at']))
    ignored = [_source(row) for row in forecasts
               if row.get('garden') == garden and not is_credible(row)]
    return {
        'garden': garden,
        'deadline': rows[0]['frost_at'] if rows else None,
        'bounds': [row['frost_at'] for row in rows],
        'sources': [_source(row) for row in rows],
        'ignored_sources': ignored,
        'credible_count': len(rows),
    }


def all_windows(forecasts, gardens=None):
    if gardens is None:
        gardens = sorted({row['garden'] for row in forecasts})
    return {garden: window(garden, forecasts) for garden in gardens}


def allows(garden, finish_at, forecasts):
    """True when finish_at is strictly before every credible frost bound."""
    edge = deadline(garden, forecasts)
    if edge is None:
        return True
    return _parse(finish_at) < _parse(edge)


def windows_changed(before, after):
    """Gardens whose conservative deadline moved between two forecast sets."""
    moved = []
    for garden in sorted(set(before) | set(after)):
        if before.get(garden, {}).get('deadline') != \
                after.get(garden, {}).get('deadline'):
            moved.append(garden)
    return moved
