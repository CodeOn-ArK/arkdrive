"""Conservative pixel recognizer for short/code-shaped OCR content.

This verifies complete glyph rasters against local font rendering. It never
maps one recognized character to another. Unsupported fonts, antialiasing,
sizes, touching glyphs or ambiguous rasters retain the Tesseract result.
"""
from collections import defaultdict
from functools import lru_cache
import math
import re
import string


def ink(image):
    return image.point(lambda p: 255 if p < 128 else 0).getbbox()


ATLAS_RENDERINGS = ('original', 'live-full', 'gray-none', 'gray-slight', 'gray-full')


@lru_cache(maxsize=1)
def prepare_templates():
    """Prepare the original modes during backend startup, before ready."""
    return build_templates(ATLAS_RENDERINGS[:2])


@lru_cache(maxsize=1)
def templates():
    # Stage construction across the existing startup and first-read budgets,
    # both still 1 s. All drawing is serial: concurrent font rasterization on
    # this Cairo stack can produce inconsistent shared-cache glyph rasters.
    original, widths = prepare_templates()
    lookup = defaultdict(dict, {key: dict(fonts) for key, fonts in original.items()})
    spaces = dict(widths)
    extra, extra_widths = build_templates(ATLAS_RENDERINGS[2:])
    for key, fonts in extra.items():
        lookup[key].update(fonts)
    spaces.update(extra_widths)
    return lookup, spaces


def build_templates(renderings, alpha_gray=True):
    """Build exact, separate configurations; also used by raster diagnostics."""
    import cairo
    from PIL import Image
    lookup = defaultdict(lambda: defaultdict(list))
    spaces = {}
    chars = string.ascii_letters + string.digits + string.punctuation
    # One complete configuration must match every glyph and its metrics.
    # GRAY/FULL matches the real X capture; keep subpixel and default modes
    # for their independently measured pixels. No per-glyph mixing is needed.
    for rendering in renderings:
        options = cairo.FontOptions()
        if rendering != 'original':
            options.set_antialias(cairo.ANTIALIAS_GRAY if rendering.startswith('gray-') else cairo.ANTIALIAS_SUBPIXEL)
            options.set_subpixel_order(cairo.SUBPIXEL_ORDER_RGB)
            options.set_hint_style({'none': cairo.HINT_STYLE_NONE, 'slight': cairo.HINT_STYLE_SLIGHT,
                                    'medium': cairo.HINT_STYLE_MEDIUM, 'full': cairo.HINT_STYLE_FULL}
                                   [rendering.split('-')[-1]])
            options.set_hint_metrics(cairo.HINT_METRICS_ON)
        for face in ('Sans', 'Serif', 'Monospace'):
            for size in range(10, 49):
                font = (face, size, rendering)
                alpha = alpha_gray and rendering.startswith('gray-')
                fmt = cairo.FORMAT_A8 if alpha else cairo.FORMAT_RGB24
                probe = cairo.Context(cairo.ImageSurface(fmt, 1, 1))
                probe.set_font_options(options)
                probe.select_font_face(face)
                probe.set_font_size(size)
                extents = {char: probe.text_extents(char) for char in chars}
                # Tight cells retain a blank pixel around Cairo's ink extents.
                # Rendering empty padding dominated strip conversion time.
                origin = 1-math.floor(min(e.x_bearing for e in extents.values()))
                baseline = 1-math.floor(min(e.y_bearing for e in extents.values()))
                cell = origin+math.ceil(max(e.x_bearing+e.width for e in extents.values()))+1
                height = baseline+math.ceil(max(e.y_bearing+e.height for e in extents.values()))+1
                surface = cairo.ImageSurface(fmt, cell*len(chars), height)
                ctx = cairo.Context(surface)
                ctx.set_font_options(options)
                if not alpha:
                    ctx.set_source_rgb(1, 1, 1)
                    ctx.paint()
                ctx.set_source_rgb(0, 0, 0)
                ctx.set_scaled_font(probe.get_scaled_font())
                # Cairo's toy text mapping is one glyph per ASCII character.
                # Batch drawing avoids re-entering its text/font lookup per cell.
                ids = ctx.get_scaled_font().text_to_glyphs(0, 0, chars, False)
                if len(ids) != len(chars):
                    raise ValueError('ocr_atlas_glyph_mapping')
                ctx.show_glyphs([(glyph[0], index*cell+origin, baseline)
                                 for index, glyph in enumerate(ids)])
                surface.flush()
                # Black on white is 255-alpha: L<128 equals alpha>127.
                # Only explicitly GRAY modes use this; DEFAULT on A8 may
                # resolve different font options from DEFAULT on RGB24.
                if alpha:
                    image = Image.frombytes('L', (surface.get_width(), height), bytes(surface.get_data()),
                                            'raw', 'L', surface.get_stride())
                    image = image.point(lambda p: 255 if p > 127 else 0)
                else:
                    image = Image.frombytes('RGB', (surface.get_width(), height), bytes(surface.get_data()),
                                            'raw', 'BGRX', surface.get_stride()).convert('L')
                    image = image.point(lambda p: 255 if p < 128 else 0)
                for index, char in enumerate(chars):
                    crop = image.crop((index*cell, 0, (index+1)*cell, height))
                    bounds = crop.getbbox()
                    if bounds:
                        glyph = crop.crop(bounds)
                        key = (glyph.size, glyph.tobytes())
                        lookup[key][font].append((char, bounds[0]-origin,
                                                  extents[char].x_advance,
                                                  bounds[1]-baseline))
                spaces[font] = ctx.text_extents(' ').x_advance
    return lookup, spaces


def recognize(image, recognized, diagnostics=None):
    """Return exact-font lines only; the gate uses recognized content shape."""
    def record(state, reason=None, **detail):
        if diagnostics is not None:
            diagnostics.update(state=state, reason=reason, **detail)
    if not any(re.search(r'\b\w{1,4}\b|\d', line['text']) for line in recognized):
        record('abstained', 'content_gate')
        return None
    image = image.point(lambda p: 0 if p < 128 else 255)
    bounds = ink(image)
    if bounds is None:
        record('abstained', 'no_ink')
        return None
    lookup, spaces = templates()
    # Horizontal projection finds physical lines independently of PSM splits.
    rows = [y for y in range(bounds[1], bounds[3])
            if ink(image.crop((bounds[0], y, bounds[2], y+1)))]
    runs = []
    for y in rows:
        if not runs or y > runs[-1][1]+1:
            runs.append([y, y])
        else:
            runs[-1][1] = y
    # Join detached dots/accents using the source glyph height, not a label.
    merged = []
    for top, bottom in runs:
        if merged and top-merged[-1][1] <= max(3, (bottom-top+1)//2):
            merged[-1][1] = bottom
        else:
            merged.append([top, bottom])
    result = []
    for top, bottom in merged:
        band = image.crop((0, top, image.width, bottom+1))
        bb = ink(band)
        columns = [x for x in range(bb[0], bb[2]) if ink(band.crop((x, 0, x+1, band.height)))]
        spans = []
        for x in columns:
            if not spans or x > spans[-1][1]+1:
                spans.append([x, x])
            else:
                spans[-1][1] = x
        candidates = None
        glyphs = []
        for left, right in spans:
            crop = band.crop((left, 0, right+1, band.height))
            gb = ink(crop)
            crop = crop.crop(gb)
            matches = lookup.get((crop.size, crop.point(lambda p: 255-p).tobytes()), {})
            candidates = set(matches) if candidates is None else candidates.intersection(matches)
            glyphs.append((left, right, top+gb[1], matches))
        decoded = set()
        for font in candidates or ():
            text, prior_end, baseline = '', None, None
            for left, right, y, matches in glyphs:
                options = matches[font]
                if len(options) != 1:
                    break
                char, bearing, advance, ybearing = options[0]
                origin = left-bearing
                if baseline is not None and y-ybearing != baseline:
                    break
                baseline = y-ybearing
                if prior_end is not None:
                    gap = origin-prior_end
                    if gap < 0 or gap > spaces[font]*1.5:
                        break
                    if gap > spaces[font]*.5:
                        text += ' '
                    elif gap != 0:
                        break
                text += char
                prior_end = origin+advance
            else:
                decoded.add(text)
        if len(decoded) != 1:
            record('abstained', 'raster_miss' if not candidates else 'metrics_or_ambiguity',
                   band=[top, bottom], glyphs=len(glyphs),
                   unmatched=sum(not g[3] for g in glyphs),
                   candidate_fonts=len(candidates or ()), decodings=sorted(decoded))
            return None
        text = decoded.pop()
        if any(c.isalnum() for c in text):
            result.append(dict(text=text, box=[bb[0], top, bb[2]-bb[0], bottom-top+1], conf=100.0))
    record('engaged', lines=len(result))
    return result
