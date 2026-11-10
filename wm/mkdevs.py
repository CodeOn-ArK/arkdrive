#!/usr/bin/env python3
"""Root helper: one session's keyboard and pointer, adapted from Spike B."""

import argparse
import ctypes
import fcntl
import glob
import json
import os
from pathlib import Path
import selectors
import socket
import threading
import time


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


class DormantEventSocket:
    """W0 plumbing only: refuse every request, without touching uinput.

    A separate thread keeps idle clients from blocking the existing quit FIFO.
    The control path is retained for W6; it is deliberately never opened here.
    """

    def __init__(self, path, owner_uid, control):
        self.path = Path(path)
        self.owner_uid = owner_uid
        self.control_path = Path(control)
        self.stopping = threading.Event()
        self.selector = selectors.DefaultSelector()
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.bound = False
        self.thread = None

    def start(self):
        # Bind privately: there must be no interval with world-writable access.
        previous_umask = os.umask(0o177)
        try:
            self.listener.bind(str(self.path))
            self.bound = True
        finally:
            os.umask(previous_umask)
        os.chown(self.path, self.owner_uid, -1)
        self.path.chmod(0o600)
        self.listener.listen()
        self.listener.setblocking(False)
        self.selector.register(self.listener, selectors.EVENT_READ, None)
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while not self.stopping.is_set():
            for key, _ in self.selector.select(timeout=0.1):
                connection = key.fileobj
                try:
                    if connection is self.listener:
                        client, _ = self.listener.accept()
                        client.setblocking(False)
                        self.selector.register(client, selectors.EVENT_READ, b"")
                        continue
                    if key.events == selectors.EVENT_READ:
                        if connection.recv(4096):
                            self.selector.modify(connection, selectors.EVENT_WRITE,
                                                 b'{"error": "dormant"}\n')
                            continue
                    else:
                        remaining = key.data[connection.send(key.data):]
                        if remaining:
                            self.selector.modify(connection, selectors.EVENT_WRITE, remaining)
                            continue
                except BlockingIOError:
                    continue
                except OSError:
                    if connection is self.listener:
                        continue
                self.selector.unregister(connection)
                connection.close()

    def close(self):
        self.stopping.set()
        if self.thread is not None:
            self.thread.join()
        for key in list(self.selector.get_map().values()):
            key.fileobj.close()
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


def make_dev(name, kind):
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
                        os.chmod(node, 0o666)
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
    events = None
    try:
        if args.event_socket is not None:
            events = DormantEventSocket(args.event_socket, args.owner_uid, args.control)
            events.start()
        nodes = {}
        for kind in ("kbd", "ptr"):
            fd, node = make_dev(f"{args.name_prefix} {kind}", kind)
            fds.append(fd)
            nodes[kind] = node
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
            for fd in fds:
                try:
                    destroy(fd)
                except OSError:
                    pass
            devices_json.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
