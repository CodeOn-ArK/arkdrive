"""One lazy, unmapped CLIPBOARD owner on the worker's session ev connection.

All replies are handled by the owner event loop; there are no helper commands,
threads, host clipboard access, or synchronous X round trips.
"""
from Xlib import X, Xatom
from Xlib.protocol import event, request

from worker_act import ActError

ATOMS = ('CLIPBOARD', 'UTF8_STRING', 'STRING', 'TARGETS')


class Clipboard:
    def __init__(self, worker):
        self.w = worker
        self.window = None
        self.health = dict(state='unavailable', reason='not_created')
        self.text = ''
        self.attempt = self.step_evidence = None
        self.authority = None
        self.target = None

    def x_error(self, exc, req):
        self.health = dict(state='down', reason='clipboard_x_error:'+type(exc).__name__)
        return 1

    def own(self, text, target, authority, attempt):
        x = self.w.x
        if self.health['state'] == 'down':
            self.health = dict(state='unavailable', reason='taking_ownership')
        try:
            if self.window is None:
                self.w.poll_control(force=True)
                authority()
                self.window = x.root.create_window(0, 0, 1, 1, 0, X.CopyFromParent,
                                                   X.InputOutput, X.CopyFromParent, onerror=self.x_error)
                x.ignored.add(self.window.id)
                self.health = dict(state='ok')
            self.w.poll_control(force=True)
            authority()  # Immediately before SetSelectionOwner, including reuse.
            self.window.set_selection_owner(x.atoms['CLIPBOARD'], X.CurrentTime, onerror=self.x_error)
            self.text, self.target, self.authority, self.attempt = text, target, authority, attempt
            self.step_evidence = attempt
            attempt.update(clipboard_overwritten=True, selection_requested=False)
            result = []
            x.ax.ask(request.GetSelectionOwner, lambda value: result.append(value['owner']),
                     selection=x.atoms['CLIPBOARD'])
            while not result:
                authority()
                yield
            owner = getattr(result[0], 'id', result[0])
            if self.health['state'] == 'down' and self.health.get('reason', '').startswith('clipboard_x_error:'):
                raise RuntimeError(self.health['reason'])
            if owner != self.window.id:
                raise RuntimeError('clipboard_ownership_failed')
            self.health = dict(state='ok')
        except (OSError, RuntimeError) as exc:
            self.health = dict(state='down', reason=str(exc))
            raise ActError('channel_down', str(exc)) from exc

    def events(self, events):
        if self.window is None:
            return
        x = self.w.x
        for e in events:
            if e.type == X.SelectionClear and getattr(e, 'selection', None) == x.atoms['CLIPBOARD']:
                self.health = dict(state='down', reason='selection_lost')
            if e.type != X.SelectionRequest or e.selection != x.atoms['CLIPBOARD']:
                continue
            if getattr(e.owner, 'id', e.owner) != self.window.id:
                continue
            prop = e.property or e.target
            requestor = x.ev.create_resource_object('window', getattr(e.requestor, 'id', e.requestor))
            win = self.w.act.window(self.target)
            # XIDs from one client share their resource base, including its
            # private selection transfer window, which need not be mapped.
            mask = x.ev.display.info.resource_id_mask
            if self.attempt is not None and win and requestor.id & ~mask == win['xid'] & ~mask:
                self.attempt['selection_requested'] = True
                if self.step_evidence is not None:
                    self.step_evidence['selection_requested'] = True
            try:
                self.w.poll_control(force=True)
                self.authority()
                if e.target == x.atoms['TARGETS']:
                    requestor.change_property(prop, Xatom.ATOM, 32, [x.atoms[a] for a in ATOMS[1:]])
                elif e.target in (x.atoms['UTF8_STRING'], x.atoms['STRING']):
                    encoding = 'utf-8' if e.target == x.atoms['UTF8_STRING'] else 'latin-1'
                    requestor.change_property(prop, e.target, 8, self.text.encode(encoding, errors='replace'))
                else:
                    prop = X.NONE
            except ActError:
                continue
            self.w.poll_control(force=True)
            try:
                self.authority()
            except ActError:
                continue
            requestor.send_event(event.SelectionNotify(time=e.time, requestor=requestor.id,
                                  selection=e.selection, target=e.target, property=prop), event_mask=0)
