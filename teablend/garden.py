"""Garden certification and picking authority decisions."""


def decide(events):
    certs = {}
    pickers = {}
    decisions = {}
    for e in events:
        if e['kind'] == 'certificate':
            key = (e['garden'], e['variety'])
            certs.setdefault(key, []).append(e)
        elif e['kind'] == 'picker':
            pickers[e['id']] = e
    for key, rows in certs.items():
        for e in rows:
            decisions[f"certificate:{e['id']}"] = {
                'status': 'revoked' if e['status'] == 'revoked' else 'certified',
                'garden': e['garden'], 'variety': e['variety'], 'start': e['start'], 'finish': e['finish']}
    for e in pickers.values():
        valid = e['finish'] > e['start'] and e['status'] == 'active'
        decisions[f"picker:{e['id']}"] = {'status': 'authorized' if valid else 'inactive',
            'garden': e['garden'], 'start': e['start'], 'finish': e['finish']}
    return certs, pickers, decisions


def certificate_for(certs, garden, variety, at):
    rows = certs.get((garden, variety), [])
    good = [c for c in rows if c['start'] <= at < c['finish'] and c['status'] == 'active']
    revoked = [c for c in rows if c['status'] == 'revoked' and c['at'] <= at]
    if revoked:
        good = [c for c in good if c['version'] > max(r['version'] for r in revoked)]
    versions = {}
    for c in good:
        versions.setdefault(c['version'], set()).add((c['start'], c['finish'], c['organic']))
    if any(len(v)>1 for v in versions.values()):
        return None, 'certificate_conflict'
    return (max(good, key=lambda c:c['version']), 'certified') if good else (None, 'certificate_missing')


def picker_for(pickers, picker, garden, at):
    p = pickers.get(picker)
    return bool(p and p['status']=='active' and p['garden']==garden and p['start']<=at<p['finish'])
