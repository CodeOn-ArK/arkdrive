"""WM-C headless: real journals/control pages/engine, fake session X/devices.

No display is opened. The helper oracle records every write independently.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import struct
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from test_worker_w2 import Harness, w, GEN
from test_worker_w3 import decode
import test_worker_wm_b_controller as controller_fixture
from input_protocol import PAGE, control_snapshot
from mkdevs import EventProtocol
from presenter_scene import Scene, Transform
from presenter_x11 import Renderer, host_display, decode_png
from worker_human import Takeover, HumanError, identity, signature
from worker_pixels import Pixels, Shadow
from worker_framefeed import FrameFeed


class HumanCase(Harness):
    def setUp(self):
        super().setUp()
        self.w.record['incarnation'] = 'I'
        self.w.record['instance'] = 'a'*32
        (self.path / 'driver').mkdir()
        self.signing_key = bytes.fromhex('ac'*32)
        (self.path / 'wmctl-key.json').write_text(json.dumps(self.signing_key.hex()))
        (self.path / 'control').write_bytes(w.encode_control(GEN))
        self.w.control_file = (self.path / 'control').open('rb')
        self.addCleanup(self.w.control_file.close)
        stat = (self.path / 'control').stat()
        self.w.control_identity = stat.st_dev, stat.st_ino
        self.w.control_raw = None
        self.w.control_due = 0
        self.w.poll_control = lambda force=False: w.Worker.poll_control(self.w, force=True)
        self.w.pixels = Pixels(self.w, Shadow(640, 480))
        self.addCleanup(self.w.pixels.shadow.close)
        self.w.frame_feed = FrameFeed(self.w)
        self.w.pixels.mark([self.w.pixels.bounds])
        # Fake GetImage settlement uses a static XWD-equivalent byte buffer.
        self.w.pixels.shadow.mode = 'mmap'
        self.w.settle_pixels()
        self.w.frame_feed.publish()
        self.w.x = None
        self.errors = []
        def respond(peer, ident, result=None, exc=None):
            if exc:
                self.errors.append((ident, exc.reason))
            else:
                self.replies.append((ident, copy.deepcopy(result)))
        self.w.respond = respond
        self.w.human = self.h = Takeover(self.w, clock=lambda: self.now)
        self.h.kill_file = self.path / 'kill'
        self.human_peer = NS(waits=[])
        self.released = []
        # Independent native-device oracle. No human payload goes to Engine.
        def write(event):
            self.released.append(event)
            kind, detail, *xy = event
            if kind == 'key_down': self.t.down.add(detail)
            elif kind == 'key_up' and self.t.release_ack: self.t.down.discard(detail)
            elif kind == 'button_down': self.t.buttons.add(detail)
            elif kind == 'button_up' and self.t.release_ack: self.t.buttons.discard(detail)
            elif kind == 'motion': self.t.position = dict(x=xy[0], y=xy[1])
            self.t.mask = sum(1 << (7+b) for b in self.t.buttons)
            if self.t.ack and (not kind.endswith('up') or self.t.release_ack):
                self.t.emit(event, True)
        self.t.write = write

    def grant(self, **extra):
        grant = dict(incarnation='I', generation=GEN, layout_version=4, nonce=secrets.token_hex(8),
                     expires=time.time()+100, controller_pid=os.getpid(), controller_start=identity(os.getpid()), **extra)
        return dict(grant=grant, signature=signature(self.signing_key, grant), explicit_action=True)

    def tick(self):
        self.w.cache.commit()
        self.e.tick()
        self.w.settle_pixels()
        self.w.frame_feed.publish()
        self.h.tick()
        self.now += .001

    def drain_human(self, limit=6000):
        for _ in range(limit):
            self.tick()
            if self.h.runner is None:
                return
        self.fail('human transition did not finish')

    def begin(self):
        self.h.handle(self.human_peer, 81, 'human.begin', self.grant())
        self.drain_human()
        self.assertEqual(self.h.state, 'human', self.errors)
        return self.replies[-1][1]

    def hp(self, **extra):
        return dict(incarnation='I', token=self.h.token, generation=GEN, layout_version=self.h.layout, **extra)

    def event(self, kind, **extra):
        self.h.handle(self.human_peer, 82, 'human.event', self.hp(seq=self.h.seq+1, kind=kind, **extra))
        self.drain_human()

    def test_sequence_gate_epoch_revoke_release_before_human(self):
        order = []
        store = __import__('worker_human').control_store
        def watched(page, offset, value, bits):
            store(page, offset, value, bits)
            if offset in (24, 32):
                order.append((offset, value, self.w.lease))
                if offset == 32 and value == 1:
                    self.assertIsNone(control_snapshot(self.path/'control', GEN))
        self.t.down.add(38)
        self.e.holds['xtest']['key'].add(38)
        # Existing engine release sends via its acknowledgment path.
        self.e.send = lambda a, r, events, cleanup=False: self.engine_release(events)
        with patch('worker_human.control_store', side_effect=watched):
            self.h.handle(self.human_peer, 81, 'human.begin', self.grant())
            self.assertEqual(self.h.state, 'closing')
            self.assertIsNone(self.w.lease)
            self.assertEqual(PAGE.unpack((self.path/'control').read_bytes())[3], 2)
            self.drain_human()
        self.assertEqual(order[:2], [(32, 1, 'lease'), (24, 2, 'lease')])
        self.assertFalse(self.t.down)
        self.assertEqual(self.h.state, 'human')
        self.assertIsNone(self.w.lease)
        self.assertEqual(self.rows()[-1]['type'], 'human.ready')

    def engine_release(self, events):
        for event in events:
            self.t.write(event)
        self.e.holds['xtest']['key'].clear()
        self.e.holds['xtest']['button'].clear()
        if False: yield
        return True

    def test_gate_closure_preempts_inflight_driver_and_never_replays(self):
        self.submit(self.params([self.key()], action_id='interrupted'))
        for _ in range(2000):
            self.tick()
            if any(e[0] == 'key_down' for e in self.released): break
        self.assertTrue(self.e.active)
        boundary = len(self.released)
        self.h.handle(self.human_peer, 81, 'human.begin', self.grant())
        self.drain_human()
        self.assertTrue(all(e[0].endswith('up') for e in self.released[boundary:]))
        receipt = self.e.get(GEN, 'interrupted')['receipt']
        self.assertIn(receipt['status'], ('partial', 'failed', 'unknown'))
        self.assertFalse(receipt['effect']['status'] == 'observed')
        with self.assertRaisesRegex(w.RPCError, 'invalid_lease'):
            self.submit(self.params(action_id='late'))
        self.assertTrue(all(e[0].endswith('up') for e in self.released[boundary:]))
        self.assertEqual(self.h.state, 'human')

    def test_stale_generation_and_layout_reject_without_dispatch(self):
        self.begin()
        before = list(self.released)
        for field, value, reason in [('generation', 'f'*32, 'stale_generation'), ('layout_version', 3, 'stale_layout')]:
            p = self.hp(seq=1, kind='key_down', detail='a')
            p[field] = value
            with self.assertRaisesRegex(HumanError, reason): self.h.handle(self.human_peer, 3, 'human.event', p)
        self.assertEqual(self.released, before)
        self.assertEqual(self.h.seq, 0)

    def test_drag_lifetime_and_wrong_drag_cannot_release_or_move(self):
        self.begin()
        self.event('button_down', detail=1, drag_id='D')
        for kind, extra in [('motion', dict(x=400, y=100)), ('button_up', dict(detail=1))]:
            with self.assertRaisesRegex(HumanError, 'drag_target_changed'):
                self.h.handle(self.human_peer, 2, 'human.event', self.hp(seq=self.h.seq+1, kind=kind, drag_id='other', **extra))
        self.event('motion', x=639, y=479, drag_id='D')
        self.event('button_up', detail=1, drag_id='D')
        self.assertIsNone(self.h.drag)
        self.assertFalse(self.t.buttons)

    def test_layout_change_mid_drag_closes_gate_and_releases(self):
        self.begin()
        self.event('button_down', detail=1, drag_id='D')
        self.h.handle(self.human_peer, 4, 'human.layout', self.hp() | dict(layout_version=5))
        self.assertIsNone(control_snapshot(self.path/'control', GEN, self.h.token, self.h.epoch))
        self.drain_human()
        self.assertFalse(self.t.buttons)
        self.assertEqual(self.h.state, 'paused')

    def test_kill_file_ends_takeover_and_releases_all_inputs(self):
        self.begin()
        self.event('key_down', detail='a')
        self.event('button_down', detail=1, drag_id='D')
        self.h.kill_file.touch()
        self.drain_human()
        self.assertEqual(self.h.state, 'paused')
        self.assertFalse(self.t.down | self.t.buttons)
        self.assertIsNone(self.h.token)
        self.assertIsNone(control_snapshot(self.path/'control', GEN))
        self.assertEqual(self.rows()[-1]['reason'], 'global_kill')

    def test_controller_death_and_presenter_disconnect_release(self):
        for reason in ('controller_lost', 'presenter_lost', 'presenter_timeout'):
            self.begin()
            self.event('button_down', detail=1, drag_id='D')
            if reason == 'controller_lost':
                self.h.grant['controller_start'] = 'dead'
            elif reason == 'presenter_lost':
                self.h.disconnected(self.human_peer)
            else:
                self.now += 2
            self.drain_human()
            self.assertEqual(self.h.state, 'paused')
            self.assertFalse(self.t.buttons)
            self.assertEqual(self.rows()[-1]['reason'], reason)
            self.h.grant = None

    def test_resume_forces_fresh_capture_new_attach_and_full_observe(self):
        self.begin()
        before = self.w.pixels.serial
        old_lease = 'lease'
        self.h.handle(self.human_peer, 90, 'human.end', self.hp(resume=True))
        self.h.tick()
        self.assertEqual(self.h.state, 'resuming')
        self.assertIsNone(control_snapshot(self.path/'control', GEN))
        self.assertGreater(self.w.pixels.serial, before)
        self.drain_human()
        self.assertEqual(self.h.state, 'idle')
        self.assertEqual(self.w.frame_feed.stamp, (id(self.w.pixels), self.w.pixels.serial))
        self.assertTrue(self.h.resume_observe)
        self.assertIsNone(self.w.lease)
        self.w.stopping = False
        self.w.channels = lambda: {}
        self.w.message(self.peer, json.dumps(dict(jsonrpc='2.0', id=99, method='session.attach', params=dict(client='api', name='fresh'))).encode())
        new_lease = self.w.lease
        self.assertNotEqual(new_lease, old_lease)
        self.assertIsNone(self.e.lease_state[new_lease]['observed'])
        p = dict(lease=new_lease, since_rev=self.w.cache.rev, scope=dict(kind='window', window=self.handle))
        self.w.message(self.peer, json.dumps(dict(jsonrpc='2.0', id=100, method='window.observe', params=p)).encode())
        self.assertEqual(self.errors[-1], (100, 'full_observe_required'))
        result = dict(full=True, settled=True, rev=self.w.cache.rev)
        self.w.observed(self.peer, dict(lease=new_lease, scope=dict(kind='session')), result)
        self.assertFalse(self.h.resume_observe)
        self.assertEqual(self.e.lease_state[new_lease]['observed'], self.w.cache.rev)

    def test_resume_capture_failure_keeps_gate_closed(self):
        self.begin()
        self.h.handle(self.human_peer, 90, 'human.end', self.hp(resume=True))
        self.h.tick()
        self.w.frame_feed.snapshot = None
        self.now += 4
        self.h.tick()
        self.assertEqual(self.h.state, 'paused')
        self.assertIsNone(control_snapshot(self.path/'control', GEN))

    def test_journal_has_counts_and_kinds_never_key_content(self):
        self.begin()
        self.event('key_down', detail='a')
        self.event('key_up', detail='a')
        rows = [r for r in self.rows() if r['type'] == 'human.events']
        self.assertEqual([r['kinds'] for r in rows], [{'key_down': 1}, {'key_up': 1}])
        for row in rows:
            self.assertEqual(set(row), {'ts', 'type', 'session', 'generation', 'count', 'kinds'})
        journal = self.w.journal.path.read_text()
        self.assertNotIn('"detail"', journal)
        self.assertNotIn('"keycode"', journal)
        self.assertNotIn('"events": [["key_', journal)
        self.assertNotIn(self.h.token, journal)

    def test_release_unconfirmed_never_arms_human(self):
        self.t.release_ack = False
        self.t.down.add(38)
        self.h.keys.add(38)
        self.h.handle(self.human_peer, 81, 'human.begin', self.grant())
        self.drain_human()
        self.assertNotEqual(self.h.state, 'human')
        self.assertIsNone(control_snapshot(self.path/'control', GEN))
        self.assertIn('release_unconfirmed', [r for _, r in self.errors])
        self.assertEqual(self.e.fence_reason, 'F1:release_unconfirmed')

    def test_explicit_action_signature_incarnation_and_replay(self):
        for change, reason in [(dict(explicit_action=False), 'explicit_action_required'),
                               (dict(signature='bad'), 'invalid_capability')]:
            with self.assertRaisesRegex(HumanError, reason):
                self.h.handle(self.human_peer, 1, 'human.begin', self.grant() | change)
        grant = self.grant()
        grant['grant']['incarnation'] = 'other'
        grant['signature'] = signature(self.signing_key, grant['grant'])
        with self.assertRaisesRegex(HumanError, 'stale_capability'):
            self.h.handle(self.human_peer, 1, 'human.begin', grant)
        grant = self.grant()
        self.h.handle(self.human_peer, 1, 'human.begin', grant)
        self.drain_human()
        self.h.disconnected(self.human_peer)
        self.drain_human()
        with self.assertRaisesRegex(HumanError, 'spent_capability'):
            self.h.handle(self.human_peer, 1, 'human.begin', grant)


    def test_evdev_direct_path_uses_authorized_gate_and_release_held(self):
        from worker_evdev import EvdevTransport
        transport = EvdevTransport(self.w, self.t, clock=lambda: self.now, keycodes={38:30})
        transport.keycodes = {38:30}
        self.e.evdev = transport
        self.addCleanup(transport.close)
        operations = []
        native = []
        def writer(dev, event):
            native.append((dev,event))
            typ, code, value = event
            if typ == 1:
                if dev == 'kbd':
                    if value: self.t.down.add(38)
                    else: self.t.down.discard(38)
                elif code == 0x110:
                    if value: self.t.buttons.add(1)
                    else: self.t.buttons.discard(1)
                self.t.mask = sum(1 << (7+b) for b in self.t.buttons)
        protocol = EventProtocol({d: lambda event, d=d: writer(d,event) for d in ('kbd','ptr')},
            lambda gen: control_snapshot(self.path/'control',gen), lambda dev:'busy',
            human_gate=lambda gen,token,epoch:control_snapshot(self.path/'control',gen,token,epoch))
        def exchange(req):
            operations.append(req.get('op'))
            if False: yield
            return protocol.handle(req)
        transport.exchange = exchange
        self.begin()
        self.event('key_down', detail='a')
        self.event('button_down',detail=1,drag_id='D')
        self.assertIn(('kbd',(1,30,1)), native)
        self.assertEqual(self.h.wire['kbd'], {30})
        self.h.kill_file.touch()
        self.drain_human()
        self.assertIn('release_held', operations)
        self.assertFalse(protocol.held['kbd'] | protocol.held['ptr'])
        self.assertFalse(self.t.down | self.t.buttons)
        self.assertNotIn('wire_holds', self.w.journal.path.read_text())
        self.assertIsNone(control_snapshot(self.path/'control',GEN))

    def test_real_controller_process_sigkill_mid_drag_releases(self):
        process = subprocess.Popen(['/usr/bin/python3','-B','-c','import time; time.sleep(30)'])
        self.addCleanup(lambda: process.poll() is None and process.kill())
        self.begin()
        self.event('button_down',detail=1,drag_id='D')
        self.h.grant.update(controller_pid=process.pid,controller_start=identity(process.pid))
        process.kill()
        process.wait(timeout=2)
        self.drain_human()
        self.assertEqual(self.h.state,'paused')
        self.assertFalse(self.t.buttons)
        self.assertEqual(self.rows()[-1]['reason'],'controller_lost')

    def test_new_controller_grant_can_recover_paused_takeover(self):
        self.begin()
        self.h.grant['controller_start'] = 'dead'
        self.drain_human()
        self.assertEqual(self.h.state,'paused')
        self.begin()
        self.assertEqual(self.h.state,'human')

    def test_wire_human_method_routes_without_driver_lease(self):
        def rpc(ident,method,params):
            self.w.message(self.human_peer,json.dumps(dict(jsonrpc='2.0',id=ident,method=method,params=params)).encode())
            self.drain_human()
        rpc(70,'human.begin',self.grant())
        self.assertEqual(self.h.state,'human')
        self.assertIsNone(self.w.lease)
        rpc(71,'human.event',self.hp(seq=1,kind='key_down',detail='a'))
        self.assertIn(38,self.t.down)
        rpc(72,'human.event',self.hp(seq=2,kind='key_up',detail='a') | dict(generation='bad'))
        self.assertEqual(self.errors[-1],(72,'stale_generation'))
        rpc(73,'human.end',self.hp(resume=False))
        self.assertFalse(self.t.down)
        self.assertEqual(self.h.state,'paused')

    def test_stale_incarnation_cannot_use_token(self):
        self.begin()
        with self.assertRaisesRegex(HumanError,'stale_incarnation'):
            self.h.handle(self.human_peer,3,'human.event',self.hp(seq=1,kind='key_down',detail='a') | dict(incarnation='replacement'))
        self.assertFalse(self.t.down)


    def test_legacy_control_lock_cannot_deadlock_takeover(self):
        import fcntl
        with (self.path/'control').open('r+b') as owner:
            fcntl.flock(owner,fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(HumanError,'control_busy'):
                self.h.page(2,token='a'*64)
            self.h.page(1,bump=True)
            self.assertIsNone(control_snapshot(self.path/'control',GEN))



    def test_control_epoch_change_during_release_never_arms_human(self):
        self.h.handle(self.human_peer,81,'human.begin',self.grant())
        (self.path/'control').write_bytes(w.encode_control(GEN,epoch=3,state=1))
        self.drain_human()
        self.assertEqual(self.h.state,'paused')
        self.assertNotIn('human.ready',[r['type'] for r in self.rows()])
        self.assertIsNone(control_snapshot(self.path/'control',GEN))

    def test_takeover_never_carries_clear_fence_authority(self):
        self.e.fence_reason = 'F2:existing'
        (self.path/'control').write_bytes(w.encode_control(GEN,flags=1))
        self.begin()
        self.h.handle(self.human_peer,90,'human.end',self.hp(resume=True))
        self.drain_human()
        self.assertEqual(self.e.fence_reason,'F2:existing')
        self.assertEqual(PAGE.unpack((self.path/'control').read_bytes())[5] & 1,0)

    def test_old_lease_observation_cannot_clear_resume_requirement(self):
        self.begin()
        self.h.handle(self.human_peer,90,'human.end',self.hp(resume=True))
        self.drain_human()
        self.w.lease,self.w.lease_peer = 'new-lease',self.peer
        self.e.attach('new-lease',self.peer,{})
        self.w.observed(self.peer,dict(lease='lease',scope=dict(kind='session')),
                        dict(full=True,settled=True,rev=self.w.cache.rev))
        self.assertTrue(self.h.resume_observe)
        self.assertIsNone(self.e.lease_state['new-lease']['observed'])

    def test_supervised_helper_recovery_releases_in_memory_wire_holds(self):
        self.begin()
        self.event('key_down',detail='a')
        self.h.wire['kbd'].add(30)
        evdev = Mock()
        evdev.held_codes = dict(kbd=set(),ptr=set())
        self.e.evdev = evdev
        def failed(**kwargs):
            if False: yield
            raise OSError('helper_restarted')
        def recovered():
            self.assertEqual(evdev.held_codes['kbd'],{30})
            self.t.down.clear()
            if False: yield
            return True
        evdev.request.side_effect = failed
        evdev.recover_release.side_effect = recovered
        self.h.disconnected(self.human_peer)
        self.drain_human()
        self.assertEqual(self.h.state,'paused')
        self.assertFalse(self.t.down)
        self.assertFalse(evdev.held_codes['kbd'])
        evdev.recover_release.assert_called_once()


    def test_resume_accepts_newer_settled_frame_after_intervening_damage(self):
        self.begin()
        self.h.handle(self.human_peer,90,'human.end',self.hp(resume=True))
        self.h.tick()
        self.assertEqual(self.h.state,'resuming')
        serial = self.w.pixels.serial
        self.w.pixels.mark([dict(x=0,y=0,w=64,h=64)])
        self.assertGreater(self.w.pixels.serial,serial)
        self.drain_human()
        self.assertEqual(self.h.state,'idle')
        self.assertEqual(self.w.frame_feed.stamp,(id(self.w.pixels),self.w.pixels.serial))
        self.assertTrue(self.h.resume_observe)


class HelperCase(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'control'
        self.token = 'b'*64
        self.hash = hashlib.sha256(self.token.encode()).digest()[:16]
        self.path.write_bytes(w.encode_control(GEN, epoch=5, state=2, lease_hash=self.hash))
        self.events = []
        self.protocol = EventProtocol({d: lambda e, d=d: self.events.append((d, e)) for d in ('kbd', 'ptr')},
            lambda gen: control_snapshot(self.path, gen), lambda d: 'busy',
            human_gate=lambda gen, token, epoch: control_snapshot(self.path, gen, token, epoch))

    def request(self, seq=1, human=True, **extra):
        p = dict(seq=seq, generation=GEN, dev='kbd', frames=[[[1, 30, 1]]], gap_us=0)
        if human: p.update(op='human', human_token=self.token, human_epoch=5)
        return p | extra

    def test_agent_gate_stays_closed_and_only_authorized_human_can_write(self):
        self.assertFalse(self.protocol.handle(self.request(human=False))['ok'])
        self.assertFalse(self.protocol.handle(self.request(2, human_token='a'*64))['ok'])
        self.assertFalse(self.protocol.handle(self.request(3, human_epoch=4))['ok'])
        self.assertFalse(self.events)
        self.assertTrue(self.protocol.handle(self.request(4))['ok'])
        self.assertTrue(self.events)
        self.assertNotIn(self.token, repr(self.protocol.replies))

    def test_close_between_human_frames_stops_prefix_then_release_held(self):
        count = [0]
        def writer(event):
            self.events.append(('kbd', event))
            if event == (0, 0, 0):
                count[0] += 1
                self.path.write_bytes(w.encode_control(GEN, epoch=6, state=1))
        self.protocol.writers['kbd'] = writer
        result = self.protocol.handle(self.request(frames=[[[1, 30, 1]], [[1, 31, 1]]]))
        self.assertFalse(result['ok'])
        self.assertEqual(result['frames_written'], 1)
        self.assertEqual(result['error'], 'gate_closed')
        self.assertTrue(self.protocol.handle(dict(seq=2, op='release_held', generation=GEN, holds=dict(kbd=[30], ptr=[])))['ok'])
        self.assertFalse(self.protocol.held['kbd'])
        self.assertNotIn(('kbd', (1, 31, 1)), self.events)

    def test_human_token_is_useless_after_generation_or_state_change(self):
        for page in [w.encode_control('f'*32, epoch=5, state=2, lease_hash=self.hash),
                     w.encode_control(GEN, epoch=5, state=0, lease_hash=self.hash)]:
            self.path.write_bytes(page)
            self.assertIsNone(control_snapshot(self.path, GEN, self.token, 5))
        self.assertFalse(self.events)


class SceneCase(unittest.TestCase):
    def setUp(self):
        self.s = Scene()
        self.records = [dict(incarnation=i, session='s'+str(n), parent_incarnation=p, attention=[])
                        for n, (i, p) in enumerate([('P', None), ('C', 'P'), ('G', 'C'), ('D', 'G')], 1)]
        self.s.registry(self.records)
        for i in ('P', 'C'):
            self.s.frame(i, dict(generation=GEN, rev=1, frame=dict(rect=dict(x=0,y=0,w=640,h=480)), modal_regions=[]))

    def test_two_live_levels_and_deeper_attention_chips(self):
        self.assertEqual(set(self.s.transforms), {'P', 'C'})
        self.assertEqual(self.s.chips, ['G', 'D'])
        self.records[2]['attention'] = [dict(need='passcode')]
        version = self.s.version
        self.s.registry(self.records)
        self.assertEqual(self.s.records['G']['attention'][0]['need'], 'passcode')
        self.assertEqual(self.s.version, version)
        self.s.reroot('C')
        self.assertIn('P', self.s.chips)
        self.assertEqual(set(self.s.transforms), {'C', 'G'})
        self.assertIn('D', self.s.chips)

    def test_frames_reuse_transform_until_move_resize(self):
        transform, builds = self.s.transforms['C'], self.s.layout_builds
        for rev in range(2, 15):
            self.s.frame('C', dict(self.s.frames['C'], rev=rev))
        self.assertIs(self.s.transforms['C'], transform)
        self.assertEqual(self.s.layout_builds, builds)
        self.s.place('C', 200, 200, 320, 240)
        t = self.s.transforms['C']
        self.assertEqual(t.native(t.x+t.w//2, t.y+t.h//2), (320, 240))
        self.s.resize(900, 650)
        self.assertGreater(self.s.layout_builds, builds)

    def test_drag_pins_target_and_mapping_outside_all_surfaces(self):
        t = self.s.transforms['C']
        start = self.s.pointer(t.x+20, t.y+20, 'down', 'D')
        self.assertEqual(start['incarnation'], 'C')
        self.assertEqual(self.s.pointer(-300,-300,'move','D')['incarnation'], 'C')
        end = self.s.pointer(10000,10000,'up','D')
        self.assertEqual((end['x'], end['y']), (639,479))
        self.assertIsNone(self.s.drag)

    def test_drag_layout_or_generation_change_rejects_without_retarget(self):
        for change in ('layout', 'generation'):
            self.s.drag = None
            t = self.s.transforms['C']
            self.s.pointer(t.x+1,t.y+1,'down','D')
            if change == 'layout': self.s.resize(1000,700)
            else: self.s.frames['C']['generation'] = 'f'*32
            with self.assertRaisesRegex(ValueError,'stale_drag_binding'):
                self.s.pointer(1,1,'up','D')

    def test_parent_modal_disables_child_hit_testing_and_collapse_is_local(self):
        t = self.s.transforms['C']
        self.assertEqual(self.s.hit(t.x+1,t.y+1),'C')
        self.s.frames['P']['modal_regions'] = [dict(x=0,y=0,w=20,h=20)]
        self.assertEqual(self.s.hit(t.x+1,t.y+1),'P')
        before = copy.deepcopy(self.records)
        self.s.collapse('C')
        self.assertNotIn('C',self.s.transforms)
        self.assertIn('C',self.s.chips)
        self.assertEqual(self.records,before)
        self.s.collapse('C')
        self.assertIn('C',self.s.transforms)

    def test_selection_is_pure_presenter_state(self):
        native = copy.deepcopy(self.s.frames)
        t = self.s.transforms['C']
        self.s.select(t.x+1,t.y+1)
        self.assertEqual(self.s.selected,'C')
        self.assertEqual(self.s.frames,native)

    def test_host_display_zero_refused_before_open(self):
        for name in (':0', ':0.0', ':00.1', '', None, 'localhost:0', 'host:91'):
            with self.assertRaises(ValueError): host_display(name)
        self.assertEqual(host_display(':121'), ':121')

    def test_png_decoder_preserves_native_pixels(self):
        shadow = Shadow(3, 2)
        shadow.data[:] = bytes([3,2,1,0, 6,5,4,0, 9,8,7,0])*2
        png = shadow.png(dict(x=0,y=0,w=3,h=2),1)
        width,height,pixels = decode_png(png)
        self.assertEqual((width,height),(3,2))
        self.assertEqual(pixels,bytes(shadow.data))
        with self.assertRaises(ValueError): decode_png(png[:-5])

    def test_renderer_installs_transform_once_and_composites_each_frame(self):
        r = Renderer.__new__(Renderer)
        r.rd = 1
        r.surfaces = {'P':dict(picture=2,transform=None)}
        r.transforms = {}
        r.cursors = {}
        r.version = None
        r.set_transform, r.flush, r.composite = Mock(),Mock(),Mock()
        r.dest = 3
        r.drop = Mock()
        self.s.transforms = {'P':self.s.transforms['P']}
        for _ in range(5):
            r.layout(self.s)
            r.draw_surface('P',self.s.transforms['P'])
        self.assertEqual(r.set_transform.call_count,1)
        self.assertEqual(r.composite.call_count,5)
        self.s.resize(900,650)
        r.layout(self.s)
        self.assertEqual(r.set_transform.call_count,2)


    def test_cursor_image_and_dialog_metadata_do_not_require_new_png(self):
        import base64
        feed = FrameFeed.__new__(FrameFeed)
        feed.w = NS(generation=GEN,sid='s1',x=NS(ax=NS(ask=Mock()),ev=Mock()))
        feed.snapshot = NS(w=640,h=480)
        feed.rev, feed.cursor_pending, feed.cursor_due = 3, False, 0
        feed.cursor_image = None
        feed.windows = {'H':dict(handle='H',geometry=dict(x=2,y=4,w=20,h=10),is_modal=True)}
        feed.modal = ['H']
        feed.sample_cursor()
        callback = feed.w.x.ax.ask.call_args.args[1]
        callback(dict(width=1,height=1,cursor_image=[0xff123456],x=9,y=10,xhot=0,yhot=0))
        reply = feed.query(dict(since_rev=3,metadata=True))
        self.assertEqual(reply['status'],'unchanged')
        self.assertNotIn('frame',reply)
        self.assertEqual(base64.b64decode(reply['cursor']['image']['pixels']),bytes.fromhex('563412ff'))
        self.assertEqual(reply['modal_regions'],[dict(x=2,y=4,w=20,h=10)])
        self.assertEqual(set(feed.query(dict(since_rev=3))),{'session','generation','rev','status','changed'})

    def test_modifier_and_function_key_names_are_preserved(self):
        from presenter_x11 import key_name
        from Xlib import XK
        for name in ('Shift_L','Control_L','Return','Tab','BackSpace','F2','a'):
            self.assertEqual(key_name(XK.string_to_keysym(name)),name)

    def test_multi_button_drag_remains_pinned_until_last_release(self):
        t = self.s.transforms['C']
        self.s.pointer(t.x+1,t.y+1,'down','D',button=1)
        self.s.pointer(t.x+1,t.y+1,'down','D',button=3)
        self.s.pointer(0,0,'up','D',button=1)
        self.assertEqual(self.s.pointer(0,0,'move','D')['incarnation'],'C')
        self.s.pointer(0,0,'up','D',button=3)
        self.assertIsNone(self.s.drag)

    def test_chips_wrap_and_scroll_with_attention_preserved(self):
        records = copy.deepcopy(self.records)
        records += [dict(incarnation='G'+str(i),session='s'+str(i+10),parent_incarnation='C',
                         attention=[dict(need='blocked')] if i==15 else []) for i in range(16)]
        self.s.registry(records)
        views = self.s.chip_views()
        self.assertTrue(all(0 <= x < self.s.width and self.s.chip_top <= y < self.s.height for x,y,_ in views))
        self.assertEqual(len({i for _,_,i in views}),len(views))


    def test_optional_cursor_sensing_does_not_overload_driver_budget(self):
        feed = FrameFeed.__new__(FrameFeed)
        budget = NS(count=10,limit=10,overloaded=False)
        feed.w = NS(x=NS(ev=Mock(),ax=NS(ask=Mock(),budget=budget)))
        feed.cursor_pending,feed.cursor_due = False,0
        feed.sample_cursor()
        feed.w.x.ax.ask.assert_not_called()
        self.assertFalse(budget.overloaded)
        self.assertFalse(feed.cursor_pending)


# Reuse fixture setup without discovering the inherited WM-B tests twice.
class CapabilityCase(unittest.TestCase):
    setUp = controller_fixture.ControllerCase.setUp
    records = controller_fixture.ControllerCase.records
    record = controller_fixture.ControllerCase.record
    page = controller_fixture.ControllerCase.page

    def test_controller_issues_worker_scoped_grant_without_key_content(self):
        root = self.record(self.root)
        sd = Path(root['session_dir'])
        (sd/'worker.json').write_text(json.dumps(dict(generation=GEN)))
        result = self.c.dispatch('takeover.issue',dict(incarnation=self.root,generation=GEN,layout_version=8,explicit_action=True))
        self.assertEqual(result['grant']['incarnation'], self.root)
        self.assertEqual(result['grant']['layout_version'],8)
        self.assertEqual(signature(self.c.key,result['grant']),result['signature'])
        self.assertEqual(result['socket'],str(sd/'driver/act.sock'))
        self.assertNotIn(result['signature'],self.c.log.path.read_text())
        from wm_controller import Refused
        with self.assertRaisesRegex(Refused,'explicit_action_required'):
            self.c.dispatch('takeover.issue',dict(incarnation=self.root,generation=GEN,layout_version=8))
        with self.assertRaisesRegex(Refused,'stale_generation'):
            self.c.dispatch('takeover.issue',dict(incarnation=self.root,generation='f'*32,layout_version=8,explicit_action=True))
