#!/usr/bin/python3
"""Unrealized-row rendering contracts using fake snapshots/accessibles only."""
import copy
import unittest

import test_worker_w4 as h


UNSET = -2147483648


def snapshot(bounds=(UNSET, UNSET, 201, 26)):
    child = h.node('virtual-child', 'text', 'Unrealized child', children=[
        h.node('virtual-grandchild', 'push_button', 'Unrealized action')])
    row = h.node('virtual', 'table_cell', 'w4r13', children=[child])
    row['bounds'] = list(bounds)
    return h.node(children=[row, h.node('normal', 'table_cell', 'w4r12')])


class UnrealizedRows(h.ProviderHarness):
    def page(self):
        return self.w.observation(dict(channels=['tree'], scope=dict(
            kind='window', window=self.handle)))['tree']

    def assert_served_refs_resolve(self, page):
        for line in page['lines']:
            ref = line.lstrip().split(' ', 1)[0]
            resolved = self.p.resolve(self.handle, ref, completed=True)
            self.assertEqual(resolved['ref'], ref)
            self.assertEqual(self.p.cache.identities[ref],
                             (resolved['key'], resolved['role'], resolved['name']))

    def test_sentinel_subtree_is_not_served_and_normal_ref_survives(self):
        raw = snapshot()
        self.change(h.node(children=[copy.deepcopy(raw['children'][1])]))
        normal_ref = self.refs()['normal']
        for bounds in ((UNSET, UNSET, 201, 26), (UNSET, 100, 201, 26),
                       (100, UNSET, 201, 26)):
            with self.subTest(bounds=bounds):
                self.change(snapshot(bounds))
                page = self.page()
                self.assertEqual(set(self.refs()), {'root', 'normal'})
                self.assertEqual(self.refs()['normal'], normal_ref)
                self.assertEqual(page['lines'], [
                    self.refs()['root'] + ' frame "Fixture" @100,100,100,50',
                    ' ' + normal_ref + ' table_cell "w4r12" @100,100,100,50'])
                self.assert_served_refs_resolve(page)

    def test_negative_real_coordinates_still_render(self):
        for bounds in ((-40, 100, 201, 26), (100, -40, 201, 26),
                       (-40, -50, 201, 26), (UNSET + 1, 100, 201, 26),
                       (100, UNSET + 1, 201, 26)):
            with self.subTest(bounds=bounds):
                self.change(snapshot(bounds))
                page = self.page()
                self.assertEqual(set(self.refs()), {
                    'root', 'virtual', 'virtual-child', 'virtual-grandchild', 'normal'})
                self.assertIn(' @' + ','.join(map(str, bounds)),
                              next(line for line in page['lines'] if '"w4r13"' in line))
                self.assert_served_refs_resolve(page)

    def test_complete_snapshot_inventory_still_counts_sentinel_subtree(self):
        self.change(snapshot())
        self.assertEqual({item['key'] for item in
                          self.p.cache.windows[self.handle]['enumeration']},
                         {'root', 'virtual', 'virtual-child', 'virtual-grandchild', 'normal'})
        for name, role in (('w4r13', 'table_cell'), ('Unrealized child', 'text'),
                           ('Unrealized action', 'push_button')):
            with self.subTest(name=name):
                query = dict(window=self.handle, container=self.refs()['root'],
                             name=name, role=role)
                self.assertIs(self.p.evaluate(dict(query, kind='item_present')), True)
                self.assertIs(self.p.evaluate(dict(query, kind='item_absent')), False)
        self.assertIs(self.p.evaluate(dict(kind='item_absent', window=self.handle,
                      container=self.refs()['root'], name='Missing')), True)

    def test_on_demand_inventory_keeps_unrealized_items_from_same_backend_snapshot(self):
        states = ['showing', 'visible', 'enabled', 'sensitive']
        child = h.FakeAccessible('/child', role='text', name='Unrealized child', states=states)
        row = h.FakeAccessible('/virtual', role='table cell', name='w4r13',
                               children=[child], states=states, bounds=(UNSET, UNSET, 201, 26))
        normal = h.FakeAccessible('/normal', role='table cell', name='w4r12', states=states)
        frame = h.FakeAccessible('/frame', children=[row, normal], states=states)
        app = h.FakeAccessible('/app', children=[frame])
        backend = h.Backend(h.fake_atspi([app]))
        raw = backend.snapshot(dict(app='101', window=self.handle, pid=42,
                                    rect=dict(x=100, y=100, w=100, h=50)))
        self.change(raw)
        page = self.page()
        self.assertEqual(set(self.refs()), {':1.42:/frame', ':1.42:/normal'})
        self.assert_served_refs_resolve(page)
        query = dict(window=self.handle, container=self.refs()[':1.42:/frame'],
                     name='w4r13', role='table_cell')
        self.assertIsNone(self.p.evaluate(dict(query, kind='item_present')))
        task = next(iter(self.p.pending.values()))
        self.assertEqual(task['op'], 'enumerate')
        inventory = backend.enumerate(task)
        self.assertEqual({item['name'] for item in inventory['items']},
                         {'Fixture', 'w4r13', 'Unrealized child', 'w4r12'})
        self.answer(inventory, task=task)
        self.assertIs(self.p.evaluate(dict(query, kind='item_present')), True)
        self.assertIs(self.p.evaluate(dict(query, kind='item_absent')), False)

    def test_unrealized_transition_removes_subtree_from_delta(self):
        self.change(snapshot((100, 100, 201, 26)))
        before = self.page()
        refs = self.refs()
        self.change(snapshot())
        page = self.p.cache.page(self.handle, since=before['rev'])
        self.assertEqual(set(page['delta']['removed']), {
            refs['virtual'], refs['virtual-child'], refs['virtual-grandchild']})
        self.assertEqual(page['delta']['added'], [])
        self.assertEqual(page['delta']['changed'], [])
        self.assertEqual(self.refs()['normal'], refs['normal'])
        self.assert_served_refs_resolve(self.page())


if __name__ == '__main__':
    unittest.main()
