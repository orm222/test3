"""Independent synthetic inputs and hand-defined expectations; never imports the app.

Calibration seed 104729, evaluation seed 130363. Practice data is not used.
Masks are injection coordinates, not the result of running a detector.
"""
import copy
import io
import numpy as np
from PIL import Image


def png(array, metadata=False):
    output = io.BytesIO()
    if metadata:
        from PIL.PngImagePlugin import PngInfo
        info = PngInfo()
        info.add_text('encoding', 'different bytes, same pixels')
    else:
        info = None
    Image.fromarray(array).save(output, format='PNG', pnginfo=info)
    return output.getvalue()


def pair(count=4, delta=80, seed=130363, missing=False):
    rng = np.random.RandomState(seed)
    ref = np.zeros((128, 128), dtype=np.uint8)
    ref[:, 64:] = 200
    image = np.clip(ref.astype(np.int16) + rng.randint(-5, 6, ref.shape), 0, 255).astype(np.uint8)
    coords = [(10 + i % 16, (80 if missing else 10) + i // 16) for i in range(count)]
    mask = np.zeros_like(ref)
    for y, x in coords:
        image[y, x] = int(ref[y, x]) + (-delta if missing else delta)
        if count >= 4 and delta > 32:
            mask[y, x] = 255
    bbox = None if not mask.any() else [min(x for y, x in coords), min(y for y, x in coords),
                                      max(x for y, x in coords) + 1, max(y for y, x in coords) + 1]
    return ref, image, mask, bbox


CALIBRATION = [(0, False), (4, False), (16, True), (3, False)]
EVALUATION = [(0, False), (3, False), (4, False), (16, True),
              (63, False), (64, True), (65, False), (128, True)]


def graph(lots=2, policy='A'):
    """2 equipment, 6 lots, 8 runs, 2 policies. r0 is always inspected.

    A window: r0=L0 at t0; r1=L1 exactly -60m; r2=L1 -30m.
    r3/r4 add L2/L3 when requested; otherwise before window.
    r5 future, r6 other equipment, r7 one microsecond before window.
    """
    nodes, edges = [], []
    def node(id_, type_, **props):
        nodes.append(dict(id=id_, type=type_, properties=props))
    def edge(id_, type_, source, target):
        edges.append(dict(id=id_, type=type_, from_id=source, to_id=target))
    for i in range(6):
        node('L'+str(i), 'Lot')
    for i in range(2):
        node('E'+str(i), 'Equipment')
    for name, pixels, lot_count in [('A', 64, 3), ('B', 128, 4)]:
        node(name, 'QualityPolicy', urgent_pixel_threshold=pixels,
             urgent_lot_threshold=lot_count, standard_queue=name+'_STD', urgent_queue=name+'_URG')
    edge('policy0', 'REVIEW_POLICY', 'E0', policy)
    edge('policy1', 'REVIEW_POLICY', 'E1', 'B' if policy == 'A' else 'A')
    times = ['12:00:00', '11:00:00', '11:30:00',
             '11:40:00' if lots >= 3 else '10:59:00',
             '11:50:00' if lots >= 4 else '10:58:00',
             '12:00:00.000001', '11:45:00', '10:59:59.999999']
    for i, (lot, time) in enumerate(zip([0, 1, 1, 2, 3, 4, 5, 5], times)):
        node('r'+str(i), 'ProcessRun', ended_at='2026-01-01T'+time+'+00:00')
        edge('lot'+str(i), 'OF_LOT', 'r'+str(i), 'L'+str(lot))
        edge('eq'+str(i), 'ON', 'r'+str(i), 'E1' if i == 6 else 'E0')
    return dict(nodes=nodes, relations=edges)


EXPECTED_RUNS = {'L0': ['r0'], 'L1': ['r1', 'r2'], 'L2': ['r3'], 'L3': ['r4']}


def invalid_graphs():
    baseline = graph()
    variants = []
    for key, value in [('urgent_pixel_threshold', None), ('urgent_pixel_threshold', True),
                       ('urgent_pixel_threshold', 3), ('urgent_lot_threshold', 0),
                       ('urgent_lot_threshold', '3'), ('standard_queue', ''),
                       ('urgent_queue', 'A_STD')]:
        g = copy.deepcopy(baseline)
        props = next(n for n in g['nodes'] if n['id'] == 'A')['properties']
        if value is None:
            del props[key]
        else:
            props[key] = value
        variants.append(g)
    for kind in ['missing_policy', 'duplicate_policy', 'dangling', 'direction',
                 'duplicate_node', 'duplicate_edge', 'missing_lot', 'duplicate_equipment',
                 'naive_time', 'bad_type']:
        g = copy.deepcopy(baseline)
        if kind == 'missing_policy':
            g['relations'].pop(0)
        elif kind == 'duplicate_policy':
            g['relations'].append(dict(id='extra', type='REVIEW_POLICY', from_id='E0', to_id='B'))
        elif kind == 'dangling':
            g['relations'][0]['to_id'] = 'absent'
        elif kind == 'direction':
            g['relations'][0]['from_id'], g['relations'][0]['to_id'] = 'A', 'E0'
        elif kind == 'duplicate_node':
            g['nodes'].append(copy.deepcopy(g['nodes'][0]))
        elif kind == 'duplicate_edge':
            g['relations'].append(copy.deepcopy(g['relations'][0]))
        elif kind == 'missing_lot':
            g['relations'] = [e for e in g['relations'] if e['id'] != 'lot0']
        elif kind == 'duplicate_equipment':
            g['relations'].append(dict(id='extra', type='ON', from_id='r0', to_id='E1'))
        elif kind == 'naive_time':
            next(n for n in g['nodes'] if n['id'] == 'r0')['properties']['ended_at'] = '2026-01-01T12:00:00'
        else:
            g['nodes'][0]['type'] = 'Unknown'
        variants.append(g)
    return variants
