"""Step-5 churn and genuine pressure regressions; fake X only, no display."""
import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock

from test_worker_w2 import Harness, w
from memory_step5_fix1_trace import ChurnX


class ChurnBounds(unittest.TestCase):
    def test_decorated_ten_window_batch_is_paced_and_drains_without_shedding(self):
        h = ChurnX()
        ids, events = h.batch(10, decorations=20)
        self.assertEqual(events, 630)
        self.assertFalse(h.x.budget.overloaded, h.stats())
        self.assertLess(h.x.budget.count, h.x.budget.limit)
        self.assertTrue(h.x.refreshing)  # queued work must block coverage
        h.drain()
        self.assertEqual(h.rejected, 0)
        self.assertEqual(h.completed, h.issued)
        self.assertEqual(h.x.budget.count, 0)
        self.assertFalse(h.x.refreshing or h.x.again or h.x.refresh_waiting)
        self.assertEqual(set(h.w.cache.windows), set(ids))
        self.assertTrue(all(h.w.cache.windows[i]['mapped'] for i in ids))
        self.assertLessEqual(h.peak, h.x.budget.limit // 2 + 1)

    def test_repeated_structure_events_share_one_root_query_per_batch(self):
        h = ChurnX()
        h.batch(10, repeats=60)
        # Ten ancestry queries plus one root discovery, not 1,800 root walks.
        self.assertEqual(h.by_kind['QueryTree'], 11)
        h.drain()
        self.assertFalse(h.rejected or h.x.budget.overloaded)

    def test_queued_refresh_samples_latest_change_without_redundant_again(self):
        h = ChurnX()
        ids, _ = h.batch(10, decorations=20)
        wid = ids[-1]
        self.assertIn(wid, h.x.refresh_waiting)
        h.windows[wid]['title'] = 'Changed while queued'
        for _ in range(100):
            h.x.refresh(wid)
        self.assertNotIn(wid, h.x.again)
        h.drain()
        self.assertEqual(h.w.cache.windows[wid]['title'], 'Changed while queued')
        self.assertFalse(h.rejected)

    def test_completed_batch_slots_are_released_before_callback_fanout(self):
        budget = w.XBudget()
        connection = Mock()
        connection.pending_events.return_value = False
        channel = w.AsyncX(connection, [], budget)
        issued, rejected = [], []
        def factory(**args):
            req = NS(_data=None, _error=None)
            issued.append(req)
            return req
        def fanout(data):
            for _ in range(2):
                rejected.append(not channel.ask(factory, lambda d: None))
        # All 2,000 replies complete together. The first 1,000 callbacks
        # generate 2,000 new requests; true outstanding never exceeds 2,000.
        for i in range(2000):
            self.assertTrue(channel.ask(factory, fanout if i < 1000 else lambda d: None))
        for req in issued:
            req._data = {}
        channel.pump()
        self.assertFalse(any(rejected))
        self.assertFalse(budget.overloaded)
        self.assertEqual(budget.count, 2000)
        self.assertEqual(len(issued), 4000)

    def test_genuine_two_connection_stall_still_rejects_before_construction(self):
        h = ChurnX(replies_per_turn=0)
        for i in range(h.x.budget.limit):
            channel = h.x.ax if i % 2 else h.x.inp_ax
            self.assertTrue(channel.ask(w.request.GetInputFocus, lambda d: None))
        self.assertFalse(h.x.ax.ask(w.request.GetInputFocus, lambda d: None))
        self.assertEqual(h.issued, 2048)
        self.assertEqual(h.x.budget.count, 2048)
        self.assertTrue(h.x.budget.overloaded)
        h.pump()
        self.assertEqual(h.x.budget.count, 2048)
        h.replies_per_turn = 128
        h.drain()
        self.assertEqual(h.completed, h.issued)
        self.assertEqual(h.x.budget.count, 0)

    def test_internal_refresh_flood_has_bounded_coalesced_backlog(self):
        h = ChurnX(replies_per_turn=0)
        for wid in range(2, 10002):
            h.windows[wid] = {}
            h.x.refresh(wid)
        self.assertEqual(len(h.x.refreshing), h.x.budget.limit)
        self.assertLessEqual(len(h.x.refresh_waiting), h.x.budget.limit)
        self.assertLessEqual(h.x.budget.count, h.x.budget.limit // 2)
        self.assertTrue(h.x.budget.overloaded)
        # Collection must recover even with unsent dirty-window IDs remaining.
        owner = w.Worker.__new__(w.Worker)
        owner.x, owner.x_errors = h.x, []
        owner.poll_control, owner.prune_history = Mock(), Mock()
        owner.cache = h.w.cache
        owner.log_due, owner.watchdog = float('inf'), float('inf')
        owner.resync_baseline = ('old',)
        h.replies_per_turn = 128
        for _ in range(100):
            owner.ingest()
            if not h.x.budget.overloaded:
                break
        self.assertFalse(h.x.budget.overloaded)
        self.assertFalse(h.x.refreshing or h.x.again or h.x.refresh_waiting)
        self.assertEqual(owner.watchdog, 0)
        self.assertIsNone(owner.resync_baseline)


class Workload(Harness):
    def setUp(self):
        super().setUp()
        self.peer = w.Peer(Mock())
        self.w.peers, self.w.lease_peer = {self.peer}, self.peer
        self.e.lease_state['lease']['peer'] = self.peer
        self.t.events = lambda events: None
        self.h = ChurnX(self.w)
        self.h.windows[10] = dict(title='Fixture')
        self.h.x.clients.add(10)
        self.h.x.focus_xid = self.h.x.input_focus = 10
        self.w.watchdog = float('inf')
        self.w.covered = lambda: w.Worker.covered(self.w)
        self.t.refresh = self.h.x.sample
        self.errors = []
        self.w.respond = lambda peer, ident, result=None, exc=None: (
            self.errors.append(exc.reason) if exc else self.replies.append((ident, result)))

    def tick(self):
        self.h.pump()
        super().tick()

    def observe(self):
        params = dict(lease='lease', channels=['state'])
        self.w.message(self.peer, json.dumps(dict(jsonrpc='2.0', id='obs',
                       method='window.observe', params=params)).encode())
        self.assertFalse(self.errors)
        self.h.drain()
        self.w.pending()
        self.assertFalse(self.errors or self.peer.waits)
        observation = self.replies[-1][1]
        self.assertTrue(observation['settled'])
        return observation

    def remove(self, ids):
        # Include WM child windows and preserve in-order unmap/destroy notices.
        removed = [wid for wid, row in self.h.windows.items()
                   if wid in ids or row.get('parent') in ids]
        for wid in removed:
            del self.h.windows[wid]
            self.h.x.clients.discard(wid)
        events = [NS(type=kind, window=wid) for wid in removed
                  for kind in (w.X.UnmapNotify, w.X.DestroyNotify)]
        self.h.x.events(events)
        self.observe()
        self.assertFalse(set(ids) & set(self.w.cache.windows))

    def test_step0_sized_churn_acts_and_popups_do_not_return_queue_full(self):
        for start in range(100, 300, 10):
            ids, _ = self.h.batch(start, decorations=20)
            observation = self.observe()  # RPC arrives with X work pending
            self.assertTrue(set(ids) <= {win['xid'] for win in self.w.cache.windows.values()})
            self.assertTrue(observation['coverage']['state']['complete'])
            self.remove(ids)
        for i in range(300):
            step = dict(intent=dict(kind='wait'), expect=dict(kind='window_present', window=self.handle))
            result = self.run_action(self.params([step], action_id=f'wait-{i}', base_rev=self.w.cache.rev))
            self.assertEqual(result['status'], 'success', result)
            self.assertNotEqual(result['reason'], 'queue_full')
        for start in range(400, 440, 10):
            ids, _ = self.h.batch(start, popup=True)
            self.observe()
            self.assertTrue(all(self.w.cache.windows[i]['type'] == 'popup' for i in ids))
            self.remove(ids)
        self.assertFalse(self.errors or self.h.rejected or self.h.x.budget.overloaded)
        self.assertEqual(self.h.issued, self.h.completed)
        self.assertEqual(self.h.x.budget.count, 0)
        self.assertEqual(self.w.memory_counters()['pending_x_requests'], 0)

    def test_memory_gauge_counts_detached_incomplete_work_during_callbacks(self):
        seen = []
        def completed(data):
            # A detached request on this channel and a sibling request have
            # not completed. This collected reply no longer occupies a slot.
            seen.append(self.w.memory_counters()['pending_x_requests'])
        self.h.x.ax.ask(w.request.GetInputFocus, completed)
        self.h.x.ax.ask(w.request.GetInputFocus, lambda data: None)
        self.h.x.inp_ax.ask(w.request.GetInputFocus, lambda data: None)
        self.h.replies_per_turn = 1
        self.h.x.ax.pump()
        self.assertEqual(seen, [2])
        self.h.drain()
        self.assertEqual(self.w.memory_counters()['pending_x_requests'], 0)

    def test_foreground_act_sample_succeeds_while_background_churn_is_pending(self):
        self.h.batch(100, decorations=20)
        self.assertFalse(self.w.covered())
        step = dict(intent=dict(kind='wait'), expect=dict(kind='window_present', match=dict(title='Fixture')))
        result = self.run_action(self.params([step], base_rev=self.w.cache.rev))
        self.assertEqual(result['status'], 'success', result)
        self.assertFalse(self.h.rejected or self.h.x.budget.overloaded)
        self.assertLessEqual(self.h.peak, self.h.x.budget.limit)
        self.h.drain()
