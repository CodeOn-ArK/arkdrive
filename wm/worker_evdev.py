"""Session evdev input, with cooperative stream I/O and XI2 slave receipts.

No input nodes or uinput fds are opened by the worker. XTEST's session
keysym/character mapping and raw stream are shared, never duplicated.
"""
import ctypes as C
import errno
import json
import os
import stat
from pathlib import Path
import socket
import struct
import time

from input_protocol import MAX_MESSAGE, packet
from worker_act import Ack, ActError
from worker_keymap import EVDEV_NAMES


class ChannelError(OSError):
    def __init__(self, reason, submitted=False):
        self.reason, self.submitted = reason, submitted
        super().__init__(reason)


def compile_keycodes(display, keyboard):
    """Compile this session slave's XKB key names; never X keycode minus 8.

    The temporary XCB connection explicitly names the verified session display.
    All FFI queries happen at startup, before input admission.
    """
    xcb = C.CDLL('libxcb.so.1')
    xkb = C.CDLL('libxkbcommon.so.0')
    x11 = C.CDLL('libxkbcommon-x11.so.0')
    def function(lib, name, args, result):
        fn = getattr(lib, name)
        fn.argtypes, fn.restype = args, result
        return fn
    ptr, i, u = C.c_void_p, C.c_int, C.c_uint32
    connect = function(xcb, 'xcb_connect', [C.c_char_p, ptr], ptr)
    disconnect = function(xcb, 'xcb_disconnect', [ptr], None)
    has_error = function(xcb, 'xcb_connection_has_error', [ptr], i)
    setup = function(x11, 'xkb_x11_setup_xkb_extension', [ptr, C.c_uint16, C.c_uint16, i, ptr, ptr, ptr, ptr], i)
    context_new = function(xkb, 'xkb_context_new', [i], ptr)
    context_free = function(xkb, 'xkb_context_unref', [ptr], None)
    make = function(x11, 'xkb_x11_keymap_new_from_device', [ptr, ptr, i, i], ptr)
    free = function(xkb, 'xkb_keymap_unref', [ptr], None)
    minimum = function(xkb, 'xkb_keymap_min_keycode', [ptr], u)
    maximum = function(xkb, 'xkb_keymap_max_keycode', [ptr], u)
    name = function(xkb, 'xkb_keymap_key_get_name', [ptr, u], C.c_char_p)
    connection, context, keymap = connect(display.encode(), None), None, None
    try:
        if not connection or has_error(connection) or not setup(connection, 1, 0, 0, None, None, None, None):
            raise OSError('session_xkb_unavailable')
        context = context_new(0)
        if not context:
            raise OSError('session_xkb_context')
        keymap = make(context, connection, keyboard, 0)
        if not keymap:
            raise OSError('session_xkb_keymap')
        result = {}
        for code in range(minimum(keymap), maximum(keymap) + 1):
            physical = name(keymap, code)
            linux = EVDEV_NAMES.get(physical.decode() if physical else '')
            if linux is not None:
                result[code] = linux
        if not result:
            raise OSError('session_xkb_empty')
        return result
    finally:
        if keymap:
            free(keymap)
        if context:
            context_free(context)
        if connection:
            disconnect(connection)


class EvdevTransport:
    route = 'evdev'

    def __init__(self, worker, xtest, *, connector=None, clock=time.monotonic, keycodes=None):
        self.w, self.xtest, self.clock = worker, xtest, clock
        self.path = Path(worker.sd) / 'input.sock'
        self.sources, self.keycodes = {}, keycodes
        self.sock, self.seq, self.reconnects = None, 0, 0
        self.connector = connector or self.connect
        self.health = {'state': 'untested', 'grab': False, 'keyboard': 'untested', 'last_error': None}
        self.initialised = False
        self.inflight = False
        self.last_ack = None

    @property
    def raw(self):
        return self.xtest.raw

    @property
    def raw_seq(self):
        return self.xtest.raw_seq

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def down(self, reason):
        self.health.update(state='down', last_error=reason)
        self.close()

    def connect(self):
        info = self.path.stat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o777 != 0o600:
            raise OSError('helper_socket_permissions')
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.setblocking(False)
        try:
            result = sock.connect_ex(str(self.path))
            if result not in (0, errno.EINPROGRESS, errno.EAGAIN):
                raise OSError(result, 'helper_connect')
            return sock
        except BaseException:
            sock.close()
            raise

    def exchange(self, req):
        data, received, sent = packet(req), bytearray(), 0
        deadline = self.clock() + 0.25 + len(req.get('frames', [])) * req.get('gap_us', 0) / 1e6
        try:
            if self.sock is None:
                self.sock = self.connector()
            while True:
                if self.clock() >= deadline:
                    raise ChannelError('helper_timeout', sent == len(data))
                try:
                    if sent < len(data):
                        count = self.sock.send(data[sent:])
                        if not count:
                            raise ChannelError('helper_disconnected', sent == len(data))
                        sent += count
                    else:
                        chunk = self.sock.recv(4096)
                        if not chunk:
                            raise ChannelError('helper_disconnected', True)
                        received.extend(chunk)
                        if len(received) >= 4:
                            size = struct.unpack('!I', received[:4])[0]
                            if not 0 < size <= MAX_MESSAGE:
                                raise ChannelError('helper_invalid_reply', True)
                            if len(received) >= size + 4:
                                reply = json.loads(received[4:size + 4])
                                if not isinstance(reply, dict) or type(reply.get('seq')) is not int or reply['seq'] != req['seq']:
                                    raise ChannelError('helper_seq_mismatch', True)
                                return reply
                except (BlockingIOError, InterruptedError):
                    pass
                yield
        except (OSError, ValueError, TypeError) as exc:
            raise ChannelError(getattr(exc, 'reason', 'helper_socket_error'), sent == len(data)) from exc

    def request(self, **fields):
        if self.inflight:
            raise ChannelError('helper_request_in_progress')
        self.inflight = True
        try:
            return (yield from self._request(**fields))
        finally:
            self.inflight = False

    def _request(self, **fields):
        self.seq += 1
        req = dict(seq=self.seq, **fields)
        # A reconnect uses the identical sequence and payload. The helper
        # caches replies across connections, so lost acknowledgements cannot
        # cause a second write. Only one reconnect in this generation.
        submitted = False
        while True:
            try:
                return (yield from self.exchange(req))
            except ChannelError as exc:
                submitted |= exc.submitted
                self.close()
                if self.reconnects:
                    self.down(exc.reason)
                    raise ChannelError(exc.reason, submitted) from exc
                self.reconnects += 1

    def discover(self, devices):
        for dev in devices:
            def get(k):
                return getattr(dev, k) if hasattr(dev, k) else dev[k]
            name = get('name')
            if isinstance(name, bytes):
                name = name.decode('utf-8', 'replace')
            # Xorg uses the config Identifiers, not uinput product names.
            from Xlib.ext import xinput
            if get('enabled') and (name, get('use')) in (('kbd', xinput.SlaveKeyboard), ('ptr', xinput.SlavePointer)):
                self.sources['keyboard' if name == 'kbd' else 'pointer'] = get('deviceid')
        self.initialised = True

    def grab_check(self):
        try:
            reply = yield from self.request(op='grab_check')
            busy = reply.get('kbd') == reply.get('ptr') == 'busy' and reply.get('ok') is True
            self.health['grab'] = busy
            if not busy:
                self.down('no_exclusive_grab')
            return busy
        except ChannelError as exc:
            self.down(exc.reason)
            return False

    def startup(self):
        if self.health['state'] == 'down':
            return False
        if set(self.sources) != {'keyboard', 'pointer'}:
            self.down('assigned_slaves_unavailable')
            return False
        if not (yield from self.grab_check()):
            return False
        try:
            if self.keycodes is None:
                self.keycodes = compile_keycodes(':' + str(self.w.record['display']), self.sources['keyboard'])
        except (OSError, ValueError, AttributeError):
            self.down('session_keymap_unavailable')
            return False
        return True

    def events(self, events):
        # XTEST owns the sole raw-event stream and test surface. A changed
        # keymap retires this compiled evdev mapping rather than guessing.
        from Xlib import X
        from Xlib.ext import xinput
        for event in events:
            if event.type == X.MappingNotify:
                self.down('session_keymap_changed')
            if (getattr(event, 'evtype', None) == xinput.HierarchyChanged and
                    getattr(event, 'extension', None) == self.w.x.ev.display.get_extension_major('XInputExtension')):
                self.down('assigned_device_changed')

    @staticmethod
    def guard(intent):
        if any(key in ('Sys_Req', 'Print') for key in intent.get('keys', []) + intent.get('modifiers', [])):
            raise ActError('invalid_request')

    def plan(self, events, point):
        """(dev, frame, expected raw events), splitting motion into <=40 units."""
        result = []
        x, y = point['x'], point['y']
        for kind, detail, *xy in events:
            if kind.startswith('key'):
                code = self.keycodes.get(detail)
                if code is None or code == 99:
                    raise ActError('invalid_request', 'unmapped_evdev_key')
                result.append(('kbd', [[1, code, int(kind == 'key_down')]], [(kind, detail)]))
            elif kind == 'motion':
                tx, ty = xy
                while (x, y) != (tx, ty):
                    dx, dy = max(-40, min(40, tx-x)), max(-40, min(40, ty-y))
                    frame = ([[2, 0, dx]] if dx else []) + ([[2, 1, dy]] if dy else [])
                    result.append(('ptr', frame, [('motion', 0)]))
                    x, y = x+dx, y+dy
            elif detail in (1, 2, 3):
                code = {1: 0x110, 2: 0x112, 3: 0x111}[detail]
                result.append(('ptr', [[1, code, int(kind == 'button_down')]], [(kind, detail)]))
            elif detail in (4, 5, 6, 7):
                if kind == 'button_down':
                    code, value = {4: (8, 1), 5: (8, -1), 6: (6, -1), 7: (6, 1)}[detail]
                    result.append(('ptr', [[2, code, value]], [('button_down', detail), ('button_up', detail)]))
            else:
                raise ActError('invalid_request')
        return result

    def release_all(self):
        try:
            reply = yield from self.request(op='release_all')
            return reply.get('ok') is True
        except ChannelError:
            return False

    def send_frames(self, dev, frames, expected, gap_us):
        ack = self.last_ack = Ack(expected, self.sources, self.raw_seq)
        result = {'route': 'evdev', 'written': 0, 'frames_written': 0, 'expected': len(frames),
                  'written_prefix': {'frames': 0}, 'dispatch_status': 'not_dispatched',
                  'accepted': False, 'reason': None, 'error': None, 'server_ack': ack.receipt()}
        started = self.clock()
        result['started_ns'] = int(started * 1e9)
        try:
            if self.health['state'] == 'down':
                result['reason'] = 'channel_down'
                return result
            reply = yield from self.request(generation=self.w.generation, dev=dev, frames=frames, gap_us=gap_us)
            written = reply.get('frames_written')
            if (type(written) is not int or not 0 <= written <= len(frames) or
                    type(reply.get('ok')) is not bool or reply.get('error') not in (None, 'rejected', 'gate_closed', 'io_error', 'grab_lost') or
                    reply['ok'] != (written == len(frames) and reply['error'] is None)):
                raise ChannelError('helper_invalid_reply', True)
            result.update(written=written, frames_written=written, written_prefix={'frames': written}, error=reply['error'])
            if reply['error'] == 'grab_lost':
                result.update(dispatch_status='accepted' if written else 'not_dispatched',
                              accepted=bool(written), reason='channel_down')
                self.health['grab'] = False
                yield from self.release_all()
                self.down('grab_lost')
                return result
            if written < len(frames):
                if written:
                    result.update(dispatch_status='accepted', accepted=True, reason='channel_down')
                elif reply.get('may_have_written'):
                    result.update(dispatch_status='unknown', accepted=None, reason='channel_down')
                else:
                    result['reason'] = 'preempted' if reply['error'] == 'gate_closed' else 'channel_down'
                # Release holds even with a closed page. Never replay a prefix.
                if written or reply.get('may_have_written'):
                    yield from self.release_all()
                elif self.reconnects == 0 and reply['error'] != 'gate_closed':
                    self.close()
                    self.reconnects += 1
                    try:
                        yield from self.request(op='grab_check')
                    except ChannelError:
                        pass
                self.down(reply['error'] or 'helper_partial_write')
                return result
            deadline = started + 0.15 + max(0, len(frames)-1)*gap_us/1e6
            while True:
                ack.feed(self.raw)
                if ack.complete or self.clock() >= deadline:
                    break
                yield
            result.update(dispatch_status='accepted' if ack.complete else 'unknown',
                          accepted=True if ack.complete else None,
                          reason=None if ack.complete else 'input_unacknowledged')
        except ChannelError as exc:
            result.update(dispatch_status='unknown' if exc.submitted else 'not_dispatched',
                          accepted=None if exc.submitted else False,
                          reason='channel_down', error=exc.reason)
            self.down(exc.reason)
        finally:
            ack.feed(self.raw)
            result.update(ended_ns=int(self.clock()*1e9), server_ack=ack.receipt())
        return result
