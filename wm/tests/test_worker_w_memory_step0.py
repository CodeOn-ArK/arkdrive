"""Step 0: real retained structures, fake transports; never open a display."""
import contextlib
import copy
import io
import json
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from test_worker_w2 import Harness, Engine, w, GEN
from worker_pixels import Frames
from worker_tree import Provider, Breaker
import memory_step0 as soak


class MemoryCounters(Harness):
    def setUp(self):
        super().setUp()
        self.w.pixels = self.w.tree = self.w.x = None

    def test_completed_acts_retire_but_leases_still_grow(self):
        before = self.w.memory_counters()
        for i in range(3):
            result = self.run_action(self.params(action_id=f'a-{i}', base_rev=self.w.cache.rev))
            self.assertEqual(result['status'], 'success')
        self.e.attach('second', NS(), {})
        after = self.w.memory_counters()
        self.assertEqual(after['actions_retained'] - before['actions_retained'], 0)
        self.assertEqual(after['action_stubs'] - before['action_stubs'], 3)
        self.assertEqual(after['journal_actions_indexed'] - before['journal_actions_indexed'], 3)
        self.assertEqual(after['lease_records'] - before['lease_records'], 1)
        self.assertEqual(len(self.e.actions), 0)

    def test_removed_history_grows_on_real_window_lifecycles(self):
        before = self.w.memory_counters()
        template = self.w.cache.windows[10]
        for xid in range(20, 24):
            self.w.cache.upsert(xid, dict(template, xid=xid))
            self.w.cache.commit()
            self.w.cache.remove(xid)
            self.w.cache.commit()
        after = self.w.memory_counters()
        self.assertEqual(after['removed_windows'] - before['removed_windows'], 4)
        observation = self.w.cache.observe({'kind': 'session'}, None)
        self.assertEqual(len(observation['state']['removed']), after['removed_windows'])

    def test_frame_identities_and_files_are_distinct_and_reads_do_not_evict(self):
        frames = Frames(self.path / 'frames')
        self.w.pixels = NS(frames=frames)
        meta = dict(rev=1, rect=dict(x=0, y=0, w=1, h=1), scale=1, space='desktop')
        self.assertEqual(self.w.memory_counters()['frame_files'], 0)
        frames.put(b'first-png', meta)
        frames.put(b'first-png', dict(meta, rev=2))
        frames.put(b'second-png', dict(meta, rev=3))
        (frames.directory / 'orphan.tmp').write_bytes(b'orphan')
        (frames.directory / 'directory.png').mkdir()
        issued = copy.deepcopy(frames.issued)
        with patch.object(frames, 'evict', side_effect=AssertionError('measurement must not evict')):
            counts = self.w.memory_counters()
        self.assertEqual((counts['frame_identities'], counts['frame_files']), (3, 2))
        self.assertEqual(frames.issued, issued)
        self.assertTrue((frames.directory / 'orphan.tmp').exists())

    def test_parent_app_count_and_both_pending_x_channels(self):
        provider = Provider.__new__(Provider)
        provider.apps = {}
        self.w.tree = provider
        self.w.x = NS(ax=NS(pending=[]), inp_ax=NS(pending=[]))
        before = self.w.memory_counters()
        provider.apps.update(app1=Breaker(), app2=Breaker())
        self.w.x.ax.pending.extend([object(), object()])
        self.w.x.inp_ax.pending.append(object())
        after = self.w.memory_counters()
        self.assertEqual(after['atspi_apps'] - before['atspi_apps'], 2)
        self.assertEqual(after['pending_x_requests'] - before['pending_x_requests'], 3)
        self.assertEqual(len(self.w.x.ax.pending), 2)
        self.assertEqual(len(self.w.x.inp_ax.pending), 1)

    def test_journal_bytes_are_current_open_generation_only(self):
        before = self.w.memory_counters()['journal_bytes']
        (self.path / 'acts/other.jsonl').write_bytes(b'x'*10000)
        self.assertEqual(self.w.memory_counters()['journal_bytes'], before)
        self.w.journal.append('measurement_fixture', text='Unicode α')
        after = self.w.memory_counters()['journal_bytes']
        self.assertGreater(after, before)
        self.assertEqual(after, len(self.w.journal.path.read_bytes()))

    def test_unavailable_and_failed_measurements_are_explicit(self):
        self.w.act = None
        empty = self.w.memory_counters()
        self.assertIsNone(empty['pending_x_requests'])
        for field in ('actions_retained', 'lease_records', 'frame_files', 'frame_identities', 'atspi_apps'):
            self.assertEqual(empty[field], 0)
        self.w.pixels = NS(frames=Frames(self.path / 'frames'))
        with patch.object(w.os, 'scandir', side_effect=PermissionError), \
                patch.object(w.os, 'fstat', side_effect=OSError):
            unknown = self.w.memory_counters()
        self.assertIsNone(unknown['frame_files'])
        self.assertIsNone(unknown['journal_bytes'])

    def test_system_info_adds_memory_without_changing_existing_counters(self):
        self.w.health = lambda: dict(counters=dict(self.w.counters), worker={'state': 'ready'})
        before = copy.deepcopy(self.w.health())
        self.w.settle_pixels = lambda: True
        self.w.message(self.peer, w.json_bytes(dict(jsonrpc='2.0', id=9, method='system.info', params={})))
        result = self.result()
        memory = result.pop('memory')
        self.assertEqual(result, before)
        self.assertEqual(memory, dict(schema=1, actions_retained=0, action_stubs=0,
            journal_actions_indexed=0, lease_records=1,
            removed_windows=0, frame_identities=0, frame_files=0, atspi_apps=0,
            journal_bytes=0, pending_x_requests=None))


class MeasurementHarness(unittest.TestCase):
    def test_dry_run_opens_no_socket_or_fixture(self):
        stream = io.StringIO()
        with patch.object(soak.socket, 'socket', side_effect=AssertionError('no socket')), \
                patch.object(soak.subprocess, 'Popen', side_effect=AssertionError('no fixture')), \
                contextlib.redirect_stdout(stream):
            self.assertEqual(soak.main(['--dry-run', '--windows', '3', '--acts', '4', '--popups', '2']), 0)
        rows = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual([r['phase'] for r in rows[:4]],
                         ['window_churn', 'act_churn', 'popup_flood', 'observe_size'])
        self.assertTrue(all(r['status'] == 'NOT RUN' for r in rows[:4]))

    def test_display_zero_rejected_before_any_connection(self):
        with patch.object(soak, 'MeasureRPC', side_effect=AssertionError('no connection')):
            for display in (0, '91', 90, 121):
                with self.assertRaises(ValueError):
                    soak.Soak(NS(), {'display': display}, Path('/unused'))

    def test_session_environment_excludes_host_plumbing(self):
        record = dict(display=91, home='/session/home', runtime_dir='/session/run',
                      bus_address='session-bus', atspi_address='session-atspi', session='s1')
        with patch.dict(soak.os.environ, dict(DISPLAY=':0', XAUTHORITY='/host', SESSION_MANAGER='host')):
            env = soak.session_environment(record, Path('/session/state'))
        self.assertEqual(env['DISPLAY'], ':91')
        self.assertEqual(env['DBUS_SESSION_BUS_ADDRESS'], 'session-bus')
        self.assertNotIn('XAUTHORITY', env)
        self.assertNotIn('SESSION_MANAGER', env)

    def test_proc_pss_and_hwm_are_distinct_and_missing_is_unknown(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
            proc = Path(tmp)
            (proc / '123').mkdir()
            (proc / '123/smaps_rollup').write_text('Pss: 17 kB\n')
            (proc / '123/status').write_text('VmHWM: 89 kB\n')
            row = soak.proc_memory(123, proc)
            self.assertEqual((row['pss_kib'], row['vmhwm_kib'], row['errors']), (17, 89, {}))
            (proc / '123/smaps_rollup').unlink()
            row = soak.proc_memory(123, proc)
            self.assertIsNone(row['pss_kib'])
            self.assertEqual(row['vmhwm_kib'], 89)
            self.assertEqual(row['errors']['smaps_rollup'], 'FileNotFoundError')

    def test_current_worker_children_and_helper_descendants_are_discovered(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
            proc = Path(tmp)
            for pid, children in ((10, '11'), (11, ''), (20, '21'), (21, '')):
                path = proc / str(pid)
                (path / 'task' / str(pid)).mkdir(parents=True)
                (path / 'task' / str(pid) / 'children').write_text(children)
                (path / 'cmdline').write_bytes(b'fixture\0child')
                (path / 'status').write_text('VmHWM: 1 kB\n')
                (path / 'smaps_rollup').write_text('Pss: 2 kB\n')
            rows = soak.process_samples(10, {'mkdevs_supervisor_pid': 20}, proc)
            self.assertEqual([r['pid'] for r in rows], [10, 11, 20, 21])
            self.assertEqual(rows[-1]['role'], 'helper_descendant')

    def test_wire_measurement_can_report_adapter_cap_failure(self):
        # Fragmented fake socket, no listener/display. Read the entire response
        # to keep the persistent connection synchronized past the adapter cap.
        result = dict(rev=1, state={'removed': ['x'*soak.MAX_FRAME]})
        body = soak.encoded(dict(jsonrpc='2.0', id=1, result=result))
        wire = bytearray(struct.pack('!I', len(body)) + body)
        def recv(size):
            part = bytes(wire[:min(size, 1001)])
            del wire[:len(part)]
            return part
        sock = Mock()
        sock.recv.side_effect = recv
        with patch.object(soak.socket, 'socket', return_value=sock), patch.object(soak, 'check_kill'):
            rpc = soak.MeasureRPC('/unused')
            obs = rpc.call('window.observe', {})
            size = soak.observe_size(obs, rpc.response_bytes)
            rpc.close()
        self.assertTrue(size['exceeds_adapter_cap'])
        self.assertEqual(size['response_bytes'], len(body))
        self.assertEqual(size['removed_count'], 1)
        self.assertEqual(wire, b'')

    def test_uncertain_act_is_never_replayed(self):
        run = soak.Soak.__new__(soak.Soak)
        run.rpc = Mock()
        run.rpc.call.return_value = dict(status='partial', pending_effects=[{'action_id': 'a'}])
        run.wait_for = lambda predicate, **kwargs: ({'rev': 1}, {})
        run.seq, run.prefix = 0, 'test'
        run.meta = dict(generation=GEN, lease='lease', control_epoch=1)
        run.record = dict(session='s1')
        with self.assertRaisesRegex(RuntimeError, 'no replay'):
            run.act(dict(intent={'kind': 'wait'}, expect={'kind': 'window_present', 'window': 'w1'}))
        run.rpc.call.assert_called_once()

    def test_all_phases_with_fake_fixture_lifetimes_and_real_request_schema(self):
        windows, removed, acts = {}, [], []
        class Fixture:
            def __init__(self, command, **kwargs):
                self.tag = command[command.index('--tag')+1]
                self.returncode = None
                windows[self.tag] = dict(title=self.tag, handle='w-' + self.tag)
            def poll(self):
                return self.returncode
            def terminate(self):
                self.returncode = 0
                for title in (self.tag, self.tag+' popup'):
                    if title in windows:
                        removed.append(windows.pop(title)['handle'])
            def wait(self, **kwargs):
                return self.returncode
        class RPC:
            response_bytes = 1
            def call(self, method, params):
                if method == 'system.info':
                    return dict(worker=dict(generation=GEN, state='ready', pid=123),
                        session=dict(display=':91'), memory=dict(schema=1,
                        actions_retained=len(acts), removed_windows=len(removed), journal_bytes=len(acts)*100))
                if method == 'session.attach':
                    return dict(generation=GEN, lease='lease', control_epoch=1)
                if method == 'window.observe':
                    return dict(rev=1, settled=True, state=dict(windows=list(windows.values()), removed=removed[:]))
                if method == 'window.act':
                    Engine.__new__(Engine).normalise(params)
                    acts.append(params)
                    step = params['steps'][0]
                    if step['intent']['kind'] == 'key':
                        title = next(row['title'] for row in windows.values() if row['handle'] == step['target'])
                        if step['intent']['keys'] == ['Return']:
                            removed.append(windows.pop(title)['handle'])
                        else:
                            windows[title+' popup'] = dict(title=title+' popup', handle='popup-' + title)
                    return dict(status='success', pending_effects=[])
                raise AssertionError(method)
            def close(self):
                pass
        args = NS(windows=3, acts=4, popups=2, batch=2, popup_after=10, timeout=1)
        record = dict(display=91, session='s1', session_dir='/unused', home='/unused',
                      runtime_dir='/unused', bus_address='bus', atspi_address='atspi')
        output = io.StringIO()
        with patch.object(soak, 'MeasureRPC', return_value=RPC()), \
                patch.object(soak.subprocess, 'Popen', side_effect=Fixture), \
                patch.object(soak, 'process_samples', return_value=[]), \
                patch.object(soak, 'check_kill'), contextlib.redirect_stdout(output):
            run = soak.Soak(args, record, Path('/unused'))
            try:
                run.run()
            finally:
                run.close()
        summaries = [row for row in map(json.loads, output.getvalue().splitlines()) if row['event'] == 'summary']
        self.assertEqual([row['phase'] for row in summaries],
                         ['window_churn', 'act_churn', 'popup_flood', 'observe_size'])
        self.assertEqual([row.get('completed') for row in summaries[:3]], [3, 4, 2])
        self.assertEqual(summaries[1]['memory_delta']['actions_retained'], 4)
        self.assertEqual(len(acts), 8)  # 4 waits + 2 popup triggers + 2 dismissals
        self.assertEqual(len(removed), 8)  # 3 churn + 1 act fixture + 2 popup pairs
        self.assertEqual(windows, {})


if __name__ == '__main__':
    unittest.main()
