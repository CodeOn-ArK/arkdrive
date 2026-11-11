#!/usr/bin/python3
"""Headless Cairo OCR corpus and candidate comparison; no display or bus.

Run: OMP_THREAD_LIMIT=1 /usr/bin/python3 wm/tests/ocr_corpus.py --output PATH
Each source line is crossed with every face and size, never used as an OCR hint.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import time

os.environ.setdefault('OMP_THREAD_LIMIT', '1')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cairo
from worker_ocr import Backend

TEXTS = (
    'Open the door', 'Orange juice', 'Order complete', 'October morning',
    'Look outside', 'Book a room', 'The moon is bright', 'Follow the road',
    'Hello', 'Piano', 'Radio', 'Go',
    '0 starts here', '0.25 seconds', '0 errors found', '0 items selected',
    'room 101', 'room 202', 'port 8080', 'value 1005',
    'Count: 0', 'Errors: 0', 'Level 10', 'Score 20',
    'W0', 'O0', 'A1B2', 'X0Y', '0AB', 'AB0', 'O2', 'B0X',
    '10:30', '08:00', '23:59', 'v2.0', 'v10.2', 'Version 3.0',
    '1234567890', '100', '2026', '456', '42', '987654',
    'Save changes', 'Add to list', 'Ready for input', 'Click to continue',
    'Status: OK', 'Code: Z0', 'Output O', 'Option O',
)
FACES = ('Sans', 'Serif', 'Monospace')
SIZES = (14, 18, 24, 32)
FIXTURE = (('ArkDrive canvas', 24, 52), ('Alpha 123', 24, 112),
           ('Bravo 456', 260, 172), ('Target: W0', 24, 232))
RENDER_MODES = ('original', 'live-full', 'live-medium', 'live-slight',
                'gray-none', 'gray-slight', 'gray-medium', 'gray-full')


def font_options(mode):
    options = cairo.FontOptions()
    if mode != 'original':
        options.set_antialias(cairo.ANTIALIAS_GRAY if mode == 'full-gray' or mode.startswith('gray-') else cairo.ANTIALIAS_SUBPIXEL)
        options.set_subpixel_order(cairo.SUBPIXEL_ORDER_RGB)
        options.set_hint_style({'live-full': cairo.HINT_STYLE_FULL,
                                'live-medium': cairo.HINT_STYLE_MEDIUM,
                                'live-slight': cairo.HINT_STYLE_SLIGHT,
                                'full-gray': cairo.HINT_STYLE_FULL,
                                'gray-none': cairo.HINT_STYLE_NONE,
                                'gray-slight': cairo.HINT_STYLE_SLIGHT,
                                'gray-medium': cairo.HINT_STYLE_MEDIUM,
                                'gray-full': cairo.HINT_STYLE_FULL}[mode])
        options.set_hint_metrics(cairo.HINT_METRICS_ON)
    return options


def render(commands, face='Sans', size=24, width=None, height=None, rendering='original'):
    probe = cairo.Context(cairo.ImageSurface(cairo.FORMAT_RGB24, 1, 1))
    probe.set_font_options(font_options(rendering))
    probe.select_font_face(face)
    probe.set_font_size(size)
    width = width or int(max(x+probe.text_extents(t).x_advance for t, x, y in commands)+16)
    height = height or size+24
    surface = cairo.ImageSurface(cairo.FORMAT_RGB24, width, height)
    ctx = cairo.Context(surface)
    ctx.set_font_options(font_options(rendering))
    ctx.set_source_rgb(1, 1, 1)
    ctx.paint()
    ctx.set_source_rgb(0, 0, 0)
    ctx.select_font_face(face)
    ctx.set_font_size(size)
    for text, x, y in commands:
        ctx.move_to(x, y)
        ctx.show_text(text)
    surface.flush()
    # Cairo RGB24 is native endian BGRx on ArK2, matching fx_canvas's stack.
    from PIL import Image
    rgb = Image.frombytes('RGB', (width, height), bytes(surface.get_data()),
                          'raw', 'BGRX', surface.get_stride()).tobytes()
    return rgb, dict(x=0, y=0, w=width, h=height)


def samples(rendering='original'):
    return [(f'{face}/{size}/{text}', text,
             *render(((text, 12, size+8),), face, size, rendering=rendering))
            for face in FACES for size in SIZES for text in TEXTS]


def measure(backend, corpus, line_mode=None, rendering='original'):
    results, times = [], []
    for key, expected, rgb, rect in corpus:
        start = time.perf_counter()
        actual = '\n'.join(l['text'] for l in backend.read(rgb, rect, mode=line_mode))
        times.append((time.perf_counter()-start)*1000)
        results.append(dict(key=key, expected=expected, actual=actual, exact=actual == expected,
                            glyphs=getattr(backend, 'last_shapes', None)))
    rgb, rect = render(FIXTURE, width=560, height=280, rendering=rendering)
    start = time.perf_counter()
    fixture = [l['text'] for l in backend.read(rgb, rect)]
    return dict(exact=sum(r['exact'] for r in results), total=len(results),
                rate=sum(r['exact'] for r in results)/len(results),
                median_ms=statistics.median(times), max_ms=max(times),
                fixture_ms=(time.perf_counter()-start)*1000,
                fixture=fixture, fixture_exact=fixture == [t for t, x, y in FIXTURE],
                fixture_glyphs=getattr(backend, 'last_shapes', None),
                results=results)


def live_style_report(output, before=None):
    """Full cross product in each mode; retain exact outputs and regressions."""
    prior = json.loads(before.read_text()) if before else None
    if prior and 'modes' in prior:
        prior = prior['modes']
    report = dict(cairo=cairo.cairo_version_string(), comparison_before=str(before) if before else None, modes={})
    old, adopted = Backend(shapes=False), Backend()
    try:
        for mode in RENDER_MODES:
            corpus = samples(mode)
            baseline = measure(old, corpus, rendering=mode)
            current = measure(adopted, corpus, rendering=mode)
            current['baseline_exact'] = baseline['exact']
            current['regressions_base'] = [r['key'] for p, r in zip(baseline['results'], current['results'])
                                            if p['exact'] and not r['exact']]
            if prior and mode in prior:
                assert [r['key'] for r in prior[mode]['results']] == [r['key'] for r in current['results']]
                current['regressions_before'] = [r['key'] for p, r in zip(prior[mode]['results'], current['results'])
                                                 if p['exact'] and not r['exact']]
            report['modes'][mode] = current
            print(mode, current['exact'], '/', current['total'],
                  'fixture', current['fixture'], 'median_ms', round(current['median_ms'], 2),
                  'regressions', current.get('regressions_before'), flush=True)
            output.write_text(json.dumps(report, indent=2)+'\n')
    finally:
        old.close()
        adopted.close()


REAL_CAPTURE = Path(__file__).parent/'fixtures/live_canvas_xvfb.png'
REAL_SHA256 = '55736729c3fcbcbe99246b04dd91ef323ce5021a021eb4e61ee14d7df6beb52c'
# Fixed source regions for scoring, never supplied to the glyph decoder as text.
# Bravo's top is below the pointer; the entire W0 line is retained.
REAL_LINES = (
    ('ArkDrive canvas', (0, 25, 560, 65)),
    ('Alpha 123', (0, 85, 560, 125)),
    ('Bravo 456', (0, 152, 560, 185)),
    ('Target: W0', (8, 200, 240, 248)),
)


def real_capture_report():
    from PIL import Image, ImageChops
    from worker_ocr_shapes import build_templates
    image = Image.open(REAL_CAPTURE).convert('RGB')
    configs = ('original', 'live-full', 'gray-none', 'gray-slight', 'gray-medium', 'gray-full')
    lookup, _ = build_templates(configs)
    crop = image.crop(REAL_LINES[-1][1]).convert('L')
    mask = crop.point(lambda p: 255 if p < 128 else 0)
    bb = mask.getbbox()
    spans = []
    for x in range(bb[0], bb[2]):
        if mask.crop((x, 0, x+1, mask.height)).getbbox():
            if spans and x == spans[-1][1]+1:
                spans[-1][1] = x
            else:
                spans.append([x, x])
    glyphs = []
    # Labels are attached only after lookup, for the diagnostic table.
    for left, right in spans:
        glyph = mask.crop((left, 0, right+1, mask.height))
        bounds = glyph.getbbox()
        glyph = glyph.crop(bounds)
        matches = lookup.get((glyph.size, glyph.tobytes()), {})
        glyphs.append(dict(box=[left+8, bounds[1]+200, right-left+1, bounds[3]-bounds[1]],
                           exact_configs=[c for c in configs if ('Sans', 24, c) in matches],
                           all_matches=[dict(font=f, glyphs=v) for f, v in sorted(matches.items())],
                           unmatched_anywhere=not bool(matches)))
    for label, glyph in zip('Target:W0', glyphs):
        glyph['label'] = label
    synthetic, _ = render(FIXTURE, width=560, height=280, rendering='gray-full')
    synthetic = Image.frombytes('RGB', (560, 280), synthetic).convert('L')
    binary = synthetic.crop(REAL_LINES[-1][1]).point(lambda p: 255 if p < 128 else 0)
    diff = ImageChops.difference(mask, binary)
    backend = Backend()
    raw = Backend(shapes=False)
    results = []
    try:
        for expected, box in REAL_LINES:
            part = image.crop(box)
            rect = dict(x=box[0], y=box[1], w=part.width, h=part.height)
            baseline = raw.read(part.tobytes(), rect, mode='line')
            actual = backend.read(part.tobytes(), rect, mode='line')
            results.append(dict(expected=expected, region=box, actual=actual, baseline=baseline,
                                exact=[l['text'] for l in actual] == [expected],
                                glyphs=backend.last_shapes.copy()))
    finally:
        backend.close()
        raw.close()
    return dict(source=str(REAL_CAPTURE), sha256=hashlib.sha256(REAL_CAPTURE.read_bytes()).hexdigest(),
                dimensions=image.size, glyphs=glyphs, results=results,
                gray_full_binary_diff_pixels=sum(p != 0 for p in diff.tobytes()),
                exact=sum(r['exact'] for r in results),
                false_glyph_accepts=sum(r['glyphs']['state'] == 'engaged' and not r['exact'] for r in results))


def raster_diagnosis():
    """Ablate the hinted atlas and compare source glyphs at threshold 128."""
    from PIL import Image
    from unittest.mock import patch
    from worker_ocr_shapes import templates, recognize
    lookup, spaces = templates()
    original = {key: {f: v for f, v in fonts.items() if f[2] == 'original'}
                for key, fonts in lookup.items()}
    original = {k: v for k, v in original.items() if v}
    report = {}
    for mode in RENDER_MODES + ('full-gray',):
        rgb, rect = render(FIXTURE, width=560, height=280, rendering=mode)
        image = Image.frombytes('RGB', (rect['w'], rect['h']), rgb).convert('L')
        before, after = {}, {}
        # Deliberately unrelated initial content opens the shape gate without
        # supplying expected text or any candidate characters to the decoder.
        with patch('worker_ocr_shapes.templates', return_value=(original, spaces)):
            recognize(image, [dict(text='a1')], before)
        recognize(image, [dict(text='a1')], after)
        differences = []
        for char in sorted(set(''.join(t for t, x, y in FIXTURE)) - {' '}):
            rgb, rect = render(((char, 24, 48),), width=96, height=96, rendering=mode)
            mask = Image.frombytes('RGB', (96, 96), rgb).convert('L').point(lambda p: 255 if p < 128 else 0)
            glyph = mask.crop(mask.getbbox())
            matching = original.get((glyph.size, glyph.tobytes()), {})
            differences.append(dict(char=char, size=glyph.size,
                                    original_exact=any(f[:2] == ('Sans', 24) for f in matching)))
        report[mode] = dict(round1=before, round2=after, rasters=differences)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--best', type=Path, default=Path(__file__).parent/'fixtures/tessdata_best')
    parser.add_argument('--shapes-only', action='store_true')
    parser.add_argument('--live-style', action='store_true', help='compare all eight rendering modes')
    parser.add_argument('--diagnose-rasters', action='store_true', help='record exact raster/atlas mismatch diagnostics')
    parser.add_argument('--real-capture', action='store_true', help='diagnose and score the unmodified X capture')
    parser.add_argument('--before', type=Path, help='round-1 live-style outputs for regression scoring')
    parser.add_argument('--allow-missing-best', action='store_true',
                        help='exploratory local sweep; comparison remains incomplete')
    args = parser.parse_args()
    if args.real_capture:
        args.output.write_text(json.dumps(real_capture_report(), indent=2)+'\n')
        return 0
    if args.diagnose_rasters:
        args.output.write_text(json.dumps(raster_diagnosis(), indent=2)+'\n')
        return 0
    if args.live_style:
        live_style_report(args.output, args.before)
        return 0
    corpus = samples()
    report = json.loads(args.output.read_text()) if args.shapes_only else dict(
        cairo=cairo.cairo_version_string(), sources=len(TEXTS), samples=len(corpus), candidates={})
    report['models'] = {}
    report.pop('best_unavailable', None)
    for model, path in [('fast', None), ('best', args.best)]:
        if path and not (path/'eng.traineddata').exists():
            report['best_unavailable'] = str(path/'eng.traineddata')
            continue
        model_file = path/'eng.traineddata' if path else Path('/usr/share/tesseract-ocr/5/tessdata/eng.traineddata')
        report['models'][model] = dict(path=str(model_file), sha256=hashlib.sha256(model_file.read_bytes()).hexdigest())
        for scale in ((2,) if args.shapes_only else (2, 3, 4)):
            for psm in ((None,) if args.shapes_only else (None, 7, 11, 'line7-block11')):
                name = f'{model}/x{scale}/psm{psm or "shape48"}' + ('/glyphs' if args.shapes_only else '')
                backend = Backend(datapath=path, scale=scale,
                                  psm=psm if isinstance(psm, int) else None, shapes=args.shapes_only)
                try:
                    result = measure(backend, corpus, 'line' if psm == 'line7-block11' else None)
                finally:
                    backend.close()
                report['candidates'][name] = result
                baseline = report['candidates']['fast/x2/psmshape48']['results']
                result['regressions'] = [r['key'] for old, r in zip(baseline, result['results'])
                                         if old['exact'] and not r['exact']]
                print(name, result['exact'], '/', result['total'],
                      'median_ms', round(result['median_ms'], 2),
                      'fixture', result['fixture'], 'regressions', len(result['regressions']), flush=True)
                args.output.write_text(json.dumps(report, indent=2)+'\n')
    report['comparison_complete'] = 'best_unavailable' not in report
    args.output.write_text(json.dumps(report, indent=2)+'\n')
    if not report['comparison_complete']:
        print('INCOMPLETE: official tessdata_best model unavailable at '+report['best_unavailable'], file=sys.stderr)
        if not args.allow_missing_best:
            return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
