"""Pure presenter layout/hit testing. No session X focus or lifecycle calls."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Transform:
    x: int
    y: int
    w: int
    h: int
    native_w: int
    native_h: int

    def contains(self, x, y):
        return self.x <= x < self.x + self.w and self.y <= y < self.y + self.h

    def native(self, x, y):
        # Clamp a pinned drag at the original session edge, never retarget.
        return (max(0, min(self.native_w-1, int((x-self.x)*self.native_w/self.w))),
                max(0, min(self.native_h-1, int((y-self.y)*self.native_h/self.h))))


class Scene:
    chrome = 28

    def __init__(self):
        self.records, self.frames, self.transforms = {}, {}, {}
        self.collapsed = set()
        self.root = self.selected = None
        self.version = 1
        self.width, self.height = 1100, 760
        self.placements = {}
        self.chips = []
        self.chip_offset = 0
        self.chip_top = self.height-32
        self.chip_columns, self.chip_capacity = 1, 1
        self.drag = None
        self.drag_buttons = set()
        self.layout_builds = 0

    def registry(self, records):
        records = {r['incarnation']: r for r in records if r.get('lifecycle', 'ready') == 'ready'}
        old = {i: r.get('parent_incarnation') for i, r in self.records.items()}
        new = {i: r.get('parent_incarnation') for i, r in records.items()}
        self.records = records
        if self.root not in records:
            self.root = next((i for i, r in records.items() if r.get('parent_incarnation') is None), next(iter(records), None))
        if self.selected not in records:
            self.selected = self.root
        if old != new:
            self.relayout()

    def frame(self, incarnation, frame):
        if incarnation not in self.records:
            return
        previous = self.frames.get(incarnation)
        self.frames[incarnation] = frame
        old = previous and previous.get('frame', {}).get('rect')
        rect = frame.get('frame', {}).get('rect')
        if old != rect:
            self.relayout()
        # No transform construction for ordinary pixel/cursor/modal updates.

    def children(self, incarnation):
        return sorted(i for i, r in self.records.items() if r.get('parent_incarnation') == incarnation)

    def relayout(self):
        self.version += 1
        self.layout_builds += 1
        self.transforms = {}
        self.chips = []
        if self.root is None:
            return
        def surface(i, x, y, w, h):
            rect = self.frames.get(i, {}).get('frame', {}).get('rect', dict(w=1024, h=768))
            # Fit once per placement change. Native session resolution is fixed.
            scale = min(w/rect['w'], h/rect['h'])
            dw, dh = max(1, int(rect['w']*scale)), max(1, int(rect['h']*scale))
            self.transforms[i] = Transform(x, y, dw, dh, rect['w'], rect['h'])
        children = self.children(self.root)
        for child in children:
            if child in self.collapsed:
                self.chips.append(child)
            todo = self.children(child)
            while todo:
                item = todo.pop(0)
                self.chips.append(item)
                todo.extend(self.children(item))
        # Other root sessions stay reachable without widening the live budget.
        self.chips.extend(i for i, r in sorted(self.records.items())
                          if r.get('parent_incarnation') is None and i != self.root and i not in self.chips)
        self.chip_columns = max(1, (self.width-16)//200)
        rows = min(max(1, self.height//2//24), (len(self.chips)+self.chip_columns-1)//self.chip_columns)
        self.chip_capacity = max(1, rows*self.chip_columns)
        self.chip_offset = min(self.chip_offset, max(0, len(self.chips)-self.chip_capacity))
        self.chip_top = self.height-max(32, rows*24+8)
        surface(self.root, 8, 64, max(1, self.width-16), max(1, self.chip_top-72))
        parent = self.transforms[self.root]
        for index, child in enumerate(children):
            if child not in self.collapsed:
                default = (parent.x + parent.w//2 + (index % 2)*12,
                           parent.y + self.chrome + (index//2)*max(60, parent.h//3),
                           max(40, parent.w//2-24), max(40, parent.h//3-self.chrome))
                x, y, w, h = self.placements.get(child, default)
                w, h = min(w, parent.w), min(h, max(1, parent.h-self.chrome))
                x = max(parent.x, min(x, parent.x+parent.w-w))
                y = max(parent.y+self.chrome, min(y, parent.y+parent.h-h))
                surface(child, x, y, w, h)

    def chip_views(self):
        return [(8+(index % self.chip_columns)*200, self.chip_top+24*(index//self.chip_columns), incarnation)
                for index, incarnation in enumerate(self.chips[self.chip_offset:self.chip_offset+self.chip_capacity])]

    def scroll_chips(self, delta):
        self.chip_offset = max(0, min(max(0, len(self.chips)-self.chip_capacity), self.chip_offset+delta*self.chip_columns))

    def resize(self, w, h):
        if (w, h) != (self.width, self.height):
            self.width, self.height = max(100, w), max(150, h)
            self.relayout()

    def place(self, incarnation, x, y, w, h):
        self.placements[incarnation] = (int(x), int(y), max(40, int(w)), max(40, int(h)))
        self.relayout()

    def collapse(self, incarnation):
        if incarnation == self.root:
            return
        if incarnation in self.collapsed:
            self.collapsed.remove(incarnation)
        else:
            self.collapsed.add(incarnation)
        self.relayout()

    def reroot(self, incarnation):
        if incarnation in self.records:
            self.root = self.selected = incarnation
            self.relayout()

    def modal(self, incarnation):
        return self.frames.get(incarnation, {}).get('modal_regions', [])

    def hit(self, x, y):
        # A native parent dialog disables all child hit testing until cleared.
        if self.root in self.transforms and self.modal(self.root):
            return self.root if self.transforms[self.root].contains(x, y) else None
        return next((i for i, t in reversed(list(self.transforms.items())) if t.contains(x, y)), None)

    def select(self, x, y):
        selected = self.hit(x, y)
        if selected:
            self.selected = selected
        return selected

    def binding(self, incarnation):
        return dict(incarnation=incarnation, generation=self.frames[incarnation]['generation'], layout_version=self.version)

    def pointer(self, x, y, phase, drag_id=None, button=1):
        if self.drag:
            i, binding, transform, token = self.drag
            if drag_id != token:
                raise ValueError('drag_target_changed')
            if binding != self.binding(i):
                raise ValueError('stale_drag_binding')
        else:
            i = self.hit(x, y)
            if i is None or i not in self.frames:
                return None
            binding, transform, token = self.binding(i), self.transforms[i], drag_id
            if phase == 'down':
                if not drag_id:
                    raise ValueError('invalid_drag')
                self.drag = i, binding, transform, token
        nx, ny = transform.native(x, y)
        if phase == 'down':
            self.drag_buttons.add(button)
        if phase == 'up':
            self.drag_buttons.discard(button)
            if not self.drag_buttons:
                self.drag = None
        return dict(binding, x=nx, y=ny, drag_id=token)
