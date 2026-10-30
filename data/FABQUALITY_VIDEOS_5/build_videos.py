"""Build deterministic FabQuality video fixtures (Python 3.9+)."""
import argparse
import csv
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

FPS = 12
SECONDS = 12
FRAME_COUNT = FPS * SECONDS
CASES = [
    ('01_normal_noise', 'Normal / noise only', 'dev/normal_reference.png', [(0, 12, 0)], 'addition', 3),
    ('02_pattern_addition', 'Pattern addition / 9 pixels', 'dev/addition_reference.png', [(0, 3, 0), (3, 9, 9), (9, 12, 0)], 'addition', 3),
    ('03_pattern_removal', 'Pattern removal / 6 pixels', 'dev/removal_reference.png', [(0, 3, 0), (3, 9, 6), (9, 12, 0)], 'removal', 3),
    ('04_boundary_3_to_4', 'Detection boundary / 3 to 4', 'eval/tiny3_reference.png', [(0, 3, 0), (3, 6, 3), (6, 9, 4), (9, 12, 0)], 'addition', 3),
    ('05_boundary_63_64_65', 'Priority boundary / 63, 64, 65', 'eval/addition64_reference.png', [(0, 3, 0), (3, 6, 63), (6, 9, 64), (9, 12, 65)], 'addition', 2),
]


def coordinates(case_id, n):
    if 'removal' in case_id:
        return [(50 + i % 3, 30 + i // 3) for i in range(n)]
    if '3_to_4' in case_id:
        return [(3, 3), (4, 3), (3, 4), (4, 4)][:n]
    if '63_64_65' in case_id:
        return [(8 + i % 8, 48 + i // 8) for i in range(n)]
    return [(10 + i % 3, 20 + i // 3) for i in range(n)]


def expected(case_id, n, lots):
    points = coordinates(case_id, n)
    defect = n >= 4
    bbox = None
    mask = np.zeros((128, 128), dtype=np.uint8)
    if defect:
        xs, ys = zip(*points)
        bbox = [min(xs), min(ys), max(xs) + 1, max(ys) + 1]
        for x, y in points:
            mask[y, x] = 255
    priority = ('urgent' if n >= 64 or lots >= 3 else 'standard') if defect else None
    return mask, {
        'injected_pixel_count': n, 'candidate_pixels': n,
        'verdict': 'defect' if defect else 'normal',
        'defect_pixels': n if defect else 0, 'bbox': bbox,
        'review_lot_ids': (['L1', 'L2', 'L3'] if lots == 3 else ['L1', 'L2']) if defect else None,
        'policy_id': 'P_A' if defect else None,
        'priority': priority, 'queue_code': priority + '-A' if defect else None,
        'expected_state_after_first_step': 'WAITING_APPROVAL' if defect else 'CLEARED',
        'expected_calls_before_approval': 0,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-data', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    src = args.source_data
    bundled = out / 'source_data'
    for _, _, rel, _, _, _ in CASES:
        target = bundled / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if (src / rel).resolve() != target.resolve():
            shutil.copy2(src / rel, target)
    if (src / 'ontology.json').resolve() != (bundled / 'ontology.json').resolve():
        shutil.copy2(src / 'ontology.json', bundled / 'ontology.json')
    graph = json.loads((src / 'ontology.json').read_text())
    fixture = json.loads(json.dumps(graph))
    fixture['nodes'] = [n for n in fixture['nodes'] if n['id'] != 'R8']
    fixture['edges'] = [e for e in fixture['edges'] if e['from_id'] != 'R8' and e['to_id'] != 'R8']
    (out / 'ontology_policy_A_2_lots.json').write_text(json.dumps(fixture, indent=2) + '\n')
    all_labels = []
    overview = []
    representative = []
    for case_number, (case_id, title, ref_rel, stages, change, lots) in enumerate(CASES, 1):
        directory = out / 'frames' / case_id
        directory.mkdir(parents=True, exist_ok=True)
        mask_dir = out / 'masks' / case_id
        mask_dir.mkdir(parents=True, exist_ok=True)
        reference_dir = out / 'references'
        reference_dir.mkdir(exist_ok=True)
        reference_file = reference_dir / (case_id + '.png')
        shutil.copy2(src / ref_rel, reference_file)
        reference = np.array(Image.open(reference_file), dtype=np.uint8)
        rng = np.random.default_rng(20261031 + case_number)
        case_labels = []
        for index in range(FRAME_COUNT):
            time_seconds = index / FPS
            n = next(n for start, end, n in stages if start <= time_seconds < end)
            pixels = reference.astype(np.int16).copy()
            for x, y in coordinates(case_id, n):
                pixels[y, x] = 240 if change == 'addition' else 0
            pixels = np.clip(pixels + rng.integers(-5, 6, size=(128, 128)), 0, 255).astype(np.uint8)
            frame_file = directory / ('%04d.png' % index)
            mask_file = mask_dir / ('%04d.png' % index)
            Image.fromarray(pixels).save(frame_file)
            mask, label = expected(case_id, n, lots)
            Image.fromarray(mask).save(mask_file)
            label.update({
                'video_id': case_id, 'frame_index': index, 'time_seconds': time_seconds,
                'reference': str(reference_file.relative_to(out)),
                'test': str(frame_file.relative_to(out)),
                'mask': str(mask_file.relative_to(out)),
                'run_id': 'R1', 'event_id_example': '%s-frame-%04d' % (case_id, index),
                'ontology': 'ontology_policy_A_2_lots.json' if lots == 2 else 'source_data/ontology.json',
            })
            case_labels.append(label)
        all_labels.extend(case_labels)
        video = out / (case_id + '.mp4')
        subprocess.run([
            'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
            '-framerate', str(FPS), '-start_number', '0', '-i', str(directory / '%04d.png'),
            '-vf', 'scale=768:768:flags=neighbor,format=rgb24',
            '-c:v', 'libx264rgb', '-crf', '0', '-preset', 'medium',
            '-threads', '2', '-pix_fmt', 'rgb24', '-movflags', '+faststart',
            '-an', str(video)], check=True)
        print('Created ' + video.name, flush=True)
        overview.append({'id': case_id, 'title': title, 'file': video.name,
                         'fps': FPS, 'duration_seconds': SECONDS, 'frame_count': FRAME_COUNT,
                         'size': [768, 768], 'analysis_size': [128, 128],
                         'codec': 'H.264 lossless RGB', 'audio': False,
                         'stages': [{'start_seconds': s, 'end_seconds_exclusive': e,
                                     **expected(case_id, n, lots)[1]} for s, e, n in stages]})
        representative.append(directory / '0084.png')
    (out / 'labels.json').write_text(json.dumps(all_labels, indent=2) + '\n')
    columns = ['video_id', 'frame_index', 'time_seconds', 'injected_pixel_count', 'candidate_pixels',
               'verdict', 'defect_pixels', 'priority', 'queue_code', 'test', 'reference', 'ontology']
    with (out / 'labels.csv').open('w', newline='') as file:
        writer = csv.DictWriter(file, fieldnames=columns, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(all_labels)
    (out / 'videos.json').write_text(json.dumps(overview, indent=2) + '\n')
    # Contact sheet is an exact visualization of existing test pixels.
    sheet = Image.new('RGB', (1440, 410), '#f0f3f7')
    draw = ImageDraw.Draw(sheet)
    font_path = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
    font = ImageFont.truetype(font_path, 16) if Path(font_path).exists() else ImageFont.load_default()
    for i, path in enumerate(representative):
        x = i * 288 + 16
        sheet.paste(Image.open(path).resize((256, 256), Image.Resampling.NEAREST).convert('RGB'), (x, 50))
        draw.text((x, 20), '%02d / SYNTHETIC' % (i + 1), font=font, fill='#182534')
        titles = ['Normal + noise', 'Addition: 9 pixels', 'Removal: 6 pixels', 'Boundary: 4 pixels', 'Boundary: 64 pixels']
        draw.text((x, 322), titles[i], font=font, fill='#182534')
        draw.text((x, 349), '12 sec | 768 x 768', font=font, fill='#536070')
    sheet.save(out / 'PREVIEW.png')
    hashes = {str(p.relative_to(out)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted(out.rglob('*')) if p.is_file()}
    (out / 'SHA256.json').write_text(json.dumps(hashes, indent=2) + '\n')


if __name__ == '__main__':
    main()
