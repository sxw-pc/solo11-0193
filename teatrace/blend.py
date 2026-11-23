"""Receipt allocation, rework, mass balance, and origin label decisions."""


def decide(events, leaf_decisions):
    recipes=sorted((e for e in events if e['kind'] in ('blend','rework')),key=lambda e:(e['at'],e['id']))
    reworks={}
    labels=[e for e in events if e['kind']=='label']
    result={};allocated={}
    for recipe in recipes:
        revision=recipe if recipe['kind']=='rework' else None
        blend_id=recipe['blend'] if revision else recipe['id']
        if revision:
            reworks[blend_id]=revision
            if f'blend:{blend_id}' not in result:
                result[f"rework:{revision['id']}"]={'status':'blend_missing','blend':blend_id,'old_labels_revoked':False}
                continue
        receipt_ids=recipe['receipts']
        inputs=[leaf_decisions.get(f'receipt:{x}') for x in receipt_ids]
        missing=any(x is None for x in inputs)
        blocked=any(x and x['status']!='eligible' for x in inputs)
        duplicate=len(set(receipt_ids))!=len(receipt_ids)
        already=[rid for rid in receipt_ids if rid in allocated and allocated[rid]!=blend_id]
        rework_needed=any(x and x['route']=='rework' for x in inputs) and revision is None
        total=sum(x['net_kg'] for x in inputs if x)
        output=recipe['output_kg'];yield_floor=recipe.get('minimum_yield_pct',70)
        if missing:status='source_missing'
        elif blocked:status='source_blocked'
        elif output>total or output<=0 or total<=0 or output*100<total*yield_floor:status='mass_imbalance'
        elif duplicate or already:status='receipt_allocated'
        elif rework_needed:status='rework_required'
        else:status='balanced'
        if status=='balanced':
            if revision:
                for rid,owner in list(allocated.items()):
                    if owner==blend_id and rid not in receipt_ids:del allocated[rid]
            for rid in receipt_ids:allocated[rid]=blend_id
        origins={}
        for x in inputs:
            if x:origins[x['garden']]=origins.get(x['garden'],0)+x['net_kg']
        result[f"blend:{blend_id}"]={'status':status,'input_kg':total,'output_kg':output,
            'all_organic':bool(inputs and all(x['organic'] for x in inputs if x)),
            'grades':sorted(set(x['grade'] for x in inputs if x)),
            'origins':sorted(origins),'origin_kg':origins,'allocated_conflicts':already,
            'revision':revision['id'] if revision else blend_id}
        if revision:
            result[f"rework:{revision['id']}"]={'status':'applied' if status=='balanced' else 'blocked',
                'blend':blend_id,'old_labels_revoked':True}
    for label in labels:
        b=result.get(f"blend:{label['blend']}")
        revision=reworks.get(label['blend'])
        if not b:status='blend_missing'
        elif revision and label['at']<revision['at']:status='revoked_by_rework'
        elif b['status']!='balanced':status='blend_blocked'
        elif label['organic'] and not b['all_organic']:status='organic_mislabel'
        elif label['grade']=='premium' and b['grades']!=['premium']:status='grade_mislabel'
        elif label['origin']=='mixed' and len(b['origins'])<2:status='origin_mislabel'
        elif label['origin']!='mixed' and (label['origin'] not in b['origin_kg'] or
             b['origin_kg'][label['origin']]*100 < b['input_kg']*label.get('minimum_origin_pct',100)):
            status='origin_mislabel'
        else:status='label_allowed'
        result[f"label:{label['id']}"]={'status':status,'blend':label['blend'],
            'organic':label['organic'],'grade':label['grade'],'origin':label['origin'],
            'revision':b['revision'] if b else None}
    return result
