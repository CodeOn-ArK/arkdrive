"""W3 pixel cache, XWD shadow framebuffer and stdlib PNG evidence store.

X reads use the owner's deferred reply pump. Dirty tracking never reads pixels;
settle() is polled by the owner and settles the entire desktop before a rev is
published. No input, provider, image library or new X connection lives here.
"""
from collections import deque
from fractions import Fraction
import hashlib
import json
import mmap
from pathlib import Path
import struct
import zlib

from Xlib import X
from Xlib.protocol import request

from worker_act import ActError

TILE = 64
QUOTA = 200 * 1024 * 1024


def intersect(a, b):
    x, y = max(a['x'], b['x']), max(a['y'], b['y'])
    r, d = min(a['x'] + a['w'], b['x'] + b['w']), min(a['y'] + a['h'], b['y'] + b['h'])
    return dict(x=x, y=y, w=max(0, r-x), h=max(0, d-y))


def bbox(rects):
    rects = list(rects)
    if not rects:
        return None
    x, y = min(r['x'] for r in rects), min(r['y'] for r in rects)
    return dict(x=x, y=y, w=max(r['x']+r['w'] for r in rects)-x,
                h=max(r['y']+r['h'] for r in rects)-y)


class Shadow:
    def __init__(self, width, height, path=None, byte_order=0):
        self.w, self.h = width, height
        self.offset, self.stride, self.byte_order = 0, width * 4, byte_order
        self.file = self.mapping = None
        self.mode = 'getimage'
        if path is not None and Path(path).exists():
            self.file = Path(path).open('rb')
            try:
                header = self.file.read(100)
                fields = struct.unpack('>25I', header)
                (header_size, version, fmt, depth, w, h, xoffset, order,
                 unit, bit_order, pad, bpp, stride, visual, red, green, blue,
                 bits_rgb, entries, ncolors, *_) = fields
                offset = header_size + ncolors * 12
                if (version != 7 or fmt != X.ZPixmap or depth != 24 or (w, h) != (width, height)
                        or xoffset or visual != 4 or bpp != 32 or order not in (0, 1) or stride < width*4
                        or (red, green, blue) != (0xff0000, 0xff00, 0xff)
                        or header_size < 100 or offset + stride*h > self.file.seek(0, 2)):
                    raise ValueError('unsupported_xwd')
                self.offset, self.stride, self.byte_order = offset, stride, order
                self.mapping = mmap.mmap(self.file.fileno(), 0, access=mmap.ACCESS_READ)
                self.data, self.mode = self.mapping, 'mmap'
            except Exception:
                self.file.close()
                raise
        else:
            self.data = bytearray(self.stride * height)

    def close(self):
        if self.mapping is not None:
            self.mapping.close()
            self.file.close()

    def update(self, rect, data):
        # Depth 24 / bpp 32 has no extra scanline padding beyond width*4.
        stride = rect['w'] * 4
        if len(data) != stride * rect['h']:
            raise ValueError('invalid_getimage_length')
        for y in range(rect['h']):
            start = (rect['y'] + y) * self.stride + rect['x'] * 4
            self.data[start:start+stride] = data[y*stride:(y+1)*stride]

    def row(self, x, y, width):
        start = self.offset + y*self.stride + x*4
        raw = self.data[start:start+width*4]
        rgb = bytearray(width*3)
        r, g, b = (2, 1, 0) if self.byte_order == 0 else (1, 2, 3)
        rgb[0::3], rgb[1::3], rgb[2::3] = raw[r::4], raw[g::4], raw[b::4]
        return bytes(rgb)

    def digest(self, rect):
        h = hashlib.blake2b(digest_size=8)
        for y in range(rect['y'], rect['y'] + rect['h']):
            h.update(self.row(rect['x'], y, rect['w']))
        return h.digest()

    def png(self, rect, scale):
        width, height = max(1, int(rect['w']*scale)), max(1, int(rect['h']*scale))
        compressor, chunks = zlib.compressobj(1), []
        xs = [int(x/scale)*3 for x in range(width)] if scale != 1 else None
        gathers = None
        if xs is not None:
            ratio = Fraction(scale).limit_denominator(65536)
            period, step = ratio.numerator, ratio.denominator*3
            # Rational scales (including both profile defaults) can gather
            # whole interleaved columns with C-level slices. Check the exact
            # float mapping once; unusual scales retain the general fallback.
            if period <= 64 and all(xs[i] == xs[i % period] + (i//period)*step for i in range(width)):
                gathers = [(phase*3+c, xs[phase]+c,
                            xs[phase]+c+((width-1-phase)//period)*step+1)
                           for phase in range(min(period, width)) for c in range(3)]
        for y in range(height):
            row = self.row(rect['x'], rect['y'] + int(y/scale), rect['w'])
            if gathers is not None:
                output = bytearray(width*3)
                for dst, start, end in gathers:
                    output[dst::period*3] = row[start:end:step]
                row = output
            elif xs is not None:
                row = b''.join(row[x:x+3] for x in xs)
            chunks.append(compressor.compress(b'\0' + row))
        chunks.append(compressor.flush())
        def chunk(kind, data):
            return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind+data))
        return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>2I5B', width, height, 8, 2, 0, 0, 0))
                + chunk(b'IDAT', b''.join(chunks)) + chunk(b'IEND', b''))


class Frames:
    def __init__(self, directory, quota=QUOTA):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.quota, self.issued = quota, {}
        self.pins = set()
        self.receipts = deque(maxlen=50)

    @staticmethod
    def references(value):
        if isinstance(value, dict):
            found = {Path(value['path']).name} if 'frame' in value and isinstance(value.get('path'), str) else set()
            for v in value.values():
                found.update(Frames.references(v))
            return found
        if isinstance(value, list):
            return set().union(*(Frames.references(v) for v in value)) if value else set()
        return set()

    def retain(self, receipt):
        self.receipts.append(self.references(receipt))
        self.evict()

    def restore(self, state, session):
        # Surviving files and the last 50 durable session receipts survive restart.
        rows = []
        for path in sorted((Path(state) / 'acts').glob('*.jsonl')):
            recent = deque(maxlen=50)
            with path.open() as stream:
                for line in stream:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        break
                    if row.get('session') == session and row.get('type') == 'receipt':
                        recent.append(row)
            rows.extend(recent)
        for row in sorted(rows, key=lambda r: r.get('ts', ''))[-50:]:
            self.receipts.append(self.references(row['receipt']))
        self.evict()

    def evict(self, extra=0, protect=()):
        pinned = set(protect).union(self.pins, *self.receipts)
        files = sorted(self.directory.glob('*.png'), key=lambda p: (p.stat().st_mtime_ns, p.name))
        total = sum(p.stat().st_size for p in files)
        for path in files:
            if total + extra <= self.quota:
                break
            if path.name not in pinned:
                total -= path.stat().st_size
                path.unlink()
        self.issued = {k: v for k, v in self.issued.items() if Path(v['path']).exists()}
        if total + extra > self.quota:
            raise ActError('provider_degraded', 'frame_quota')

    def put(self, png, metadata):
        digest = hashlib.sha256(png).hexdigest()
        path = self.directory / (digest + '.png')
        self.evict(0 if path.exists() else len(png), [path.name])
        if not path.exists():
            temp = path.with_suffix('.tmp')
            try:
                with temp.open('wb') as stream:
                    stream.write(png)
                temp.chmod(0o600)
                temp.replace(path)
            finally:
                temp.unlink(missing_ok=True)
        # Identical PNGs at different desktop offsets share a file, never a
        # coordinate transform. Frame identity includes its issuance metadata.
        identity = hashlib.sha256(json.dumps([digest, metadata], sort_keys=True).encode()).hexdigest()
        value = dict(metadata, frame='f:' + identity, path=str(path))
        self.issued[value['frame']] = value
        return {k: value[k] for k in ('frame', 'path', 'rev', 'rect', 'scale', 'space')}


class Pixels:
    def __init__(self, worker, shadow):
        self.w, self.shadow = worker, shadow
        self.bounds = dict(x=0, y=0, w=shadow.w, h=shadow.h)
        self.cols = (shadow.w + TILE-1)//TILE
        self.rects = [dict(x=x, y=y, w=min(TILE, shadow.w-x), h=min(TILE, shadow.h-y))
                      for y in range(0, shadow.h, TILE) for x in range(0, shadow.w, TILE)]
        self.hashes = [None] * len(self.rects)
        self.dirty, self.serial, self.pending, self.failure = {}, 0, False, None
        self.frames = Frames(worker.sd / 'driver/frames')
        self.frames.restore(worker.state, worker.sid)
        self.mark([self.bounds])

    def facet(self, tile):
        return f'pixels[{tile}]'

    def tiles(self, rect):
        r = intersect(rect, self.bounds)
        if not r['w'] or not r['h']:
            return set()
        return {y*self.cols+x for y in range(r['y']//TILE, (r['y']+r['h']-1)//TILE+1)
                for x in range(r['x']//TILE, (r['x']+r['w']-1)//TILE+1)}

    def mark(self, rects):
        self.serial += 1
        for rect in rects:
            for tile in self.tiles(rect):
                self.dirty[tile] = (self.w.cache.rev+1, self.serial)

    def settle(self):
        if self.failure:
            raise ActError('provider_degraded', self.failure)
        if self.pending:
            return False
        if not self.dirty:
            return True
        batch = dict(self.dirty)
        def hashed():
            changed = []
            for tile, stamp in batch.items():
                digest = self.shadow.digest(self.rects[tile])
                old = self.hashes[tile]
                self.hashes[tile] = digest
                if old is not None and digest != old:
                    changed.append(tile)
                else:
                    self.w.cache.facets.setdefault(self.facet(tile), 1)
                if self.dirty.get(tile) == stamp:
                    del self.dirty[tile]
            self.w.cache.changed.update(self.facet(t) for t in changed)
            self.w.cache.commit()
        if self.shadow.mode == 'mmap':
            hashed()
            return not self.dirty
        rect = bbox(self.rects[t] for t in batch)
        if sum(self.rects[t]['w']*self.rects[t]['h'] for t in batch) > self.shadow.w*self.shadow.h/2:
            rect = self.bounds
        self.pending = True
        def captured(data):
            try:
                if not data or data['depth'] != 24:
                    raise ValueError('capture_failed')
                self.shadow.update(rect, data['data'])
                hashed()
            except Exception as exc:
                self.failure = str(exc)
            finally:
                self.pending = False
        self.w.x.ax.ask(request.GetImage, captured, drawable=self.w.x.root,
                        x=rect['x'], y=rect['y'], width=rect['w'], height=rect['h'],
                        format=X.ZPixmap, plane_mask=0xffffffff)
        return False

    def window(self, handle):
        win = next((v for v in self.w.cache.windows.values() if v['handle'] == handle and v['mapped']), None)
        if win is None:
            raise ActError('target_not_found')
        return win

    def desktop_point(self, raw, base=None):
        space = raw.get('space', 'desktop')
        x, y = raw['x'], raw['y']
        if space == 'desktop':
            return x, y
        if not isinstance(space, str):
            raise ActError('invalid_request')
        if space.startswith('window:'):
            win = self.window(space[7:])
            rev = raw.get('at_rev', base)
            g = win['geometry']
            if not 0 <= x < g['w'] or not 0 <= y < g['h']:
                raise ActError('invalid_request')
            x, y = x+g['x'], y+g['y']
            guards = {win['handle']: self.w.cache.facets.get('geometry['+win['handle']+']', 1)}
        elif space.startswith('frame:'):
            frame = self.frames.issued.get(space[6:])
            if frame is None or not Path(frame['path']).exists():
                raise ActError('transform_stale')
            rev, g, scale = frame['rev'], frame['rect'], frame['scale']
            if not 0 <= x < max(1, int(g['w']*scale)) or not 0 <= y < max(1, int(g['h']*scale)):
                raise ActError('invalid_request')
            x, y = g['x']+int(x/scale), g['y']+int(y/scale)
            guards = frame['geometry']
        else:
            raise ActError('transform_stale')
        if type(rev) is not int or not 1 <= rev <= self.w.cache.rev:
            raise ActError('stale_rev')
        for handle in guards:
            if (not any(v['handle'] == handle for v in self.w.cache.windows.values()) or
                    self.w.cache.facets.get('geometry['+handle+']', 1) > rev):
                raise ActError('stale_rev')
        return x, y

    def region(self, params):
        wins = self.w.cache.selected(params.get('scope', {'kind': 'session'}))
        rect = self.bounds if params.get('scope', {}).get('kind', 'session') == 'session' else bbox(
            win['geometry'] for win in wins if win['mapped'])
        raw = params.get('region')
        if raw is not None:
            if (not isinstance(raw, dict) or set(raw) - {'space', 'x', 'y', 'w', 'h', 'at_rev'} or
                    any(type(raw.get(k)) is not int for k in ('x', 'y', 'w', 'h')) or raw['w'] <= 0 or raw['h'] <= 0):
                raise ActError('invalid_params')
            x, y = self.desktop_point(raw, params.get('since_rev'))
            scale = self.frames.issued[raw['space'][6:]]['scale'] if raw.get('space', '').startswith('frame:') else 1
            rect = intersect(rect or self.bounds, dict(x=x, y=y, w=max(1, int(raw['w']/scale)), h=max(1, int(raw['h']/scale))))
        rect = intersect(rect or dict(x=0, y=0, w=0, h=0), self.bounds)
        return rect, wins

    def delta(self, rect, since):
        changed = [t for t in sorted(self.tiles(rect)) if since is None or self.w.cache.facets.get(self.facet(t), 1) > since]
        return {'bbox': bbox(intersect(self.rects[t], rect) for t in changed), 'tiles': changed} if changed else None

    def frame(self, rect, wins, scale=None):
        if self.dirty or self.pending:
            raise ActError('provider_degraded', 'capture_pending')
        if not rect['w'] or not rect['h']:
            raise ActError('target_not_found')
        default = min(1, 640/rect['w'], 360/rect['h'])
        if scale is None:
            scale = default
        if type(scale) not in (float, int) or not 0 < scale <= 1:
            raise ActError('invalid_params', 'frame_scale')
        visible = []
        for win in wins:
            clipped = intersect(win['geometry'], rect)
            if win['mapped'] and clipped['w'] and clipped['h']:
                visible.append(win)
        wins = visible
        metadata = dict(rev=self.w.cache.rev, rect=rect, scale=scale, space='desktop',
                        generation=self.w.generation,
                        geometry={win['handle']: self.w.cache.facets.get('geometry['+win['handle']+']', 1) for win in wins})
        return self.frames.put(self.shadow.png(rect, scale), metadata)

    def receipt_frames(self, receipt):
        for step in receipt['steps']:
            if step['disposition'] == 'unexecuted' or step['effect']['status'] not in ('not_observed', 'contradicted', 'unknown'):
                continue
            handle = step['target_resolved']
            if handle is None:
                continue
            try:
                win = next((v for v in self.w.cache.windows.values() if v['handle'] == handle),
                           self.w.cache.tombstones.get(handle))
                if win is None:
                    raise ActError('target_not_found')
                frame = self.frame(intersect(win['geometry'], self.bounds), [win], scale=1)
                step['effect']['evidence'].append(dict(frame, type='frame'))
                # Protect every frame in this in-progress receipt immediately.
                self.frames.pins.update(self.frames.references(receipt))
            except ActError as exc:
                step['effect']['capture_error'] = exc.detail or exc.reason
        self.frames.retain(receipt)
        self.frames.pins.clear()
