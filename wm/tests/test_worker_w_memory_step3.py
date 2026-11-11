"""Step 3 history floor: real caches/engine/framing, fake X only; no display."""
import copy
import json
import struct
from types import SimpleNamespace as NS
from unittest.mock import Mock

from test_worker_w2 import Harness, w, GEN
from test_worker_w4 import ProviderHarness


def churn(owner, count, start=20):
    template = dict(owner.cache.windows[10])
    handles = []
    for xid in range(start, start + count):
        owner.cache.upsert(xid, dict(template, xid=xid))
        owner.cache.commit()
        handle = owner.cache.handle(xid)
        owner.cache.changed.add('tree[' + handle + ']')
        owner.cache.remove(xid)
        owner.cache.commit()
        owner.prune_history()
        handles.append(handle)
    return handles


class HistoryFloor(Harness):
    def test_churn_beyond_production_floor_plateaus_all_history_maps_and_bytes(self):
        limit = self.w.cache.REMOVED_LIMIT
        sizes = []
        for batch in range(3):
            churn(self.w, limit, start=20 + batch * limit)
            c = self.w.cache
            self.assertEqual(self.w.memory_counters()['removed_windows'], limit)
            self.assertEqual(set(c.removed), set(c.tombstones))
            self.assertEqual(len(c.last_window), 1)
            self.assertLessEqual(sum(f.startswith('geometry[') for f in c.facets), limit + 1)
            self.assertTrue(all('geometry[' + h + ']' in c.facets for h in c.removed))
            self.assertFalse(any(f.startswith('tree[') for f in c.facets))
            full = self.w.observation(dict(channels=['state']))
            self.assertEqual(len(full['state']['removed']), limit)
            self.assertEqual(full['state']['removed_elided'], batch * limit)
            sizes.append(len(w.json_bytes(full)))
        # Handle digit widths change, but there is no linear history growth.
        self.assertLess(max(sizes) - min(sizes), 2 * limit)

    def test_below_floor_and_unknown_revisions_return_full_in_all_live_scopes(self):
        self.w.cache.REMOVED_LIMIT = 2
        churn(self.w, 5)
        c = self.w.cache
        self.assertGreater(c.history_floor, 1)
        for scope in ({'kind': 'session'}, {'kind': 'window', 'window': self.handle},
                      {'kind': 'family', 'window': self.handle}):
            for since in (None, 0, 1, c.history_floor - 1, c.rev + 1):
                result = self.w.observation(dict(scope=scope, since_rev=since, channels=['state']))
                self.assertTrue(result['full'])
                self.assertFalse(result['unchanged'])
                self.assertIn(self.handle, [v['handle'] for v in result['state']['windows']])
        delta = c.observe({'kind': 'session'}, c.history_floor)
        self.assertFalse(delta['full'])
        self.assertEqual(delta['state']['removed'], list(c.removed))
        self.assertTrue(c.observe({'kind': 'session'}, c.rev)['unchanged'])

    def test_same_revision_removal_boundary_does_not_claim_incomplete_delta(self):
        c = self.w.cache
        c.REMOVED_LIMIT = 2
        for xid in range(20, 25):
            c.upsert(xid, dict(c.windows[10], xid=xid))
        c.commit()
        before = c.rev
        for xid in range(20, 25):
            c.remove(xid)
        c.commit()
        self.w.prune_history()
        self.assertEqual(c.history_floor, c.rev)
        self.assertTrue(c.observe({'kind': 'session'}, before)['full'])
        self.assertTrue(c.observe({'kind': 'session'}, c.rev)['unchanged'])

    def test_observe_prunes_idle_history_and_baseline_resets_floor_and_count(self):
        c = self.w.cache
        c.REMOVED_LIMIT = 2
        with_owner = self.w.prune_history
        self.w.prune_history = Mock()
        churn(self.w, 5)
        self.assertEqual(len(c.removed), 5)
        self.w.prune_history = with_owner
        result = self.w.observation(dict(channels=['state'], since_rev=1))
        self.assertTrue(result['full'])
        self.assertEqual(len(c.removed), 2)
        self.assertEqual(result['state']['removed_elided'], 3)
        c.baseline()
        self.assertEqual((c.history_floor, c.removed_elided), (1, 0))
        self.assertFalse(c.removed)
        self.assertFalse(c.tombstones)

    def test_uncommitted_removal_cannot_advance_floor(self):
        c = self.w.cache
        c.REMOVED_LIMIT = 1
        churn(self.w, 1)
        c.upsert(30, dict(c.windows[10], xid=30))
        c.remove(30)
        self.w.prune_history()
        self.assertEqual(len(c.removed), 2)
        self.assertEqual(c.history_floor, 1)
        c.commit()
        self.w.prune_history()
        self.assertEqual(len(c.removed), 1)

    def test_below_floor_rpc_runs_full_observe_pointer_path(self):
        self.w.cache.REMOVED_LIMIT = 2
        churn(self.w, 5)
        self.peer.waits = []
        callbacks = []
        self.w.x = NS(root=1, ax=NS(ask=lambda cls, cb, **kw: callbacks.append(cb)))
        self.w.observe(self.peer, 7, dict(lease='lease', since_rev=1, channels=['state']))
        self.assertEqual(len(self.peer.waits), 1)
        self.assertTrue(self.peer.waits[0]['pointer_pending'])
        callbacks[0](dict(root_x=10, root_y=12))
        self.w.cache.commit()
        self.w.peers = [self.peer]
        self.w.pending()
        self.assertFalse(self.peer.waits)
        self.assertTrue(self.result()['full'])
        self.assertEqual(self.result()['state']['removed_elided'], 3)

    def test_full_response_with_floor_and_128_live_windows_fits_adapter(self):
        churn(self.w, self.w.cache.REMOVED_LIMIT + 200)
        template = dict(self.w.cache.windows[10])
        for xid in range(20000, 20127):
            self.w.cache.upsert(xid, dict(template, xid=xid, title='Live ' + 'x' * 512))
        self.w.cache.commit()
        result = self.w.observation(dict(channels=['state']))
        self.assertEqual(len(result['state']['windows']), 128)
        self.assertEqual(len(result['state']['removed']), 4096)
        self.assertEqual(result['state']['removed_elided'], 200)
        # Use the real response writer and envelope, not result-only encoding.
        self.peer.outgoing = bytearray()
        self.peer.sock = Mock()
        self.w.peers, self.w.selector = [self.peer], Mock()
        self.w.respond = w.Worker.respond.__get__(self.w)
        self.w.deliver_observation(self.peer, 'floor-size', {}, result)
        size = struct.unpack('!I', self.peer.outgoing[:4])[0]
        self.assertLess(size, w.MAX_FRAME - 48 * 1024)
        self.assertEqual(json.loads(self.peer.outgoing[4:])['result'], result)

    def test_oversized_observe_returns_bounded_error_without_reconciliation(self):
        self.peer.outgoing = bytearray()
        self.peer.sock = Mock()
        self.w.peers, self.w.selector = [self.peer], Mock()
        self.w.respond = w.Worker.respond.__get__(self.w)
        result = self.w.observation(dict(channels=['state']))
        result['state']['windows'][0]['title'] = 'x' * w.MAX_FRAME
        self.w.observed = Mock()
        self.w.deliver_observation(self.peer, 7, {}, result)
        size = struct.unpack('!I', self.peer.outgoing[:4])[0]
        self.assertLess(size, w.MAX_FRAME)
        value = json.loads(self.peer.outgoing[4:])
        self.assertEqual(value['error']['data'], dict(reason='response_too_large', detail='observe_frame_cap'))
        self.w.observed.assert_not_called()

    def test_history_crosses_floor_mid_act_and_reconciliation_completes(self):
        c = self.w.cache
        c.REMOVED_LIMIT = 2
        old = churn(self.w, 2)
        base = c.rev
        stamp = self.e.family_stamp(self.handle)
        self.t.ack = False
        self.submit(self.params([self.key()], base_rev=base))
        for _ in range(100):
            self.tick()
            if self.t.log:
                break
        self.assertIsNotNone(self.e.active)
        self.assertTrue(self.t.log, 'must cross floor after dispatch')
        active = self.e.active
        # Different family: eviction alone must not manufacture a reaction in
        # the original family's before/after stamp used for route learning.
        template = dict(c.windows[10])
        for xid in range(30, 35):
            c.upsert(xid, dict(template, xid=xid, family='unrelated'))
            c.commit()
            c.remove(xid)
            c.commit()
            self.w.prune_history()
            self.assertTrue(all(h in c.removed and h in c.tombstones for h in old))
            self.assertEqual(self.e.family_stamp(self.handle), stamp)
        snapshot = copy.deepcopy((c.removed, c.tombstones, c.facets, c.last_window))
        observed = self.w.observation(dict(channels=['state'], since_rev=base))
        self.assertTrue(observed['full'])
        self.assertEqual(len(observed['state']['removed']), 2)
        self.assertEqual(observed['state']['removed_elided'], 5)
        self.assertEqual((c.removed, c.tombstones, c.facets, c.last_window), snapshot)
        self.assertIs(self.e.active, active)
        self.t.ack = True
        for event in self.t.log:
            self.t.emit(event)
        self.drain()
        receipt = self.result()
        self.assertEqual(receipt['dispatch_status'], 'accepted')
        self.assertTrue(self.e.pending_effects)
        self.assertEqual(len(c.removed), 2)
        self.assertTrue(all(h not in c.tombstones for h in old))
        self.assertGreater(c.history_floor, base)
        full = self.w.observation(dict(channels=['state'], since_rev=base))
        self.w.deliver_observation(self.peer, 8, {}, full)
        self.assertFalse(self.e.pending_effects)
        recovered = self.e.get(GEN, 'a')
        self.assertTrue(any(row['type'] == 'reconciled' and row['action_id'] == 'a' for row in self.rows()))
        self.assertEqual(recovered['receipt'], receipt)


class FacetCleanup(ProviderHarness):
    def test_provider_prune_sweeps_tree_facet_on_unmap_before_tombstone_eviction(self):
        facet = 'tree[' + self.handle + ']'
        self.assertIn(facet, self.w.cache.facets)
        self.w.cache.upsert(10, dict(self.w.cache.windows[10], mapped=False))
        self.w.cache.commit()
        self.p.tick()
        self.assertNotIn(self.handle, self.p.cache.windows)
        self.assertNotIn(facet, self.w.cache.facets)
        self.assertFalse(self.w.cache.removed)

    def test_below_floor_full_observe_resets_tree_delta_baseline(self):
        self.w.cache.REMOVED_LIMIT = 2
        self.e.lease_state['lease']['trees'] = {self.handle: 1}
        churn(self.w, 5)
        result = self.w.observation(dict(channels=['tree'], since_rev=1))
        self.assertTrue(result['full'])
        self.assertTrue(result['tree']['full'])
        self.assertNotIn('delta', result['tree'])
