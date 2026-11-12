"""WM-C presenter. Selection/layout are host-local; only Takeover sends input."""
import argparse
import json
import os
from pathlib import Path
import secrets
import queue
import threading
import select
import signal
import socket
import struct
import time

from input_protocol import packet
from presenter_scene import Scene
from presenter_x11 import Renderer, host_display, key_name


class Stream:
    """Persistent direct worker RPC; uncertain calls are never retried."""
    def __init__(self, path, timeout=.8):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(str(path))
        self.seq = 0

    def call(self, method, params):
        self.seq += 1
        self.sock.sendall(packet(dict(jsonrpc='2.0', id=self.seq, method=method, params=params)))
        def read(size):
            result = bytearray()
            while len(result) < size:
                chunk = self.sock.recv(size-len(result))
                if not chunk:
                    raise ValueError('worker_disconnected')
                result.extend(chunk)
            return result
        length, = struct.unpack('!I', read(4))
        if not 0 < length <= 4*1024*1024:
            raise ValueError('invalid_reply')
        reply = json.loads(read(length))
        if reply.get('id') != self.seq:
            raise ValueError('reply_mismatch')
        if 'error' in reply:
            raise ValueError(reply['error']['data']['reason'])
        return reply['result']

    def close(self):
        self.sock.close()


def call(path, method, params, timeout=.2):
    stream = Stream(path, timeout)
    try:
        return stream.call(method, params)
    finally:
        stream.close()


class Presenter:
    def __init__(self, state, host):
        # Validate before either X connection is opened. Never inherit DISPLAY.
        self.host = host_display(host)
        self.state = Path(state)
        self.controller = self.state / 'wmctl.sock'
        self.scene = Scene()
        self.human = None
        self.status = 'Select a surface; Takeover explicitly grants human input.'
        self.kill_file = Path(os.environ.get('CU_KILL_FILE', '/tmp/cu-executor.kill'))
        self.running = True
        self.move = None
        self.registry_due = self.frame_due = self.heartbeat_due = 0
        self.reset_feeds = set()
        self.registry_queue = queue.Queue(maxsize=1)
        self.registry_thread = None

    def control(self, method, params):
        return call(self.controller, method, params, timeout=5)

    def begin(self):
        i = self.scene.selected
        if i not in self.scene.frames or i not in self.scene.transforms:
            return
        self.end(False)
        binding = self.scene.binding(i)
        grant = self.control('takeover.issue', dict(binding, explicit_action=True))
        stream = Stream(grant['socket'], timeout=4)
        try:
            result = stream.call('human.begin', dict(grant=grant['grant'], signature=grant['signature'], explicit_action=True))
            stream.sock.settimeout(.8)
        except BaseException:
            stream.close()
            raise
        self.human = dict(stream=stream, incarnation=i, token=result['token'], seq=0, binding=binding)
        self.heartbeat_due = 0
        self.status = 'HUMAN: ' + self.scene.records[i]['session'] + ' | Resume requires a new driver attach + full observe'

    def params(self):
        return dict(self.human['binding'], token=self.human['token'])

    def end(self, resume):
        human, self.human = self.human, None
        self.scene.drag = None
        self.scene.drag_buttons.clear()
        if human:
            try:
                human['stream'].sock.settimeout(4)
                result = human['stream'].call('human.end', dict(human['binding'], token=human['token'], resume=resume))
                self.status = 'Control: ' + result['state'] + '; driver must attach and observe.'
            finally:
                human['stream'].close()

    def abort(self):
        human, self.human = self.human, None
        if human:
            human['stream'].close()  # disconnect => worker closes gate/releases
        self.scene.drag = None
        self.scene.drag_buttons.clear()

    def changed(self):
        if self.human and self.human['incarnation'] != self.scene.root and self.scene.modal(self.scene.root):
            self.end(False)
        if self.human and self.human['binding'] != self.scene.binding(self.human['incarnation']):
            # Notify worker of new layout before any newly mapped event. Holds
            # release on change; old events can never acquire the new target.
            if self.human['binding']['generation'] == self.scene.frames[self.human['incarnation']]['generation']:
                self.human['stream'].call('human.layout', dict(self.params(), layout_version=self.scene.version))
            self.end(False)

    def event(self, kind, **fields):
        if not self.human:
            return
        self.changed()
        if not self.human:
            return
        self.human['seq'] += 1
        self.human['stream'].call('human.event', dict(self.params(), seq=self.human['seq'], kind=kind, **fields))
        self.heartbeat_due = time.monotonic()+.2

    def poll(self):
        now = time.monotonic()
        if self.kill_file.exists():
            self.abort()
            self.running = False
            return
        # Read-only registry polling runs off the input thread. Controller
        # stalls cannot hold up a human event or a direct worker heartbeat.
        if now >= self.registry_due and (self.registry_thread is None or not self.registry_thread.is_alive()):
            self.registry_due = now+1
            def refresh():
                try:
                    result = self.control('registry', {})['sessions']
                except (OSError, ValueError):
                    result = None
                self.registry_queue.put(result)
            self.registry_thread = threading.Thread(target=refresh, daemon=True)
            self.registry_thread.start()
        try:
            records = self.registry_queue.get_nowait()
            if records is not None:
                active = self.human and self.human['incarnation']
                if active and active not in {r['incarnation'] for r in records}:
                    self.abort()
                self.scene.registry(records)
                self.changed()
        except queue.Empty:
            pass
        if now >= self.frame_due:
            self.frame_due = now+.1
            for i in list(self.scene.transforms):
                record = self.scene.records[i]
                old = self.scene.frames.get(i)
                params = dict(generation=old['generation'], since_rev=old['rev']) if old and i not in self.reset_feeds else {}
                params['metadata'] = True
                try:
                    result = call(Path(record['session_dir']) / 'driver/frames-feed.sock', 'frames.get', params, timeout=.1)
                    if result['session'] != record['session']:
                        raise ValueError('feed_session_mismatch')
                    self.reset_feeds.discard(i)
                    if result['status'] == 'frame':
                        self.scene.frame(i, result)
                        self.renderer.upload(i, result)
                    elif result['status'] == 'unchanged' and old:
                        old.update(cursor=result.get('cursor'), modal_regions=result.get('modal_regions', []))
                    self.renderer.cursor(i, result.get('cursor'))
                    self.changed()
                except (OSError, ValueError, KeyError):
                    if self.human and i == self.human['incarnation']:
                        self.abort()
                    # Retry without an old generation; never send old input to it.
                    self.reset_feeds.add(i)
        if self.human and now >= self.heartbeat_due:
            self.human['stream'].call('human.heartbeat', self.params())
            self.heartbeat_due = now+.2

    def pointer(self, event, phase):
        x, y = event.event_x, event.event_y
        scene = self.scene
        if phase == 'down' and y < 28:
            control = next((n for n, a, b in [('takeover', 0, 104), ('resume', 104, 200),
                    ('collapse', 200, 304), ('reroot', 304, 432), ('parent', 432, 540)] if a <= x < b), None)
            if control == 'takeover':
                self.begin()
            elif control == 'resume':
                self.end(True)
            elif control == 'collapse':
                self.end(False)
                scene.collapse(scene.selected)
            elif control == 'reroot':
                self.end(False)
                scene.reroot(scene.selected)
            elif control == 'parent':
                self.end(False)
                scene.reroot(scene.records.get(scene.root, {}).get('parent_incarnation'))
            return
        if phase == 'down' and y >= scene.chip_top:
            if event.detail in (4, 5):
                scene.scroll_chips(-1 if event.detail == 4 else 1)
                return
            for cx, cy, i in scene.chip_views():
                if cx <= x < cx+200 and cy <= y < cy+24:
                    self.end(False)
                    scene.selected = i
                    if i in scene.collapsed:
                        scene.collapse(i)
                    else:
                        scene.reroot(i)
                    break
            return
        if not self.human:
            if phase == 'down':
                for i, t in reversed(list(scene.transforms.items())):
                    if i != scene.root and not scene.modal(scene.root) and t.x <= x < t.x+t.w and t.y-24 <= y < t.y:
                        scene.selected = i
                        self.move = i, x, y, t, event.detail == 3
                        return
                scene.select(x, y)  # NO worker RPC; presentation focus only
            elif phase == 'move' and self.move:
                i, sx, sy, t, resize = self.move
                scene.place(i, t.x if resize else t.x+x-sx, t.y if resize else t.y+y-sy,
                            t.w+x-sx if resize else t.w, t.h+y-sy if resize else t.h)
            elif phase == 'up':
                self.move = None
            return
        i = self.human['incarnation']
        hit = scene.hit(x, y)
        if not scene.drag and hit != i:
            if phase == 'down':
                self.end(False)
                scene.select(x, y)
            return
        drag_id = scene.drag[3] if scene.drag else secrets.token_hex(12)
        # Scrolling is a pulse, not a held drag.
        pinned = scene.pointer(x, y, phase if event.detail <= 3 or phase == 'move' else 'move', drag_id, button=event.detail)
        if pinned is None or pinned['incarnation'] != i:
            return
        self.event('motion', x=pinned['x'], y=pinned['y'], drag_id=pinned['drag_id'])
        if phase in ('down', 'up'):
            self.event('button_' + phase, detail=event.detail, drag_id=pinned['drag_id'])

    def run(self):
        from Xlib import X, XK, display
        self.d = display.Display(self.host)
        root = self.d.screen().root
        self.window = root.create_window(20, 20, self.scene.width, self.scene.height, 0,
            X.CopyFromParent, X.InputOutput, X.CopyFromParent, background_pixel=0x171c25,
            event_mask=X.ExposureMask | X.StructureNotifyMask | X.ButtonPressMask | X.ButtonReleaseMask |
                       X.PointerMotionMask | X.KeyPressMask | X.KeyReleaseMask | X.FocusChangeMask)
        self.window.set_wm_name('ArkDrive — session presenter')
        delete = self.d.intern_atom('WM_DELETE_WINDOW')
        self.window.set_wm_protocols([delete])
        self.window.map()
        self.renderer = Renderer(self.d, self.window, self.host)
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, 'running', False))
        signal.signal(signal.SIGINT, lambda *_: setattr(self, 'running', False))
        try:
            while self.running:
                try:
                    self.poll()
                    while self.d.pending_events():
                        event = self.d.next_event()
                        if event.type == X.ClientMessage and event.data[1][0] == delete:
                            self.running = False
                        elif event.type == X.ConfigureNotify:
                            self.scene.resize(event.width, event.height)
                            self.changed()
                        elif event.type == X.FocusOut:
                            self.abort()
                        elif event.type in (X.ButtonPress, X.ButtonRelease, X.MotionNotify):
                            if event.type == X.MotionNotify:
                                event.detail = 0
                            self.pointer(event, {X.ButtonPress: 'down', X.ButtonRelease: 'up', X.MotionNotify: 'move'}[event.type])
                        elif event.type in (X.KeyPress, X.KeyRelease) and self.human:
                            sym = self.d.keycode_to_keysym(event.detail, 0)
                            name = key_name(sym)
                            if name:
                                self.event('key_down' if event.type == X.KeyPress else 'key_up', detail=name,
                                           drag_id=self.scene.drag[3] if self.scene.drag else None)
                    self.renderer.draw(self.scene, self.status)
                except (OSError, ValueError, KeyError) as exc:
                    self.abort()
                    # Named errors only; never include event/key payloads.
                    self.status = 'Input stopped; select Takeover again when ready.'
                select.select([self.d.fileno()], [], [], .01)
        finally:
            self.abort()
            self.renderer.close()
            self.window.destroy()
            self.d.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, default=Path(os.path.expanduser('~/.local/state/ark-sessiond')))
    parser.add_argument('--host-display', required=True, help='explicit local human/test host X display; :0 is refused')
    args = parser.parse_args()
    try:
        Presenter(args.state, args.host_display).run()
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps(dict(ok=False, error=str(exc))))
        return 1
