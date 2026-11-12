"""Shared bounded helper framing and fail-closed control-page decoding."""
import ctypes
import fcntl
import json
import hashlib
import hmac
import mmap
import os
import struct
import sys
from pathlib import Path

MAX_MESSAGE = 65536
PAGE = struct.Struct('<4sI16sQIIQ16s')


def control_store(page, offset, value, bits):
    # Aligned native integer store into the shared mapping (ArK2 is LE).
    if sys.byteorder != "little":
        raise RuntimeError("unsupported_control_endian")
    scalar = (ctypes.c_uint64 if bits == 64 else ctypes.c_uint32).from_buffer(page, offset)
    scalar.value = value
    del scalar


def close_control_gate(path):
    """Use the worker's state-first store; absent/invalid-size pages are closed."""
    try:
        stream = Path(path).open('r+b')
    except FileNotFoundError:
        return  # No worker/control page yet (also valid during failed start).
    with stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        if os.fstat(stream.fileno()).st_size != PAGE.size:
            return  # The helper refuses these pages already.
        with mmap.mmap(stream.fileno(), PAGE.size) as page:
            control_store(page, 32, 1, 32)  # QUIESCING before helper shutdown.
            page.flush()
        os.fsync(stream.fileno())


def packet(value):
    body = json.dumps(value, separators=(',', ':'), allow_nan=False).encode()
    if not 0 < len(body) <= MAX_MESSAGE:
        raise ValueError('message_size')
    return struct.pack('!I', len(body)) + body


def control_snapshot(path, generation, human_token=None, human_epoch=None):
    """Reopen and double-read each frame; missing/torn/replaced pages close."""
    try:
        with Path(path).open('rb') as stream:
            a = stream.read(65)
            stream.seek(0)
            b = stream.read(65)
        if a != b or len(a) != PAGE.size:
            return None
        magic, version, gen, epoch, state, flags, permit, lease = PAGE.unpack(a)
        if (magic != b'ARKC' or version != 1 or gen.hex() != generation or
                not epoch or permit):
            return None
        if human_token is None:
            if state != 0:
                return None
        elif (state != 2 or epoch != human_epoch or not isinstance(human_token, str) or
              not hmac.compare_digest(lease, hashlib.sha256(human_token.encode()).digest()[:16])):
            return None
        return epoch
    except (OSError, ValueError, TypeError):
        return None
