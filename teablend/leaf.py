"""Receipt, duplicate, weight and leaf grade decisions."""

from .garden import certificate_for, picker_for


def decide(events, certs, pickers):
    receipts = [e for e in events if e['kind']=='receipt']
    weights = [e for e in events if e['kind']=='weight']
    committed_at = {}
    for e in events:
        if e['kind'] in ('blend','rework'):
            for rid in e['receipts']:
                committed_at[rid]=min(e['at'],committed_at.get(rid,e['at']))
    by_receipt = {}
    for w in weights:
        by_receipt.setdefault(w['receipt'], []).append(w)
    seen = {}
    decisions = {}
    for r in receipts:
        cert, cert_state = certificate_for(certs,r['garden'],r['variety'],r['picked_at'])
        picker_ok = picker_for(pickers,r['picker'],r['garden'],r['picked_at'])
        key=r['pick_ticket']
        duplicate=key in seen
        seen.setdefault(key,r['id'])
        if duplicate: status='duplicate_pick'
        elif not cert: status=cert_state
        elif not picker_ok: status='picker_unauthorized'
        elif r['gross_kg']<=0: status='invalid_mass'
        else: status='eligible'
        weight_rows=by_receipt.get(r['id'],[])
        signed=[w for w in weight_rows if w['garden_sign'] and w['factory_sign'] and
                w['garden_sign']!=w['factory_sign'] and w['at']>=r['picked_at']]
        accepted=min(signed,key=lambda w:w['at']) if signed else None
        if status=='eligible' and not accepted:status='weight_unverified'
        if status=='eligible' and any(w['net_kg']<=0 for w in signed):status='invalid_mass'
        if status=='eligible' and len({w['net_kg'] for w in signed})>1:status='mass_dispute'
        if status=='eligible' and abs(accepted['net_kg']-r['gross_kg'])>r['gross_kg']*0.1:
            status='mass_dispute'
        if status=='eligible' and any(
                c['status']=='revoked' and c['at']>r['picked_at'] and c['version']>cert['version']
                and committed_at.get(r['id'],'~')>=c['at']
                for c in certs.get((r['garden'],r['variety']),[])):
            status='certificate_revoked_pending'
        from datetime import datetime
        delay_hours=(datetime.fromisoformat(r['delivered_at'])-datetime.fromisoformat(r['picked_at'])).total_seconds()/3600
        grade=r['grade']
        if (delay_hours>8 or r.get('leaf_age_days',0)>2) and grade=='premium':grade='standard'
        overnight=r['picked_at'][:10]!=r['delivered_at'][:10]
        if overnight and grade=='premium':grade='standard'
        decisions[f"receipt:{r['id']}"]={'status':status,'certificate':cert['id'] if cert else None,
            'organic':bool(cert and cert['organic']) if status=='eligible' else False,
            'grade':grade,'net_kg':accepted['net_kg'] if status=='eligible' else 0,
            'garden':r['garden'],'pick_ticket':r['pick_ticket'],'delay_hours':round(delay_hours,2),
            'route':'rework' if overnight or r.get('leaf_age_days',0)>2 else 'normal'}
        for w in weight_rows:
            decisions[f"weight:{w['id']}"]={'status':'dual_signed' if w in signed else 'unverified',
                'receipt':r['id'],'net_kg':w['net_kg']}
    return decisions
