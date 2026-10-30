"""Independent synthetic corpus. Truth comes from injection coordinates only."""
import argparse
import json
from pathlib import Path

import numpy as np
from .core import png

CALIBRATION = [(0,False),(4,False),(16,True),(3,False)]
EVALUATION = [(0,False),(3,False),(4,False),(16,True),(63,False),(64,True),(65,False),(128,True)]


def pair(count=4, missing=False, seed=130363, delta=80):
    random = np.random.RandomState(seed)
    ref = np.zeros((128,128),dtype=np.uint8)
    ref[:,64:] = 200
    test = np.clip(ref.astype(np.int16)+random.randint(-5,6,ref.shape),0,255).astype(np.uint8)
    truth = np.zeros_like(ref)
    for i in range(count):
        y,x = 10+i%16, (80 if missing else 10)+i//16
        test[y,x] = int(ref[y,x])+(-delta if missing else delta)
        if count >= 4 and delta > 32:
            truth[y,x] = 255
    return png(ref),png(test),truth


def graph(lots=2, policy='A'):
    nodes = [dict(id='L'+str(i),type='Lot',properties={}) for i in range(6)]
    nodes += [dict(id='E'+str(i),type='Equipment',properties={}) for i in range(2)]
    for name,pixels,number in [('A',64,3),('B',128,4)]:
        nodes.append(dict(id=name,type='QualityPolicy',properties=dict(urgent_pixel_threshold=pixels,
            urgent_lot_threshold=number,standard_queue=name+'_STD',urgent_queue=name+'_URG')))
    edges = [dict(id='policy'+str(i),type='REVIEW_POLICY',from_id='E'+str(i),
                  to_id=policy if i==0 else ('B' if policy=='A' else 'A')) for i in range(2)]
    times = ['12:00:00','11:00:00','11:30:00','11:40:00' if lots>=3 else '10:59:00',
             '11:50:00' if lots>=4 else '10:58:00','12:00:00.000001','11:45:00','10:59:59.999999']
    for i,lot in enumerate([0,1,1,2,3,4,5,5]):
        run = 'r'+str(i)
        nodes.append(dict(id=run,type='ProcessRun',properties=dict(ended_at='2026-01-01T'+times[i]+'+00:00')))
        edges += [dict(id='lot'+str(i),type='OF_LOT',from_id=run,to_id='L'+str(lot)),
                  dict(id='eq'+str(i),type='ON',from_id=run,to_id='E1' if i==6 else 'E0')]
    return dict(nodes=nodes,relations=edges)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output',default='generated')
    args = parser.parse_args()
    root = Path(args.output)
    for split,seed,specs in [('calibration',104729,CALIBRATION),('evaluation',130363,EVALUATION)]:
        directory = root/split
        directory.mkdir(parents=True,exist_ok=True)
        for i,(count,missing) in enumerate(specs):
            ref,test,truth = pair(count,missing,seed+i)
            for suffix,raw in [('reference',ref),('test',test),('truth',png(truth))]:
                (directory/(str(i)+'-'+suffix+'.png')).write_bytes(raw)
    (root/'ontology.json').write_text(json.dumps(graph(),indent=2))
    print('Generated 4 calibration and 8 evaluation pairs; truth is evaluation-only.')

if __name__ == '__main__':
    main()
