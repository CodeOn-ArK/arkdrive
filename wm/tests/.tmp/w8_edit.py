from pathlib import Path
p=Path('wm/worker_act.py');s=p.read_text()
s=s.replace('from pathlib import Path\n','from pathlib import Path\n\nfrom worker_routes import RouteTable, ladder\n')
s=s.replace('        self.evdev = None\n', '        self.evdev = None\n        from worker_paste import Clipboard\n        self.clipboard = Clipboard(worker)\n        self.routes = RouteTable(worker.state / "route-table.json", worker.generation)\n',1)
s=s.replace('"worker_keymap.py", "input_protocol.py"','"worker_keymap.py", "worker_routes.py", "worker_paste.py", "input_protocol.py"')
start=s.index('    def dispatch(self, a, s, r, handle, point):')
route_end=s.index('        # Resolve the entire macro',start)
s=s[:start]+'''    def route_context(self, s, handle):
        intent, kind = s['intent'], s['intent']['kind']
        win = self.window(handle) or {}
        tree = getattr(self.w, 'tree', None)
        raw = getattr(tree, 'raw', {}).get(handle, {})
        nodes = getattr(getattr(tree, 'cache', None), 'windows', {}).get(handle, {}).get('nodes', {})
        node = s.get('_node')
        if kind == 'text' and node is None:
            focused = [n for n in nodes.values() if 'focused' in n.get('states', []) and
                       'editable' in n.get('states', []) and 'disabled' not in n.get('states', [])]
            if len(focused) == 1:
                node = s['_fallback_node'] = dict(focused[0])
        app = win.get('app_build') or win.get('app') or win.get('class') or 'unknown'
        identity = dict(profile_hash=self.w.profile_hash, app_build=app,
                        toolkit=raw.get('toolkit', win.get('toolkit', 'unknown')),
                        surface_role=(node or {}).get('role', raw.get('role', win.get('type', 'unknown'))),
                        operation=kind, input_class='keyboard' if kind in ('key', 'text') else 'pointer')
        routes = ladder(self.w.record['profile'], kind, intent.get('via', 'auto'),
                        all(self.t.character(c) is not None for c in intent.get('text', '')),
                        len(intent.get('text', '')), node is not None and 'editable' in node.get('states', []),
                        isinstance(s.get('target'), dict) and 'ref' in s['target'] and
                        bool((node or {}).get('actions')) and not intent.get('modifiers') and intent.get('count', 1) == 1)
        requested = intent.get('route', 'auto')
        if requested not in ('auto', 'input'):
            routes = [route for route in routes if route == ('paste' if requested == 'clipboard' else requested)]
        apps = self.w.profile_data.get('applications', {})
        policy = apps.get(app, apps.get(win.get('class'), {})) if isinstance(apps, dict) else {}
        try:
            key = self.routes.entry(identity, routes, policy.get('synthetic_input') == 'rejected')
            # A new generation's successful physical selftest is recovery proof.
            for route in routes:
                state = self.routes.entries[key]['states'][route]
                healthy = (route == 'xtest' and self.channel['state'] == 'ok' or
                           route == 'evdev' and self.evdev and self.evdev.health['state'] == 'up')
                if state['state'] == 'down' and state.get('generation') != self.w.generation and healthy:
                    self.routes.flip(key, route, 'ok')
        except OSError as exc:
            raise ActError('worker_fault', 'route_table_unavailable') from exc
        return key, routes

    def route_flip(self, key, route, state):
        try:
            self.routes.flip(key, route, state)
        except OSError as exc:
            raise ActError('worker_fault', 'route_table_unavailable') from exc

    def route_exhausted(self, key, r, kind):
        if kind == 'invoke':
            raise ActError('provider_degraded')
        if self.routes.state(key, 'xtest') == 'rejected':
            r['escalate'] = 'relaunch_on_xorg' if self.w.record['profile'] == 'xvfb' else 'restart_session'
            raise ActError('backend_unsupported', 'xtest_rejected')
        raise ActError('backend_unsupported')

    @staticmethod
    def rung_attempt(route, groups, error, started, ended):
        if len(groups) == 1:
            result = groups[0]
            result['route'] = route
            return result
        statuses = [g['dispatch_status'] for g in groups]
        acked = [g['server_ack'] for g in groups if g.get('server_ack')]
        status = 'unknown' if 'unknown' in statuses else 'accepted' if 'accepted' in statuses else 'not_dispatched'
        return dict(route=route, started_ns=started, ended_ns=ended, dispatch_status=status,
                    accepted=True if status == 'accepted' else False if status == 'not_dispatched' else None,
                    written=sum(g.get('written', 0) for g in groups), expected=sum(g.get('expected', 0) for g in groups),
                    written_prefix=dict(events=sum(g.get('written', 0) for g in groups)),
                    server_ack=dict(expected=sum(v['expected'] for v in acked), seen=sum(v['seen'] for v in acked),
                                    source=acked[-1]['source']) if acked else None,
                    frames_written=sum(g.get('frames_written', 0) for g in groups),
                    provider_result=None, error=next((g['error'] for g in groups if g.get('error')), None),
                    reason=error.reason if error else None, input_groups=groups)

    def dispatch(self, a, s, r, handle, point):
        kind = s['intent']['kind']
        if kind == 'wait':
            return
        key, routes = self.route_context(s, handle)
        r['_route_key'], r['_routes'] = key, routes
        initial = self.routes.next(key, routes)
        if routes and initial != routes[0]:
            r.setdefault('route_change', dict(from_=routes[0], to=initial, reason='learned_route'))
            r['route_change']['from'] = r['route_change'].pop('from_', r['route_change'].get('from'))
        if initial is None:
            self.route_exhausted(key, r, kind)
        for route in routes[routes.index(initial):]:
            if self.routes.state(key, route) in ('down', 'rejected'):
                continue
            if r.get('_rung_count', 0) >= 3:
                break
            r['_rung_count'] = r.get('_rung_count', 0) + 1
            offset, started, error = len(r['attempts']), int(self.clock()*1e9), None
            r['route'] = route
            try:
                yield from self.dispatch_rung(a, s, r, handle, point, route)
            except ActError as exc:
                error = exc
            finally:
                groups = r['attempts'][offset:]
                attempt = self.rung_attempt(route, groups, error, started, int(self.clock()*1e9))
                r['attempts'][offset:] = [attempt]
                if route == 'paste':
                    attempt.update(clipboard_overwritten=r.get('clipboard_overwritten', False),
                                   selection_requested=r.get('selection_requested', False))
                    self.clipboard.attempt = attempt
                r['dispatch_status'] = self.aggregate(r['attempts'])
            if error is None:
                return
            status = attempt['dispatch_status']
            if status == 'unknown':
                if error.reason == 'input_unacknowledged':
                    self.route_flip(key, self.route, 'suspect')
                raise error
            if error.reason in ('channel_down', 'backend_unsupported', 'provider_degraded'):
                self.route_flip(key, route, 'down')
            # Only proof of zero delivery permits a non-idempotent replay.
            # Unknown and acknowledged absence of effect never change rungs.
            if (status != 'not_dispatched' or r['dispatch_status'] != 'not_dispatched' or
                    error.reason not in ('channel_down', 'backend_unsupported', 'provider_degraded')):
                raise error
            next_route = self.routes.next(key, routes[routes.index(route)+1:])
            if next_route is None or r['_rung_count'] >= 3:
                # Keep ordinary channel failures when no rejection escalation applies.
                if self.routes.state(key, 'xtest') == 'rejected':
                    self.route_exhausted(key, r, kind)
                raise error
            r['route_change'] = {'from': route, 'to': next_route, 'reason': error.reason}
            self.w.counters['route_changes'] += 1
        self.route_exhausted(key, r, kind)

    def dispatch_rung(self, a, s, r, handle, point, route):
        intent, k = s['intent'], s['intent']['kind']
        self.route = route if route in ('evdev', 'xtest') else 'xtest'
        if route == 'paste':
            key = r['_route_key']
            physical = ['evdev', 'xtest'] if self.w.record['profile'] == 'xorg' else ['xtest']
            self.route = next((v for v in physical if self.routes.state(key, v) not in ('rejected', 'down') and
                              (v == 'xtest' and self.channel['state'] == 'ok' or
                               v == 'evdev' and self.evdev and self.evdev.health['state'] == 'up')), None)
            if self.route is None:
                raise ActError('backend_unsupported', 'paste_input_unavailable')
        if self.route == 'evdev':
            if not self.evdev or self.evdev.health['state'] != 'up':
                raise ActError('channel_down')
            self.evdev.guard(intent)
            yield from self.wait_evdev(a)
            r['evdev_intent_epoch'] = self.evdev.map_epoch
            if not a.get('evdev_grab_checked'):
                a['evdev_grab_checked'] = True
                if not (yield from self.evdev.grab_check()):
                    raise ActError('channel_down')
''' + s[route_end:]
s=s.replace('        if k == "text":\n            text = [self.t.character(c)', '        if k == "text" and route not in ("paste", "atspi"):\n            text = [self.t.character(c)',1)
s=s.replace('        if route == "evdev":\n            all_codes', '        if self.route == "evdev":\n            all_codes',1)
s=s.replace('            if k == "text":\n                all_codes', '            if k == "text" and route != "paste":\n                all_codes',1)
s=s.replace('        requested = intent.get("route", "auto")\n        if requested not in ("auto", "input", route) or intent.get("via") == "paste":\n            raise ActError("backend_unsupported")\n', '',1)
s=s.replace('        if route == "xtest" and self.channel["state"] != "ok":','        if route not in ("atspi", "wm") and self.route == "xtest" and self.channel["state"] != "ok":',1)
a=s.index('        if k == "invoke":\n',s.index('    def dispatch_rung'))
b=s.index('        if k in ("key", "text"):',a)
semantic=s[a:b]
semantic=semantic.replace('        if k == "invoke":', '        if route == "atspi":\n            node = s.get("_node") or s.get("_fallback_node")\n            if node is None or k == "text" and "editable" not in node.get("states", []):\n                raise ActError("backend_unsupported")\n            node = yield from self.current_tree_ref(a, handle, node["ref"], r["rev"]["base"])\n            if k == "text" and "editable" not in node.get("states", []):\n                raise ActError("backend_unsupported")\n            yield from self.focus(a, r, handle)\n            self.route_snapshot(r, handle)\n            fields = dict(text=intent["text"]) if k == "text" else dict(action=intent.get("action"))')
semantic=semantic.replace('            task = self.w.tree.request("invoke", handle, key=s["_node"]["key"], action=intent.get("action"),\n                                       role=s["_node"]["role"], name=s["_node"].get("name", ""),', '            task = self.w.tree.request("set_text" if k == "text" else "invoke", handle, key=node["key"],\n                                       role=node["role"], name=node.get("name", ""), **fields,')
semantic=semantic.replace('            if task is None:\n                raise', '            r["attempts"].append(attempt)\n            if task is None:\n                raise')
semantic=semantic.replace('            r["attempts"].append(attempt)\n            r["route"]', '            r["route"]')
s=s[:a]+semantic+s[b:]
# paste uses the standard focus/target preparation, then owns clipboard and sends select all + ctrl+v
s=s.replace('            if k == "text" and s.get("_node"):', '            if k == "text" and route != "paste" and s.get("_node"):',1)
s=s.replace('        if k in ("focus_window", "close_window"):\n            self.gate(a)', '''        self.route_snapshot(r, handle)
        if route == 'paste':
            self.gate(a)
            self.arm(a, r)
            yield from self.clipboard.own(intent['text'], handle, lambda: self.gate(a), r)
            paste_codes = self.mapped_keys(['Control_L', 'v'])
            if intent['replace']:
                yield from self.send(a, r, self.key_events(self.mapped_keys(self.t.select_all(handle))))
            yield from self.send(a, r, self.key_events(paste_codes))
            return
        if k in ("focus_window", "close_window"):
            self.gate(a)''',1)
# Remove internal fields before durable receipt, do learning after verify before stop
s=s.replace('                    if a["preempted"]:\n                        raise ActError("preempted")\n                    if r["effect"]', '                    self.learn_route(r, handle)\n                    if a["preempted"]:\n                        raise ActError("preempted")\n                    if r["effect"]',1)
s=s.replace('            s.pop("windows_before", None)', '''            s.pop("windows_before", None)
            for field in ('_route_key', '_routes', '_rung_count', '_family_before', '_focus_dispatch'):
                s.pop(field, None)''',1)
p.write_text(s)
