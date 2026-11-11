"""The recognition adoption bar, exercised on all font/size cross products."""
import unittest

from ocr_corpus import TEXTS, FACES, SIZES, samples, measure, render
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
        self.assertGreaterEqual(len(TEXTS), 40)
        corpus = samples()
        self.assertEqual(len(corpus), len(TEXTS)*len(FACES)*len(SIZES))
        old, adopted = Backend(shapes=False), Backend()
        try:
            baseline = measure(old, corpus)
            current = measure(adopted, corpus)
        finally:
            old.close()
            adopted.close()
        self.assertTrue(current['fixture_exact'], current['fixture'])
        self.assertGreaterEqual(current['rate'], .98)
        self.assertFalse([r for prior, r in zip(baseline['results'], current['results'])
                          if prior['exact'] and not r['exact']])
        self.assertLess(current['median_ms'], 250)


if __name__ == '__main__':
    unittest.main()
