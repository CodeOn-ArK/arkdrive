#!/usr/bin/python3
"""Callback starvation regressions with fake bindings/GLib/IPC only."""
import io
import queue
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

import test_worker_w4 as h
import worker_atspi as atspi


class GuardBackend(atspi.Backend):
    """Record forbidden entries even if the listener contains the exception."""
    def __init__(self, api):
        super().__init__(api)
        self.in_listener = False
        self.forbidden = []

    def changed(self, event, results):
        previous, self.in_listener = self.in_listener, True
        try:
            return super().changed(event, results)
        finally:
            self.in_listener = previous

    def read(self, *args):
        if self.in_listener:
            self.forbidden.append('read')
            raise AssertionError('binding read in callback')
        return super().read(*args)

    def call(self, *args):
        if self.in_listener:
            self.forbidden.append('call')
            raise AssertionError('binding call in callback')
        return super().call(*args)


class LocalSource:
    """app/bus/path are local; every other accessible lookup is forbidden."""
    def __init__(self, accesses, app=None):
        self.accesses, self.app = accesses, app

    def __getattribute__(self, name):
        if name not in ('accesses', '__dict__', '__class__'):
            accesses = object.__getattribute__(self, 'accesses')
            accesses.append(name)
            if name not in ('app', 'bus_name', 'path'):
                raise AssertionError('remote accessible lookup: '+name)
        return object.__getattribute__(self, name)


class Listener(unittest.TestCase):
    def setUp(self):
        h.BackendContracts.setUp(self)
        self.b = GuardBackend(self.b.a)
        self.results = queue.Queue()
        self.now = 100.0
        self.clock = patch('worker_atspi.time.monotonic', side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def test_app_less_stream_never_enters_read_or_call_and_coalesces(self):
        for app in (None, NS(bus_name=None), NS(bus_name=':unregistered')):
            for _ in range(1000):
                self.b.changed(NS(source=NS(app=app), type='object:text-changed'), self.results)
        self.assertEqual(self.b.forbidden, [])
        self.assertEqual(self.b.event_drops, 0)
        self.assertEqual(self.b.global_version, 3000)
        self.assertTrue(self.results.empty())
        self.assertEqual(self.b.global_due, 100.04)
        self.now += .041
        self.b.flush_events(self.results)
        self.assertEqual(self.results.get_nowait(), dict(event='changed', global_invalidation=True))
        self.assertTrue(self.results.empty())

    def test_local_source_and_removed_identity_keep_targeted_invalidation_without_calls(self):
        self.b.bus_apps.update({':1.42': '42', ':other': 'other'})
        for source, removed, apps in (
                (self.obj, self.obj, ['42']),
                (NS(app=None), self.obj, ['42']),
                (self.obj, NS(app=NS(bus_name=':other'), path='/gone'), ['42', 'other'])):
            with self.subTest(apps=apps):
                self.b.changed(NS(source=source, type='object:children-changed:remove',
                                  any_data=removed), self.results)
                rows = [self.results.get_nowait() for _ in apps]
                self.assertCountEqual([row['app'] for row in rows], apps)
                self.assertEqual(next(row for row in rows if 'removed' in row)['removed'],
                                 removed.app.bus_name+':'+removed.path)
        self.assertEqual(self.b.forbidden, [])
        self.assertIsNone(self.b.global_due)

    def test_removal_without_local_owner_uses_global_without_parent_lookup(self):
        accesses = []
        with patch('sys.stderr', new_callable=io.StringIO):
            self.b.changed(NS(source=LocalSource(accesses), type='object:children-changed:remove',
                              any_data=LocalSource(accesses)), self.results)
        self.assertEqual(self.b.forbidden, [])
        self.assertNotIn('parent', accesses)
        self.assertLessEqual(len(accesses), 6)
        self.assertEqual(self.b.global_version, 1)
        self.now += .041
        self.b.flush_events(self.results)
        self.assertEqual(self.results.get_nowait(), dict(event='changed', global_invalidation=True))

    def test_registered_listener_flood_has_constant_work_and_pump_completes_snapshot(self):
        wire, callbacks, listeners, counts = h.FakeWire(), [], [], []
        api = self.b.a
        api.setTimeout = Mock()
        api.Registry.registerEventListener = lambda cb, *args: listeners.append(cb)
        original = self.b.call
        def timed_call(*args):
            # Give every fake binding operation a 1 ms simulated cost so the
            # normal job/walk budget is exercised with an advancing clock.
            self.now += .001
            return original(*args)
        def run():
            accesses = []
            for i in range(5000):
                source = LocalSource(accesses, app=NS(bus_name=':unknown') if i % 2 else None)
                before = len(accesses)
                listeners[0](NS(source=source, type='object:text-changed:insert'))
                counts.append(len(accesses)-before)
            # Submit through the normal IPC source after the listener burst.
            wire.incoming.append(self.task)
            self.now += .041
            self.assertTrue(callbacks[0]())
            self.assertEqual(self.b.forbidden, [])
            self.assertNotIn('parent', accesses)
            self.assertLessEqual(max(counts), 2)
            self.assertEqual(len(set(counts)), 1)
        glib = NS(MainLoop=lambda: NS(run=run), timeout_add=lambda ms, cb: callbacks.append(cb))
        with patch.dict('sys.modules', {'gi': NS(require_version=Mock()), 'pyatspi': api,
                                      'gi.repository': NS(GLib=glib)}), \
             patch.dict('os.environ', AT_SPI_BUS_ADDRESS='fake', DBUS_SESSION_BUS_ADDRESS='fake'), \
             patch('worker_atspi.socket.socket'), patch('worker_atspi.Wire', return_value=wire), \
             patch('worker_atspi.Backend', return_value=self.b), \
             patch.object(self.b, 'call', side_effect=timed_call):
            atspi.main(123)
        replies = [row for row in wire.sent if row.get('id') == self.task['id']]
        self.assertEqual(len(replies), 1)
        self.assertNotIn('error', replies[0])
        self.assertEqual(replies[0]['result']['children'][0]['name'], 'Create Folder')
        self.assertGreater(replies[0]['elapsed_ms'], 0)
        self.assertLess(replies[0]['elapsed_ms'], 2000)
        self.assertFalse(replies[0]['breach'])
        self.assertEqual(self.b.global_version, 5000)
        self.assertEqual(wire.sent.count(dict(event='changed', global_invalidation=True)), 1)
        self.assertEqual(len(wire.sent), 3)  # ready, snapshot, coalesced invalidation

    def test_reentrant_flood_during_binding_call_returns_and_preserves_dirty_snapshot(self):
        original = self.obj.getState
        def state():
            for _ in range(2000):
                self.b.changed(NS(source=NS(app=None), type='object:text-changed'), self.results)
            return original()
        self.obj.getState = state
        raw = self.b.snapshot(self.task)
        self.assertEqual(self.b.forbidden, [])
        self.assertTrue(raw['dirty'])
        self.assertEqual(raw['children'][0]['name'], 'Create Folder')
        self.assertEqual(self.b.global_version, 2000)
        self.assertTrue(self.results.empty())


if __name__ == '__main__':
    unittest.main()
