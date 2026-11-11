"""Step 1: durable action retirement with real JSONL, headless fake X only."""
import json
from types import SimpleNamespace as NS
from unittest.mock import patch

from test_worker_w2 import Harness, w, GEN, ActError
from worker_act import action_digest, canonical


class ActionRetirement(Harness):
    def assert_full(self, action_id='a'):
        self.assertIn((GEN, action_id), self.e.actions)
        self.assertNotIn(action_digest(action_id), self.e.action_stubs)

    def test_compacted_dedupe_is_identical_after_original_lease_is_gone(self):
        params = self.params(action_id='Unicode α')
        original = self.run_action(params)
        digest = action_digest(params['action_id'])
        self.assertFalse(self.e.actions)
        request_hash, offset = self.e.action_stubs[digest]
        self.assertEqual(request_hash, original['request_hash'])
        with self.w.journal.path.open('rb') as stream:
            stream.seek(offset)
            self.assertEqual(json.loads(stream.readline())['receipt'], original)
        log, rows = list(self.t.log), self.rows()
        self.w.lease = self.w.lease_peer = None
        self.e.lease_state.clear()
        with patch.object(self.w, 'require_lease', side_effect=AssertionError('dedupe must precede auth')):
            self.e.submit(NS(), 2, params)
        self.assertEqual(canonical(self.result()), canonical(original))
        self.assertEqual(self.t.log, log)
        self.assertEqual(self.rows(), rows)
        self.assertEqual(self.e.get(GEN, params['action_id'])['receipt'], original)

    def test_stub_ceiling_preserves_oldest_dedupe_and_conflict(self):
        self.e.ACTION_STUB_LIMIT = 2
        originals = []
        for i in range(6):
            params = self.params(action_id=f'a-{i}', base_rev=self.w.cache.rev)
            originals.append((params, self.run_action(params)))
        self.assertEqual(len(self.e.action_stubs), 2)
        self.assertNotIn(action_digest('a-0'), self.e.action_stubs)
        self.assertEqual(len(self.w.journal.index), 6)
        self.w.lease = self.w.lease_peer = None
        for params, original in originals:
            self.submit(params)
            self.assertEqual(self.result(), original)
            changed = dict(params, deadline_ms=9999)
            with self.assertRaisesRegex(ActError, 'action_id_conflict'):
                self.submit(changed)
        self.assertFalse(self.e.actions)

    def test_unknown_action_stays_full_even_after_observation(self):
        def change():
            self.later()
            self.w.cache.set('focus', self.w.cache.handle(11))
            self.w.cache.commit()
            self.w.cache.set('focus', self.handle)
            self.w.cache.commit()
        self.t.hook = change
        result = self.run_action(self.params([self.key()]))
        self.assertEqual(result['effect']['status'], 'unknown')
        self.assert_full()
        self.e.observed(self.peer, {}, dict(full=True, settled=True, rev=self.w.cache.rev))
        self.e.get(GEN, 'a')
        self.assert_full()

    def test_contradicted_action_stays_full_even_after_observation(self):
        def change():
            self.later()
            self.w.cache.upsert(12, dict(self.w.cache.windows[10], xid=12, title='Unrelated'))
            self.w.cache.set('focus', self.w.cache.handle(12))
            self.w.cache.commit()
        self.t.hook = change
        result = self.run_action(self.params([self.key()]))
        self.assertEqual(result['effect']['status'], 'contradicted')
        self.assert_full()
        self.e.observed(self.peer, {}, dict(full=True, settled=True, rev=self.w.cache.rev))
        self.e.get(GEN, 'a')
        self.assert_full()

    def test_pending_effect_keeps_full_record_and_reconciliation_works(self):
        original = self.run_action(self.params([self.key()]))
        self.assertTrue(self.e.pending_effects)
        self.assert_full()
        self.later()
        self.e.reconcile()
        self.assertFalse(self.e.pending_effects)
        ram = self.e.get(GEN, 'a')
        indexed = self.w.journal.action('a')
        historical = w.Journal.history_action(self.w.journal.path, 'a')
        self.assertEqual(ram, indexed)
        self.assertEqual(ram, historical)
        self.assertEqual(ram['receipt'], original)
        self.assertEqual(ram['reconciliation'], [dict(step_id='1', rev=self.w.cache.rev, effect='verified')])

    def test_successful_action_get_shape_matches_before_and_after_compaction(self):
        with patch.object(self.e, 'compact'):
            self.run_action()
            ram = self.e.get(GEN, 'a')
        self.e.compact(self.e.actions[(GEN, 'a')])
        self.assertFalse(self.e.actions)
        self.assertEqual(self.e.get(GEN, 'a'), ram)
        self.assertEqual(w.Journal.history_action(self.w.journal.path, 'a'), ram)

    def test_unfinished_action_never_compacts(self):
        self.submit()
        action = self.e.actions[(GEN, 'a')]
        self.e.compact(action)
        self.assert_full()
        self.assertEqual(self.e.get(GEN, 'a')['status'], 'pending')

    def test_incomplete_ack_and_unconfirmed_cleanup_never_compact(self):
        with patch.object(self.e, 'compact'):
            self.run_action()
        action = self.e.actions[(GEN, 'a')]
        action['acks'].append(('1', NS(complete=False)))
        self.e.compact(action)
        self.assert_full()
        action['acks'].pop()
        action['receipt']['steps'][0]['cleanup']['confirmed'] = False
        self.e.compact(action)
        self.assert_full()

    def test_receipt_fsync_failure_keeps_full_until_durable_repair(self):
        append = self.w.journal.append
        def append_with_failed_sync(kind, **fields):
            if kind == 'receipt':
                with patch.object(w.os, 'fsync', side_effect=OSError('disk full')):
                    return append(kind, **fields)
            return append(kind, **fields)
        with patch.object(self.w.journal, 'append', side_effect=append_with_failed_sync):
            self.run_action()
            self.assertEqual(self.result()['status'], 'pending')
            self.assert_full()
            self.assertIsNone(self.w.journal.entry('a').receipt)
        self.assertFalse(self.e.actions[(GEN, 'a')]['final'])
        repaired = self.e.get(GEN, 'a')
        self.assertEqual(repaired['status'], 'final')
        self.assertFalse(self.e.actions)
        self.submit()
        self.assertEqual(self.result(), repaired['receipt'])

    def test_intent_fsync_failure_reserves_full_record(self):
        with patch.object(w.os, 'fsync', side_effect=OSError('disk full')):
            with self.assertRaises(ActError):
                self.submit()
        self.assert_full()
        self.assertIsNone(self.w.journal.entry('a'))
        self.submit()
        self.assertEqual(self.result()['status'], 'pending')
        self.assertFalse(self.e.queue)

    def test_unreadable_compacted_receipt_never_redispatches(self):
        self.run_action()
        log = list(self.t.log)
        self.w.journal.path.unlink()
        with self.assertRaisesRegex(w.RPCError, 'history_expired'):
            self.submit()
        self.assertEqual(self.t.log, log)
        self.assertFalse(self.e.queue)

    def test_indexed_lookup_reads_one_row_independent_of_history_size(self):
        originals = []
        for i in range(12):
            params = self.params(action_id=f'α-{i}', base_rev=self.w.cache.rev)
            originals.append(self.run_action(params))
        real_loads = w.json.loads
        with patch.object(w.json, 'loads', wraps=real_loads) as loads:
            self.assertEqual(self.w.journal.action('α-0')['receipt'], originals[0])
            self.assertEqual(loads.call_count, 1)
            self.assertEqual(self.w.journal.action('missing')['status'], 'not_found')
            self.assertEqual(loads.call_count, 1)

    def test_existing_current_generation_reindexes_unicode_offsets_once(self):
        original = self.run_action(self.params(action_id='α'))
        journal = w.Journal(self.w.journal.path, 's1', GEN)
        self.addCleanup(journal.close)
        self.assertEqual(journal.action('α')['receipt'], original)
        self.assertEqual(journal.entry('α').receipt, self.w.journal.entry('α').receipt)

    def test_invalid_index_fails_closed_instead_of_missing_dedupe(self):
        self.run_action()
        self.w.journal.index_error = True
        with self.assertRaisesRegex(w.RPCError, 'journal_unavailable'):
            self.submit()
        self.assertFalse(self.e.queue)

    def test_bad_receipt_offset_fails_closed_instead_of_redispatching(self):
        self.run_action()
        digest = action_digest('a')
        request_hash, offset = self.e.action_stubs[digest]
        self.e.action_stubs[digest] = (request_hash, offset + 1)
        with self.assertRaisesRegex(w.RPCError, 'journal_unavailable'):
            self.submit()
        self.assertFalse(self.e.queue)

    def test_headless_act_churn_full_records_and_stubs_plateau(self):
        self.w.pixels = self.w.tree = self.w.x = None
        self.e.ACTION_STUB_LIMIT = 8
        samples = []
        for i in range(128):
            step = dict(intent=dict(kind='wait'), expect=dict(kind='window_present', window=self.handle))
            result = self.run_action(self.params([step], action_id=f'churn-{i}', base_rev=self.w.cache.rev))
            self.assertEqual(result['status'], 'success')
            if (i + 1) % 16 == 0:
                samples.append(self.w.memory_counters())
        self.assertTrue(all(s['actions_retained'] == 0 and s['action_stubs'] == 8 for s in samples))
        self.assertEqual([s['journal_actions_indexed'] for s in samples], list(range(16, 129, 16)))
        self.assertEqual(self.w.counters['batches'], 128)
