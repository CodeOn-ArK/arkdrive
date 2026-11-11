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

    def test_default_meets_corpus_and_fixture_adoption_bar(self):
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
        old, round1, adopted = Backend(shapes=False), Backend(), Backend()
        try:
            for mode in RENDER_MODES:
                with self.subTest(rendering=mode):
                    corpus = samples(mode)
                    self.assertEqual(len(corpus), len(TEXTS)*len(FACES)*len(SIZES))
                    baseline = measure(old, corpus, rendering=mode)
                    with patch('worker_ocr_shapes.templates', return_value=(original_lookup, spaces)):
                        prior = measure(round1, corpus, rendering=mode)
                    current = measure(adopted, corpus, rendering=mode)
                    self.assertTrue(current['fixture_exact'], current['fixture'])
                    self.assertEqual(current['fixture_glyphs']['state'], 'engaged')
                    self.assertGreaterEqual(current['rate'], .98)
                    for reference in (baseline, prior):
                        self.assertFalse([r for p, r in zip(reference['results'], current['results'])
                                          if p['exact'] and not r['exact']])
                    self.assertFalse([r for r in current['results']
                                      if r['glyphs']['state'] == 'engaged' and not r['exact']])
                    self.assertLess(current['median_ms'], 250)
        finally:
            old.close()
            round1.close()
            adopted.close()


if __name__ == '__main__':
    unittest.main()
