#!/usr/bin/env python3
"""Install laneq and its shim, verifying both original cua backups first."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stage(source, directory):
    fd, name = tempfile.mkstemp(prefix=".laneq-install-", dir=directory)
    os.close(fd)
    path = Path(name)
    try:
        shutil.copyfile(source, path)
        path.chmod(0o755)
        return path
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def install(directory):
    source = Path(__file__).resolve().parent
    cua = directory / "cua"
    real = directory / "cua.real"
    backup = directory / "cua.bak-laneq"
    is_shim = cua.read_bytes() == (source / "cua_shim.py").read_bytes()
    original = real if is_shim else cua
    checksum = digest(original)
    # Refuse to overwrite unrelated existing backups.
    for path in (real, backup):
        if path.exists() and digest(path) != checksum:
            raise RuntimeError(f"existing backup differs from original cua: {path}")
    staged_laneq = stage(source / "laneq.py", directory)
    staged_shim = stage(source / "cua_shim.py", directory)
    try:
        if not backup.exists():
            shutil.copy2(original, backup)
        if not real.exists():
            # Move the actual original, preserving its bytes and metadata.
            os.replace(cua, real)
        if digest(real) != checksum or digest(backup) != checksum:
            raise RuntimeError("cua backup checksum verification failed")
        os.replace(staged_laneq, directory / "laneq")
        os.replace(staged_shim, cua)
    except BaseException:
        if not cua.exists() and real.exists():
            shutil.copy2(real, cua)
        raise
    finally:
        staged_laneq.unlink(missing_ok=True)
        staged_shim.unlink(missing_ok=True)
    print(json.dumps({"ok": True, "original_cua_sha256": checksum,
                      "cua_real_sha256": digest(real), "cua_backup_sha256": digest(backup)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bin-dir", type=Path, default=Path.home() / "bin")
    install(parser.parse_args().bin_dir.expanduser())
