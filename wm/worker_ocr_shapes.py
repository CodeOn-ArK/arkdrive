"""Conservative pixel recognizer for short/code-shaped OCR content.

This verifies complete glyph rasters against local font rendering. It never
maps one recognized character to another. Unsupported fonts, antialiasing,
sizes, touching glyphs or ambiguous rasters retain the Tesseract result.
"""
from collections import defaultdict
from functools import lru_cache
import re
import string


def ink(image):
    return image.point(lambda p: 255 if p < 128 else 0).getbbox()


@lru_cache(maxsize=1)
def templates():
    import cairo
    from PIL import Image
    lookup = defaultdict(lambda: defaultdict(list))
    spaces = {}
    for face in ('Sans', 'Serif', 'Monospace'):
        for size in range(10, 49):
            font = (face, size)
            for char in string.ascii_letters + string.digits + string.punctuation:
                surface = cairo.ImageSurface(cairo.FORMAT_RGB24, size*3, size*3)
                ctx = cairo.Context(surface)
                ctx.set_source_rgb(1, 1, 1)
                ctx.paint()
                ctx.set_source_rgb(0, 0, 0)
                ctx.select_font_face(face)
                ctx.set_font_size(size)
                ctx.move_to(size, size*2)
                ctx.show_text(char)
                surface.flush()
                image = Image.frombytes('RGB', (size*3, size*3), bytes(surface.get_data()),
                                        'raw', 'BGRX', surface.get_stride()).convert('L')
                bounds = ink(image)
                if bounds:
                    glyph = image.crop(bounds).point(lambda p: 0 if p < 128 else 255)
                    key = (glyph.size, glyph.tobytes())
                    lookup[key][font].append((char, bounds[0]-size,
                                              ctx.text_extents(char).x_advance,
                                              bounds[1]-size*2))
                spaces[font] = ctx.text_extents(' ').x_advance
    return lookup, spaces


def recognize(image, recognized):
    """Return exact-font lines only; the gate uses recognized content shape."""
    if not any(re.search(r'\b\w{1,4}\b|\d', line['text']) for line in recognized):
        return None
    image = image.point(lambda p: 0 if p < 128 else 255)
    bounds = ink(image)
    if bounds is None:
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
            matches = lookup.get((crop.size, crop.tobytes()), {})
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
            return None
        text = decoded.pop()
        if any(c.isalnum() for c in text):
            result.append(dict(text=text, box=[bb[0], top, bb[2]-bb[0], bottom-top+1], conf=100.0))
    return result
