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


def render(commands, face='Sans', size=24, width=None, height=None):
    probe = cairo.Context(cairo.ImageSurface(cairo.FORMAT_RGB24, 1, 1))
    probe.select_font_face(face)
    probe.set_font_size(size)
    width = width or int(max(x+probe.text_extents(t).x_advance for t, x, y in commands)+16)
    height = height or size+24
    surface = cairo.ImageSurface(cairo.FORMAT_RGB24, width, height)
    ctx = cairo.Context(surface)
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


def samples():
    return [(f'{face}/{size}/{text}', text, *render(((text, 12, size+8),), face, size))
            for face in FACES for size in SIZES for text in TEXTS]


def measure(backend, corpus, line_mode=None):
    results, times = [], []
    for key, expected, rgb, rect in corpus:
        start = time.perf_counter()
        actual = '\n'.join(l['text'] for l in backend.read(rgb, rect, mode=line_mode))
        times.append((time.perf_counter()-start)*1000)
        results.append(dict(key=key, expected=expected, actual=actual, exact=actual == expected))
    rgb, rect = render(FIXTURE, width=560, height=280)
    start = time.perf_counter()
    fixture = [l['text'] for l in backend.read(rgb, rect)]
    return dict(exact=sum(r['exact'] for r in results), total=len(results),
                rate=sum(r['exact'] for r in results)/len(results),
                median_ms=statistics.median(times), max_ms=max(times),
                fixture_ms=(time.perf_counter()-start)*1000,
                fixture=fixture, fixture_exact=fixture == [t for t, x, y in FIXTURE],
                results=results)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--best', type=Path, default=Path(__file__).parent/'fixtures/tessdata_best')
    parser.add_argument('--shapes-only', action='store_true')
    parser.add_argument('--allow-missing-best', action='store_true',
                        help='exploratory local sweep; comparison remains incomplete')
    args = parser.parse_args()
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
