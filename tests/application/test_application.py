import asyncio
import base64
import copy
import io
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest

import httpx
import numpy as np
from PIL import Image
from fabquality.app import create_app
from fabquality.core import MockTickets, Service, ServiceError
from fabquality.data import pair, graph
from fabquality.evaluation import run, ratio


class ApplicationTests(unittest.TestCase):
    def test_asgi_endpoints(self):
        async def check():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(testing=True)),base_url='http://local') as c:
                ref,test,_=pair()
                files={'reference':('a',ref),'test':('b',test)}
                r=await c.post('/cases',data={'event_id':'e','run_id':'r0'},files=files)
                self.assertEqual(r.status_code,200)
                cid=r.json()['case_id']; path='/cases/'+cid
                self.assertIsNone((await c.get(path)).json()['inspection'])
                self.assertEqual((await c.post(path+'/decision',json={'approve':1})).status_code,422)
                self.assertEqual((await c.post(path+'/step')).json()['state'],'WAITING_APPROVAL')
                self.assertEqual((await c.post(path+'/step')).status_code,409)
                self.assertEqual((await c.get('/_test/mock')).json()['calls'],0)
                self.assertEqual((await c.post(path+'/decision',json={'approve':True})).json()['state'],'APPROVED')
                done=(await c.post(path+'/step')).json()
                self.assertEqual(done['state'],'COMPLETED')
                self.assertEqual((await c.post(path+'/decision',json={'approve':True})).json(),done)
                self.assertEqual((await c.post(path+'/decision',json={'approve':False})).status_code,409)
                self.assertEqual((await c.post(path+'/step')).json(),done)
                self.assertEqual((await c.get('/_test/mock')).json()['calls'],1)
                self.assertEqual((await c.post('/cases',data={'event_id':'e','run_id':'r0'},files=files)).json()['case_id'],cid)
                self.assertEqual((await c.post('/cases',data={'event_id':'e','run_id':'r1'},files=files)).status_code,409)
                self.assertEqual((await c.get('/cases/missing')).status_code,404)
                self.assertEqual((await c.post('/cases/missing/step')).status_code,404)
                self.assertEqual((await c.post('/cases/missing/decision',json={})).status_code,404)
                report=(await c.post('/eval/run')).json()
                self.assertEqual(report['metrics']['lot_recall'],ratio(43,43))
                self.assertEqual((await c.post('/eval/run')).json(),report)
                self.assertEqual((await c.get(path)).json(),done)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()),base_url='http://local') as c:
                self.assertEqual((await c.get('/_test/mock')).status_code,404)
        before=set(threading.enumerate())
        asyncio.run(check())
        self.assertEqual(set(threading.enumerate()),before)

    def test_mock_changed_draft_and_copy(self):
        mock=MockTickets('normal'); draft={'queue_code':'A_STD'}
        tid=mock.create_review_ticket('c',draft)
        draft['queue_code']='changed'
        self.assertEqual(mock.create_review_ticket('c',{'queue_code':'A_STD'}),tid)
        with self.assertRaises(ValueError): mock.create_review_ticket('c',draft)
        self.assertEqual(mock.snapshot()['tickets'][0]['draft'],{'queue_code':'A_STD'})

    def test_eval_truth_and_failure_detection_in_isolated_copy(self):
        report=run()
        self.assertTrue(all(s['passed'] for s in report['scenario_results']))
        self.assertEqual(report['metrics']['path_validity'],ratio(61,61))
        self.assertEqual(ratio(0,0)['value'],None)
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copytree(Path(__file__).resolve().parents[2]/'fabquality',Path(tmp)/'fabquality')
            target=Path(tmp)/'fabquality/core.py'
            target.write_text(target.read_text().replace('> 32','> 100'))
            p=subprocess.run([str(Path('.venv/bin/python').absolute()),'-B','-c',
                'from fabquality.evaluation import run; r=run(); assert r["metrics"]["image_accuracy"]["numerator"]<8; assert any(not x["passed"] for x in r["scenario_results"]); assert r["failures"]; print("injected detector fault detected")'],
                cwd=tmp,capture_output=True,text=True,timeout=20)
            self.assertEqual(p.returncode,0,p.stderr)
            print(p.stdout.strip())

    def test_ontology_validation_and_policy_boundaries(self):
        baseline=graph()
        for key,value in [('urgent_pixel_threshold',True),('urgent_pixel_threshold',3),
                          ('urgent_lot_threshold',0),('urgent_lot_threshold','3'),
                          ('standard_queue',''),('urgent_queue','A_STD')]:
            bad=copy.deepcopy(baseline)
            next(n for n in bad['nodes'] if n['id']=='A')['properties'][key]=value
            with self.assertRaises(ValueError): Service(bad)
        for kind in ['missing','duplicate','direction','dangling','node','edge','time','lot','type']:
            bad=copy.deepcopy(baseline)
            if kind=='missing': bad['relations'].pop(0)
            if kind=='duplicate': bad['relations'].append(dict(id='new',type='REVIEW_POLICY',from_id='E0',to_id='B'))
            if kind=='direction': bad['relations'][0]['from_id']='A'
            if kind=='dangling': bad['relations'][0]['to_id']='absent'
            if kind=='node': bad['nodes'].append(copy.deepcopy(bad['nodes'][0]))
            if kind=='edge': bad['relations'].append(copy.deepcopy(bad['relations'][0]))
            if kind=='time': next(n for n in bad['nodes'] if n['id']=='r0')['properties']['ended_at']='2026-01-01T12:00:00'
            if kind=='lot': bad['relations']=[e for e in bad['relations'] if e['id']!='lot0']
            if kind=='type': bad['nodes'][0]['type']='Unknown'
            with self.subTest(kind=kind), self.assertRaises(ValueError): Service(bad)
        for pixels,lots,name,urgent in [(63,2,'A',False),(64,2,'A',True),(65,2,'A',True),
                                       (64,2,'B',False),(127,2,'B',False),(128,2,'B',True),
                                       (4,2,'A',False),(4,3,'A',True),(4,4,'A',True),(4,3,'B',False),(4,4,'B',True)]:
            data=graph(lots,name); original=copy.deepcopy(data); s=Service(data)
            a,b,_=pair(pixels); cid=s.receive('e','r0',a,b)['case_id']; result=s.step(cid)
            self.assertEqual(data,original)
            self.assertEqual(result['policy']['priority'],'urgent' if urgent else 'standard')
            self.assertEqual(len(result['evidence']),lots+1)
            self.assertEqual(result['review_lot_ids'],['L'+str(i) for i in range(lots)])
            result['draft']['queue_code']='mutated response'
            self.assertNotEqual(s.get(cid)['draft']['queue_code'],'mutated response')
        data=graph()
        next(n for n in data['nodes'] if n['id']=='r1')['properties']['ended_at']='2026-01-01T20:00:00+09:00'
        next(e for e in data['relations'] if e['id']=='eq2')['to_id']='E1'
        s=Service(data); a,b,_=pair(); cid=s.receive('e','r0',a,b)['case_id']
        self.assertEqual([p['related_run_id'] for p in s.step(cid)['evidence']],['r0','r1'])

    def test_recovery_and_boundaries(self):
        for mode,state,attempts,tickets in [('normal','COMPLETED',1,1),('after_commit_once','COMPLETED',2,1),('always_fail','FAILED',3,0)]:
            s=Service(graph(),mode); a,b,_=pair(); cid=s.receive('e','r0',a,b)['case_id']; s.step(cid); s.decision(cid,True)
            for _ in range(attempts): result=s.step(cid)
            self.assertEqual(result['state'],state); self.assertEqual(len(s.mock.tickets),tickets)
            self.assertEqual(s.step(cid),result)
        s=Service(); a,b,_=pair()
        with self.assertRaises(ServiceError) as error: s.receive('bad','unknown',b'x'*1048577,b)
        self.assertEqual(error.exception.status,413)
        self.assertFalse(s.cases)
        s=Service(graph(),draft_mode='fail'); a,b,_=pair(64); cid=s.receive('e','r0',a,b)['case_id']; r=s.step(cid)
        self.assertEqual(r['state'],'BLOCKED'); self.assertIsNotNone(r['policy']); self.assertIsNotNone(r['evidence']); self.assertIsNone(r['draft'])
        for count,delta,expected in [(3,33,0),(4,32,0),(4,33,4)]:
            s=Service(); a,b,_=pair(count,delta=delta); cid=s.receive('e','r0',a,b)['case_id']
            self.assertEqual(s.step(cid)['inspection']['defect_pixels'],expected)
