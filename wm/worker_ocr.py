#!/usr/bin/python3
"""W7 CPU OCR child and its nonblocking worker-side supervisor.

Only Backend loads libtesseract. The worker copies settled W3 RGB pixels to a
single inherited memfd; framed IPC contains descriptors/results, never X IDs
to capture. One flight owns the buffer until its reply or child retirement.
"""
from collections import OrderedDict
import copy
import ctypes as C
import hashlib
import math
import mmap
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import time
import unicodedata

from worker_act import ActError
from worker_pixels import intersect
from worker_tree import Wire, packet, MAX_IPC

OCR_KINDS = {'ocr_text', 'ocr_text_present', 'ocr_text_absent'}
IMAGE_LIMIT = 64 * 1024 * 1024
PARAMETERS = ('eng', 'grey', 2, 60, 'sparse/line48', 'glyph-v4')


class PipePair:
    """Wire's byte-stream interface over two inherited, session-local pipes."""
    def __init__(self, read_fd, write_fd):
        self.read_fd, self.write_fd = read_fd, write_fd

    def setblocking(self, value):
        os.set_blocking(self.read_fd, value)
        os.set_blocking(self.write_fd, value)

    def recv(self, count):
        return os.read(self.read_fd, count)

    def send(self, data):
        return os.write(self.write_fd, data)

    def close(self):
        os.close(self.read_fd)
        os.close(self.write_fd)


def ordered(lines):
    return sorted(lines, key=lambda l: (l['box'][1], l['box'][0], l['text']))


def overlaps(line, rect):
    x, y, w, h = line['box']
    return x < rect['x']+rect['w'] and x+w > rect['x'] and y < rect['y']+rect['h'] and y+h > rect['y']


def merge_lines(old, new, slab, damage):
    """Keep unaffected boundary lines whole, including their original boxes."""
    preserved = [l for l in old if not overlaps(l, slab) or
                 (not overlaps(l, damage) and
                  (l['box'][1] <= slab['y'] or l['box'][1]+l['box'][3] >= slab['y']+slab['h']))]
    fresh = []
    for line in new:
        top, bottom = line['box'][1], line['box'][1]+line['box'][3]
        edge = top <= slab['y'] or bottom >= slab['y']+slab['h']
        if edge and any(overlaps(line, dict(zip(('x', 'y', 'w', 'h'), prior['box'])))
                        for prior in preserved):
            continue
        fresh.append(line)
    return ordered(preserved + fresh)


class Backend:
    def __init__(self, datapath=None, scale=2, psm=None, shapes=True):
        from PIL import Image
        self.Image = Image
        self.lib = lib = C.CDLL('libtesseract.so.5')
        p, i = C.c_void_p, C.c_int
        signatures = {
            'TessBaseAPICreate': (p, []), 'TessBaseAPIDelete': (None, [p]),
            'TessBaseAPIInit3': (i, [p, C.c_char_p, C.c_char_p]),
            'TessBaseAPISetVariable': (i, [p, C.c_char_p, C.c_char_p]),
            'TessBaseAPISetPageSegMode': (None, [p, i]),
            'TessBaseAPISetImage': (None, [p, p, i, i, i, i]),
            'TessBaseAPISetSourceResolution': (None, [p, i]),
            'TessBaseAPIRecognize': (i, [p, p]),
            'TessBaseAPIGetTsvText': (p, [p, i]),
            'TessDeleteText': (None, [p]), 'TessBaseAPIClear': (None, [p]),
        }
        for name, (result, args) in signatures.items():
            fn = getattr(lib, name)
            fn.restype, fn.argtypes = result, args
        self.scale, self.psm, self.shapes = scale, psm, shapes
        if shapes:
            from worker_ocr_shapes import prepare_templates
            prepare_templates()
        self.api = lib.TessBaseAPICreate()
        if not self.api or lib.TessBaseAPIInit3(self.api, os.fsencode(datapath) if datapath else None, b'eng'):
            self.close()
            raise RuntimeError('ocr_init_failed')
        lib.TessBaseAPISetVariable(self.api, b'debug_file', b'/dev/null')
        lib.TessBaseAPISetVariable(self.api, b'classify_enable_learning', b'0')

    def close(self):
        if self.api:
            self.lib.TessBaseAPIDelete(self.api)
            self.api = None

    @staticmethod
    def lines(tsv, rect, scale=2, min_conf=60):
        groups = OrderedDict()
        for row in tsv.splitlines()[1:]:
            fields = row.split('\t', 11)
            if len(fields) != 12 or fields[0] != '5':
                continue
            conf = float(fields[10])
            if conf < min_conf or not fields[11].strip():
                continue
            left, top, width, height = map(int, fields[6:10])
            groups.setdefault(tuple(fields[1:5]), []).append((fields[11], left, top, width, height, conf))
        result = []
        for words in groups.values():
            text = ' '.join(v[0] for v in words)
            if not any(c.isalnum() for c in text):
                continue
            x, y = min(v[1] for v in words), min(v[2] for v in words)
            right, bottom = max(v[1]+v[3] for v in words), max(v[2]+v[4] for v in words)
            # Round outwards after undoing preprocessing; never frame-scaled.
            result.append(dict(text=text,
                               box=[rect['x']+x//scale, rect['y']+y//scale,
                                    math.ceil(right/scale)-x//scale, math.ceil(bottom/scale)-y//scale],
                               conf=min(v[5] for v in words)))
        return ordered(result)

    def read(self, rgb, rect, mode=None):
        w, h = rect['w'], rect['h']
        if w <= 0 or h <= 0 or len(rgb) != w*h*3 or len(rgb) > IMAGE_LIMIT:
            raise ValueError('ocr_image_invalid')
        image = self.Image.frombytes('RGB', (w, h), rgb).convert('L')
        source = image
        scale = self.scale
        image = image.resize((w*scale, h*scale), self.Image.Resampling.BICUBIC)
        raw = C.create_string_buffer(image.tobytes())
        lib, api = self.lib, self.api
        lib.TessBaseAPISetPageSegMode(api, self.psm if self.psm is not None else
                                   7 if mode == 'line' or mode is None and h <= 48 else 11)
        lib.TessBaseAPISetImage(api, raw, w*scale, h*scale, 1, w*scale)
        lib.TessBaseAPISetSourceResolution(api, 96*scale)
        text = None
        try:
            if lib.TessBaseAPIRecognize(api, None):
                raise RuntimeError('ocr_recognition_failed')
            text = lib.TessBaseAPIGetTsvText(api, 0)
            if not text:
                raise RuntimeError('ocr_result_missing')
            tsv = C.string_at(text).decode('utf-8')
            if self.shapes:
                from worker_ocr_shapes import recognize
                self.last_shapes = {}
                refined = recognize(source, self.lines(tsv, dict(x=0, y=0), scale, min_conf=0),
                                    self.last_shapes, fallback=self.lines(tsv, dict(x=0, y=0), scale))
                if refined is not None:
                    for line in refined:
                        line['box'][0] += rect['x']
                        line['box'][1] += rect['y']
                    return ordered(refined)
            return self.lines(tsv, rect, scale)
        finally:
            if text:
                lib.TessDeleteText(text)
            lib.TessBaseAPIClear(api)


class Provider:
    def __init__(self, worker, factory=None, clock=time.monotonic):
        self.w, self.clock = worker, clock
        self.factory = factory or self.spawn
        self.fd = os.memfd_create('ark-ocr-pixels', os.MFD_CLOEXEC)
        os.ftruncate(self.fd, IMAGE_LIMIT)
        self.image = mmap.mmap(self.fd, IMAGE_LIMIT)
        self.cache, self.latest = OrderedDict(), OrderedDict()
        self.wire = self.child = self.flight = None
        self.retired = []
        self.ready, self.seq, self.requests = False, 0, 0
        self.child_requests = 0
        self.state, self.reason = 'pending', None
        self.breaches, self.failures, self.attempt, self.due = 0, [], 0, 0
        self.start()

    def spawn(self):
        child_read, parent_write = os.pipe2(os.O_CLOEXEC)
        try:
            parent_read, child_write = os.pipe2(os.O_CLOEXEC)
        except OSError:
            os.close(child_read)
            os.close(parent_write)
            raise
        env = os.environ.copy()
        # Explicitly no display/bus discovery, including inherited GPU hints.
        for name in ('DISPLAY', 'WAYLAND_DISPLAY', 'DBUS_SESSION_BUS_ADDRESS', 'AT_SPI_BUS_ADDRESS'):
            env.pop(name, None)
        env.update(OMP_THREAD_LIMIT='1', PYTHONDONTWRITEBYTECODE='1')
        env.update(HOME=self.w.record.get('home', str(self.w.sd)),
                   ARK_SESSIOND=str(self.w.state), ARK_SESSION_ID=self.w.sid)
        if self.w.record.get('runtime_dir'):
            env['XDG_RUNTIME_DIR'] = self.w.record['runtime_dir']
        else:
            env.pop('XDG_RUNTIME_DIR', None)
        log = None
        try:
            log = (self.w.sd / 'logs/ocr-provider.log').open('ab')
            proc = subprocess.Popen(['/usr/bin/python3', str(Path(__file__).resolve()),
                                     str(child_read), str(child_write), str(self.fd)],
                                    pass_fds=(child_read, child_write, self.fd), env=env,
                                    stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        except Exception:
            os.close(parent_read)
            os.close(parent_write)
            raise
        finally:
            os.close(child_read)
            os.close(child_write)
            if log:
                log.close()
        return Wire(PipePair(parent_read, parent_write)), proc

    def start(self):
        self.started, self.ready = self.clock(), False
        self.child_requests = 0
        if self.flight:
            self.flight['sent'] = False
        try:
            self.wire, self.child = self.factory()
        except (OSError, ValueError):
            self.fail('provider_unavailable')

    def retire(self):
        if self.wire:
            self.wire.close()
        if self.child:
            if self.child.poll() is None:
                self.child.kill()
            self.retired.append(self.child)
        self.wire = self.child = None
        self.ready = False

    def close(self):
        self.retire()
        for child in self.retired:
            child.wait()
        self.image.close()
        os.close(self.fd)

    def fail(self, reason):
        now = self.clock()
        self.retire()
        self.breaches += 1
        self.failures = [t for t in self.failures if now-t < 60] + [now]
        self.reason = reason
        if len(self.failures) >= 2:
            self.state = 'down'
            self.due = now + min(30, 2 ** min(self.attempt, 5))
            self.attempt += 1
            self.flight = None
        else:
            self.state = 'degraded'
            # Restart immediately. A ready message retries the single read.
            self.start()

    def health(self):
        return dict(state=self.state, reason=self.reason, breaches=self.breaches,
                    next_probe_ms=max(0, int((self.due-self.clock())*1000)))

    def coverage(self):
        pixels = getattr(self.w, 'pixels', None)
        complete = bool(pixels and self.state == 'healthy' and not pixels.dirty and not pixels.pending and not pixels.failure)
        if complete:
            for win in self.w.cache.windows.values():
                if not win['mapped']:
                    continue
                rect = intersect(win['geometry'], pixels.bounds)
                value = self.latest.get((win['handle'], tuple(rect.values()), PARAMETERS))
                if (value is None or self.w.cache.facets.get('geometry['+win['handle']+']', 1) > value['rev'] or
                        any(self.w.cache.facets.get(pixels.facet(t), 1) > value['rev'] for t in pixels.tiles(rect))):
                    complete = False
                    break
        return dict(complete=complete, reason=None if complete else
                    'provider_degraded' if self.state in ('down', 'degraded') else 'refresh_pending')

    @staticmethod
    def remember(cache, key, value):
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > 256:
            cache.popitem(last=False)

    def read(self, window, rect=None, mode=None):
        pixels = getattr(self.w, 'pixels', None)
        if pixels is None or pixels.dirty or pixels.pending or pixels.failure:
            return None
        win = pixels.window(window)
        rect = intersect(rect or win['geometry'], intersect(win['geometry'], pixels.bounds))
        if not rect['w'] or not rect['h'] or rect['w']*rect['h']*3 > IMAGE_LIMIT:
            return None
        # Hash source RGB, not OCR or a lossy/downscaled evidence frame.
        rows = [pixels.shadow.row(rect['x'], y, rect['w']) for y in range(rect['y'], rect['y']+rect['h'])]
        rgb_region = b''.join(rows)
        digest = hashlib.sha256(rgb_region).hexdigest()
        parameters = PARAMETERS + (mode,) if mode is not None else PARAMETERS
        identity = (window, tuple(rect.values()), parameters)
        key = (tuple(rect.values()), digest, parameters)
        if key in self.cache:
            value = self.cache[key]
            value['rev'] = self.w.cache.rev
            self.cache.move_to_end(key)
            self.remember(self.latest, identity, value)
            return copy.deepcopy(value['lines'])
        if self.state == 'down' or self.flight is not None:
            return None
        hashes = [hashlib.blake2b(row, digest_size=8).digest() for row in rows]
        stride = rect['w']*3
        columns = [hashlib.blake2b(b''.join(rgb_region[x*3+c::stride] for c in range(3)),
                                   digest_size=8).digest() for x in range(rect['w'])]
        previous = self.latest.get(identity)
        slab, damage = dict(rect), dict(rect)
        if previous:
            changed = [i for i, (a, b) in enumerate(zip(hashes, previous['rows'])) if a != b]
            if changed:
                xs = [i for i, (a, b) in enumerate(zip(columns, previous['columns'])) if a != b]
                damage.update(x=rect['x']+min(xs), w=max(xs)-min(xs)+1,
                              y=rect['y']+min(changed), h=max(changed)-min(changed)+1)
                top = max(rect['y'], damage['y']-24)
                bottom = min(rect['y']+rect['h'], damage['y']+damage['h']+24)
                slab.update(y=top, h=bottom-top)
        first = slab['y']-rect['y']
        rgb = b''.join(rows[first:first+slab['h']])
        self.flight = dict(key=key, identity=identity, rows=hashes, columns=columns, digest=digest, rect=rect,
                           rev=self.w.cache.rev, mode=mode,
                           slab=slab, damage=damage, previous=previous, rgb=rgb, sent=False)
        self.dispatch()
        return None

    def dispatch(self):
        if not self.ready or not self.flight or self.flight['sent']:
            return
        task = self.flight
        self.seq += 1
        self.image[:len(task['rgb'])] = task['rgb']
        task.update(id=self.seq, started=self.clock(), sent=True)
        self.wire.send(dict(id=self.seq, rect=task['slab'], size=len(task['rgb']), mode=task['mode']))
        self.requests += 1

    def tick(self):
        self.retired = [c for c in self.retired if c.poll() is None]
        if self.wire is None:
            if self.clock() >= self.due:
                self.state = 'pending'
                self.start()
            return
        try:
            if self.child.poll() is not None:
                raise EOFError()
            rows = self.wire.pump()
            # A late reply cannot erase a budget breach.
            if (self.flight and self.flight['sent'] and self.clock()-self.flight['started'] >= 1 or
                    not self.ready and self.clock()-self.started >= 1):
                self.fail('timeout')
                return
            for row in rows:
                if row.get('event') == 'ready':
                    self.ready = True
                    if self.flight:
                        self.flight['sent'] = False
                    self.dispatch()
                    if self.state == 'pending':
                        self.state, self.reason = 'healthy', None
                elif self.flight and row.get('id') == self.flight.get('id'):
                    if row.get('error'):
                        self.fail('provider_crashed')
                        return
                    task = self.flight
                    self.child_requests = row.get('requests', self.child_requests+1)
                    lines = row['lines']
                    if not isinstance(lines, list):
                        raise ValueError('ocr_invalid_result')
                    for line in lines:
                        if (not isinstance(line['text'], str) or len(line['box']) != 4 or
                                any(type(v) is not int for v in line['box']) or
                                not 60 <= line['conf'] <= 100):
                            raise ValueError('ocr_invalid_result')
                        x, y, width, height = line['box']
                        slab = task['slab']
                        if (width <= 0 or height <= 0 or x < slab['x'] or y < slab['y'] or
                                x+width > slab['x']+slab['w'] or y+height > slab['y']+slab['h']):
                            raise ValueError('ocr_invalid_box')
                    if task['previous']:
                        lines = merge_lines(task['previous']['lines'], lines, task['slab'], task['damage'])
                    lines = [l for l in lines if any(c.isalnum() for c in l['text'])]
                    value = dict(lines=ordered(lines), rows=task['rows'], columns=task['columns'], digest=task['digest'], rev=task['rev'])
                    self.remember(self.cache, task['key'], value)
                    self.remember(self.latest, task['identity'], value)
                    self.flight = None
                    self.state, self.reason, self.due, self.attempt = 'healthy', None, 0, 0
        except (OSError, EOFError, ValueError, KeyError, TypeError):
            self.fail('provider_crashed')

    def region(self, p):
        window = p.get('window', p.get('within')) or self.w.cache.focus
        if window == 'modal':
            window = next(iter(self.w.cache.modal), None)
        params = dict(scope=dict(kind='window', handle=window))
        if p.get('region', p.get('crop')) is not None:
            raw = p.get('region', p.get('crop'))
            if isinstance(raw, dict):
                raw = dict(raw)
                for original, short in (('width', 'w'), ('height', 'h')):
                    if original in raw:
                        raw[short] = raw.pop(original)
                raw.pop('mode', None)
            params['region'] = raw
        rect, _ = self.w.pixels.region(params)
        return window, rect

    @staticmethod
    def mode(p):
        mode = p.get('mode', p.get('crop', {}).get('mode') if isinstance(p.get('crop'), dict) else None)
        if mode not in (None, 'line', 'block'):
            raise ActError('invalid_expectation')
        return mode

    def evaluate(self, p):
        if not getattr(self.w, 'pixels', None) or not any(k in p for k in ('text', 'value', 're')):
            return None
        try:
            window, rect = self.region(p)
            lines = self.read(window, rect, self.mode(p))
        except ActError:
            return None
        if lines is None:
            return None
        norm = lambda s: unicodedata.normalize('NFC', s.replace('\r\n', '\n'))
        text = p.get('text', p.get('value'))
        if p['kind'] == 'ocr_text':
            found = any(norm(l['text']) == norm(text) for l in lines) if isinstance(text, str) else False
        else:
            if not isinstance(text, str) and 're' not in p:
                return None
            found = any(re.search(p['re'], l['text']) if 're' in p else text in l['text'] for l in lines)
        return not found if p['kind'] == 'ocr_text_absent' else found

    def evidence(self, p):
        window, rect = self.region(p)
        mode = self.mode(p)
        lines = self.read(window, rect, mode)
        parameters = PARAMETERS + (mode,) if mode is not None else PARAMETERS
        return [dict(kind='ocr', provider='tesseract', rev=self.w.cache.rev,
                     sample_ns=int(self.clock()*1e9), scope=dict(kind='window', handle=window, region=rect),
                     hash=self.latest[(window, tuple(rect.values()), parameters)]['digest'],
                     ref=None, coverage='region', redactions=[],
                     comparison=dict(predicate=p, result=True, lines=lines))] if lines is not None else []


def child_main(read_fd, write_fd, image_fd):
    # No worker imports of PIL/libtesseract and no capture in the child.
    backend = Backend()
    image = mmap.mmap(image_fd, IMAGE_LIMIT, access=mmap.ACCESS_READ)
    def receive(n):
        data = bytearray()
        while len(data) < n:
            chunk = os.read(read_fd, n-len(data))
            if not chunk:
                raise EOFError()
            data.extend(chunk)
        return data
    import json
    def send(value):
        raw = memoryview(packet(value))
        while raw:
            raw = raw[os.write(write_fd, raw):]
    try:
        send(dict(event='ready'))
        requests = 0
        while True:
            size = struct.unpack('!I', receive(4))[0]
            if size > MAX_IPC:
                raise ValueError('ocr_ipc_budget')
            task = json.loads(receive(size))
            rect = task['rect']
            if (type(task['size']) is not int or not 0 < task['size'] <= IMAGE_LIMIT or
                    task['size'] != rect['w']*rect['h']*3):
                raise ValueError('ocr_image_invalid')
            lines = backend.read(image[:task['size']], rect, task.get('mode'))
            requests += 1
            send(dict(id=task['id'], lines=lines, requests=requests))
    except EOFError:
        pass
    finally:
        backend.close()
        image.close()
        os.close(read_fd)
        os.close(write_fd)


if __name__ == '__main__':
    child_main(int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]))
