#!/usr/bin/env python3
"""Root helper: one session's keyboard and pointer, adapted from Spike B."""

import argparse
import ctypes
import errno
import fcntl
import glob
import json
import os
from pathlib import Path
import selectors
import socket
import struct
import threading
import time

from input_protocol import MAX_MESSAGE, control_snapshot, packet


UINPUT = "/dev/uinput"
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_SET_RELBIT = 0x40045566
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502
EV_SYN, EV_KEY, EV_REL = 0, 1, 2
REL_X, REL_Y, REL_HWHEEL, REL_WHEEL = 0, 1, 6, 8
BTN_LEFT, BTN_RIGHT, BTN_MIDDLE = 0x110, 0x111, 0x112
BUS_VIRTUAL = 0x06


class EventProtocol:
    """No device creation here: writers/grabs/clock are injectable for tests."""
    def __init__(self, writers, gate, grab, sleep=time.sleep, *, frame_grab=None):
        self.writers, self.gate, self.grab, self.sleep = writers, gate, grab, sleep
        self.frame_grab = frame_grab or grab
        self.held = {"kbd": set(), "ptr": set()}
        self.replies = {}
        self.high_seq = -1
        self.generation = None

    @staticmethod
    def valid(req):
        if set(req) != {"seq", "generation", "dev", "frames", "gap_us"}:
            return False
        if (not isinstance(req["generation"], str) or len(req["generation"]) != 32 or
                any(c not in "0123456789abcdef" for c in req["generation"])):
            return False
        dev, frames, gap = req["dev"], req["frames"], req["gap_us"]
        if (dev not in ("kbd", "ptr") or type(gap) is not int or not 0 <= gap <= 50000 or
                not isinstance(frames, list) or not 1 <= len(frames) <= 64):
            return False
        for frame in frames:
            if not isinstance(frame, list) or not 1 <= len(frame) <= 4:
                return False
            for ev in frame:
                if not isinstance(ev, list) or len(ev) != 3 or any(type(v) is not int for v in ev):
                    return False
                typ, code, val = ev
                if dev == "kbd":
                    good = typ == EV_KEY and 1 <= code <= 248 and code != 99 and val in (0, 1)
                else:
                    good = (typ == EV_KEY and 0x110 <= code <= 0x112 and val in (0, 1) or
                            typ == EV_REL and code in (0, 1, 6, 8) and abs(val) <= 40)
                if not good:
                    return False
        return True

    def write(self, dev, frame):
        # Track at event granularity, including a prefix before an I/O error.
        for typ, code, val in frame:
            self.writers[dev]((typ, code, val))
            if typ == EV_KEY:
                if val:
                    self.held[dev].add(code)
                else:
                    self.held[dev].discard(code)
        self.writers[dev]((EV_SYN, 0, 0))

    def handle(self, req):
        seq = req.get("seq") if isinstance(req, dict) else None
        reply = {"seq": seq, "ok": False, "frames_written": 0, "error": "rejected"}
        if not isinstance(req, dict) or type(seq) is not int or seq < 0:
            return reply
        payload = json.dumps(req, sort_keys=True, separators=(",", ":"))
        if seq in self.replies:
            previous, result = self.replies[seq]
            return dict(result) if previous == payload else reply
        if seq <= self.high_seq:
            return reply  # Evicted chunks can never be rewritten.
        self.high_seq = seq
        try:
            if req.get("op") in ("release_all", "grab_check") and set(req) == {"seq", "op"}:
                if req["op"] == "release_all":
                    for dev in ("kbd", "ptr"):
                        if self.held[dev]:
                            self.write(dev, [(EV_KEY, code, 0) for code in sorted(self.held[dev])])
                else:
                    reply.update({dev: self.grab(dev) for dev in ("kbd", "ptr")})
                reply.update(ok=True, error=None)
            elif self.valid(req) and self.generation in (None, req["generation"]):
                epoch = self.gate(req["generation"])
                # Pin one generation per device lifetime. W1 restarts still
                # work through XTEST; new evdev authority needs new devices.
                if epoch is not None:
                    self.generation = req["generation"]
                reply["error"] = None
                for n, frame in enumerate(req["frames"]):
                    if n and req["gap_us"]:
                        self.sleep(req["gap_us"] / 1e6)
                    current = self.gate(req["generation"])
                    if epoch is None or current != epoch:
                        reply["error"] = "gate_closed"
                        break
                    try:
                        grabbed = self.frame_grab(req["dev"]) == "busy"
                    except OSError:
                        grabbed = False
                    if not grabbed:
                        reply["error"] = "grab_lost"
                        break
                    # A failed event write may precede the first SYN_REPORT.
                    reply["may_have_written"] = True
                    self.write(req["dev"], frame)
                    reply["frames_written"] += 1
                reply["ok"] = reply["error"] is None
        except OSError:
            reply.update(ok=False, error="io_error")
        # Bounded memory; high-water rejection preserves at-most-once after eviction.
        self.replies[seq] = (payload, dict(reply))
        if len(self.replies) > 4096:
            del self.replies[min(self.replies)]
        return reply


def node_grab(node):
    fd = os.open(node, os.O_RDONLY | os.O_NONBLOCK)
    try:
        return fd_grab(fd)
    finally:
        os.close(fd)


def fd_grab(fd):
    try:
        fcntl.ioctl(fd, 0x40044590, 1)  # EVIOCGRAB
    except OSError as exc:
        if exc.errno == errno.EBUSY:
            return "busy"
        raise
    fcntl.ioctl(fd, 0x40044590, 0)
    return "free"


class FrameGrab:
    """One persistent probe fd; retire on error, including failed ungrab."""
    def __init__(self, node):
        self.fd = os.open(node, os.O_RDONLY | os.O_NONBLOCK)

    def __call__(self):
        if self.fd is None:
            raise OSError(errno.EBADF, 'grab probe retired')
        try:
            return fd_grab(self.fd)
        except OSError:
            # Closing also releases a successful probe grab if ungrab failed.
            self.close()
            raise

    def close(self):
        if self.fd is not None:
            fd, self.fd = self.fd, None
            os.close(fd)


def device_writer(fd):
    def write(ev):
        data = struct.pack('@llHHi', 0, 0, *ev)
        if os.write(fd, data) != len(data):
            raise OSError('short uinput write')
    return write


class EventSocket:
    """One authenticated stream at a time; FIFO quit remains independent."""
    def __init__(self, path, owner_uid, protocol):
        self.path, self.owner_uid, self.protocol = Path(path), owner_uid, protocol
        self.stopping = threading.Event()
        self.selector = selectors.DefaultSelector()
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.bound, self.thread, self.client = False, None, None
        self.incoming, self.outgoing = bytearray(), bytearray()

    def start(self):
        previous_umask = os.umask(0o177)
        try:
            self.listener.bind(str(self.path))
            self.bound = True
        finally:
            os.umask(previous_umask)
        os.chown(self.path, self.owner_uid, -1)
        self.path.chmod(0o600)
        self.listener.listen(1)
        self.listener.setblocking(False)
        self.selector.register(self.listener, selectors.EVENT_READ)
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def authorised(self, client):
        return struct.unpack('3i', client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1] == self.owner_uid

    def disconnect(self):
        if self.client is not None:
            self.selector.unregister(self.client)
            self.client.close()
            self.client = None
        self.incoming.clear()
        self.outgoing.clear()

    def serve(self):
        while not self.stopping.is_set():
            for key, mask in self.selector.select(timeout=0.1):
                connection = key.fileobj
                try:
                    if connection is self.listener:
                        client, _ = self.listener.accept()
                        if self.client is not None or not self.authorised(client):
                            client.close()
                            continue
                        self.client = client
                        client.setblocking(False)
                        self.selector.register(client, selectors.EVENT_READ)
                        continue
                    if mask & selectors.EVENT_WRITE:
                        del self.outgoing[:connection.send(self.outgoing)]
                    elif mask & selectors.EVENT_READ:
                        data = connection.recv(4096)
                        if not data:
                            self.disconnect()
                            continue
                        self.incoming.extend(data)
                    while not self.outgoing and len(self.incoming) >= 4:
                        size = struct.unpack('!I', self.incoming[:4])[0]
                        if not 0 < size <= MAX_MESSAGE:
                            self.disconnect()
                            break
                        if len(self.incoming) < size + 4:
                            break
                        body = bytes(self.incoming[4:size + 4])
                        del self.incoming[:size + 4]
                        try:
                            req = json.loads(body)
                        except (ValueError, UnicodeError):
                            req = None
                        self.outgoing.extend(packet(self.protocol.handle(req)))
                    if self.client is not None:
                        self.selector.modify(self.client, selectors.EVENT_WRITE if self.outgoing else selectors.EVENT_READ)
                except BlockingIOError:
                    continue
                except OSError:
                    if connection is not self.listener:
                        self.disconnect()

    def close(self):
        self.stopping.set()
        if self.thread is not None:
            self.thread.join()
        self.disconnect()
        self.listener.close()
        self.selector.close()
        if self.bound:
            self.path.unlink(missing_ok=True)


class InputID(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint16) for name in
                ("bustype", "vendor", "product", "version")]


class UinputUserDev(ctypes.Structure):
    _fields_ = [("name", ctypes.c_char * 80), ("id", InputID),
                ("ff_effects_max", ctypes.c_uint32),
                *[(name, ctypes.c_int32 * 64) for name in
                  ("absmax", "absmin", "absfuzz", "absflat")]]


def destroy(fd):
    try:
        fcntl.ioctl(fd, UI_DEV_DESTROY)
    finally:
        os.close(fd)


def make_dev(name, kind, owner_uid):
    """Spike B make_dev recipe; ctypes encodes its 1116-byte payload."""
    fd = os.open(UINPUT, os.O_WRONLY | os.O_NONBLOCK)
    try:
        fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
        fcntl.ioctl(fd, UI_SET_EVBIT, EV_SYN)
        if kind == "kbd":
            for code in range(1, 249):
                fcntl.ioctl(fd, UI_SET_KEYBIT, code)
        else:
            fcntl.ioctl(fd, UI_SET_EVBIT, EV_REL)
            for code in (REL_X, REL_Y, REL_HWHEEL, REL_WHEEL):
                fcntl.ioctl(fd, UI_SET_RELBIT, code)
            for code in (BTN_LEFT, BTN_RIGHT, BTN_MIDDLE):
                fcntl.ioctl(fd, UI_SET_KEYBIT, code)
        payload = UinputUserDev()
        payload.name = name.encode()
        payload.id = InputID(BUS_VIRTUAL, 0x51B0, 1 if kind == "kbd" else 2, 1)
        os.write(fd, bytes(payload))
        fcntl.ioctl(fd, UI_DEV_CREATE)
        for _ in range(100):
            for ev in glob.glob("/sys/class/input/event*/device/name"):
                try:
                    if Path(ev).read_text().strip() == name:
                        node = "/dev/input/" + ev.split("/")[-3]
                        os.chmod(node, 0o600)
                        os.chown(node, owner_uid, -1)
                        return fd, node
                except FileNotFoundError:
                    pass
            time.sleep(0.1)
        raise RuntimeError("node not found for " + name)
    except BaseException:
        try:
            destroy(fd)
        except OSError:
            pass
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fifo", required=True)
    parser.add_argument("--devices-json", required=True)
    parser.add_argument("--name-prefix", required=True)
    parser.add_argument("--event-socket")
    parser.add_argument("--owner-uid", type=int)
    parser.add_argument("--control")
    args = parser.parse_args()
    plumbing = (args.event_socket, args.owner_uid, args.control)
    if any(value is not None for value in plumbing) and not all(
            value is not None for value in plumbing):
        parser.error("--event-socket, --owner-uid and --control must be supplied together")
    devices_json = Path(args.devices_json)
    Path(str(devices_json) + ".pid").write_text(str(os.getpid()) + "\n")
    if not os.path.exists(args.fifo):
        os.mkfifo(args.fifo)
    fds = []
    probes = {}
    events = None
    try:
        nodes = {}
        for kind in ("kbd", "ptr"):
            fd, node = make_dev(f"{args.name_prefix} {kind}", kind, args.owner_uid if args.owner_uid is not None else os.getuid())
            fds.append(fd)
            nodes[kind] = node
        if args.event_socket is not None:
            for dev, node in nodes.items():
                probes[dev] = FrameGrab(node)
            protocol = EventProtocol(dict(zip(("kbd", "ptr"), map(device_writer, fds))),
                                     lambda gen: control_snapshot(args.control, gen),
                                     lambda dev: node_grab(nodes[dev]),
                                     frame_grab=lambda dev: probes[dev]())
            events = EventSocket(args.event_socket, args.owner_uid, protocol)
            events.start()
        devices_json.write_text(json.dumps(nodes) + "\n")
        while True:
            with open(args.fifo) as fifo:
                for line in fifo:
                    if line.strip() == "quit":
                        return 0
    finally:
        try:
            if events is not None:
                events.close()
        finally:
            for probe in probes.values():
                probe.close()
            for fd in fds:
                try:
                    destroy(fd)
                except OSError:
                    pass
            devices_json.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
