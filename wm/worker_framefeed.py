"""Lease-free local presentation feed. No X connection or driver frame store.

The owner publishes settled snapshots; requests only encode that immutable
snapshot. PNG paths are local, ephemeral artifacts, just as on observe. They
live in a separate FIFO (32 issuances / 64 MiB), with no evidence pins.
"""
from collections import OrderedDict
import base64
import time
import copy
import hashlib
import json
import os
from pathlib import Path
import selectors
import socket
import struct

from worker_pixels import Shadow, intersect

FRAME_COUNT = 32
FRAME_BYTES = 64 * 1024 * 1024
SNAPSHOT_BYTES = 64 * 1024 * 1024
CLIENT_COUNT = 8
REQUEST_BYTES = 8192


class FeedError(Exception):
    pass


class FrameRing:
    """Independent FIFO of served transforms; PNGs deduplicate by content."""
    def __init__(self, directory, count=FRAME_COUNT, quota=FRAME_BYTES):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        self.count, self.quota, self.used = count, quota, 0
        self.entries, self.files = OrderedDict(), {}
        # Feed evidence is never durable and never restored across workers.
        for path in self.directory.iterdir():
            if path.suffix in ('.png', '.tmp') and path.is_file():
                path.unlink()

    def drop_oldest(self):
        key = next(iter(self.entries))
        value = self.entries[key]
        name = value['frame'][2:]
        size, refs = self.files[name]
        if refs == 1:
            (self.directory / (name + '.png')).unlink(missing_ok=True)
            self.used -= size
            del self.files[name]
        else:
            self.files[name] = size, refs - 1
        del self.entries[key]

    def put(self, key, png, metadata):
        if key in self.entries:
            return dict(self.entries[key])
        if len(png) > self.quota or self.count < 1:
            raise FeedError('frame_too_large')
        digest = hashlib.sha256(png).hexdigest()
        while self.entries and (len(self.entries) >= self.count or
                self.used + (0 if digest in self.files else len(png)) > self.quota):
            self.drop_oldest()
        path = self.directory / (digest + '.png')
        if digest not in self.files:
            temp = path.with_suffix('.tmp')
            try:
                fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(png)
                temp.replace(path)
            finally:
                temp.unlink(missing_ok=True)
            self.files[digest] = len(png), 0
            self.used += len(png)
        size, refs = self.files[digest]
        self.files[digest] = size, refs + 1
        value = dict(metadata, frame='f:' + digest, path=str(path))
        self.entries[key] = value
        return dict(value)


class FeedClient:
    def __init__(self, sock):
        self.sock = sock
        self.incoming, self.outgoing = bytearray(), bytearray()


class FrameFeed:
    def __init__(self, worker):
        self.w = worker
        self.path = worker.sd / 'driver/frames-feed.sock'
        self.ring = FrameRing(worker.sd / 'driver/feed-frames')
        self.listener = None
        self.bound = False
        self.clients = set()  # Never Worker.peers; no lease/retirement semantics.
        self.snapshot = None
        self.stamp = None
        self.rev = None
        self.windows = {}
        self.cursor = None
        self.modal = None
        self.cursor_image = None
        self.cursor_pending, self.cursor_due = False, 0

    def start(self):
        self.path.unlink(missing_ok=True)
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        previous = os.umask(0o177)
        try:
            self.listener.bind(str(self.path))
            self.bound = True
        finally:
            os.umask(previous)
        self.path.chmod(0o600)
        self.listener.listen(CLIENT_COUNT)
        self.listener.setblocking(False)
        self.w.selector.register(self.listener, selectors.EVENT_READ, self)

    def sample_cursor(self):
        # Independent presentation sensing, on the worker's existing session X
        # connection. No helper/lease and no host display is opened.
        x = getattr(self.w, 'x', None)
        if not x or not getattr(x, 'ev', None) or self.cursor_pending or time.monotonic() < self.cursor_due:
            return
        self.cursor_due = time.monotonic() + .1
        budget = getattr(x.ax, 'budget', None)
        if budget is not None and (budget.overloaded or budget.count >= budget.limit):
            return  # Optional presentation sensing must not overload driver sensing.
        self.cursor_pending = True
        from Xlib.ext import xfixes
        def got(data):
            self.cursor_pending = False
            width, height = data['width'], data['height']
            if not 0 < width*height <= 128*128:
                return
            words = data['cursor_image']
            if len(words) != width*height:
                return
            self.cursor_image = dict(width=width, height=height, format='argb32-le',
                pixels=base64.b64encode(struct.pack('<' + 'I'*len(words), *words)).decode())
            self.cursor = dict(position=dict(x=data['x'], y=data['y']), image=self.cursor_image,
                               hotspot=dict(x=data['xhot'], y=data['yhot']))
        def failed(_):
            self.cursor_pending = False
        if x.ax.ask(xfixes.GetCursorImage, got, optional=True, error_callback=failed,
                    opcode=x.ev.display.get_extension_major('XFIXES')) is False:
            self.cursor_pending = False

    def publish(self):
        self.sample_cursor()
        """Called only by the normal owner loop; never settles/captures itself."""
        pixels, cache = self.w.pixels, self.w.cache
        if (not pixels or pixels.failure or pixels.pending or pixels.dirty or
                any(h is None for h in pixels.hashes) or cache.changed or cache.upserts or
                not self.w.covered()):
            return
        stamp = (id(pixels), pixels.serial)
        if stamp == self.stamp and cache.rev == self.rev:
            return
        shadow = pixels.shadow
        if shadow.w * shadow.h * 4 > SNAPSHOT_BYTES:
            self.snapshot, self.rev, self.stamp = None, None, None
            self.windows = {}
            return
        if stamp != self.stamp:
            # Xvfb's mmap is live. Never retain it as a settled frame: copy
            # native rows now so damage/partial GetImage cannot tear a query.
            snapshot = Shadow.__new__(Shadow)
            snapshot.w, snapshot.h = shadow.w, shadow.h
            snapshot.offset, snapshot.stride = 0, shadow.w * 4
            snapshot.byte_order = shadow.byte_order
            if shadow.stride == snapshot.stride:
                snapshot.data = bytes(shadow.data[shadow.offset:
                                      shadow.offset + snapshot.stride*shadow.h])
            else:
                snapshot.data = b''.join(
                    shadow.data[shadow.offset + y*shadow.stride:
                                shadow.offset + y*shadow.stride + shadow.w*4]
                    for y in range(shadow.h))
            self.snapshot = snapshot
        self.stamp, self.rev = stamp, cache.rev
        self.windows = {v['handle']: copy.deepcopy(v) for v in cache.windows.values() if v['mapped']}
        if self.cursor_image is None:
            self.cursor = dict(position=copy.deepcopy(cache.pointer), image=None, hotspot=None)
        self.modal = copy.deepcopy(cache.modal)

    def query(self, params):
        if not isinstance(params, dict) or set(params) - {'scope', 'since_rev', 'generation', 'metadata'}:
            raise FeedError('invalid_params')
        if 'metadata' in params and type(params['metadata']) is not bool:
            raise FeedError('invalid_params')
        if params.get('generation', self.w.generation) != self.w.generation:
            raise FeedError('stale_generation')
        since = params.get('since_rev')
        if since is not None and (type(since) is not int or since < 1):
            raise FeedError('invalid_params')
        scope = params.get('scope', {'kind': 'session'})
        if not isinstance(scope, dict):
            raise FeedError('invalid_params')
        kind = scope.get('kind')
        if kind == 'session' and set(scope) == {'kind'}:
            handle = None
        elif (kind == 'window' and set(scope) == {'kind', 'window'} and
                isinstance(scope['window'], str)):
            handle = scope['window']
        else:
            raise FeedError('invalid_params')
        result = dict(session=self.w.sid, generation=self.w.generation, rev=self.rev)
        if self.snapshot is None or handle is not None and handle not in self.windows:
            return dict(result, status='no_frame_yet', changed=False)
        if since == self.rev:
            result.update(status='unchanged', changed=False)
            if params.get('metadata'):
                result.update(cursor=copy.deepcopy(self.cursor), modal_regions=self.modal_regions())
            return result
        bounds = dict(x=0, y=0, w=self.snapshot.w, h=self.snapshot.h)
        rect = intersect(self.windows[handle]['geometry'], bounds) if handle else bounds
        if not rect['w'] or not rect['h']:
            return dict(result, status='no_frame_yet', changed=False)
        key = (self.rev, handle)
        value = self.ring.entries.get(key)
        if value is None:
            value = self.ring.put(key, self.snapshot.png(rect, 1),
                                  dict(rev=self.rev, rect=rect, scale=1, space='desktop'))
        result.update(status='frame', changed=True, frame=dict(value), modal_regions=self.modal_regions())
        if handle is None:
            result['cursor'] = copy.deepcopy(self.cursor)
        return result

    def modal_regions(self):
        return [dict(win['geometry']) for win in self.windows.values()
                if win['handle'] in (self.modal or []) or win.get('is_modal') or win.get('type') == 'dialog']

    def accept(self):
        sock = None
        try:
            sock, _ = self.listener.accept()
            _, uid, _ = struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        except OSError:
            if sock is not None:
                sock.close()
            return
        if uid != os.getuid() or len(self.clients) >= CLIENT_COUNT:
            sock.close()
            return
        sock.setblocking(False)
        client = FeedClient(sock)
        self.clients.add(client)
        self.w.selector.register(sock, selectors.EVENT_READ, client)

    def disconnect(self, client):
        if client not in self.clients:
            return
        self.clients.discard(client)
        self.w.selector.unregister(client.sock)
        client.sock.close()
        client.incoming.clear()
        client.outgoing.clear()

    def message(self, payload):
        ident = None
        try:
            value = json.loads(payload)
            if (not isinstance(value, dict) or value.get('jsonrpc') != '2.0' or
                    type(value.get('id')) not in (int, str)):
                raise FeedError('invalid_request')
            ident = value['id']
            if value.get('method') != 'frames.get':
                raise FeedError('read_only')
            result = self.query(value.get('params', {}))
            return dict(jsonrpc='2.0', id=ident, result=result)
        except (ValueError, UnicodeError, RecursionError):
            reason = 'parse_error'
        except FeedError as exc:
            reason = str(exc)
        except OSError:
            reason = 'feed_unavailable'
        code = -32700 if reason == 'parse_error' else -32601 if reason == 'read_only' else -32602
        return dict(jsonrpc='2.0', id=ident, error=dict(code=code, message=reason, data=dict(reason=reason)))

    def io(self, client, mask):
        try:
            if mask & selectors.EVENT_WRITE:
                sent = client.sock.send(client.outgoing)
                del client.outgoing[:sent]
                if not client.outgoing:
                    self.w.selector.modify(client.sock, selectors.EVENT_READ, client)
            elif mask & selectors.EVENT_READ:
                data = client.sock.recv(REQUEST_BYTES + 4 - len(client.incoming))
                if not data:
                    self.disconnect(client)
                    return
                client.incoming.extend(data)
                if len(client.incoming) >= 4:
                    size = struct.unpack('!I', client.incoming[:4])[0]
                    if not 0 < size <= REQUEST_BYTES or len(client.incoming) > size + 4:
                        self.disconnect(client)  # One request in flight; no pipelining.
                        return
                    if len(client.incoming) == size + 4:
                        value = self.message(bytes(client.incoming[4:]))
                        client.incoming.clear()
                        payload = json.dumps(value, separators=(',', ':')).encode()
                        client.outgoing.extend(struct.pack('!I', len(payload)) + payload)
                        # Backpressure: no more reads/encoding until reply drains.
                        self.w.selector.modify(client.sock, selectors.EVENT_WRITE, client)
        except BlockingIOError:
            pass
        except OSError:
            self.disconnect(client)

    def close(self):
        for client in list(self.clients):
            self.disconnect(client)
        if self.listener is not None:
            if self.listener in self.w.selector.get_map():
                self.w.selector.unregister(self.listener)
            self.listener.close()
            self.listener = None
        if self.bound:
            self.path.unlink(missing_ok=True)
            self.bound = False
        self.snapshot = None
