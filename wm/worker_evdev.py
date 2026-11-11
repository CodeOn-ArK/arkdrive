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
    Queries run during settled establishment or a settled cache refresh.
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
        # Establishment is independent of the later delivery-test health. Xorg
        # may still be attaching our slaves while this transport is created.
        self.established = False
        self.revision = 0
        self.dirty = False
        self.recompiles = 0
        self.map_epoch = 0
        self.frame_epoch = 0
        self.recompile_runner = None
        self.settle_until = self.clock() + 0.05
        self.provided_keycodes = keycodes
        self.inflight = False
        self.refresh_inflight = False
        self.stopping = False
        self.last_ack = None
        self.instance = None
        self.sequence_instance = None
        # Wire codes are captured before submission, not recomputed from a
        # possibly refreshed XKB table during crash cleanup.
        self.held_codes = {'kbd': set(), 'ptr': set()}

    @property
    def raw(self):
        return self.xtest.raw

    @property
    def raw_seq(self):
        return self.xtest.raw_seq

    def close(self):
        sock, self.sock = self.sock, None
        if sock is not None:
            try:
                sock.close()
            except Exception as exc:
                # Cleanup is also a transport boundary. Detach first so a
                # failed close cannot leave a dead socket available to reuse.
                if self.health['state'] != 'down':
                    self.health.update(state='down', last_error=self.failure(exc).reason)

    def down(self, reason):
        # Retain the initiating fault through self-test/act cleanup failures.
        if self.health['state'] != 'down':
            self.health.update(state='down', last_error=reason)
        self.close()

    @staticmethod
    def failure(exc, submitted=False):
        if isinstance(exc, ChannelError):
            return ChannelError(exc.reason, submitted or exc.submitted)
        if isinstance(exc, OSError):
            disconnected = exc.errno in (errno.EBADF, errno.EPIPE, errno.ECONNRESET, errno.ENOTCONN)
            reason = 'helper_disconnected' if disconnected else 'helper_socket_error'
        else:
            reason = 'helper_transport_error:' + type(exc).__name__
        return ChannelError(reason, submitted)

    def connect(self):
        info = self.path.stat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o777 != 0o600:
            raise OSError('helper_socket_permissions')
        token_path = Path(str(self.path) + '.instance')
        token_info = token_path.stat()
        token = token_path.read_text().strip()
        if (token_info.st_uid != os.getuid() or token_info.st_mode & 0o777 != 0o600 or
                len(token) != 32 or any(c not in '0123456789abcdef' for c in token)):
            raise OSError('helper_instance_invalid')
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.setblocking(False)
            result = sock.connect_ex(str(self.path))
            if result not in (0, errno.EINPROGRESS, errno.EAGAIN):
                raise OSError(result, 'helper_connect')
            self.instance = token
            return sock
        except BaseException:
            try:
                sock.close()
            except Exception:
                pass
            raise

    def exchange(self, req):
        sock, sent, submitted = self.sock, 0, False
        try:
            deadline = self.clock() + 0.25 + len(req.get('frames', [])) * req.get('gap_us', 0) / 1e6
            if sock is None:
                sock = self.sock = self.connector()
            if self.instance is not None:
                previous = req.get('instance')
                if previous is not None and previous != self.instance:
                    # A new process has an EMPTY reply cache. Replaying ACT
                    # here would duplicate a prefix; only cleanup can cross it.
                    raise ChannelError('helper_restarted')
                req['instance'] = self.instance
                self.sequence_instance = self.instance
            data, received = packet(req), bytearray()
            while True:
                # X ingestion may retire this channel while I/O yields. Never
                # dereference self.sock again or switch to a replacement stream.
                if sock is None or self.sock is not sock:
                    raise ChannelError('helper_disconnected', submitted)
                if self.clock() >= deadline:
                    raise ChannelError('helper_timeout', submitted)
                try:
                    if sent < len(data):
                        if 'frames' in req:
                            # No final packet bytes from a dirty/stale plan.
                            # Cancel before submission and free the stream for
                            # the refresh's grab probe (never deadlock it).
                            if self.dirty or self.frame_epoch != self.map_epoch:
                                if sent:
                                    self.close()
                                raise ActError('provider_degraded')
                        count = sock.send(data[sent:])
                        if not count:
                            raise ChannelError('helper_disconnected', submitted)
                        sent += count
                        submitted = sent == len(data)
                    else:
                        chunk = sock.recv(4096)
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
                                if reply.get('error') == 'helper_restarted':
                                    raise ChannelError('helper_restarted', submitted)
                                return reply
                except (BlockingIOError, InterruptedError):
                    pass
                yield
        except ActError:
            raise
        except Exception as exc:
            raise self.failure(exc, submitted) from exc

    def request(self, *, _map_epoch=None, _refresh=False, **fields):
        # Bootstrap grab checks, per-batch probes and releases share the event
        # stream. A busy probe must not down()/close() another request's socket.
        while self.inflight:
            yield
        self.inflight = True
        self.refresh_inflight = _refresh
        self.frame_epoch = self.map_epoch if _map_epoch is None else _map_epoch
        try:
            if self.health['state'] == 'down' and fields.get('op') not in ('release_all', 'release_held'):
                raise ChannelError(self.health['last_error'] or 'channel_down')
            return (yield from self._request(**fields))
        except ActError:
            raise
        except Exception as exc:
            failure = self.failure(exc)
            self.down(failure.reason)
            raise failure from exc
        finally:
            self.inflight = False
            self.refresh_inflight = False

    def _request(self, **fields):
        self.seq += 1
        req = dict(seq=self.seq, **fields)
        if self.instance is not None:
            req['instance'] = self.instance
        # A reconnect uses the identical sequence and payload. The helper
        # caches replies across connections, so lost acknowledgements cannot
        # cause a second write. Only one reconnect in this generation.
        submitted, last_fault = False, None
        while True:
            try:
                return (yield from self.exchange(req))
            except ActError:
                # A cancelled retry cannot erase an earlier full submission.
                if submitted:
                    raise ChannelError(last_fault.reason, True) from last_fault
                raise
            except Exception as fault:
                exc = self.failure(fault)
                last_fault = exc
                submitted |= exc.submitted
                self.close()
                if self.reconnects or self.health['state'] == 'down':
                    self.down(exc.reason)
                    raise ChannelError(exc.reason, submitted) from exc
                self.reconnects += 1

    def discover(self, devices, *, commit=True):
        sources = {}
        for dev in devices:
            def get(k):
                return getattr(dev, k) if hasattr(dev, k) else dev[k]
            name = get('name')
            if isinstance(name, bytes):
                name = name.decode('utf-8', 'replace')
            # Xorg uses the config Identifiers, not uinput product names.
            from Xlib.ext import xinput
            if get('enabled') and (name, get('use')) in (('kbd', xinput.SlaveKeyboard), ('ptr', xinput.SlavePointer)):
                sources['keyboard' if name == 'kbd' else 'pointer'] = get('deviceid')
        if commit:
            self.sources = sources
            self.initialised = True
        return sources

    def devices(self, callback):
        from Xlib.ext import xinput
        self.w.x.ax.ask(xinput.XIQueryDevice, lambda d: callback(d['devices']),
                        error_callback=lambda _: callback(None),
                        opcode=self.w.x.ev.display.get_extension_major('XInputExtension'),
                        deviceid=xinput.AllDevices)

    def snapshot(self, *, commit=True):
        devices = []
        try:
            self.devices(lambda value: devices.append(value))
        except (OSError, ValueError, AttributeError, TypeError) as exc:
            raise ChannelError('assigned_slaves_unavailable') from exc
        deadline = self.clock() + 0.25
        while not devices:
            if self.clock() >= deadline:
                raise ChannelError('assigned_slaves_unavailable')
            yield
        if devices[0] is None:
            raise ChannelError('assigned_slaves_unavailable')
        return self.discover(devices[0], commit=commit)

    def barrier(self):
        done = []
        self.xtest.barrier(lambda _: done.append(True))
        deadline = self.clock() + 0.25
        while not done:
            if self.clock() >= deadline:
                raise ChannelError('session_settlement_timeout')
            yield
        # Even a synchronous test callback must allow event ingestion before
        # the revision is compared and the channel is established.
        yield

    def grab_check(self, *, retire=True, refresh=False):
        try:
            reply = yield from self.request(op='grab_check', _refresh=refresh)
            busy = reply.get('kbd') == reply.get('ptr') == 'busy' and reply.get('ok') is True
            self.health['grab'] = busy
            if not busy and retire:
                self.down('no_exclusive_grab')
            return busy
        except ChannelError as exc:
            self.down(exc.reason)
            return False

    def tick(self):
        if self.recompile_runner is not None:
            try:
                next(self.recompile_runner)
            except StopIteration:
                self.recompile_runner = None

    def stop_refresh(self):
        # Drain an owned grab reply before release_all; cancel a refresh that
        # is only waiting on another request. No new probes during shutdown.
        self.stopping = True
        if self.refresh_inflight:
            self.tick()
        if not self.refresh_inflight and self.recompile_runner is not None:
            self.recompile_runner.close()
            self.recompile_runner = None
        return self.refresh_inflight

    def startup(self):
        if self.established:
            while self.dirty and self.health['state'] != 'down':
                yield
            return self.health['state'] != 'down'
        return (yield from self.settle())

    def settle(self, *, recompile=False):
        deadline = self.clock() + 2
        while self.health['state'] != 'down':
            if self.clock() >= deadline:
                self.down('session_settlement_timeout')
                return False
            if self.clock() < self.settle_until:
                yield
                continue
            revision, failure = self.revision, None
            try:
                sources = yield from self.snapshot(commit=not recompile)
                if self.revision != revision:
                    continue
                if recompile and sources != self.sources:
                    failure = 'assigned_device_changed'
                elif set(sources) != {'keyboard', 'pointer'}:
                    failure = 'assigned_slaves_unavailable'
                else:
                    try:
                        keycodes = dict(self.provided_keycodes if self.provided_keycodes is not None else
                                        compile_keycodes(':' + str(self.w.record['display']), sources['keyboard']))
                    except (OSError, ValueError, AttributeError):
                        failure = 'session_keymap_unavailable'
                # A round trip on the event connection orders notifications
                # already generated during discovery/compilation before commit.
                yield from self.barrier()
            except ChannelError as exc:
                failure = exc.reason
            if self.revision != revision:
                continue
            if failure:
                self.down(failure)
                return False
            busy = yield from self.grab_check(retire=False, refresh=recompile)
            if self.stopping:
                return False
            try:
                yield from self.barrier()
            except ChannelError as exc:
                self.down(exc.reason)
                return False
            if self.revision != revision:
                continue
            if self.health['state'] == 'down':
                return False
            if not busy:
                self.down('no_exclusive_grab')
                return False
            if recompile:
                self.recompiles += 1
                if keycodes != self.keycodes:
                    self.map_epoch += 1
                    self.health['keyboard'] = 'untested'
            self.keycodes = keycodes
            self.dirty = False
            self.established = True
            return True
        return False

    def events(self, events):
        from Xlib import X
        from Xlib.ext import xinput
        for event in events:
            mapping = (event.type == X.MappingNotify and
                       getattr(event, 'request', X.MappingKeyboard) in (X.MappingKeyboard, X.MappingModifier))
            hierarchy = (getattr(event, 'evtype', None) == xinput.HierarchyChanged and
                         getattr(event, 'extension', None) == self.w.x.ev.display.get_extension_major('XInputExtension'))
            if hierarchy and self.established:
                # XI2's top-level flags aggregate ALL devices. Only per-device
                # evidence can retire our assigned pair; unrelated hotplug is
                # neither a map invalidation nor a reason to rediscover.
                data = getattr(event, 'data', event)
                info = data.get('info', []) if isinstance(data, dict) else getattr(data, 'info', [])
                changed = xinput.SlaveRemoved | xinput.SlaveDetached | xinput.DeviceDisabled
                for dev in info:
                    def get(key, default=None):
                        return dev.get(key, default) if isinstance(dev, dict) else getattr(dev, key, default)
                    if (get('deviceid') in self.sources.values() and
                            (get('flags', 0) & changed or (get('enabled') is not None and not get('enabled')))):
                        self.down('assigned_device_changed')
                continue
            if (mapping or hierarchy) and self.health['state'] != 'down' and not self.stopping:
                self.revision += 1
                self.settle_until = self.clock() + 0.05
                if self.established:
                    self.dirty = True
                    if self.recompile_runner is None:
                        self.recompile_runner = self.settle(recompile=True)

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
            if reply.get('ok') is True:
                return True
        except ChannelError:
            pass
        return False

    def recover_release(self):
        """Bounded wait for sessiond's replacement; never submits ACT frames.

        Keep evdev down for this generation. XTEST is available only after the
        engine independently observes that the recorded holds are released.
        """
        if self.instance is None:
            return False  # Unsupervised/legacy transport has no incarnation.
        deadline, retry_at = self.clock() + 2, self.clock()
        while self.clock() < deadline:
            if self.clock() < retry_at:
                yield
                continue
            self.close()
            try:
                # Establish the new incarnation before allocating its cleanup
                # sequence. The dead helper's requests are never carried over.
                self.sock = self.connector()
                if self.sequence_instance != self.instance:
                    self.seq = 0
                    self.sequence_instance = self.instance
                reply = yield from self.request(op='release_held', generation=self.w.generation,
                    holds={dev: sorted(codes) for dev, codes in self.held_codes.items()})
                if reply.get('ok') is True:
                    return True
            except Exception as exc:
                self.down(self.failure(exc).reason)
            retry_at = self.clock() + .02
            yield
        return False

    def send_frames(self, dev, frames, expected, gap_us, *, map_epoch=None):
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
            reply = yield from self.request(_map_epoch=map_epoch, generation=self.w.generation, dev=dev, frames=frames, gap_us=gap_us)
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
                preempted = reply['error'] == 'gate_closed'
                if written:
                    result.update(dispatch_status='accepted', accepted=True,
                                  reason='preempted' if preempted else 'channel_down')
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
                # A closed authority page is a designed stop, not a broken
                # helper/device. Keep the channel for the next attached epoch.
                if not preempted:
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
        except ActError as exc:
            result['reason'] = exc.reason
        except ChannelError as exc:
            result.update(dispatch_status='unknown' if exc.submitted else 'not_dispatched',
                          accepted=None if exc.submitted else False,
                          reason='channel_down', error=exc.reason)
            self.down(exc.reason)
        finally:
            ack.feed(self.raw)
            result.update(ended_ns=int(self.clock()*1e9), server_ack=ack.receipt())
        return result
