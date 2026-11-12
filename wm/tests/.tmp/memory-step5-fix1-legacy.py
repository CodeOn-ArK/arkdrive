# Original step-5 methods, transcribed from the pre-edit read. Used only for
# regression replay; no production file is rewritten by this runner.
def pump(self):
    events = []
    while self.connection.pending_events():
        events.append(self.connection.next_event())
    pending, self.pending = self.pending, []
    for req, callback, optional, error_callback in pending:
        if req._error is not None or req._data is not None:
            self.budget.count -= 1
        if req._error is not None:
            if error_callback is not None:
                error_callback(req._error)
            elif optional and isinstance(req._error, (error.BadWindow, error.BadDrawable)):
                callback(None)
            else:
                raise req._error
        elif req._data is not None:
            callback(req._data)
        else:
            self.pending.append((req, callback, optional, error_callback))
    return events


def refresh(self, wid):
    if not wid or wid == self.root.id or wid in getattr(self, "ignored", set()):
        return
    if wid in self.refreshing:
        self.again.add(wid)
        return
    budget = getattr(self, 'budget', None)
    if budget and (budget.overloaded or budget.count > budget.limit - 20):
        budget.overloaded = True
        return
    self.refreshing.add(wid)
    def finished():
        self.refreshing.discard(wid)
        if wid in self.again:
            self.again.remove(wid)
            self.refresh(wid)
    self.read_window(wid, finished)
