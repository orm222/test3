import base64
import copy
import importlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image
from fastapi.testclient import TestClient
from fixtures import (png, pair, graph, invalid_graphs, EXPECTED_RUNS,
                      CALIBRATION, EVALUATION)


class FabQualityAcceptance(unittest.TestCase):
    def setUp(self):
        # Deliberately fail each required test when the application is absent.
        self.factory = importlib.import_module('fabquality.app').create_app
        self.serial = 0
        self.open_app()

    def open_app(self, lots=2, policy='A', ticket_mode='normal', draft_mode='normal'):
        self.client = TestClient(self.factory(ontology=graph(lots, policy), testing=True,
                                            ticket_mode=ticket_mode, draft_mode=draft_mode))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def upload(self, count=4, delta=80, missing=False, run='r0', event=None, client=None):
        self.serial += 1
        ref, image, mask, bbox = pair(count, delta, missing=missing)
        response = (client or self.client).post('/cases', data={'event_id': event or 'e'+str(self.serial), 'run_id': run},
            files={'reference': ('opaque.png', png(ref), 'image/png'),
                   'test': ('opaque.png', png(image), 'image/png')})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['case_id'], mask, bbox

    def get(self, cid):
        r = self.client.get('/cases/'+cid)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def step(self, cid, status=200):
        r = self.client.post('/cases/'+cid+'/step')
        self.assertEqual(r.status_code, status, r.text)
        return r.json()

    def decide(self, cid, approve=True, status=200):
        r = self.client.post('/cases/'+cid+'/decision', json={'approve': approve})
        self.assertEqual(r.status_code, status, r.text)
        return r.json()

    def mock(self):
        r = self.client.get('/_test/mock')
        self.assertEqual(r.status_code, 200)
        return r.json()

    def inspection(self, result, count, mask, bbox):
        actual = result['inspection']
        self.assertEqual(actual['verdict'], 'defect' if count else 'normal')
        self.assertEqual(actual['defect_pixels'], count)
        self.assertEqual(actual['bbox'], bbox)
        image = Image.open(io.BytesIO(base64.b64decode(actual['mask_png_base64'])))
        self.assertEqual(image.mode, 'L')
        self.assertEqual(image.size, (128, 128))
        np.testing.assert_array_equal(np.asarray(image), mask)

    def policy(self, result, pixels, lots, name='A'):
        pth, lth = (64, 3) if name == 'A' else (128, 4)
        urgent = pixels >= pth or lots >= lth
        expected = dict(policy_id=name, relation_id='policy0', urgent_pixel_threshold=pth,
                        urgent_lot_threshold=lth, defect_pixels=pixels, review_lot_count=lots,
                        pixel_condition=pixels >= pth, lot_condition=lots >= lth,
                        priority='urgent' if urgent else 'standard',
                        queue_code=name+('_URG' if urgent else '_STD'))
        self.assertEqual(result['policy'], expected)
        if result['draft'] is not None:
            for key in ['policy_id', 'priority', 'queue_code']:
                self.assertEqual(result['draft'][key], expected[key])

    def paths(self, result, lots=2):
        self.assertEqual(result['review_lot_ids'], ['L'+str(i) for i in range(lots)])
        paths = result['evidence']
        expected_runs = EXPECTED_RUNS
        self.assertEqual(len(paths), lots+1)
        g = graph(lots)
        times = {n['id']: n['properties']['ended_at'] for n in g['nodes'] if n['type'] == 'ProcessRun'}
        for i in range(lots):
            rows = [p for p in paths if p['lot_id'] == 'L'+str(i)]
            self.assertEqual(sorted(p['related_run_id'] for p in rows), expected_runs['L'+str(i)])
            for p in rows:
                rid = p['related_run_id']
                self.assertEqual(p['node_ids'], [result['case_id'], 'r0', 'E0', rid, 'L'+str(i)])
                self.assertEqual(p['relation_ids'], ['inspects:'+result['case_id'], 'eq0', 'eq'+rid[1:], 'lot'+rid[1:]])
                self.assertEqual(p['inspected_ended_at'], times['r0'])
                self.assertEqual(p['related_ended_at'], times[rid])

    def ticket(self, cid, draft):
        result = self.step(cid)
        self.assertEqual(result['state'], 'COMPLETED')
        self.assertEqual(result['attempts'], 1)
        self.assertEqual(result['draft'], draft)
        self.assertTrue(result['registration']['ticket_id'])
        mock = self.mock()
        self.assertEqual(mock['calls'], 1)
        self.assertEqual(len(mock['tickets']), 1)
        self.assertEqual(mock['tickets'][0]['draft'], draft)
        self.assertEqual(mock['tickets'][0]['case_id'], cid)
        self.assertEqual(mock['tickets'][0]['ticket_id'], result['registration']['ticket_id'])
        return result

    def test_01_normal_and_initial_nulls(self):
        cid, mask, bbox = self.upload(0)
        initial = self.get(cid)
        self.assertEqual(initial['state'], 'RECEIVED')
        for key in ['inspection', 'review_lot_ids', 'evidence', 'policy', 'draft', 'decision', 'registration']:
            self.assertIsNone(initial[key])
        result = self.step(cid)
        self.assertEqual(result['state'], 'CLEARED')
        self.inspection(result, 0, mask, bbox)
        for key in ['review_lot_ids', 'evidence', 'policy', 'draft', 'registration']:
            self.assertIsNone(result[key])
        self.assertEqual(self.step(cid), result)
        self.assertEqual(self.mock(), {'calls': 0, 'tickets': []})

    def test_02_defect_trace_draft_approval_ticket(self):
        cid, mask, bbox = self.upload()
        result = self.step(cid)
        self.assertEqual(result['state'], 'WAITING_APPROVAL')
        self.inspection(result, 4, mask, bbox)
        self.paths(result)
        self.policy(result, 4, 2)
        draft = result['draft']
        self.assertEqual(draft['source_case_id'], cid)
        self.assertEqual(draft['inspected_lot_id'], 'L0')
        self.assertEqual(draft['review_lot_ids'], ['L0', 'L1'])
        self.assertEqual(draft['evidence'], result['evidence'])
        self.assertEqual(draft['steps'], ['검사 근거 확인', '연관 LOT 이력 검토', '담당자 판단 요청'])
        self.assertIsInstance(draft['summary'], str)
        self.assertTrue(draft['summary'].strip())
        self.assertEqual(self.mock()['calls'], 0)
        self.assertEqual(self.decide(cid)['state'], 'APPROVED')
        done = self.ticket(cid, draft)
        self.assertEqual(self.step(cid), done)
        history = done['history']
        self.assertEqual([h['sequence'] for h in history], list(range(1, len(history)+1)))
        for row in history:
            self.assertTrue({'from_state', 'to_state', 'action', 'error'} <= row.keys())

    def test_03_time_window_duplicate_paths(self):
        self.open_app(lots=4)
        cid, _, _ = self.upload()
        result = self.step(cid)
        self.paths(result, 4)
        self.policy(result, 4, 4)
        # Equivalent offset timestamp must still fall exactly on the lower bound.
        g = graph()
        next(n for n in g['nodes'] if n['id'] == 'r1')['properties']['ended_at'] = '2026-01-01T20:00:00+09:00'
        # Move r2 to the other equipment: L1 remains but its duplicate path disappears.
        next(e for e in g['relations'] if e['id'] == 'eq2')['to_id'] = 'E1'
        self.client = TestClient(self.factory(ontology=g, testing=True))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        cid, _, _ = self.upload()
        changed = self.step(cid)
        self.assertEqual(changed['review_lot_ids'], ['L0', 'L1'])
        self.assertEqual(sorted(p['related_run_id'] for p in changed['evidence']), ['r0','r1'])
        self.assertEqual(next(p for p in changed['evidence'] if p['related_run_id']=='r1')['related_ended_at'],
                         '2026-01-01T20:00:00+09:00')

    def test_04_invalid_png_fields_run_and_graph(self):
        ref, image, _, _ = pair()
        for content in [b'broken', png(np.zeros((64,128), dtype=np.uint8)),
                        png(np.zeros((128,128,3), dtype=np.uint8)),
                        png(np.zeros((128,128), dtype=np.uint16))]:
            with self.subTest(content_length=len(content)):
                r = self.client.post('/cases', data={'event_id': 'invalid', 'run_id': 'r0'},
                    files={'reference': ('a.png', png(ref)), 'test': ('b.png', content)})
                self.assertEqual(r.status_code, 422)
        for data in [{}, {'event_id': 'invalid'}, {'event_id': 'invalid', 'run_id': 'unknown'}]:
            r = self.client.post('/cases', data=data, files={'reference': ('a.png', png(ref)), 'test': ('b.png', png(image))})
            self.assertEqual(r.status_code, 422)
        r = self.client.post('/cases', data={'event_id': 'invalid', 'run_id': 'r0'})
        self.assertEqual(r.status_code, 422)
        # Reuse rejected event ID: rejection must not reserve it.
        self.upload(event='invalid')
        for g in invalid_graphs():
            with self.subTest(graph=g), self.assertRaises(ValueError):
                with TestClient(self.factory(ontology=g, testing=True)):
                    pass
        for path in ['/cases/missing', '/cases/missing/step', '/cases/missing/decision']:
            r = self.client.get(path) if path == '/cases/missing' else self.client.post(path, json={'approve': True})
            self.assertEqual(r.status_code, 404)

    def test_05_event_and_decision_idempotency(self):
        cid, _, _ = self.upload(event='same')
        result = self.step(cid)
        ref, image, _, _ = pair()
        r = self.client.post('/cases', data={'event_id': 'same', 'run_id': 'r0'},
            files={'reference': ('renamed.png', png(ref, True)), 'test': ('renamed.png', png(image, True))})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['case_id'], cid)
        self.assertEqual(self.get(cid), result)
        for run, count in [('r0', 5), ('r1', 4)]:
            _, other, _, _ = pair(count)
            r = self.client.post('/cases', data={'event_id': 'same', 'run_id': run},
                files={'reference': ('a', png(ref)), 'test': ('b', png(other))})
            self.assertEqual(r.status_code, 409)
        other, _, _ = self.upload(event='different')
        self.assertNotEqual(cid, other)
        approved = self.decide(cid)
        self.assertEqual(self.decide(cid), approved)
        self.decide(cid, False, 409)
        done = self.ticket(cid, result['draft'])
        self.assertEqual(self.decide(cid), done)
        self.decide(cid, False, 409)

    def test_06_approval_barrier_rejection(self):
        cid, _, _ = self.upload()
        self.decide(cid, status=409)
        self.step(cid)
        self.step(cid, 409)
        self.assertEqual(self.mock()['calls'], 0)
        rejected = self.decide(cid, False)
        self.assertEqual(rejected['state'], 'REJECTED')
        self.assertEqual(self.decide(cid, False), rejected)
        self.decide(cid, True, 409)
        self.assertEqual(self.step(cid), rejected)
        self.assertEqual(self.mock(), {'calls': 0, 'tickets': []})

    def test_07_response_loss_retry(self):
        self.open_app(ticket_mode='after_commit_once')
        cid, _, _ = self.upload()
        draft = self.step(cid)['draft']
        self.decide(cid)
        first = self.step(cid)
        self.assertEqual(first['state'], 'APPROVED')
        self.assertEqual(first['attempts'], 1)
        self.assertTrue(first['errors'])
        self.assertEqual(len(self.mock()['tickets']), 1)
        second = self.step(cid)
        self.assertEqual(second['state'], 'COMPLETED')
        self.assertEqual(second['attempts'], 2)
        self.assertEqual(second['errors'], first['errors'])
        self.assertEqual(self.mock()['calls'], 2)
        self.assertEqual(len(self.mock()['tickets']), 1)
        self.assertEqual(self.mock()['tickets'][0]['draft'], draft)
        self.assertEqual(self.step(cid), second)

    def test_08_blocked_preserves_results_and_permanent_failure(self):
        self.open_app(draft_mode='fail')
        cid, mask, bbox = self.upload(64)
        blocked = self.step(cid)
        self.assertEqual(blocked['state'], 'BLOCKED')
        self.inspection(blocked, 64, mask, bbox)
        self.paths(blocked)
        self.policy(blocked, 64, 2)
        self.assertIsNone(blocked['draft'])
        self.assertTrue(blocked['errors'])
        self.assertEqual(self.get(cid), blocked)
        self.assertEqual(self.step(cid), blocked)
        self.decide(cid, status=409)
        self.assertEqual(self.mock(), {'calls': 0, 'tickets': []})
        self.open_app(ticket_mode='always_fail')
        cid, _, _ = self.upload()
        self.step(cid)
        self.decide(cid)
        for attempt in range(1, 4):
            result = self.step(cid)
            self.assertEqual(result['attempts'], attempt)
            self.assertEqual(result['state'], 'FAILED' if attempt == 3 else 'APPROVED')
            self.assertEqual(len(result['errors']), attempt)
            self.assertEqual(self.mock(), {'calls': attempt, 'tickets': []})
        self.assertEqual(self.step(cid), result)

    def test_09_pixel_policy_boundaries_and_B(self):
        for count, name in [(63,'A'), (64,'A'), (65,'A'), (64,'B'), (127,'B'), (128,'B')]:
            with self.subTest(count=count, policy=name):
                self.open_app(policy=name)
                cid, mask, bbox = self.upload(count)
                result = self.step(cid)
                self.inspection(result, count, mask, bbox)
                self.policy(result, count, 2, name)
                self.assertEqual(self.mock()['calls'], 0)
                self.decide(cid)
                self.ticket(cid, result['draft'])

    def test_10_lot_policy_and_urgent_retry(self):
        for lots in [2, 3, 4]:
            for name in ['A', 'B']:
                with self.subTest(lots=lots, policy=name):
                    self.open_app(lots=lots, policy=name)
                    cid, _, _ = self.upload()
                    result = self.step(cid)
                    self.paths(result, lots)
                    self.policy(result, 4, lots, name)
        self.open_app(lots=4, ticket_mode='after_commit_once')
        cid, _, _ = self.upload(128)
        original = self.step(cid)
        self.policy(original, 128, 4)
        self.assertEqual(self.mock()['calls'], 0)
        self.decide(cid)
        self.assertEqual(self.step(cid)['state'], 'APPROVED')
        final = self.step(cid)
        self.assertEqual(final['state'], 'COMPLETED')
        self.assertEqual(final['policy'], original['policy'])
        self.assertEqual(final['draft'], original['draft'])
        self.assertEqual(final['attempts'], 2)
        self.assertEqual(self.mock()['calls'], 2)
        self.assertEqual(len(self.mock()['tickets']), 1)
        self.assertEqual(self.mock()['tickets'][0]['draft'], original['draft'])

    def test_11_signed_difference_32_and_three_four(self):
        for count, delta, missing, expected in [(4,32,False,0), (3,33,False,0),
                (4,33,False,4), (4,80,True,4), (3,80,True,0)]:
            with self.subTest(count=count, delta=delta, missing=missing):
                cid, mask, bbox = self.upload(count, delta, missing)
                result = self.step(cid)
                self.inspection(result, expected, mask, bbox)
                self.assertEqual(result['state'], 'WAITING_APPROVAL' if expected else 'CLEARED')
        # Four disconnected singletons count together; no component-size filtering.
        ref = np.zeros((128,128), dtype=np.uint8)
        image = ref.copy()
        expected = ref.copy()
        for y,x in [(1,1),(1,100),(100,1),(100,100)]:
            image[y,x] = 255
            expected[y,x] = 255
        r = self.client.post('/cases', data={'event_id':'disconnected','run_id':'r0'},
            files={'reference':('a',png(ref)), 'test':('b',png(image))})
        self.assertEqual(r.status_code,200)
        self.inspection(self.step(r.json()['case_id']),4,expected,[1,1,101,101])

    def test_12_size_priority_no_event_created(self):
        ref, image, _, _ = pair()
        for field in ['reference', 'test']:
            for data in [{'event_id':'oversize','run_id':'unknown'}, {'event_id':'oversize'}]:
                files = {'reference': ('a', png(ref)), 'test': ('b', png(image))}
                files[field] = ('large', b'x' * (1024*1024+1))
                self.assertEqual(self.client.post('/cases', data=data, files=files).status_code, 413)
        # Exactly 1MiB is not too large, though these bytes are invalid PNG.
        self.assertEqual(self.client.post('/cases', data={'event_id':'oversize','run_id':'unknown'},
            files={'reference': ('a', b'x'*(1024*1024)), 'test': ('b', png(image))}).status_code, 422)
        self.upload(event='oversize')

    def test_13_reference_excluded_service_scope(self):
        # No SELECTION_REFERENCE path is passed or required for startup or HTTP processing.
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                self.open_app()
                cid, _, _ = self.upload()
                self.assertEqual(self.step(cid)['state'], 'WAITING_APPROVAL')
            finally:
                os.chdir(cwd)

    def test_14_twelve_independent_pairs(self):
        self.assertEqual(len(CALIBRATION), 4)
        self.assertEqual(len(EVALUATION), 8)
        for seed, specs in [(104729, CALIBRATION), (130363, EVALUATION)]:
            for index, (count, missing) in enumerate(specs):
                ref, image, mask, bbox = pair(count, seed=seed+index, missing=missing)
                r = self.client.post('/cases', data={'event_id':str(seed)+'-'+str(index),'run_id':'r0'},
                    files={'reference': ('image.png', png(ref)), 'test': ('image.png', png(image))})
                self.assertEqual(r.status_code, 200)
                result = self.step(r.json()['case_id'])
                self.inspection(result, count if count >= 4 else 0, mask, bbox)

    def test_15_eval_metrics_isolated_and_repeatable(self):
        cid, _, _ = self.upload()
        before = self.get(cid)
        first = self.client.post('/eval/run')
        self.assertEqual(first.status_code, 200)
        report = first.json()
        self.assertEqual(report['scenario_count'], 10)
        self.assertEqual(report['image_pair_count'], 8)
        self.assertEqual(report['scenario_results'], [dict(id=i, passed=True) for i in range(1,11)])
        # Independent evaluation mask sum: 4+16+63+64+65+128 = 340.
        expected = {'image_accuracy':(8,8), 'mask_precision':(340,340), 'mask_recall':(340,340),
                    'lot_precision':(43,43), 'lot_recall':(43,43), 'path_validity':(61,61),
                    'automation_match':(10,10), 'policy_match':(18,18), 'ticket_policy_match':(8,8)}
        for key, (n,d) in expected.items():
            self.assertEqual(report['metrics'][key], {'numerator':n,'denominator':d,'value':n/d})
        self.assertEqual(report['metrics']['approval_before_calls'], 0)
        self.assertEqual(report['metrics']['duplicate_tickets'], 0)
        self.assertEqual(self.client.post('/eval/run').json(), report)
        self.assertEqual(self.get(cid), before)
        self.assertEqual(self.mock(), {'calls':0,'tickets':[]})


if __name__ == '__main__':
    unittest.main()
