"""Fresh-state scenario execution and independent, fixed oracle accounting."""
import base64
import io
import json

import numpy as np
from PIL import Image
from .core import Service, ServiceError
from .data import EVALUATION, pair, graph


def ratio(n,d):
    return dict(numerator=int(n),denominator=int(d),value=n/d if d else None)


def run():
    counts = {k:[0,0] for k in ['image_accuracy','mask_precision','mask_recall','lot_precision',
                               'lot_recall','path_validity','automation_match','policy_match','ticket_policy_match']}
    failures, results = [], []
    before_calls = duplicates = 0
    # These truth masks are injection outputs; they never enter Service.
    for i,(pixels,missing) in enumerate(EVALUATION):
        ref,test,truth = pair(pixels,missing,130363+i)
        s = Service(graph())
        cid = s.receive('image','r0',ref,test)['case_id']
        result = s.step(cid)
        prediction = result['inspection']
        actual = np.asarray(Image.open(io.BytesIO(base64.b64decode(prediction['mask_png_base64']))))>0
        expected = truth>0
        tp,fp,fn = int((actual&expected).sum()),int((actual&~expected).sum()),int((~actual&expected).sum())
        counts['image_accuracy'][0] += prediction['verdict'] == ('defect' if pixels>=4 else 'normal')
        counts['image_accuracy'][1] += 1
        for key,n,d in [('mask_precision',tp,tp+fp),('mask_recall',tp,tp+fn)]:
            counts[key][0] += n
            counts[key][1] += d
        if fp or fn or prediction['verdict'] != ('defect' if pixels>=4 else 'normal'):
            failures.append(dict(image=i,mask=prediction,paths=result['evidence'],history=result['history']))

    for scenario in range(1,11):
        services, cases, checks = [], [], []
        def new(lots=2,policy='A',ticket='normal',draft='normal'):
            s = Service(graph(lots,policy),ticket,draft)
            services.append(s)
            return s
        def case(s,pixels=4,lots=2,policy='A',event=None):
            ref,test,_ = pair(pixels)
            cid = s.receive(event or 'event-'+str(len(cases)),'r0',ref,test)['case_id']
            result = s.step(cid)
            if pixels>=4:
                cases.append((s,cid,pixels,lots,policy))
            return cid,result
        def expect_error(status,fn):
            try:
                fn()
                checks.append(False)
            except ServiceError as exc:
                checks.append(exc.status==status)
        def approve(s,cid,retry=False):
            s.decision(cid,True)
            first = s.step(cid)
            if retry:
                checks.append(first['state']=='APPROVED' and first['attempts']==1 and len(s.mock.tickets)==1)
                done = s.step(cid)
                checks.append(done['attempts']==2 and len(done['errors'])==1)
            else:
                done = first
            checks.append(done['state']=='COMPLETED')
        try:
            if scenario==1:
                s=new(); cid,r=case(s,0)
                checks += [r['state']=='CLEARED',s.step(cid)==r,len(s.mock.tickets)==0]
            elif scenario==2:
                s=new(); cid,r=case(s); approve(s,cid)
            elif scenario==3:
                s=new(4); cid,r=case(s,lots=4)
                checks.append(r['state']=='WAITING_APPROVAL')
            elif scenario==4:
                s=new(); ref,test,_=pair()
                expect_error(422,lambda:s.receive('invalid','r0',ref,b'bad'))
                expect_error(422,lambda:s.receive('invalid','unknown',ref,test))
                expect_error(422,lambda:s.receive(None,'r0',ref,test))
                expect_error(413,lambda:s.receive('invalid','unknown',b'x'*1048577,test))
                bad=graph(); bad['relations'].pop(0)
                try:
                    Service(bad); checks.append(False)
                except ValueError:
                    checks.append(True)
                checks.append(not s.cases)
            elif scenario==5:
                s=new(); cid,r=case(s,event='repeat'); ref,test,_=pair()
                checks.append(s.receive('repeat','r0',ref,test)['case_id']==cid)
                expect_error(409,lambda:s.receive('repeat','r1',ref,test))
                s.decision(cid,True); original=s.get(cid)
                checks.append(s.decision(cid,True)==original)
                expect_error(409,lambda:s.decision(cid,False))
                checks.append(s.step(cid)['state']=='COMPLETED')
                other,_=case(s,event='distinct'); checks.append(other!=cid)
            elif scenario==6:
                s=new(); cid,r=case(s)
                expect_error(409,lambda:s.step(cid))
                checks.append(s.mock.calls==0)
                rejected=s.decision(cid,False)
                checks += [rejected['state']=='REJECTED',s.step(cid)==rejected,s.decision(cid,False)==rejected]
                expect_error(409,lambda:s.decision(cid,True))
                case(s)
            elif scenario==7:
                s=new(ticket='after_commit_once'); cid,r=case(s); approve(s,cid,True)
            elif scenario==8:
                s=new(draft='fail'); cid,r=case(s)
                checks += [r['state']=='BLOCKED',r['draft'] is None,r['inspection'] is not None,
                           r['policy'] is not None,r['evidence'] is not None,s.step(cid)==r]
                s=new(ticket='always_fail'); cid,r=case(s); s.decision(cid,True)
                for attempt in range(1,4):
                    r=s.step(cid)
                    checks += [r['attempts']==attempt,len(r['errors'])==attempt,
                               r['state']==('FAILED' if attempt==3 else 'APPROVED')]
                checks.append(s.step(cid)==r)
                s=new(); cid,r=case(s); checks.append(s.decision(cid,False)['state']=='REJECTED')
            elif scenario==9:
                for pixels,policy in [(63,'A'),(64,'A'),(65,'A'),(64,'B')]:
                    s=new(policy=policy); cid,r=case(s,pixels,policy=policy); approve(s,cid)
            else:
                for lots in [2,3,4]:
                    s=new(lots); case(s,lots=lots)
                s=new(4,ticket='after_commit_once'); cid,r=case(s,128,4); approve(s,cid,True)
            # Hand-derived oracle: L0/r0, L1/r1+r2, optionally L2/r3, L3/r4.
            for s,cid,pixels,lots,policy in cases:
                r=s.get(cid)
                expected_lots={'L'+str(i) for i in range(lots)}
                actual_lots=set(r['review_lot_ids'] or [])
                correct=len(actual_lots & expected_lots)
                counts['lot_precision'][0]+=correct; counts['lot_precision'][1]+=len(actual_lots)
                counts['lot_recall'][0]+=correct; counts['lot_recall'][1]+=len(expected_lots)
                checks.append(actual_lots==expected_lots)
                expected_paths = {('L0','r0'),('L1','r1'),('L1','r2')}
                if lots>=3: expected_paths.add(('L2','r3'))
                if lots>=4: expected_paths.add(('L3','r4'))
                actual_paths=set()
                oracle=graph(lots,policy)
                times={n['id']:n['properties'].get('ended_at') for n in oracle['nodes']}
                for path in r['evidence'] or []:
                    lot,rid=path['lot_id'],path['related_run_id']
                    actual_paths.add((lot,rid))
                    valid = ((lot,rid) in expected_paths and path['node_ids']==[cid,'r0','E0',rid,lot]
                             and path['relation_ids']==['inspects:'+cid,'eq0','eq'+rid[1:],'lot'+rid[1:]]
                             and path['inspected_ended_at']==times['r0'] and path['related_ended_at']==times.get(rid))
                    counts['path_validity'][0]+=valid; counts['path_validity'][1]+=1
                    checks.append(valid)
                checks.append(actual_paths==expected_paths)
                urgent=pixels>=(64 if policy=='A' else 128) or lots>=(3 if policy=='A' else 4)
                expected=(policy,'urgent' if urgent else 'standard',policy+('_URG' if urgent else '_STD'))
                actual=tuple((r['policy'] or {}).get(k) for k in ['policy_id','priority','queue_code'])
                counts['policy_match'][0]+=actual==expected; counts['policy_match'][1]+=1
                checks.append(actual==expected)
            expected_tickets={1:0,2:1,3:0,4:0,5:1,6:0,7:1,8:0,9:4,10:1}[scenario]
            checks.append(sum(len(s.mock.tickets) for s in services)==expected_tickets)
            for s in services:
                # A call without approval is a barrier violation, not a default zero.
                approved_attempts=sum(c['attempts'] for c in s.cases.values() if c['decision']=={'approve':True})
                before_calls+=max(0,s.mock.calls-approved_attempts)
                tickets=s.mock.snapshot()['tickets']
                duplicates+=len(tickets)-len({t['case_id'] for t in tickets})
                for t in tickets:
                    r=s.get(t['case_id'])
                    match=t['draft']==r['draft'] and r['decision']=={'approve':True}
                    counts['ticket_policy_match'][0]+=match; counts['ticket_policy_match'][1]+=1
                    checks.append(match)
            passed=all(checks)
        except Exception as exc:
            checks.append(False); passed=False
            failures.append(dict(scenario=scenario,error=str(exc)))
        if not passed:
            failures.append(dict(scenario=scenario,checks=checks,cases=[dict(mask=c['inspection'],paths=c['evidence'],history=c['history']) for s in services for c in s.cases.values()]))
        results.append(dict(id=scenario,passed=passed))
        counts['automation_match'][0]+=passed; counts['automation_match'][1]+=1
    metrics={key:ratio(*values) for key,values in counts.items()}
    metrics.update(approval_before_calls=before_calls,duplicate_tickets=duplicates)
    report=dict(scenario_count=10,image_pair_count=8,scenario_results=results,metrics=metrics)
    if failures: report['failures']=failures
    return report

if __name__ == '__main__':
    print(json.dumps(run(),ensure_ascii=False,indent=2))
