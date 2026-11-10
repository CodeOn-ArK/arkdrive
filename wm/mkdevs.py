#!/usr/bin/env python3
"""Root helper: one session's keyboard and pointer, adapted from Spike B."""

import argparse
import ctypes
import fcntl
import glob
import json
import os
from pathlib import Path
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
    args = parser.parse_args()
    devices_json = Path(args.devices_json)
    Path(str(devices_json) + ".pid").write_text(str(os.getpid()) + "\n")
    if not os.path.exists(args.fifo):
        os.mkfifo(args.fifo)
    fds = []
    try:
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
        for fd in fds:
            try:
                destroy(fd)
            except OSError:
                pass
        devices_json.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
