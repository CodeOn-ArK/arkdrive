"""W6 teardown regression seams: fake processes/clock, real temporary gate only."""
import errno
import importlib.machinery
import importlib.util
from pathlib import Path
import signal
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

WM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WM))
from input_protocol import PAGE, close_control_gate, control_snapshot, control_store
from mkdevs import EventProtocol

loader = importlib.machinery.SourceFileLoader('sessiond_w6_fix1', str(WM / 'ark-sessiond'))
spec = importlib.util.spec_from_loader(loader.name, loader)
sessiond = importlib.util.module_from_spec(spec)
loader.exec_module(sessiond)
GEN = '0123456789abcdef' * 2


class Teardown(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='.w6-teardown-', dir=WM.parent)
        self.addCleanup(temp.cleanup)
        self.sd = Path(temp.name)
        self.gate = self.sd / 'control'
        self.initial_page = PAGE.pack(b'ARKC', 1, bytes.fromhex(GEN), 7, 0, 1, 0, bytes(16))
        self.gate.write_bytes(self.initial_page)
        self.record = dict(session='s1', session_dir=str(self.sd), profile='xorg',
                           display=None, pid_x=101, pid_dbus=102, pid_atspi_launcher=103,
                           pid_registryd=104, mkdevs_pid=105)
        self.records = {'s1': self.record}
        self.living = {101, 102, 103, 104, 105}
        self.log, self.now, self.death_at = [], 0., None
        self.behavior = 'quit'

    def alive(self, pid):
        if self.death_at is not None and self.now >= self.death_at and 105 in self.living:
            self.living.remove(105)
            self.log.append(('helper_exit', self.now))
        return pid in self.living

    def sleep(self, delay):
        self.log.append(('sleep', delay))
        self.now += delay

    def kill(self, pid, sig):
        if pid == 101:
            self.assertFalse(self.alive(105), 'Xorg signal before helper death')
        self.log.append(('signal', pid, sig))
        if pid == 105 and sig == signal.SIGKILL and self.behavior != 'immortal':
            self.death_at = self.now + .1  # Prove wait after SIGKILL too.
        elif pid != 105 and sig == signal.SIGKILL:
            self.living.discard(pid)  # Step 5 verifies every process has exited.

    def close_gate(self, path):
        self.assertEqual(path, self.gate)
        close_control_gate(path)
        self.log.append(('gate_closed',))

    def fifo_write(self, fd, data):
        self.assertEqual((fd, data), (99999, b'quit\n'))
        self.assertIsNone(control_snapshot(self.gate, GEN))
        self.assertEqual(self.gate.read_bytes()[32:36], b'\x01\0\0\0')
        self.log.append(('quit',))
        if self.behavior == 'quit':
            self.death_at = self.now + .1
        return len(data)

    def run_teardown(self):
        # No real process, FIFO, privileged command, registry or /tmp X paths.
        with patch.object(sessiond, 'alive', side_effect=self.alive), \
                patch.object(sessiond.time, 'monotonic', side_effect=lambda: self.now), \
                patch.object(sessiond.time, 'sleep', side_effect=self.sleep), \
                patch.object(sessiond.os, 'kill', side_effect=self.kill), \
                patch.object(sessiond.os, 'open', return_value=99999) as fifo_open, \
                patch.object(sessiond.os, 'write', side_effect=self.fifo_write), \
                patch.object(sessiond.os, 'close'), \
                patch.object(sessiond, 'close_control_gate', side_effect=self.close_gate) as gate_close, \
                patch.object(sessiond, 'marked_pids', side_effect=lambda r: sorted(self.living)), \
                patch.object(sessiond, 'execute') as execute, \
                patch.object(sessiond, 'save_registry') as save:
            sessiond.teardown(self.record, self.records)
            if self.record['profile'] == 'xvfb':
                gate_close.assert_not_called()
                fifo_open.assert_not_called()
                execute.assert_not_called()
            save.assert_called_once_with({})

    def assert_safe_order(self):
        self.assertEqual(self.log[:2], [('gate_closed',), ('quit',)])
        death = next(i for i, e in enumerate(self.log) if e[0] == 'helper_exit')
        x_signals = [i for i, e in enumerate(self.log) if e[:2] == ('signal', 101)]
        self.assertTrue(x_signals)
        self.assertTrue(all(i > death for i in x_signals))
        # Only state is changed; no new encoding, epoch or generation rewrite.
        self.assertEqual(self.gate.read_bytes(), self.initial_page[:32] + b'\x01\0\0\0' + self.initial_page[36:])

    def test_gate_then_quit_then_helper_exit_then_xorg(self):
        self.run_teardown()
        self.assert_safe_order()
        self.assertFalse(any(e[:2] == ('signal', 105) for e in self.log))

    def test_long_request_drains_at_gate_boundary_during_teardown(self):
        # Pause the serving thread at the first pacing gap; resume only once
        # sessiond has closed the real page and sent the fake FIFO quit.
        writes, replies = [], []
        paced, resume = threading.Event(), threading.Event()
        self.behavior = 'ignore_quit'  # This test observes the serving thread's exit.
        def pacing(delay):
            self.assertEqual(delay, .05)
            paced.set()
            self.assertTrue(resume.wait(2))
        protocol = EventProtocol({'kbd': writes.append},
            lambda gen: control_snapshot(self.gate, gen), lambda dev: 'busy', pacing)
        request = dict(seq=1, generation=GEN, dev='kbd',
                       frames=[[[1, 30, i % 2]] for i in range(64)], gap_us=50000)
        protocol.frame_grab = Mock(return_value='busy')
        helper = threading.Thread(target=lambda: replies.append(protocol.handle(request)))
        original_wait = sessiond.wait_exit
        def drain(pids, seconds):
            if pids == [105]:
                self.assertEqual((pids, seconds), ([105], 5))
                self.assertIsNone(control_snapshot(self.gate, GEN))
                self.assertEqual(self.log[:2], [('gate_closed',), ('quit',)])
                resume.set()
                helper.join(2)
                self.assertFalse(helper.is_alive())
                self.living.remove(105)
                self.log.append(('helper_exit', self.now))
            else:
                original_wait(pids, seconds)
        helper.start()
        try:
            self.assertTrue(paced.wait(2))
            with patch.object(sessiond, 'wait_exit', side_effect=drain):
                self.run_teardown()
        finally:
            resume.set()
            helper.join(2)
        self.assert_safe_order()
        self.assertEqual((replies[0]['error'], replies[0]['frames_written']), ('gate_closed', 1))
        self.assertEqual(writes, [(1, 30, 0), (0, 0, 0)])
        protocol.frame_grab.assert_called_once_with('kbd')

    def test_ignored_quit_kills_helper_and_waits_before_xorg(self):
        self.behavior = 'ignore_quit'
        self.run_teardown()
        self.assert_safe_order()
        kill = self.log.index(('signal', 105, signal.SIGKILL))
        death = next(i for i, e in enumerate(self.log) if e[0] == 'helper_exit')
        self.assertLess(kill, death)
        self.assertGreaterEqual(sum(e[1] for e in self.log[:kill] if e[0] == 'sleep'), 5)

    def test_helper_never_dies_aborts_without_xorg_signal_or_registry_removal(self):
        self.behavior = 'immortal'
        with self.assertRaisesRegex(sessiond.UnsafeTeardown, 'helper is still alive; refusing to signal Xorg'):
            self.run_teardown()
        self.assertEqual([e for e in self.log if e[0] == 'signal'], [('signal', 105, signal.SIGKILL)])
        self.assertIn('s1', self.records)

    def test_guard_applies_to_every_xorg_signal(self):
        with patch.object(sessiond, 'alive', return_value=True), patch.object(sessiond.os, 'kill') as kill:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                with self.assertRaisesRegex(sessiond.UnsafeTeardown, 'refusing to signal Xorg'):
                    sessiond.send_signal(self.record, 101, sig)
            kill.assert_not_called()

    def test_guard_failure_at_signal_is_not_swallowed(self):
        # Simulate a last-moment failed liveness assertion in the signal path,
        # after the initial helper drain check has passed.
        with patch.object(sessiond, 'require_helper_dead', side_effect=[None, sessiond.UnsafeTeardown('refusing to signal Xorg')]):
            with self.assertRaisesRegex(sessiond.UnsafeTeardown, 'refusing to signal Xorg'):
                self.run_teardown()
        self.assertFalse(any(e[0] == 'signal' for e in self.log))
        self.assertIn('s1', self.records)

    def test_gate_write_failure_aborts_before_quit_or_signals(self):
        with patch('input_protocol.control_store', side_effect=OSError(errno.EIO, 'gate write failed')):
            with self.assertRaisesRegex(OSError, 'gate write failed'):
                self.run_teardown()
        self.assertFalse(self.log)
        self.assertIn('s1', self.records)

    def test_xvfb_order_and_timing_unchanged(self):
        self.record.update(profile='xvfb', mkdevs_pid=None)
        self.living.remove(105)
        self.run_teardown()
        term = [('signal', p, signal.SIGTERM) for p in (101, 102, 103, 104)]
        kill = [('signal', p, signal.SIGKILL) for p in (101, 102, 103, 104)]
        signals = [e for e in self.log if e[0] == 'signal']
        self.assertEqual(signals, term + term + kill)
        first_sweep = self.log.index(term[0], 4)
        self.assertAlmostEqual(sum(e[1] for e in self.log[:first_sweep] if e[0] == 'sleep'), 1)
        self.assertEqual(self.log[first_sweep + 4], ('sleep', 1))
        self.assertEqual(self.gate.read_bytes(), self.initial_page)

    def test_missing_or_bad_size_gate_is_already_closed(self):
        self.gate.unlink()
        close_control_gate(self.gate)
        self.assertFalse(self.gate.exists())
        for data in (b'', b'short', bytes(65)):
            self.gate.write_bytes(data)
            close_control_gate(self.gate)
            self.assertIsNone(control_snapshot(self.gate, GEN))
            self.assertEqual(self.gate.read_bytes(), data)

    def test_sessiond_and_worker_share_aligned_writer(self):
        import test_worker_w1
        self.assertIs(test_worker_w1.w.control_store, control_store)


if __name__ == '__main__':
    unittest.main()
