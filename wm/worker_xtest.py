"""Session-only, asynchronous python-xlib transport for W2.

The ev connection owns queries, WM messages and XI2 acknowledgements. Only
XTEST requests are sent on inp. Neither connection is ever opened here.
"""
from collections import deque
import ctypes
import struct

from Xlib import X, XK
from Xlib.ext import ge, xinput
from Xlib.protocol import event, request

from worker_act import ActError


RAW = {13: "key_down", 14: "key_up", 15: "button_down", 16: "button_up", 17: "motion"}
# Pure keysym conversion, no X connection or host keyboard access. This also
# covers legacy (non-Unicode-encoded) Greek/Cyrillic/Euro keysyms in a session
# map. The library is loaded once, before the action path.
_xkb = ctypes.CDLL("libxkbcommon.so.0")
_xkb.xkb_keysym_to_utf32.argtypes = [ctypes.c_uint32]
_xkb.xkb_keysym_to_utf32.restype = ctypes.c_uint32
_xkb.xkb_keysym_from_name.argtypes = [ctypes.c_char_p, ctypes.c_int]
_xkb.xkb_keysym_from_name.restype = ctypes.c_uint32


def decode_raw(e, opcode):
    if e.type != ge.GenericEventCode or e.extension != opcode or e.evtype not in RAW:
        return None
    # python-xlib 0.33 GenericEvent.data begins at wire byte 10.
    data = e.data
    if not isinstance(data, bytes) or len(data) < 22:
        return None
    device, stamp, detail, source, valuators, flags = struct.unpack_from("=HIIHHI", data)
    return RAW[e.evtype], 0 if e.evtype == 17 else detail, device, source


class XTestTransport:
    def __init__(self, worker):
        self.w, self.x = worker, worker.x
        self.sources = {}
        self.raw, self.raw_seq = deque(maxlen=16384), 0
        self.keysyms, self.characters = {}, {}
        self.test = None
        self.test_presses = 0
        self.initialised = False
        self.keymap_ready = self.devices_ready = False
        info = self.x.ev.display.info
        self.x.ax.ask(request.GetKeyboardMapping, self.mapping,
                      first_keycode=info.min_keycode,
                      count=info.max_keycode - info.min_keycode + 1)
        self.x.ax.ask(xinput.XIQueryDevice, self.devices,
                      opcode=self.x.ev.display.get_extension_major("XInputExtension"), deviceid=xinput.AllDevices)
        mask = (xinput.RawKeyPressMask | xinput.RawKeyReleaseMask | xinput.RawButtonPressMask |
                xinput.RawButtonReleaseMask | xinput.RawMotionMask)
        self.x.root.xinput_select_events([(xinput.AllDevices, mask), (xinput.AllMasterDevices, xinput.MotionMask)])

    def devices(self, data):
        self.device_list = data["devices"]
        for dev in data["devices"]:
            name = dev.name if hasattr(dev, "name") else dev["name"]
            if isinstance(name, bytes):
                name = name.decode("utf-8", "replace")
            deviceid = dev.deviceid if hasattr(dev, "deviceid") else dev["deviceid"]
            use = dev.use if hasattr(dev, "use") else dev["use"]
            enabled = dev.enabled if hasattr(dev, "enabled") else dev["enabled"]
            if enabled and name == "Virtual core XTEST keyboard" and use == xinput.SlaveKeyboard:
                self.sources["keyboard"] = deviceid
            if enabled and name == "Virtual core XTEST pointer" and use == xinput.SlavePointer:
                self.sources["pointer"] = deviceid
        self.devices_ready = True
        self.initialised = self.keymap_ready

    def mapping(self, data):
        first = self.x.ev.display.info.min_keycode
        self.keysyms.clear()
        self.characters.clear()
        # GetKeyboardMapping replies contain one array per keycode in 0.33.
        for code, symbols in enumerate(data["keysyms"], first):
            for level, sym in enumerate(symbols):
                if not sym:
                    continue
                self.keysyms.setdefault(sym, code)
                if level <= 1:
                    char = None
                    scalar = _xkb.xkb_keysym_to_utf32(sym)
                    if scalar and scalar >= 32 and scalar != 127:
                        char = chr(scalar)
                    if sym in (XK.XK_Return, XK.XK_Tab):
                        char = "\n" if sym == XK.XK_Return else "\t"
                    if char:
                        old = self.characters.get(char)
                        if old is None or level == 0:
                            self.characters[char] = (code, level)
        self.keymap_ready = True
        self.initialised = self.devices_ready

    def keycode(self, name):
        sym = _xkb.xkb_keysym_from_name(name.encode("utf-8"), 0)
        if not sym and len(name) == 1:
            sym = ord(name) if ord(name) < 256 else 0x01000000 | ord(name)
        return self.keysyms.get(sym)

    def character(self, char):
        result = self.characters.get(char)
        if result is None:
            return None
        code, shift = result
        if shift:
            modifier = self.keycode("Shift_L")
            return [modifier, code] if modifier is not None else None
        return [code]

    def select_all(self, handle):
        # v0 development profile's explicit shortcut, never a host keymap.
        return self.w.profile_data["shortcuts"]["select_all"]

    def inter_key_delay_us(self, handle):
        return self.w.profile_data["input"]["inter_key_delay_us"]

    def write(self, ev):
        kind, detail, *xy = ev
        if kind == "motion":
            self.x.inp.xtest_fake_input(X.MotionNotify, x=xy[0], y=xy[1], root=self.x.root.id)
        else:
            typ = {"key_down": X.KeyPress, "key_up": X.KeyRelease,
                   "button_down": X.ButtonPress, "button_up": X.ButtonRelease}[kind]
            self.x.inp.xtest_fake_input(typ, detail=detail)

    def flush(self):
        self.x.inp_ax.flush()

    def pointer(self, cb):
        def got(d):
            self.w.cache.set("pointer", {"x": d["root_x"], "y": d["root_y"]})
            cb({"x": d["root_x"], "y": d["root_y"], "mask": d["mask"],
                "child": getattr(d["child"], "id", d["child"])})
        self.x.ax.ask(request.QueryPointer, got, window=self.x.root)

    def keymap(self, cb):
        def got(d):
            keys = d["map"]
            cb({code for code in range(256) if keys[code // 8] & (1 << (code % 8))})
        self.x.ax.ask(request.QueryKeymap, got)

    def barrier(self, cb):
        self.x.ax.ask(request.GetInputFocus, cb)

    def refresh(self, predicate, target, cb):
        return self.x.sample(predicate, target, cb)

    def belongs(self, child, win):
        if not win:
            return False
        if child == win["xid"]:
            return True
        other = self.w.cache.windows.get(child)
        if other:
            return other["family"] == win["family"]
        # Root QueryPointer returns Openbox's frame, not its client. The
        # parent chain is sampled asynchronously alongside window metadata.
        return child in self.x.ancestors.get(win["xid"], ())

    def wm(self, kind, wid):
        atom = {"focus_window": "_NET_ACTIVE_WINDOW", "close_window": "_NET_CLOSE_WINDOW",
                "raise_window": "_NET_RESTACK_WINDOW"}[kind]
        data = [2, X.CurrentTime, 0, 0, 0]
        if kind == "raise_window":
            data = [2, 0, X.Above, 0, 0]
        message = event.ClientMessage(window=wid, client_type=self.x.atoms[atom], data=(32, data))
        self.x.root.send_event(message, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
        self.x.ax.flush()

    def events(self, events):
        opcode = self.x.ev.display.get_extension_major("XInputExtension")
        for e in events:
            raw = decode_raw(e, opcode)
            if raw:
                self.raw_seq += 1
                self.raw.append((self.raw_seq, *raw))
            if self.test is not None and e.type == X.KeyPress and getattr(e, "window", None) == self.test:
                self.test_presses += 1
            if e.type == X.MappingNotify:
                # Refresh only from this session's server mapping.
                info = self.x.ev.display.info
                self.x.ax.ask(request.GetKeyboardMapping, self.mapping,
                              first_keycode=info.min_keycode, count=info.max_keycode - info.min_keycode + 1)

    def test_window(self, keyboard):
        self.test_presses = 0
        if keyboard:
            self.test = self.x.root.create_window(0, 0, 32, 32, 0, X.CopyFromParent,
                          X.InputOutput, X.CopyFromParent, override_redirect=True,
                          event_mask=X.KeyPressMask | X.KeyReleaseMask)
            self.x.ignored.add(self.test.id)
            self.test.map()
            self.test.set_input_focus(X.RevertToPointerRoot, X.CurrentTime)
            self.x.ax.flush()
        return self.test

    def end_test(self, test, old_focus):
        if self.test is not None:
            self.test.destroy()
            self.test = None
            # No client focus existed at startup; restore PointerRoot only.
            self.x.ev.set_input_focus(X.PointerRoot, X.RevertToPointerRoot, X.CurrentTime)
            self.x.ax.flush()
