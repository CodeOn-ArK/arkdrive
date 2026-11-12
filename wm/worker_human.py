"""WM-C owner-authorized direct input. Human key data never enters Engine/journal.

The controller signs a one-use grant; worker + helper enforce its generation,
layout and lifetime. The worker's sole selector thread owns all transitions.
"""
import fcntl
import hashlib
import hmac
import json
import mmap
import os
from pathlib import Path
import secrets
import time

from input_protocol import PAGE, control_store
from worker_act import ActError


def identity(pid):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return None if fields[0] == 'Z' else fields[19]
    except (OSError, IndexError):
        return None


def signature(key, grant):
    return hmac.new(key, b'human:' + json.dumps(grant, sort_keys=True,
                    separators=(',', ':'), allow_nan=False).encode(), hashlib.sha256).hexdigest()


class HumanError(Exception):
    pass


class Takeover:
    heartbeat_seconds = 1.0

    def __init__(self, worker, clock=time.monotonic):
        self.w, self.clock = worker, clock
        self.state = 'idle'
        self.peer = self.grant = self.token = None
        self.runner = None
        self.reply = None
        self.used = set()
        self.keys, self.buttons = set(), set()
        self.wire = {'kbd': set(), 'ptr': set()}
        self.drag = None
        self.seq = 0
        self.deadline = 0
        self.resume_observe = False
        self.kill_file = Path(os.environ.get('CU_KILL_FILE', '/tmp/cu-executor.kill'))

    def journal(self, kind, **fields):
        # Callers supply ONLY transition metadata or aggregate counts/kinds.
        try:
            self.w.journal.append('human.' + kind, **fields)
        except (OSError, ValueError):
            raise HumanError('journal_unavailable')

    def page(self, state, *, bump=False, token=None, expected=None, expected_epoch=None):
        with (self.w.sd / 'control').open('r+b') as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                # The legacy control CLI holds its lock while waiting for a
                # worker health reply. Never block this selector on that lock.
                # Closing can only remove authority; opening must retry.
                if state in (0, 2):
                    raise HumanError('control_busy')
            with mmap.mmap(stream.fileno(), PAGE.size) as page:
                if state in (1, 2) and (self.w.sd / 'promotion-gate.json').exists():
                    raise HumanError('promotion_busy')
                magic, version, gen, epoch, previous, flags, permit, lease = PAGE.unpack(page[:])
                if (magic != b'ARKC' or version != 1 or gen.hex() != self.w.generation or
                        not epoch or permit or expected is not None and previous != expected or
                        expected_epoch is not None and epoch != expected_epoch):
                    raise HumanError('control_changed')
                # Always close first, even when returning automation authority.
                control_store(page, 32, 1, 32)
                if bump:
                    epoch += 1
                control_store(page, 24, epoch, 64)
                control_store(page, 36, flags & ~1, 32)  # never carry clear-fence authority
                page[48:64] = hashlib.sha256(token.encode()).digest()[:16] if token else bytes(16)
                control_store(page, 32, state, 32)
                page.flush()
            os.fsync(stream.fileno())
        self.w.poll_control(force=True)
        return epoch

    def authorize(self, params):
        grant, sig = params.get('grant'), params.get('signature')
        if not isinstance(grant, dict) or not isinstance(sig, str):
            raise HumanError('invalid_capability')
        key = bytes.fromhex(json.loads((self.w.state / 'wmctl-key.json').read_text()))
        if not hmac.compare_digest(signature(key, grant), sig):
            raise HumanError('invalid_capability')
        required = {'incarnation', 'generation', 'layout_version', 'nonce', 'expires', 'controller_pid', 'controller_start'}
        if (set(grant) != required or grant['incarnation'] != self.w.record.get('incarnation') or
                grant['generation'] != self.w.generation or type(grant['layout_version']) is not int or
                grant['layout_version'] < 1 or grant['expires'] <= time.time() or
                identity(grant['controller_pid']) != grant['controller_start'] or not grant['controller_start']):
            raise HumanError('stale_capability')
        if grant['nonce'] in self.used or len(self.used) >= 4096:
            raise HumanError('spent_capability')
        return grant

    def check(self, peer, params, *, binding=True):
        if peer is not self.peer or not self.token or not isinstance(params.get('token'), str) or not secrets.compare_digest(params['token'], self.token):
            raise HumanError('invalid_capability')
        if params.get('incarnation') != self.w.record.get('incarnation'):
            raise HumanError('stale_incarnation')
        self.alive()
        if self.state != 'human':
            raise HumanError('takeover_not_ready')
        if binding:
            if params.get('generation') != self.w.generation:
                raise HumanError('stale_generation')
            if params.get('layout_version') != self.layout:
                raise HumanError('stale_layout')
        self.deadline = self.clock() + self.heartbeat_seconds

    def alive(self):
        if self.kill_file.exists():
            raise HumanError('global_kill')
        if self.grant and (time.time() >= self.grant['expires'] or
                identity(self.grant['controller_pid']) != self.grant['controller_start']):
            raise HumanError('controller_lost')
        if self.state == 'human' and self.clock() >= self.deadline:
            raise HumanError('presenter_timeout')
        if self.state == 'human':
            self.w.poll_control(force=True)
            c = self.w.cache.control or {}
            if c.get('state') != 'human' or c.get('epoch') != self.epoch:
                raise HumanError('control_changed')

    def handle(self, peer, ident, method, params):
        if method == 'human.begin':
            if (self.w.sd / 'promotion-gate.json').exists():
                raise HumanError('promotion_busy')
            if self.state not in ('idle', 'paused') or self.runner:
                raise HumanError('takeover_busy')
            if params.get('explicit_action') is not True:
                raise HumanError('explicit_action_required')
            if self.kill_file.exists():
                raise HumanError('global_kill')
            grant = self.authorize(params)
            if not self.w.act or not self.w.act.t or not self.w.frame_feed or self.w.frame_feed.snapshot is None:
                raise HumanError('takeover_unavailable')
            self.epoch = self.page(1, bump=True)  # helper gate -> epoch -> lease revoke
            self.w.release_lease()
            self.used.add(grant['nonce'])
            self.grant, self.peer, self.layout = grant, peer, grant['layout_version']
            self.token, self.seq, self.drag = secrets.token_hex(32), 0, None
            self.state = 'closing'
            try:
                self.journal('closing', epoch=self.epoch, layout_version=self.layout)
            except HumanError:
                self.finish('journal_unavailable')
                raise
            self.reply = peer, ident
            self.runner = self.begin()
            return
        self.check(peer, params, binding=method not in ('human.end', 'human.layout'))
        if self.runner:
            raise HumanError('input_busy')
        if method == 'human.heartbeat':
            self.w.respond(peer, ident, dict(state=self.state))
        elif method == 'human.layout':
            version = params.get('layout_version')
            if type(version) is not int or version <= self.layout:
                raise HumanError('stale_layout')
            self.layout = version
            if self.buttons or self.keys:
                self.finish('layout_changed')
            self.w.respond(peer, ident, dict(state=self.state, layout_version=version))
        elif method == 'human.end':
            self.reply = peer, ident
            self.finish('resume' if params.get('resume') is True else 'human_end')
        elif method == 'human.event':
            if type(params.get('seq')) is not int or params['seq'] != self.seq + 1:
                raise HumanError('stale_sequence')
            event = self.event(params)
            self.seq = params['seq']
            self.reply = peer, ident
            self.runner = self.send(event)
        else:
            raise HumanError('unknown_method')

    def event(self, p):
        kind, detail = p.get('kind'), p.get('detail')
        if kind == 'motion':
            x, y = p.get('x'), p.get('y')
            if (type(x) is not int or type(y) is not int or
                    not 0 <= x < self.w.screen['w'] or not 0 <= y < self.w.screen['h']):
                raise HumanError('invalid_event')
            event = ('motion', 0, x, y)
        elif kind in ('key_down', 'key_up'):
            if not isinstance(detail, str) or len(detail) > 128:
                raise HumanError('invalid_event')
            code = self.w.act.t.keycode(detail)
            if code is None or detail in ('Sys_Req', 'Print'):
                raise HumanError('unmapped_key')
            event = (kind, code)
        elif kind in ('button_down', 'button_up') and type(detail) is int and 1 <= detail <= 7:
            event = (kind, detail)
        else:
            raise HumanError('invalid_event')
        # A drag's identity cannot change on motion/release or another press.
        if self.drag is not None and p.get('drag_id') != self.drag:
            raise HumanError('drag_target_changed')
        if kind == 'button_down' and detail <= 3 and self.drag is None:
            if not isinstance(p.get('drag_id'), str) or not 1 <= len(p['drag_id']) <= 128:
                raise HumanError('invalid_drag')
            self.drag = p['drag_id']
        return event

    def query(self, name):
        box = []
        getattr(self.w.act.t, name)(box.append)
        deadline = self.clock() + .3
        while not box:
            if self.clock() >= deadline:
                raise HumanError('release_unconfirmed')
            yield
        return box[0]

    def release_held(self):
        act = self.w.act
        evdev = act.evdev
        if evdev:
            # Explicit wire holds survive a helper restart; release_all alone
            # cannot prove the old process's pressed keys are up.
            holds = {dev: sorted(codes) for dev, codes in self.wire.items()}
            ok = False
            try:
                if any(holds.values()):
                    reply = yield from evdev.request(op='release_held', generation=self.w.generation, holds=holds)
                    ok = reply.get('ok') is True
                else:
                    ok = yield from evdev.release_all()
            except OSError:
                pass
            if not ok:
                # Reuse the existing supervised replacement release-only
                # transport. These codes remain in memory, never in a journal.
                for dev, codes in self.wire.items():
                    evdev.held_codes[dev].update(codes)
                ok = yield from evdev.recover_release()
            if not ok:
                raise HumanError('release_unconfirmed')
        else:
            for code in sorted(self.keys):
                act.t.write(('key_up', code))
            for code in sorted(self.buttons):
                act.t.write(('button_up', code))
            act.t.flush()
        # Query on the injection connection first, then query the sensing
        # connection: cross-connection GetInputFocus alone is not a barrier.
        if getattr(self.w, 'x', None):
            box = []
            from Xlib.protocol import request
            self.w.x.inp_ax.ask(request.GetInputFocus, box.append)
            until = self.clock() + .3
            while not box:
                if self.clock() >= until:
                    raise HumanError('release_unconfirmed')
                yield
        keys = yield from self.query('keymap')
        pointer = yield from self.query('pointer')
        if self.keys & set(keys) or pointer['mask'] & sum(1 << (7+b) for b in self.buttons):
            raise HumanError('release_unconfirmed')
        self.keys.clear()
        self.buttons.clear()
        for codes in self.wire.values():
            codes.clear()
        if evdev:
            for codes in evdev.held_codes.values():
                codes.clear()
        self.drag = None

    def begin(self):
        act = self.w.act
        until = self.clock() + 3
        while act.active or act.queue or act.selftest or act.evdev and act.evdev.inflight:
            self.alive()
            if self.clock() >= until:
                raise HumanError('release_unconfirmed')
            yield
        # Existing engine cleanup owns driver holds, receipts and uncertainty.
        a = {'params': {'action_id': 'takeover_release'}, 'acks': []}
        r = {'id': 'takeover_release', 'attempts': [], 'rev': {'dispatched': None}, 'dispatch_status': 'not_dispatched'}
        if not (yield from act.release(a, r)) or any(v for h in act.holds.values() for v in h.values()):
            raise HumanError('release_unconfirmed')
        yield from self.release_held()
        self.alive()
        if self.state != 'closing':
            raise HumanError('takeover_cancelled')
        self.epoch = self.page(2, token=self.token, expected=1, expected_epoch=self.epoch)
        self.state = 'human'
        self.deadline = self.clock() + self.heartbeat_seconds
        self.journal('ready', epoch=self.epoch, released=True)
        return dict(state='human', token=self.token, generation=self.w.generation,
                    layout_version=self.layout, control_epoch=self.epoch)

    def send(self, event):
        if self.state != 'human':
            raise HumanError('takeover_cancelled')
        self.alive()
        kind, detail, *xy = event
        held = self.keys if kind.startswith('key') else self.buttons
        # Track before submission; uncertain writes are released, never replayed.
        if kind.endswith('down') and (kind.startswith('key') or detail <= 3):
            held.add(detail)
        evdev = self.w.act.evdev
        if evdev:
            point = yield from self.query('pointer')
            for dev, frame, _ in evdev.plan([event], point):
                if self.state != 'human':
                    raise HumanError('takeover_cancelled')
                self.alive()
                for typ, code, value in frame:
                    if typ == 1 and value:
                        self.wire[dev].add(code)
                reply = yield from evdev.request(op='human', human_token=self.token,
                    human_epoch=self.epoch, generation=self.w.generation, dev=dev, frames=[frame], gap_us=0)
                if not reply.get('ok'):
                    raise HumanError('human_delivery_unknown')
                for typ, code, value in frame:
                    if typ == 1 and not value:
                        self.wire[dev].discard(code)
        else:
            self.w.act.t.write(event)
            self.w.act.t.flush()
        if kind.endswith('up'):
            held.discard(detail)
            if kind == 'button_up' and not self.buttons:
                self.drag = None
        self.journal('events', kinds={kind: 1}, count=1)
        return dict(state='human', seq=self.seq, dispatched=True)

    def finish(self, reason):
        if self.state in ('idle', 'paused', 'releasing', 'resuming'):
            return
        self.page(1, bump=True)  # stop helper writes before cancelling a runner
        if self.reply and reason != 'resume' and reason != 'human_end':
            self.w.respond(*self.reply, exc=self.w.human_error('human_delivery_unknown'))
            self.reply = None
        self.state = 'releasing'
        # Drain an outstanding helper exchange; never close/replay its prefix.
        old, self.runner = self.runner, None
        self.runner = self.end(reason, old)

    def end(self, reason, old):
        if old:
            try:
                while True:
                    next(old)
                    yield
            except (StopIteration, HumanError, ActError, OSError):
                pass
        yield from self.release_held()
        self.token = None
        resume_epoch = self.page(3)
        self.state = 'paused'
        self.journal('ended', reason=reason, released=True)
        if reason == 'resume':
            self.state = 'resuming'
            pixels, feed = self.w.pixels, self.w.frame_feed
            if not pixels or not feed:
                raise HumanError('fresh_frame_required')
            pixels.mark([dict(x=0, y=0, w=pixels.shadow.w, h=pixels.shadow.h)])
            serial = pixels.serial
            until = self.clock() + 3
            while not (self.w.settle_pixels() and feed.snapshot is not None and
                       pixels.serial >= serial and feed.stamp == (id(pixels), pixels.serial) and self.w.covered()):
                if self.kill_file.exists() or self.clock() >= until:
                    raise HumanError('fresh_frame_required')
                yield
            self.alive()
            self.resume_observe = True
            self.page(0, bump=True, expected=3, expected_epoch=resume_epoch)
            self.state = 'idle'
            self.journal('resume', fresh_frame=True, full_observe_required=True)
        return dict(state=self.state, fresh_attach_required=True, full_observe_required=True)

    def disconnected(self, peer):
        if peer is self.peer:
            self.finish('presenter_lost')

    def tick(self):
        if self.state in ('closing', 'human'):
            try:
                self.alive()
            except HumanError as exc:
                self.finish(str(exc))
        if self.runner is None:
            return
        try:
            next(self.runner)
        except StopIteration as done:
            self.runner = None
            if self.reply:
                self.w.respond(*self.reply, done.value or {})
                self.reply = None
        except (HumanError, ActError, OSError, ValueError) as exc:
            self.runner = None
            if self.reply:
                self.w.respond(*self.reply, exc=self.w.human_error(str(exc)))
                self.reply = None
            if self.state in ('releasing', 'resuming'):
                release_failed = self.state == 'releasing'
                self.state = 'paused'
                self.token = None
                self.page(3)
                if release_failed:
                    self.w.act.fence('F1:release_unconfirmed')
            else:
                self.finish('input_failed')
