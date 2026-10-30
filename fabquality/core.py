import base64
import copy
import io
import json
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from PIL import Image


def png(array):
    output = io.BytesIO()
    Image.fromarray(array).save(output, format='PNG')
    return output.getvalue()


def decode(raw):
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.format != 'PNG' or image.mode != 'L' or image.size != (128, 128):
                raise ValueError('Expected 128x128 8-bit L PNG')
            image.load()
            return np.array(image)
    except Exception as exc:
        raise ValueError('Invalid PNG: ' + str(exc)) from exc


def inspect(reference, test):
    candidates = np.abs(test.astype(np.int16) - reference.astype(np.int16)) > 32
    count = int(candidates.sum())
    if count < 4:
        candidates[:] = False
        count = 0
    ys, xs = np.where(candidates)
    bbox = [int(xs.min()), int(ys.min()), int(xs.max())+1, int(ys.max())+1] if count else None
    return dict(verdict='defect' if count else 'normal', defect_pixels=count, bbox=bbox,
                mask_png_base64=base64.b64encode(png(candidates.astype(np.uint8)*255)).decode())


class Graph:
    def __init__(self, data=None):
        if data is None:
            data = json.loads(Path(__file__).with_name('ontology.json').read_text())
        self.data = copy.deepcopy(data)
        try:
            self._validate()
        except (KeyError, TypeError, AttributeError, OverflowError) as exc:
            raise ValueError('Invalid ontology: '+str(exc)) from exc

    def _validate(self):
        self.nodes, self.edges, self.links, self.times = {}, {}, {}, {}
        types = {'Lot', 'Equipment', 'ProcessRun', 'QualityPolicy', 'Inspection'}
        directions = {'OF_LOT': ('ProcessRun','Lot'), 'ON': ('ProcessRun','Equipment'),
                      'REVIEW_POLICY': ('Equipment','QualityPolicy'), 'INSPECTS': ('Inspection','ProcessRun')}
        for n in self.data['nodes']:
            ident = n['id']
            if not isinstance(ident, str) or not ident or ident in self.nodes or n['type'] not in types or not isinstance(n['properties'], dict):
                raise ValueError('Invalid or duplicate node')
            self.nodes[ident] = n
            p = n['properties']
            if n['type'] == 'ProcessRun':
                t = datetime.fromisoformat(p['ended_at'].replace('Z', '+00:00'))
                if t.utcoffset() is None:
                    raise ValueError('ended_at requires offset')
                self.times[ident] = t
            if n['type'] == 'QualityPolicy':
                for key, minimum in [('urgent_pixel_threshold',4), ('urgent_lot_threshold',1)]:
                    if type(p[key]) is not int or p[key] < minimum:
                        raise ValueError('Invalid policy threshold')
                for key in ['standard_queue','urgent_queue']:
                    if not isinstance(p[key], str) or not p[key].strip():
                        raise ValueError('Invalid queue')
                if p['standard_queue'] == p['urgent_queue']:
                    raise ValueError('Queues must differ')
        for e in self.data['relations']:
            ident, kind, source, target = e['id'], e['type'], e['from_id'], e['to_id']
            if not isinstance(ident,str) or not ident or ident in self.edges or kind not in directions:
                raise ValueError('Invalid or duplicate relation')
            if source not in self.nodes or target not in self.nodes:
                raise ValueError('Dangling relation')
            if (self.nodes[source]['type'], self.nodes[target]['type']) != directions[kind]:
                raise ValueError('Invalid relation direction')
            if (source,kind) in self.links:
                raise ValueError('Duplicate outgoing relation')
            self.edges[ident] = e
            self.links[source,kind] = e
        for ident, n in self.nodes.items():
            for kind in {'ProcessRun':['OF_LOT','ON'], 'Equipment':['REVIEW_POLICY'], 'Inspection':['INSPECTS']}.get(n['type'],[]):
                if (ident,kind) not in self.links:
                    raise ValueError('Missing required relation')

    def edge(self, source, kind):
        return self.links[source,kind]

    def trace(self, cid, run):
        eq = self.edge(run,'ON')
        t0 = self.times[run]
        paths = []
        for related, time in self.times.items():
            on = self.edge(related,'ON')
            if on['to_id'] != eq['to_id'] or not t0-timedelta(minutes=60) <= time <= t0:
                continue
            lot = self.edge(related,'OF_LOT')
            paths.append(dict(lot_id=lot['to_id'], related_run_id=related,
                node_ids=[cid,run,eq['to_id'],related,lot['to_id']],
                relation_ids=['inspects:'+cid,eq['id'],on['id'],lot['id']],
                inspected_ended_at=self.nodes[run]['properties']['ended_at'],
                related_ended_at=self.nodes[related]['properties']['ended_at']))
        paths.sort(key=lambda p:(p['lot_id'],p['related_run_id']))
        return sorted({p['lot_id'] for p in paths}), paths

    def policy(self, run, pixels, lots):
        equipment = self.edge(run,'ON')['to_id']
        edge = self.edge(equipment,'REVIEW_POLICY')
        p = self.nodes[edge['to_id']]['properties']
        pc, lc = pixels >= p['urgent_pixel_threshold'], lots >= p['urgent_lot_threshold']
        return dict(policy_id=edge['to_id'],relation_id=edge['id'],
            urgent_pixel_threshold=p['urgent_pixel_threshold'],urgent_lot_threshold=p['urgent_lot_threshold'],
            defect_pixels=pixels,review_lot_count=lots,pixel_condition=pc,lot_condition=lc,
            priority='urgent' if pc or lc else 'standard',queue_code=p['urgent_queue' if pc or lc else 'standard_queue'])


class MockTickets:
    def __init__(self, mode):
        if mode not in {'normal','after_commit_once','always_fail'}:
            raise ValueError('Unknown ticket mode')
        self.mode, self.calls, self.tickets = mode, 0, {}

    def create_review_ticket(self, case_id, draft):
        self.calls += 1
        if case_id in self.tickets:
            ticket = self.tickets[case_id]
            if ticket['draft'] != draft:
                raise ValueError('Idempotency key reused with changed draft')
            return ticket['ticket_id']
        if self.mode == 'always_fail':
            raise RuntimeError('Mock registration failed before commit')
        tid = 'ticket-'+str(len(self.tickets)+1)
        self.tickets[case_id] = dict(ticket_id=tid,case_id=case_id,draft=copy.deepcopy(draft))
        if self.mode == 'after_commit_once':
            raise RuntimeError('Mock response lost after commit')
        return tid

    def snapshot(self):
        return copy.deepcopy(dict(calls=self.calls,tickets=list(self.tickets.values())))


class ServiceError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


class Service:
    def __init__(self, ontology=None, ticket_mode='normal', draft_mode='normal'):
        self.graph = Graph(ontology)
        self.mock = MockTickets(ticket_mode)
        if draft_mode not in {'normal','fail'}:
            raise ValueError('Unknown draft mode')
        self.draft_mode = draft_mode
        self.cases, self.events, self.inputs, self.inspections = {}, {}, {}, {}

    def receive(self, event, run, reference, test):
        if any(len(raw)>1048576 for raw in (reference,test) if raw is not None):
            raise ServiceError(413,'File exceeds 1MiB')
        if not isinstance(event,str) or not event.strip() or run not in self.graph.times or reference is None or test is None:
            raise ServiceError(422,'Missing fields or unknown run')
        try:
            arrays = decode(reference), decode(test)
        except ValueError as exc:
            raise ServiceError(422,str(exc)) from exc
        if event in self.events:
            cid = self.events[event]
            oldrun, a, b = self.inputs[cid]
            if oldrun != run or not np.array_equal(a,arrays[0]) or not np.array_equal(b,arrays[1]):
                raise ServiceError(409,'Conflicting event')
            return self.get(cid)
        cid = 'case-'+str(len(self.cases)+1)
        while cid in self.graph.nodes or 'inspects:'+cid in self.graph.edges:
            cid += '-new'
        c = dict(case_id=cid,event_id=event,run_id=run,state='RECEIVED',attempts=0,
                 errors=[],history=[])
        c.update({key:None for key in ['inspection','review_lot_ids','evidence','policy','draft','decision','registration']})
        self.cases[cid], self.events[event], self.inputs[cid] = c, cid, (run,*arrays)
        self.inspections[cid] = dict(node=dict(id=cid,type='Inspection',properties={}),
            relation=dict(id='inspects:'+cid,type='INSPECTS',from_id=cid,to_id=run))
        self.log(c,None,'RECEIVED','receive')
        return self.get(cid)

    def log(self,c,old,new,action,error=None):
        c['state'] = new
        c['history'].append(dict(sequence=len(c['history'])+1,from_state=old,to_state=new,action=action,error=error))

    def get(self,cid):
        if cid not in self.cases:
            raise ServiceError(404,'Case not found')
        return copy.deepcopy(self.cases[cid])

    def decision(self,cid,approve):
        self.get(cid)
        c = self.cases[cid]
        if c['decision'] is not None:
            if c['decision']['approve'] != approve:
                raise ServiceError(409,'Opposite decision')
            return self.get(cid)
        if c['state'] != 'WAITING_APPROVAL':
            raise ServiceError(409,'Not waiting for approval')
        c['decision'] = dict(approve=approve)
        self.log(c,c['state'],'APPROVED' if approve else 'REJECTED','decision')
        return self.get(cid)

    def step(self,cid):
        self.get(cid)
        c = self.cases[cid]
        state = c['state']
        if state == 'WAITING_APPROVAL':
            raise ServiceError(409,'Approval required')
        if state == 'RECEIVED':
            run, ref, test = self.inputs[cid]
            c['inspection'] = inspect(ref,test)
            self.log(c,state,state,'inspect')
            if c['inspection']['verdict'] == 'normal':
                self.log(c,state,'CLEARED','clear')
            else:
                c['review_lot_ids'], c['evidence'] = self.graph.trace(cid,run)
                self.log(c,state,state,'trace')
                c['policy'] = self.graph.policy(run,c['inspection']['defect_pixels'],len(c['review_lot_ids']))
                self.log(c,state,state,'policy')
                try:
                    if self.draft_mode == 'fail':
                        raise RuntimeError('Explanation stub failed')
                    c['draft'] = dict(source_case_id=cid,inspected_lot_id=self.graph.edge(run,'OF_LOT')['to_id'],
                        review_lot_ids=copy.deepcopy(c['review_lot_ids']),
                        **{k:c['policy'][k] for k in ['policy_id','priority','queue_code']},
                        summary='검사 이상 {}픽셀; 동일 설비 시간 범위의 {}개 LOT 이력 검토 요청.'.format(c['inspection']['defect_pixels'],len(c['review_lot_ids'])),
                        evidence=copy.deepcopy(c['evidence']),steps=['검사 근거 확인','연관 LOT 이력 검토','담당자 판단 요청'])
                    self.log(c,state,'WAITING_APPROVAL','draft')
                except Exception as exc:
                    c['errors'].append(dict(stage='draft',message=str(exc),attempt=0))
                    self.log(c,state,'BLOCKED','draft',str(exc))
        elif state == 'APPROVED':
            c['attempts'] += 1
            try:
                tid = self.mock.create_review_ticket(cid,c['draft'])
                c['registration'] = dict(ticket_id=tid)
                self.log(c,state,'COMPLETED','ticket')
            except Exception as exc:
                c['errors'].append(dict(stage='ticket',message=str(exc),attempt=c['attempts']))
                self.log(c,state,'FAILED' if c['attempts'] == 3 else 'APPROVED','ticket',str(exc))
        return self.get(cid)
