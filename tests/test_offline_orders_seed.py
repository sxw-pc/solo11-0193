import json
from pathlib import Path
from teablend.inventory import available
from teablend.fulfillment import accepted

def test_two_offline_branches_oversubscribe_one_lot():
    d = json.loads((Path(__file__).parents[1] / 'fixtures' / 'offline-orders.json').read_text())
    assert [available(b['snapshot'], 'L1') for b in d['branches']] == [10, 10]
    assert all(accepted(b['order'], b['snapshot']) for b in d['branches'])
    assert sum(b['order']['kg'] for b in d['branches']) == 13 > d['stock']['L1']


# ---------------------------------------------------------------------------
# Extended acceptance suite (dual-shop merge, two resolutions, replay, ...)
# ---------------------------------------------------------------------------
import copy
import itertools

import pytest

from teablend.inventory import inventory_view
from teablend.fulfillment import (
    MergeBook, NoticeBook,
    allocate, fulfillable_set, label_verdict, reconcile)
from teablend.order_capture import CaptureBook, DocumentCollision

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / 'fixtures' / 'offline-orders.json'


def data():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


def scenario(name):
    """Base fixture plus one named scenario overlay applied."""
    d = data()
    out = copy.deepcopy(d)
    overlay = d['scenarios'][name]
    for key, value in overlay.items():
        if isinstance(value, list) and isinstance(out.get(key), list):
            out[key].extend(copy.deepcopy(value))
        else:
            out[key] = copy.deepcopy(value)
    return out


def test_snapshot_four_makes_both_local_checks_false():
    """Upstream snapshot 10 -> 4 causally flips both local acceptances."""
    from teablend.fulfillment import accepted
    d = data()
    for b in d['branches']:
        snap = copy.deepcopy(b['snapshot'])
        snap['L1'] = 4
        assert accepted(b['order'], snap) is False
    # And the seed values still give two local Trues.
    assert all(accepted(b['order'], b['snapshot']) is True for b in d['branches'])


# ---------------------------------------------------------------------------
# Provable multi-site inventory
# ---------------------------------------------------------------------------
def test_box_codes_prove_the_ten_physical_kg_with_versions():
    d = data()
    assert sum(b['kg'] for b in d['boxes']) == 10
    view = inventory_view(d)
    wh = view['by_location']['WH']['L1']
    assert wh['on_hand_kg'] == 10
    assert {c: view['boxes'][c]['version'] for c in ('WH-L1-01', 'WH-L1-02')} == {
        'WH-L1-01': 7, 'WH-L1-02': 7}
    assert view['inventory_version']
    assert len(view['version_sha256']) == 64


def test_inventory_reports_wh_and_two_shops_snapshot_commitment_and_transit():
    d = data()
    view = inventory_view(d)
    assert set(['WH', 'A', 'B']).issubset(view['locations'])
    a = view['by_location']['A']['L1']
    b = view['by_location']['B']['L1']
    assert a['snapshot_kg'] == 10 and b['snapshot_kg'] == 10
    assert a['locally_committed_kg'] == 8
    assert b['locally_committed_kg'] == 5
    # Catalog batches are dispatched but not inbound signed -> in transit only.
    assert view['by_location']['WH']['L1']['in_transit_kg'] == 9


def test_occupied_quantity_comes_only_from_global_plan():
    r = reconcile(scenario('replenishment'), emit_notifications=False)
    wh = r['inventory']['by_location']['WH']['L1']
    assert wh['occupied_kg'] == 13
    assert wh['available_kg'] == 0
    # Without allocations nothing global is occupied even though shops promised.
    bare = inventory_view(data())
    assert bare['by_location']['WH']['L1']['occupied_kg'] == 0


# ---------------------------------------------------------------------------
# Base global merge: the 13 - 10 conflict, one physical kg allocated once
# ---------------------------------------------------------------------------
def test_minimal_seed_world_merge_reports_gap_and_needs_consent():
    d = {'stock': {'L1': 10}, 'branches': data()['branches']}
    r = reconcile(d, emit_notifications=False)
    assert r['total_promised_kg'] == 13
    assert r['original_stock_kg'] == 10
    assert r['conflict_excess_kg'] == 3
    assert r['accepted'] == ['O1']
    assert r['revocation_consent_required'] == ['O2']
    assert r['ok'] is False
    # No box is loaded beyond its physical kg.
    for code, box in r['box_loads'].items():
        assert box['allocated_kg'] <= box['kg'] + 1e-9


def test_full_fixture_base_keeps_qualified_batch_as_awaiting_replenishment():
    r = reconcile(data(), emit_notifications=False)
    assert r['accepted'] == ['O1']
    assert r['awaiting_replenishment'] == ['O2']
    o2 = r['decisions']['O2']
    assert o2['allocated_kg'] == 2 and o2['remaining_gap_kg'] == 3
    pending_ids = [b['id'] for b in o2['pending_replenishment']]
    assert 'RP-L1-03' in pending_ids


def test_every_plan_allocates_each_physical_kg_at_most_once():
    for name in (None, 'replenishment', 'consent_needed', 'revocation'):
        world = scenario(name) if name else data()
        r = reconcile(world, emit_notifications=False)
        load = {}
        for row in r['allocations']:
            load[row['box']] = load.get(row['box'], 0) + row['kg']
        for code, kg in load.items():
            box = r['box_loads'][code]
            assert kg <= box['gross_kg'] - box['outbound_kg'] + 1e-9, (name, code)
        assert r['conflicts'] == []
        assert r['over_allocated_boxes'] == []


def test_merge_lists_signed_order_box_codes_and_version_per_decision():
    r = reconcile(scenario('replenishment'), emit_notifications=False)
    for oid in ('O1', 'O2'):
        d = r['decisions'][oid]
        assert d['signed_order_sha256'] and len(d['signed_order_sha256']) == 64
        assert d['signed_order']['id'] == oid
        assert d['inventory_version'] == r['inventory_version']
        assert d['boxes'] and all(b['code'] and b['version'] == 7 or
                                  b['code'] == 'WH-L1-R3' for b in d['boxes'])


# ---------------------------------------------------------------------------
# Path 1: qualified 3 kg transfer arrives + signed inbound -> both fulfilled
# ---------------------------------------------------------------------------
def test_qualified_replenishment_fulfills_both_orders_with_new_source():
    r = reconcile(scenario('replenishment'), emit_notifications=False)
    assert r['ok'] is True
    assert set(r['accepted']) == {'O1', 'O2'}
    assert fulfillable_set(scenario('replenishment')) == {'O1', 'O2'}

    o1 = {(a['box'], a['kg']) for a in r['decisions']['O1']['allocations']}
    o2 = {(a['box'], a['kg']) for a in r['decisions']['O2']['allocations']}
    assert o1 == {('WH-L1-01', 6), ('WH-L1-02', 2)}
    assert o2 == {('WH-L1-02', 2), ('WH-L1-R3', 3)}

    # The shared 4 kg box is split 2 + 2, never assigned twice beyond weight.
    assert sum(kg for box, kg in [*o1, *o2] if box == 'WH-L1-02') == 4
    assert r['occupied_kg'] == 13

    # O2 keeps the original promise and gains the new inventory source.
    sources = {a['source'] for a in r['decisions']['O2']['allocations']}
    assert sources == {'original', 'replenishment'}
    assert any(a['batch'] == 'RP-L1-03' for a in r['decisions']['O2']['allocations'])


def test_intransit_or_number_only_batch_cannot_close_gap():
    d = data()  # RP-L1-03 qualified but only dispatched
    r = reconcile(d, emit_notifications=False)
    assert 'O2' not in r['accepted']
    eval3 = next(e for e in r['batch_evaluations']
                 if e['id'] == 'RP-L1-03' and e['order_id'] == 'O2')
    assert eval3['contract_eligible'] is True
    assert eval3['physically_received'] is False and eval3['usable'] is False

    number_only = {'stock': {'L1': 10}, 'branches': d['branches'],
                   'replenishments': [
                       {'id': 'RP-NUM', 'label': 'L1', 'kg': 3,
                        'grade': 'premium', 'organic': True,
                        'quality_inspected': True}]}
    r2 = reconcile(number_only, emit_notifications=False)
    assert 'O2' in r2['awaiting_replenishment']
    assert r2['ok'] is False


def test_disqualified_substitute_batches_are_blocked_by_blend_label_rules():
    d = data()
    contract = d['label_contracts']['L1']
    good, _ = label_verdict(d['replenishments'][0], contract)
    bad_org, _ = label_verdict(d['replenishments'][1], contract)
    bad_grade, _ = label_verdict(d['replenishments'][2], contract)
    assert good == 'label_allowed'
    assert bad_org == 'organic_mislabel'
    assert bad_grade == 'grade_mislabel'

    # Even a physically received non-organic 3 kg cannot cover the gap.
    r = reconcile(scenario('consent_needed'), emit_notifications=False)
    org_eval = next(e for e in r['batch_evaluations']
                    if e['id'] == 'RP-BAD-ORG-03' and e['order_id'] == 'O1')
    assert org_eval['physically_received'] is True
    assert org_eval['usable'] is False
    assert 'organic_mislabel' in org_eval['reasons']
    assert r['accepted'] == ['O1']
    assert r['revocation_consent_required'] == ['O2']


def test_customer_substitution_contract_field_is_a_gate():
    base = scenario('replenishment')
    # Explicit refusal blocks even a fully conforming received batch.
    refused = copy.deepcopy(base)
    for o in refused['orders']:
        o['contract']['substitutes_allowed'] = False
    r = reconcile(refused, emit_notifications=False)
    eval3 = next(e for e in r['batch_evaluations']
                 if e['id'] == 'RP-L1-03' and e['order_id'] == 'O2')
    assert 'substitution_not_authorized_by_contract' in eval3['reasons']
    assert 'O2' not in r['accepted']

    allowed = copy.deepcopy(base)
    for o in allowed['orders']:
        o['contract']['substitutes_allowed'] = True
    assert reconcile(allowed, emit_notifications=False)['ok'] is True


# ---------------------------------------------------------------------------
# Path 2: B gives written revocation consent -> O1 stands, O2 released
# ---------------------------------------------------------------------------
def test_signed_b_revocation_keeps_o1_and_revokes_o2():
    r = reconcile(scenario('revocation'), emit_notifications=True)
    assert r['accepted'] == ['O1']
    assert r['revoked'] == ['O2']
    assert r['ok'] is True
    o1 = r['decisions']['O1']
    assert o1['allocated_kg'] == 8
    assert {(a['box'], a['kg']) for a in o1['allocations']} == {
        ('WH-L1-01', 6), ('WH-L1-02', 2)}
    o2 = r['decisions']['O2']
    assert o2['allocated_kg'] == 0 and o2['allocations'] == []
    assert o2['consent']['id'] == 'RC-O2'
    assert r['occupied_kg'] == 8


def test_unsigned_revocation_is_not_effective_under_signature_policy():
    world = scenario('revocation')
    world['revocation_consents'][0].pop('customer_sign')
    world['revocation_consents'][0]['signed'] = False
    r = reconcile(world, require_signatures=True, emit_notifications=False)
    assert r['revoked'] == []
    assert 'O2' in r['revocation_consent_required']
    rec = r['consent_records'][0]
    assert rec['valid'] is False and rec['invalid_reason'] == 'signature_required'


def test_either_shop_could_be_asked_but_upload_order_never_decides():
    d = data()
    # The unresolved merge lists both symmetric consent candidates.
    minimal = {'stock': {'L1': 10}, 'branches': d['branches']}
    r = reconcile(minimal, emit_notifications=False)
    targets = {o['revoke_order'] for o in r['consent_options']}
    assert targets == {'O1', 'O2'}

    rev = scenario('revocation')
    hashes = set()
    for perm in itertools.permutations(rev['orders']):
        world = copy.deepcopy(rev)
        world['orders'] = list(perm)
        rr = reconcile(world, emit_notifications=False)
        assert rr['accepted'] == ['O1'] and rr['revoked'] == ['O2']
        hashes.add(rr['decision_hash'])
    assert len(hashes) == 1  # fully order independent


def test_server_receipt_order_does_not_preempt_o1():
    # Upload O2 and its revocation first, O1 last: outcome still O1 accepted.
    d = data()
    book = MergeBook({
        'label_contracts': d['label_contracts'], 'boxes': d['boxes'],
        'stock': d['stock']})
    o1, o2 = d['orders'][1], d['orders'][0]
    assert o1['id'] == 'O1' and o2['id'] == 'O2'
    rc = scenario('revocation')['revocation_consents'][0]
    for doc in (o2, rc, o1):  # B first, A last
        book.upload(copy.deepcopy(doc))
    r = book.reconcile()
    assert r['accepted'] == ['O1'] and r['revoked'] == ['O2']
    assert r['decisions']['O1']['allocated_kg'] == 8


# ---------------------------------------------------------------------------
# Formal correction + old notification readback
# ---------------------------------------------------------------------------
def test_correction_notification_supersedes_but_old_notice_reads_back():
    book = MergeBook(scenario('revocation'))
    book.reconcile()

    old = book.read_notice('N-O2-LOCAL')
    assert old['title'] == '接单成功' and old['accepted'] is True

    current = book.current_notice('O2')
    assert current['kind'] == 'formal_correction'
    assert current['supersedes'] == 'N-O2-LOCAL'
    assert '更正' in current['title']
    body = current['body']
    for token in ('O2', '5', '13', '10', '3', 'RC-O2', '接单成功', '撤销'):
        assert token in body
    assert current['consent_id'] == 'RC-O2'

    # Readback never mutates the stored original.
    again = book.read_notice('N-O2-LOCAL')
    assert again == old
    chain = [n['id'] for n in book.notice_chain('O2')]
    assert chain[0] == current['id'] and 'N-O2-LOCAL' in chain


def test_notice_book_keeps_history_append_only():
    nb = NoticeBook(data()['notices'])
    assert nb.read('N-O1-LOCAL')['accepted'] is True
    correction = {'id': 'N-O1-CORR-v2', 'order_id': 'O1', 'version': 2,
                  'supersedes': 'N-O1-LOCAL', 'kind': 'formal_correction'}
    nb.append(correction)
    assert nb.current('O1')['id'] == 'N-O1-CORR-v2'
    assert nb.read('N-O1-LOCAL')['title'] == '接单成功'


# ---------------------------------------------------------------------------
# Signed-order replay / hash dedup / collision
# ---------------------------------------------------------------------------
def test_duplicate_signed_order_upload_is_idempotent_and_occupies_once():
    world = scenario('replenishment')
    book = MergeBook(world)
    first = book.reconcile()
    assert first['occupied_kg'] == 13

    out = book.ingest([copy.deepcopy(o) for o in world['orders']])
    assert out['inserted'] == 0 and out['replayed'] == 2
    second = book.reconcile()
    assert second['occupied_kg'] == 13
    assert len(second['allocations']) == len(first['allocations'])
    assert second['decision_hash'] == first['decision_hash']

    # Re-uploading again still cannot create a second occupation.
    book.ingest([copy.deepcopy(world['orders'][0])])
    third = book.reconcile()
    assert third['occupied_kg'] == 13
    assert book.capture.summary()['unique_documents'] >= 2


def test_same_id_changed_signed_order_is_content_collision():
    book = MergeBook(data())
    tampered = copy.deepcopy(data()['orders'][1])
    assert tampered['id'] == 'O1'
    tampered['kg'] = 9
    with pytest.raises(DocumentCollision):
        book.upload(tampered)
    # A colliding batch is rejected wholesale.
    good = copy.deepcopy(data()['orders'][0])
    with pytest.raises(DocumentCollision):
        book.ingest([tampered, good])
    # Reads still work after the rejected batch.
    assert book.read_notice('N-O2-LOCAL')['accepted'] is True


def test_capture_book_replay_counts_and_timeline():
    d = data()
    cap = CaptureBook()
    cap.ingest(d['orders'])
    out = cap.ingest(d['orders'])
    assert out['inserted'] == 0 and out['replayed'] == 2
    times = [o['signed_at'] for o in cap.timeline('order_signed')]
    assert times == sorted(times)


# ---------------------------------------------------------------------------
# Late return and duplicate outbound scan
# ---------------------------------------------------------------------------
def test_duplicate_late_return_is_applied_once_and_never_double_occupied():
    world = scenario('replenishment')
    book = MergeBook(world)
    before = book.reconcile()
    assert before['on_hand_kg'] == 13 and before['occupied_kg'] == 13

    rt = scenario('late_return')['returns'][0]
    assert book.upload(copy.deepcopy(rt))['status'] == 'captured'
    assert book.upload(copy.deepcopy(rt))['status'] == 'replayed'
    after = book.reconcile()
    assert after['on_hand_kg'] == 14          # +1 physical return exactly once
    assert after['occupied_kg'] == 13         # no second occupation
    ret_events = after['inventory']['return_events']
    assert len(ret_events) == 1               # capture deduped the repeat upload

    book.upload(copy.deepcopy(rt))
    again = book.reconcile()
    assert again['on_hand_kg'] == 14 and again['occupied_kg'] == 13
    assert book.replay_audit()['ok'] is True

    # If the same return id reaches the ledger twice in one raw event list,
    # the inventory movement itself is de-duplicated as well.
    raw_world = copy.deepcopy(world)
    raw_world['returns'] = [copy.deepcopy(rt), copy.deepcopy(rt)]
    raw = reconcile(raw_world, emit_notifications=False)
    assert raw['on_hand_kg'] == 14
    assert raw['inventory']['return_events'][0]['replayed'] == 1


def test_late_return_in_revocation_path_creates_no_new_occupation():
    world = scenario('revocation')
    rt = scenario('late_return')['returns'][0]
    world['returns'].append(rt)
    r = reconcile(world, emit_notifications=False)
    assert r['revoked'] == ['O2'] and r['accepted'] == ['O1']
    assert r['occupied_kg'] == 8
    rt_box = r['box_loads']['WH-L1-RT1']
    assert rt_box['allocated_kg'] == 0


def test_duplicate_outbound_scan_is_counted_once():
    r = reconcile(scenario('outbound_replay'), emit_notifications=False)
    events = {e['id']: e for e in r['inventory']['outbound_events']}
    assert len(events) == 2
    assert events['OS-O1-01']['kg'] == 6 and events['OS-O1-01']['replayed'] == 1
    o1 = r['decisions']['O1']
    assert o1['shipped_kg'] == 8 and o1['required_kg'] == 0
    # 8 kg physically scanned out; only O2's 2 kg reservation remains occupied.
    assert r['occupied_kg'] == 2


# ---------------------------------------------------------------------------
# Replay/version stability and simple helper surfaces
# ---------------------------------------------------------------------------
def test_reconcile_is_deterministic_and_history_only_grows_on_change():
    book = MergeBook(data())
    r1 = book.reconcile()
    r2 = book.reconcile()
    assert r1['decision_hash'] == r2['decision_hash']
    assert len(book.history) == 1
    assert book.replay_audit()['ok'] is True

    book.ingest(scenario('replenishment')['inbound_receipts'])
    r3 = book.reconcile()
    assert len(book.history) == 2
    assert r3['accepted'] == ['O1', 'O2']


def test_allocate_and_fulfillable_helpers_share_one_version():
    world = scenario('replenishment')
    plan = allocate(world)
    assert len(plan) == 4
    assert set(plan.by_order) == {'O1', 'O2'}
    fs = fulfillable_set(world)
    assert fs.result['inventory_version']
    assert fs == {'O1', 'O2'}


def test_existing_teatrace_modules_unaffected():
    # Importing the extended modules must not disturb the blend rule surface.
    from teablend import blend
    assert callable(blend.decide)
