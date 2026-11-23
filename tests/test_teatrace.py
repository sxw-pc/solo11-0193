import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
import teatrace
from teatrace.store import connect,ingest,report,audit

ROOT=Path(__file__).resolve().parents[1]
def fixture(name):return json.loads((ROOT/'fixtures'/f'{name}.json').read_text(encoding='utf-8'))


class TeaTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.db=connect(Path(self.temp.name)/'case.sqlite')
    def tearDown(self):self.db.close();self.temp.cleanup()
    def put(self,system,name):return ingest(self.db,system,fixture(name))
    def normal(self):
        for system,name in [('garden','garden-ok'),('leaf','leaf-ok'),('blend','blend-ok')]:self.put(system,name)
        return report(self.db)['decisions']
    def all(self):
        self.normal()
        for system,name in [('garden','garden-bad'),('leaf','leaf-bad'),('blend','blend-bad')]:self.put(system,name)
        return report(self.db)['decisions']
    def test_package_path_and_three_entity_pairs(self):
        self.normal();print('MODULE',teatrace.__file__)
        self.assertTrue(Path(teatrace.__file__).resolve().is_relative_to(ROOT))
        for table in ('certificates','pickers','receipts','weights','blends','labels'):
            self.assertGreater(self.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0],0)
    def test_garden_certificate_and_picker_authorize_picking(self):
        d=self.normal();self.assertEqual(d['receipt:R1']['status'],'eligible')
        self.assertEqual(d['certificate:C1']['status'],'certified')
    def test_revocation_blocks_later_leaf_but_keeps_prior_batch(self):
        d=self.all();self.assertEqual(d['receipt:R3']['status'],'certificate_missing')
        self.assertEqual(d['receipt:R1']['status'],'eligible')
        self.assertEqual(d['receipt:R8']['status'],'certificate_revoked_pending')
    def test_duplicate_pick_ticket_is_not_double_paid(self):
        d=self.all();self.assertEqual(d['receipt:Rdup']['status'],'duplicate_pick')
    def test_unqualified_picker_blocks_receipt(self):
        d=self.all();self.assertEqual(d['receipt:R6']['status'],'picker_unauthorized')
    def test_dual_signature_is_required(self):
        d=self.all();self.assertEqual(d['receipt:R7']['status'],'weight_unverified')
        self.assertEqual(d['weight:W7']['status'],'unverified')
    def test_delay_and_overnight_downgrade_premium(self):
        d=self.all();self.assertEqual(d['receipt:R2']['grade'],'standard')
        self.assertEqual(d['receipt:R4']['grade'],'standard')
    def test_mass_balance_blocks_overstatement(self):
        d=self.all();self.assertEqual(d['blend:B2']['status'],'mass_imbalance')
        self.assertEqual(d['label:L2']['status'],'blend_blocked')
    def test_organic_label_tracks_each_source(self):
        self.assertEqual(self.normal()['label:L0']['status'],'label_allowed')
        d=self.all();self.assertEqual(d['label:L0']['status'],'revoked_by_rework')
        self.assertEqual(d['label:L3']['status'],'organic_mislabel')
        self.assertEqual(d['label:L7']['status'],'label_allowed')
    def test_grade_label_tracks_downgrade(self):
        d=self.all();self.assertEqual(d['label:L4']['status'],'grade_mislabel')
    def test_origin_label_tracks_garden_provenance(self):
        d=self.all();self.assertEqual(d['label:L6']['status'],'origin_mislabel')
    def test_late_revocation_recalculates_unprocessed_leaf(self):
        self.normal();self.put('leaf','leaf-bad')
        self.assertEqual(report(self.db)['decisions']['receipt:R8']['status'],'eligible')
        self.put('garden','garden-bad')
        self.assertEqual(report(self.db)['decisions']['receipt:R8']['status'],'certificate_revoked_pending')
        self.assertGreater(len(report(self.db)['history']),len(report(self.db)['decisions']))
    def test_blocked_source_cannot_be_blended(self):
        d=self.all();self.assertEqual(d['blend:B4']['status'],'source_blocked')
    def test_same_event_replay_and_collision_transaction(self):
        self.put('garden','garden-ok');out=self.put('garden','garden-ok')
        self.assertEqual(out['replayed'],4)
        bad=copy.deepcopy(fixture('garden-ok')[0]);bad['organic']=False
        with self.assertRaises(ValueError):ingest(self.db,'garden',[bad])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0],4)
    def test_replay_audit_detects_tampering(self):
        self.all();self.assertTrue(audit(self.db)['ok'])
        self.db.execute("UPDATE decisions SET result='{}' WHERE id='blend:B0'");self.db.commit()
        self.assertFalse(audit(self.db)['ok'])
    def test_fresh_six_stage_cli_demo(self):
        db=Path(self.temp.name)/'demo.sqlite'
        p=subprocess.run([sys.executable,'-m','teatrace','demo','--db',str(db)],cwd=ROOT,capture_output=True,text=True)
        self.assertEqual(p.returncode,0,p.stderr)
        data=json.loads(p.stdout);self.assertEqual(len(data['steps']),6)
        self.assertTrue(data['audit']['ok'])

    def test_receipt_cannot_fund_two_batches(self):
        self.normal()
        conflict={'kind':'blend','id':'B-again','at':'2026-11-10T21:00:00','receipts':['R1'],'output_kg':80}
        ingest(self.db,'blend',[conflict])
        d=report(self.db)['decisions']
        self.assertEqual(d['blend:B-again']['status'],'receipt_allocated')
        self.assertEqual(d['blend:B-again']['allocated_conflicts'],['R1'])
        self.assertEqual(d['label:L0']['status'],'label_allowed')

    def test_rework_revokes_old_label_and_recomputes_origin_ratio(self):
        self.normal();self.put('leaf','leaf-bad');self.put('garden','garden-bad')
        self.assertEqual(report(self.db)['decisions']['label:L0']['status'],'label_allowed')
        self.put('blend','blend-bad')
        d=report(self.db)['decisions']
        self.assertEqual(d['rework:RW0']['status'],'applied')
        self.assertEqual(d['label:L0']['status'],'revoked_by_rework')
        self.assertEqual(d['label:L7']['status'],'label_allowed')
        self.assertEqual(d['blend:B0']['origin_kg'],{'G1':98,'G2':39})
        labels=[{'kind':'label','id':'L70','at':'2026-11-13T09:20:00','blend':'B0','organic':False,'grade':'standard','origin':'G1','minimum_origin_pct':70},
                {'kind':'label','id':'L80','at':'2026-11-13T09:21:00','blend':'B0','organic':False,'grade':'standard','origin':'G1','minimum_origin_pct':80}]
        ingest(self.db,'blend',labels);d=report(self.db)['decisions']
        self.assertEqual(d['label:L70']['status'],'label_allowed')
        self.assertEqual(d['label:L80']['status'],'origin_mislabel')
        self.assertTrue(audit(self.db)['ok'])

    def test_cross_garden_ticket_and_conflicting_double_signed_weight(self):
        self.normal()
        receipt=copy.deepcopy(fixture('leaf-ok')[4]);receipt.update(id='R-cross',pick_ticket='T1')
        ingest(self.db,'leaf',[receipt])
        self.assertEqual(report(self.db)['decisions']['receipt:R-cross']['status'],'duplicate_pick')
        weight=copy.deepcopy(fixture('leaf-ok')[1]);weight.update(id='W1-again',net_kg=90,at='2026-11-10T11:00:00')
        ingest(self.db,'leaf',[weight]);d=report(self.db)['decisions']
        self.assertEqual(d['receipt:R1']['status'],'mass_dispute')
        self.assertEqual(d['blend:B0']['status'],'source_blocked')

    def test_overage_leaf_requires_rework_proof(self):
        self.normal()
        receipt=copy.deepcopy(fixture('leaf-ok')[0]);receipt.update(id='R-age',pick_ticket='T-age',leaf_age_days=3)
        weight=copy.deepcopy(fixture('leaf-ok')[1]);weight.update(id='W-age',receipt='R-age',net_kg=96)
        ingest(self.db,'leaf',[receipt,weight])
        blend={'kind':'blend','id':'B-age','at':'2026-11-10T21:00:00','receipts':['R-age'],'output_kg':90}
        ingest(self.db,'blend',[blend]);d=report(self.db)['decisions']
        self.assertEqual(d['receipt:R-age']['route'],'rework')
        self.assertEqual(d['blend:B-age']['status'],'rework_required')
        rework={'kind':'rework','id':'RW-age','at':'2026-11-10T22:00:00','blend':'B-age','receipts':['R-age'],'output_kg':88}
        ingest(self.db,'blend',[rework]);d=report(self.db)['decisions']
        self.assertEqual(d['blend:B-age']['status'],'balanced')
        self.assertEqual(d['rework:RW-age']['status'],'applied')

    def test_late_rework_cannot_steal_previously_allocated_receipt(self):
        self.normal()
        other={'kind':'blend','id':'B-earlier','at':'2026-11-11T10:00:00','receipts':['R5'],'output_kg':35}
        ingest(self.db,'blend',[other])
        revision={'kind':'rework','id':'RW-conflict','at':'2026-11-13T09:00:00',
                  'blend':'B0','receipts':['R1','R5'],'output_kg':120}
        ingest(self.db,'blend',[revision]);d=report(self.db)['decisions']
        self.assertEqual(d['blend:B-earlier']['status'],'balanced')
        self.assertEqual(d['blend:B0']['status'],'receipt_allocated')
        self.assertEqual(d['rework:RW-conflict']['status'],'blocked')
        self.assertEqual(d['label:L0']['status'],'revoked_by_rework')


if __name__=='__main__':unittest.main()
