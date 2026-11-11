"""Session diagnostic caps and verified, instance-scoped scratch cleanup.

Never opens a display or touches act/sessiond journals. Append-mode child
descriptors keep the same inode after truncation. The guard also covers
sessions with no worker; a worker sweep supplies a second maintenance point.
"""
import argparse
import os
from pathlib import Path
import shutil
import stat
import time

LOG_BYTES = 4 * 1024 * 1024
LOG_TOTAL = 64 * 1024 * 1024
LOG_NAMES = ('worker.log', 'atspi-provider.log', 'ocr-provider.log', 'openbox.log',
             'mkdevs.log', 'xorg-launch.log', 'xvfb.log', 'atspi-launcher.log', 'registryd.log')


def cap_logs(directory, limit=LOG_BYTES, total=LOG_TOTAL):
    """Best-effort diagnostics only: no writes/rotation/archive allocation."""
    used = 0
    for name in LOG_NAMES:
        try:
            fd = os.open(Path(directory) / 'logs' / name,
                         os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    continue
                if info.st_size > limit or used + info.st_size > total:
                    os.ftruncate(fd, 0)
                else:
                    used += info.st_size
            finally:
                os.close(fd)
        except OSError:
            pass  # A diagnostic failure must never block or replace F3.


def matches_instance(directory, instance):
    try:
        return (bool(instance) and not Path(directory).is_symlink()
                and (Path(directory) / '.instance').read_text().strip() == instance)
    except OSError:
        return False


def reclaim_scratch(record):
    """Caller has independently proved teardown; registry absence is no proof.

    User homes and receipt/frame evidence retain their original absolute paths.
    Old (pre-instance) sessions are deliberately not garbage-collected.
    """
    directory = Path(record['session_dir'])
    if not matches_instance(directory, record.get('instance')):
        return
    for child in directory.iterdir():
        if child.name in ('home', 'driver', '.instance'):
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink(missing_ok=True)
    driver = directory / 'driver'
    if driver.is_dir() and not driver.is_symlink():
        for child in driver.iterdir():
            if child.name == 'frames':
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--guard', type=Path, required=True)
    parser.add_argument('--instance', required=True)
    args = parser.parse_args()
    while matches_instance(args.guard, args.instance):
        cap_logs(args.guard)
        time.sleep(1)


if __name__ == '__main__':
    main()
