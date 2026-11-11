#!/usr/bin/python3
"""Single-owner AT-SPI and provider-death contracts; no display, bus or GI."""
import queue
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

import test_worker_w4 as h
import worker_atspi as atspi


class Calls:
    """Instrument properties, methods, registry calls, states and iterators."""
    def __init__(self):
        self.lock = threading.Lock()
        self.rows, self.overlaps = [], []

    def call(self, label, fn, *args):
        ident = threading.get_ident()
        self.rows.append((label, ident))
        if not self.lock.acquire(blocking=False):
            self.overlaps.append((label, ident))
            raise RuntimeError('concurrent libatspi entry')
        try:
            time.sleep(.0002)  # Release the GIL, like a real binding call.
            return self.wrap(fn(*args))
        finally:
            self.lock.release()

    def wrap(self, obj):
        if isinstance(obj, (NS, h.FakeAccessible, list)):
            return Proxy(self, obj)
        if callable(obj):
            return lambda *args: self.call(obj.__name__, obj, *args)
        return obj


class Proxy:
    def __init__(self, calls, obj):
        self._calls, self._obj = calls, obj

    def __getattr__(self, attr):
        return self._calls.call(attr, getattr, self._obj, attr)

    def __iter__(self):
        iterator = self._calls.call('__iter__', iter, self._obj)
        return Iterator(self._calls, iterator)


class Iterator:
    def __init__(self, calls, iterator):
        self.calls, self.iterator = calls, iterator

    def __iter__(self):
        return self

    def __next__(self):
        return self.calls.call('__next__', next, self.iterator)


class Loop:
    """Fake GLib sources and event delivery on the thread running main()."""
    def __init__(self):
        self.sources, self.events = [], queue.Queue()
        self.stopped = threading.Event()
        self.ident = None

    def run(self):
        self.ident = threading.get_ident()
        while not self.stopped.is_set():
            try:
                self.events.get_nowait()()
            except queue.Empty:
                pass
            for source in self.sources:
                source()
            self.stopped.wait(.001)

    def quit(self):
        self.stopped.set()


class Transport:
    def __init__(self):
        self.incoming, self.outgoing = queue.Queue(), queue.Queue()

    def pump(self):
        rows = []
        while True:
            try:
                rows.append(self.incoming.get_nowait())
            except queue.Empty:
                return rows

    def send(self, row):
        self.outgoing.put(row)

    def close(self):
        pass


class ChildPlumbing(unittest.TestCase):
    @contextmanager
    def child(self, api):
        loop, wire, failures = Loop(), Transport(), []
        glib = NS(MainLoop=lambda: loop, timeout_add=lambda ms, fn: loop.sources.append(fn))
        def run():
            try:
                atspi.main(123)
            except BaseException as exc:
                failures.append(exc)
        with patch.dict('sys.modules', {'gi': NS(require_version=Mock()), 'pyatspi': api,
                                      'gi.repository': NS(GLib=glib)}), \
             patch.dict('os.environ', AT_SPI_BUS_ADDRESS='fake', DBUS_SESSION_BUS_ADDRESS='fake'), \
             patch('worker_atspi.socket.socket'), patch('worker_atspi.Wire', return_value=wire):
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            try:
                self.assertEqual(wire.outgoing.get(timeout=2)['event'], 'ready')
                yield loop, wire
            finally:
                loop.quit()
                thread.join(2)
            self.assertFalse(thread.is_alive(), 'fake child did not exit')
            self.assertEqual(failures, [])

    def reply(self, wire, ids):
        replies = {}
        deadline = time.monotonic() + 4
        while set(replies) != set(ids):
            row = wire.outgoing.get(timeout=max(.001, deadline-time.monotonic()))
            if row.get('id') in ids and 'event' not in row:
                replies[row['id']] = row
        return replies

    def test_f5_all_accessible_entry_points_share_glib_thread_under_concurrent_load(self):
        calls, listeners = Calls(), []
        states = ['showing', 'visible', 'enabled', 'sensitive', 'editable']
        entry = h.FakeAccessible('/entry', role='text', states=states)
        entry.text = 'read me'
        frame = h.FakeAccessible('/frame', children=[entry], states=states)
        app = h.FakeAccessible('/app', children=[frame])
        api = h.fake_atspi([app])
        api.setTimeout = lambda *args: None
        api.Registry.registerEventListener = lambda callback, *args: listeners.append(callback)
        task = dict(op='snapshot', app='42', pid=42, window='w', rect=dict(x=100, y=100, w=100, h=50))
        with patch('worker_atspi.check_authority'), self.child(calls.wrap(api)) as (loop, wire):
            wire.incoming.put(dict(task, id=1))
            self.assertNotIn('error', self.reply(wire, [1])[1])
            barrier = threading.Barrier(5)
            jobs = [dict(task, id=2), dict(task, id=3, app='other', window='other'),
                    dict(task, id=4, op='probe'),
                    dict(task, id=5, op='invoke', key=':1.42:/entry')]
            # Concurrent producers obey the one-flight-per-app boundary.
            jobs[2]['app'], jobs[3]['app'] = 'probe-app', 'invoke-app'
            def submit(job):
                barrier.wait()
                wire.incoming.put(job)
                for _ in range(5):
                    loop.events.put(lambda: listeners[0](calls.wrap(NS(
                        source=entry, type='object:children-changed:remove', any_data=entry))))
            threads = [threading.Thread(target=submit, args=(job,)) for job in jobs]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join()
            replies = self.reply(wire, [2, 3, 4, 5])
            wire.incoming.put(dict(task, id=6, op='enumerate', key=':1.42:/frame'))
            replies.update(self.reply(wire, [6]))
            # Drain delivered events before checking the trace.
            delivered = threading.Event()
            loop.events.put(delivered.set)
            self.assertTrue(delivered.wait(2))
        self.assertEqual(calls.overlaps, [])
        self.assertEqual({ident for _, ident in calls.rows}, {loop.ident})
        self.assertTrue(all('error' not in row for row in replies.values()), replies)
        self.assertTrue(replies[5]['result']['accepted'])
        self.assertEqual(replies[6]['result']['items'][1]['name'], 'Fixture')
        for name in ('registerEventListener', 'getDesktop', 'clearCache', 'contains',
                     'getText', 'getSelection', 'doAction', 'bus_name', 'path', '__next__'):
            self.assertIn(name, [label for label, _ in calls.rows])

    def test_f5_blocked_glib_call_is_bounded_by_worker_flight_supervision(self):
        # The call stays blocked while the independent parent crosses 5 s.
        # An Event replaces a real five-second sleep, keeping the test fast.
        entered, release, callers = threading.Event(), threading.Event(), []
        def blocked(index):
            callers.append(threading.get_ident())
            entered.set()
            release.wait(3)
            return []
        api = h.fake_atspi()
        api.setTimeout = Mock()
        api.Registry.getDesktop = blocked
        api.Registry.registerEventListener = Mock()
        harness = h.ProviderHarness()
        harness.setUp()
        self.addCleanup(harness.tearDown)
        p = harness.p
        with self.child(api) as (loop, wire):
            try:
                def pump():
                    rows = []
                    while True:
                        try:
                            rows.append(wire.outgoing.get_nowait())
                        except queue.Empty:
                            return rows
                # Connect the real parent flight to the child job dispatcher.
                p.wire = NS(send=wire.incoming.put, pump=pump, close=lambda: None)
                p.call_progress = True
                task = p.request('snapshot', harness.handle)
                self.assertTrue(entered.wait(2))
                self.assertEqual(callers, [loop.ident])
                harness.later()
                harness.w.x.window_pids[11] = 202
                queued = p.request('snapshot', harness.w.cache.handle(11))
                self.assertIsNotNone(queued, 'submission blocked behind the GLib call')
                harness.now = task['started'] + 5.001
                p.tick()
                for submitted in (task, queued):
                    self.assertTrue(submitted['done'])
                    self.assertEqual(submitted['error'], 'provider_degraded')
                self.assertEqual(harness.child.code, -9)
                self.assertEqual(p.apps['101'].breaches, 0)
                self.assertEqual(p.apps['202'].breaches, 0)
            finally:
                release.set()


class CallTiming(unittest.TestCase):
    def test_f5_state_reads_keep_individual_call_timing_on_owner(self):
        h.BackendContracts.setUp(self)
        now, delayed = [10.0], []
        original = self.obj.getState
        def state():
            values = original()
            def contains(name):
                if not delayed:
                    delayed.append(True)
                    now[0] += .21
                return values.contains(name)
            return NS(contains=contains)
        self.obj.getState = state
        with patch('worker_atspi.time.monotonic', side_effect=lambda: now[0]):
            raw = self.b.snapshot(self.task)
        self.assertTrue(raw['breach'])
        self.assertAlmostEqual(raw['max_call_ms'], 210)


class ChildDeath(h.ProviderHarness):
    def test_f6_crash_keeps_restart_reason_until_successful_recovery(self):
        self.now += 2.1
        breaker = self.p.apps['101']
        # A prior app failure must not mask the provider restart afterwards.
        breaker.fail(self.now, 1.1)
        before = breaker.breaches
        self.child.code = -6
        self.p.tick()
        self.assertEqual(breaker.breaches, before)
        self.assertEqual(self.p.health()['atspi']['apps']['101']['reason'], 'provider_restarting')
        self.now = self.p.restart_at + .001
        self.p.tick()
        self.wire, self.child = self.children[-1]
        self.p.tick()
        health = self.p.health()['atspi']['apps']['101']
        self.assertEqual(health['reason'], 'provider_restarting')
        self.assertEqual(health['breaches'], before)
        task = next(iter(self.p.pending.values()))
        self.assertEqual(task['op'], 'snapshot')
        self.assertTrue(task['recovery'])
        self.answer(self.raw, task=task, max_call_ms=10, breach=False)
        self.assertEqual((breaker.state, breaker.reason, breaker.breaches), ('healthy', None, 0))
        self.assertEqual(self.p.probes[self.handle], 'usable')

    def test_f6_crash_with_healthy_inflight_app_adds_no_breach(self):
        task = self.p.request('snapshot', self.handle)
        self.now += 5.1  # Crash detection precedes job-age accounting.
        self.child.code = -6
        self.p.tick()
        self.assertEqual(self.p.apps['101'].breaches, 0)
        self.assertEqual(self.p.apps['101'].reason, 'provider_restarting')
        self.assertTrue(task['done'])
        self.assertEqual(task['error'], 'provider_degraded')


if __name__ == '__main__':
    unittest.main()
