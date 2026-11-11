"""Deterministic churn trace: real XState/AsyncX, fake replies, no display."""
from collections import Counter, deque
from types import SimpleNamespace as NS
from unittest.mock import Mock

from test_worker_w2 import w, GEN


class ChurnX:
    def __init__(self, worker=None, replies_per_turn=128):
        self.w = worker or NS(cache=w.StateCache('s1', GEN), x_errors=[], generation=GEN)
        self.w.x_errors = []
        self.x = x = w.XState.__new__(w.XState)
        x.w, x.root, x.ev = self.w, NS(id=1), Mock()
        x.ev.extension_event.DamageNotify = 90
        x.atoms = {name: i + 100 for i, name in enumerate(w.ATOM_NAMES)}
        x.clients, x.refreshing, x.again, x.incarnations = set(), set(), set(), {}
        x.selected_windows, x.uncovered, x.ancestors, x.ignored = set(), set(), {}, set()
        x.root_order, x.focus_xid, x.input_focus = [], 0, 0
        x.initialised, x.wm_ready = True, True
        x.app_build = lambda pid, identity: identity
        x.budget = w.XBudget()
        self.replies_per_turn = replies_per_turn
        self.wire = deque()
        self.issued = self.completed = self.rejected = self.peak = 0
        self.by_kind = Counter()
        self.windows = {}
        for name in ('ax', 'inp_ax'):
            connection = Mock()
            connection.pending_events = self.receive
            channel = w.AsyncX(connection, self.w.x_errors, x.budget)
            ask = channel.ask
            def traced(cls, callback, _ask=ask, **args):
                def factory(**kw):
                    req = NS(_data=None, _error=None)
                    self.wire.append((req, self.reply(cls, args)))
                    self.issued += 1
                    self.by_kind[cls.__name__] += 1
                    self.peak = max(self.peak, self.issued-self.completed)
                    return req
                result = _ask(factory, callback, **args)
                self.rejected += not result
                return result
            channel.ask = traced
            setattr(x, name, channel)
        self.w.x = x

    def reply(self, cls, args):
        x = self.x
        wid = w.xid(args.get('window', args.get('drawable', args.get('src_wid', 0))))
        row = self.windows.get(wid)
        if cls is w.request.QueryTree:
            return dict(parent=(row or {}).get('parent', 1), children=[i for i, v in self.windows.items()
                        if v.get('parent', 1) == wid])
        if cls is w.request.GetInputFocus:
            return dict(focus=x.focus_xid)
        if cls is w.request.QueryPointer:
            return dict(root_x=2, root_y=2)
        if cls is w.request.GetWindowAttributes:
            return dict(override_redirect=bool((row or {}).get('popup')), win_class=w.X.InputOutput,
                        map_state=w.X.IsViewable) if row else None
        if cls is w.request.GetGeometry:
            return dict(width=100, height=80) if row else None
        if cls is w.request.TranslateCoords:
            return dict(x=3, y=4) if row else None
        if cls is w.request.GetProperty:
            name = next(n for n, a in x.atoms.items() if a == args['property'])
            value = (32, [])
            if name == '_NET_CLIENT_LIST':
                value = (32, sorted(x.clients))
            elif name == '_NET_ACTIVE_WINDOW':
                value = (32, [x.focus_xid])
            elif row and name in ('_NET_WM_NAME', 'WM_NAME'):
                value = (8, row.get('title', 'Fixture').encode())
            elif row and name == 'WM_CLASS':
                value = (8, b'fixture\0Fixture\0')
            return dict(property_type=1, value=value, bytes_after=0)
        raise AssertionError(cls)

    def receive(self):
        for _ in range(min(self.replies_per_turn, len(self.wire))):
            req, data = self.wire.popleft()
            req._data = data
            # Optional BadWindow is represented by its normal callback(None).
            if data is None:
                req._error = w.error.BadWindow.__new__(w.error.BadWindow)
            self.completed += 1
        return False

    def pump(self):
        events = self.x.ax.pump()
        self.x.inp_ax.pump()
        self.x.events(events)
        self.w.cache.commit()

    def drain(self):
        for _ in range(10000):
            if not self.x.ax.pending and not self.x.inp_ax.pending:
                return
            self.pump()
        raise AssertionError('X did not drain')

    def batch(self, start, count=10, decorations=0, repeats=1, popup=False):
        ids = list(range(start, start+count))
        for wid in ids:
            self.windows[wid] = dict(title=f'Fixture {wid}', popup=popup)
            self.x.clients.add(wid)
            for i in range(decorations):
                self.windows[wid*100+i] = dict(parent=wid)
        events = [NS(type=kind, window=wid) for _ in range(repeats)
                  for wid in list(self.windows) for kind in
                  (w.X.MapNotify, w.X.ConfigureNotify, w.X.ReparentNotify)]
        self.x.events(events)
        return ids, len(events)

    def stats(self):
        return dict(issued=self.issued, completed=self.completed, tracked=self.x.budget.count,
                    completed_unreaped=self.x.budget.count-(self.issued-self.completed),
                    outstanding=self.issued-self.completed, rejected=self.rejected,
                    peak_outstanding=self.peak, overloaded=self.x.budget.overloaded,
                    kinds=dict(self.by_kind))


if __name__ == '__main__':
    import json
    for decorations, repeats in ((0, 1), (0, 60), (10, 1), (20, 1)):
        h = ChurnX()
        ids, events = h.batch(10, decorations=decorations, repeats=repeats)
        print(json.dumps(dict(decorations=decorations, repeats=repeats, events=events,
                              phase='issued', **h.stats())))
        h.drain()
        print(json.dumps(dict(decorations=decorations, repeats=repeats, phase='drained', **h.stats())))
