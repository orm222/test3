"""Run: python3 -m unittest -v test_videos.py (Python 3.9+)."""
import io
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent


class VideoFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.labels = json.loads((ROOT / 'labels.json').read_text())
        cls.videos = json.loads((ROOT / 'videos.json').read_text())

    def test_01_five_videos_metadata_and_complete_decode(self):
        self.assertEqual(len(self.videos), 5)
        self.assertEqual(len(self.labels), 720)
        for video in self.videos:
            metadata = json.loads(subprocess.check_output([
                'ffprobe', '-v', 'error', '-count_frames', '-show_streams',
                '-show_format', '-of', 'json', str(ROOT / video['file'])]))
            self.assertEqual(len(metadata['streams']), 1)
            stream = metadata['streams'][0]
            self.assertEqual(stream['codec_type'], 'video')
            self.assertEqual(stream['codec_name'], 'h264')
            self.assertEqual((stream['width'], stream['height']), (768, 768))
            self.assertEqual(stream['avg_frame_rate'], '12/1')
            self.assertEqual(int(stream['nb_read_frames']), 144)
            self.assertAlmostEqual(float(metadata['format']['duration']), 12.0)

    def test_02_all_decoded_pixels_equal_source_pngs(self):
        for video in self.videos:
            decoded = subprocess.check_output([
                'ffmpeg', '-v', 'error', '-xerror', '-i', str(ROOT / video['file']),
                '-vf', 'scale=128:128:flags=neighbor', '-f', 'rawvideo',
                '-pix_fmt', 'rgb24', '-'])
            pixels = np.frombuffer(decoded, dtype=np.uint8).reshape((-1, 128, 128, 3))
            rows = [r for r in self.labels if r['video_id'] == video['id']]
            self.assertEqual(len(pixels), len(rows))
            for decoded_frame, row in zip(pixels, rows):
                source = np.array(Image.open(ROOT / row['test']))
                for channel in range(3):
                    np.testing.assert_array_equal(decoded_frame[:, :, channel], source,
                                                  err_msg=row['test'])

    def test_03_all_png_contracts_and_geometric_labels(self):
        for row in self.labels:
            images = []
            for key in ('reference', 'test', 'mask'):
                with Image.open(ROOT / row[key]) as im:
                    self.assertEqual((im.format, im.mode, im.size), ('PNG', 'L', (128, 128)))
                    images.append(np.array(im))
            ref, actual, mask = images
            candidates = np.abs(ref.astype(np.int16) - actual.astype(np.int16)) > 32
            n = int(candidates.sum())
            self.assertEqual(n, row['injected_pixel_count'])
            self.assertEqual(n, row['candidate_pixels'])
            if n < 4:
                self.assertEqual(row['verdict'], 'normal')
                self.assertEqual(row['defect_pixels'], 0)
                self.assertIsNone(row['bbox'])
                self.assertEqual(int(mask.sum()), 0)
            else:
                self.assertEqual(row['verdict'], 'defect')
                self.assertEqual(row['defect_pixels'], n)
                np.testing.assert_array_equal(mask, candidates.astype(np.uint8) * 255)
                y, x = np.where(candidates)
                self.assertEqual(row['bbox'], [int(x.min()), int(y.min()), int(x.max()) + 1, int(y.max()) + 1])

    def test_04_stage_boundaries_and_policy_expected_values(self):
        rows4 = [r for r in self.labels if r['video_id'] == '04_boundary_3_to_4']
        self.assertEqual(rows4[71]['candidate_pixels'], 3)
        self.assertEqual(rows4[71]['verdict'], 'normal')
        self.assertEqual(rows4[72]['candidate_pixels'], 4)
        self.assertEqual(rows4[72]['verdict'], 'defect')
        rows5 = [r for r in self.labels if r['video_id'] == '05_boundary_63_64_65']
        for index, count, priority in [(36, 63, 'standard'), (72, 64, 'urgent'), (108, 65, 'urgent')]:
            self.assertEqual(rows5[index]['defect_pixels'], count)
            self.assertEqual(rows5[index]['priority'], priority)
            self.assertEqual(rows5[index]['review_lot_ids'], ['L1', 'L2'])
        rows2 = [r for r in self.labels if r['video_id'] == '02_pattern_addition']
        self.assertEqual(rows2[36]['priority'], 'urgent')
        self.assertEqual(rows2[36]['review_lot_ids'], ['L1', 'L2', 'L3'])

    @unittest.skipUnless(os.environ.get('FABQUALITY_WORKER_PATH'), 'Set FABQUALITY_WORKER_PATH to test the uploaded worker too')
    def test_05_against_uploaded_vision_and_ontology_modules(self):
        sys.path.insert(0, os.environ['FABQUALITY_WORKER_PATH'])
        from vision import inspect_arrays, decode_png
        from knowledge import KnowledgeGraph
        graphs = {}
        for name in ['source_data/ontology.json', 'ontology_policy_A_2_lots.json']:
            graphs[name] = KnowledgeGraph(json.loads((ROOT / name).read_text()))
        narrow_trace = graphs['ontology_policy_A_2_lots.json'].trace('R1', 'validation')
        self.assertEqual(narrow_trace['review_lot_ids'], ['L1', 'L2'])
        self.assertEqual(len(narrow_trace['evidence']), 3)
        for row in self.labels:
            result = inspect_arrays(decode_png((ROOT / row['reference']).read_bytes()),
                                    decode_png((ROOT / row['test']).read_bytes()))
            for key in ('verdict', 'defect_pixels', 'bbox'):
                self.assertEqual(result[key], row[key], row['test'])
            returned_mask = np.array(Image.open(io.BytesIO(base64.b64decode(result['mask_png_base64']))))
            np.testing.assert_array_equal(returned_mask, np.array(Image.open(ROOT / row['mask'])))
            if row['verdict'] == 'defect':
                graph = graphs[row['ontology']]
                trace = graph.trace('R1', row['event_id_example'])
                self.assertEqual(trace['review_lot_ids'], row['review_lot_ids'])
                policy = graph.select_policy('R1', result['defect_pixels'], len(trace['review_lot_ids']))
                for key in ('policy_id', 'priority', 'queue_code'):
                    self.assertEqual(policy[key], row[key])


if __name__ == '__main__':
    unittest.main(verbosity=2)
