"""The recognition adoption bar, exercised on all font/size cross products."""
import unittest

from ocr_corpus import TEXTS, FACES, SIZES, RENDER_MODES, samples, measure, render
from worker_ocr import Backend


class Corpus(unittest.TestCase):
    def test_shape_pass_recognizes_pixels_beyond_corpus_and_initial_characters(self):
        from PIL import Image
        from worker_ocr_shapes import recognize
        # Neither recognized spelling nor corpus content supplies candidates.
        for face in FACES:
            for size in (16, 21, 27, 36):
                rgb, rect = render((('R7K9', 12, size+8),), face, size)
                image = Image.frombytes('RGB', (rect['w'], rect['h']), rgb).convert('L')
                actual = recognize(image, [dict(text='aaaa')])
                self.assertIsNotNone(actual, (face, size))
                self.assertEqual([l['text'] for l in actual], ['R7K9'])

    def test_unrecognized_content_shape_does_not_start_glyph_recognition(self):
        from PIL import Image
        from worker_ocr_shapes import recognize
        from unittest.mock import patch
        with patch('worker_ocr_shapes.templates', side_effect=AssertionError('shape gate')):
            self.assertIsNone(recognize(Image.new('L', (30, 30), 255), [dict(text='lengthy')]))
            self.assertIsNone(recognize(Image.new('L', (30, 30), 255), [dict(text='(|')]))

    def test_compact_atlas_preserves_every_rgb_raster_and_metric(self):
        from worker_ocr_shapes import templates, build_templates, ATLAS_RENDERINGS
        # All faces, sizes and characters: tight cells and GRAY alpha rendering
        # must preserve the reference black-on-white RGB rasters exactly.
        actual, spaces = templates()
        rgb, rgb_spaces = build_templates(ATLAS_RENDERINGS, alpha_gray=False)
        self.assertEqual(actual, rgb)
        self.assertEqual(spaces, rgb_spaces)

    def test_real_xvfb_capture_exact_including_whole_window_with_pointer(self):
        import hashlib
        from PIL import Image
        from ocr_corpus import REAL_CAPTURE, REAL_SHA256, REAL_LINES
        self.assertEqual(hashlib.sha256(REAL_CAPTURE.read_bytes()).hexdigest(), REAL_SHA256)
        source = Image.open(REAL_CAPTURE).convert('RGB')
        self.assertEqual(source.size, (560, 280))
        from unittest.mock import patch
        from worker_ocr_shapes import templates, recognize
        lookup, spaces = templates()
        round2 = {k: {f: v for f, v in fonts.items() if f[2] in ('original', 'live-full')}
                  for k, fonts in lookup.items()}
        round2 = {k: v for k, v in round2.items() if v}
        with patch('worker_ocr_shapes.templates', return_value=(round2, spaces)):
            diag = {}
            self.assertIsNone(recognize(source.crop(REAL_LINES[-1][1]).convert('L'),
                                        [dict(text='a1')], diag))
            self.assertEqual(diag['reason'], 'raster_miss')
            self.assertEqual((diag['glyphs'], diag['unmatched']), (9, 4))
        backend = Backend()
        states = []
        try:
            for expected, box in REAL_LINES:
                with self.subTest(text=expected):
                    crop = source.crop(box)
                    actual = backend.read(crop.tobytes(), dict(x=box[0], y=box[1],
                                          w=crop.width, h=crop.height), mode='line')
                    self.assertEqual([l['text'] for l in actual], [expected])
                    states.append(backend.last_shapes['state'])
            self.assertEqual(states, ['abstained', 'engaged', 'engaged', 'engaged'])
            actual = backend.read(source.tobytes(), dict(x=31, y=27, w=560, h=280))
            self.assertEqual([l['text'] for l in actual], [t for t, _ in REAL_LINES])
            self.assertEqual(actual[-1]['conf'], 100)
            self.assertEqual(actual[-1]['box'], [55, 242, 121, 23])
            bands = backend.last_shapes['bands']
            self.assertEqual([b['state'] for b in bands],
                             ['abstained', 'engaged', 'abstained', 'engaged'])
            self.assertEqual(bands[2]['reason'], 'raster_miss')
        finally:
            backend.close()

    def test_corrupted_band_keeps_tesseract_without_vetoing_clean_bands(self):
        from PIL import Image
        from worker_ocr_shapes import recognize
        # Source spellings are deliberately different from initial recognition.
        rgb, rect = render((('W0', 12, 40), ('B7', 12, 90), ('A0', 12, 140)),
                           'Sans', 24, width=200, height=160)
        image = Image.frombytes('RGB', (rect['w'], rect['h']), rgb).convert('L')
        # Attach an unsupported raster to the middle band's first glyph.
        for y in range(74, 86):
            for x in range(9, 16):
                image.putpixel((x, y), 0)
        fallback = [dict(text=t, box=[12, y, 40, 26], conf=c)
                    for t, y, c in [('WO', 16, 91), ('corrupted B7', 66, 82), ('AO', 116, 93)]]
        diag = {}
        actual = recognize(image, fallback, diag)
        actual.sort(key=lambda l: l['box'][1])
        self.assertEqual([l['text'] for l in actual], ['W0', 'corrupted B7', 'A0'])
        self.assertEqual(actual[1], fallback[1])
        self.assertEqual([l['conf'] for l in actual], [100, 82, 100])
        self.assertEqual([b['state'] for b in diag['bands']], ['engaged', 'abstained', 'engaged'])

    def test_content_gate_is_per_band_and_spanning_tesseract_lines_are_retained(self):
        from PIL import Image
        from worker_ocr_shapes import recognize
        rgb, rect = render((('W0', 12, 40), ('A0', 12, 90)),
                           'Sans', 24, width=200, height=110)
        image = Image.frombytes('RGB', (rect['w'], rect['h']), rgb).convert('L')
        fallback = [dict(text='lengthy', box=[12, 16, 40, 26], conf=91),
                    dict(text='AO', box=[12, 66, 40, 26], conf=92)]
        diag = {}
        actual = recognize(image, fallback, diag)
        self.assertIn(fallback[0], actual)
        self.assertEqual(diag['bands'][0]['reason'], 'content_gate')
        self.assertEqual(diag['bands'][1]['state'], 'engaged')
        spanning = [dict(text='WO AO', box=[12, 16, 40, 76], conf=80)]
        self.assertIsNone(recognize(image, spanning, diag))
        self.assertTrue(all(b['reason'] == 'line_spans_bands' for b in diag['bands']))

    def test_default_meets_corpus_and_fixture_adoption_bar(self):
        import json
        from pathlib import Path
        from unittest.mock import patch
        from worker_ocr_shapes import templates
        self.assertGreaterEqual(len(TEXTS), 40)
        lookup, spaces = templates()
        # Exact original-rendering atlas reproduces round 1 independently of
        # the added hinted candidates. Compare previously correct lines in
        # every mode, as well as the original Tesseract baseline.
        original_lookup = {key: {f: v for f, v in fonts.items() if f[2] == 'original'}
                           for key, fonts in lookup.items()}
        original_lookup = {k: v for k, v in original_lookup.items() if v}
        round2_lookup = {key: {f: v for f, v in fonts.items() if f[2] in ('original', 'live-full')}
                         for key, fonts in lookup.items()}
        round2_lookup = {k: v for k, v in round2_lookup.items() if v}
        old, round1, round2, adopted = Backend(shapes=False), Backend(), Backend(), Backend()
        saved = json.loads((Path(__file__).resolve().parents[2] /
                            'bench/w7-round3-after.json').read_text())['modes']
        try:
            for mode in RENDER_MODES:
                with self.subTest(rendering=mode):
                    corpus = samples(mode)
                    self.assertEqual(len(corpus), len(TEXTS)*len(FACES)*len(SIZES))
                    baseline = measure(old, corpus, rendering=mode)
                    with patch('worker_ocr_shapes.templates', return_value=(original_lookup, spaces)):
                        prior = measure(round1, corpus, rendering=mode)
                    with patch('worker_ocr_shapes.templates', return_value=(round2_lookup, spaces)):
                        prior2 = measure(round2, corpus, rendering=mode)
                    current = measure(adopted, corpus, rendering=mode)
                    self.assertEqual([r['key'] for r in saved[mode]['results']],
                                     [r['key'] for r in current['results']])
                    self.assertTrue(current['fixture_exact'], current['fixture'])
                    self.assertEqual(current['fixture_glyphs']['state'], 'engaged')
                    self.assertGreaterEqual(current['rate'], .98)
                    for reference in (baseline, prior, prior2, saved[mode]):
                        self.assertFalse([r for p, r in zip(reference['results'], current['results'])
                                          if p['exact'] and not r['exact']])
                    self.assertFalse([r for r in current['results']
                                      if r['glyphs']['state'] == 'engaged' and not r['exact']])
                    self.assertLess(current['median_ms'], 250)
        finally:
            old.close()
            round1.close()
            round2.close()
            adopted.close()


if __name__ == '__main__':
    unittest.main()
