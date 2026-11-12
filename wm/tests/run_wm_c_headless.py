"""WM-C/full suite runner; all scratch stays under wm/tests/.tmp."""
import os
from pathlib import Path
import sys
import tempfile
import unittest

root = Path(__file__).resolve().parents[2]
scratch = root / 'wm/tests/.tmp'
scratch.mkdir(exist_ok=True)
os.environ['TMPDIR'] = str(scratch)
tempfile.tempdir = str(scratch)
original = tempfile.TemporaryDirectory


def redirected(*args, **kwargs):
    if kwargs.get('dir') == root:
        kwargs['dir'] = scratch
    return original(*args, **kwargs)


tempfile.TemporaryDirectory = redirected
sys.path[:0] = [str(root / 'wm'), str(root / 'wm/tests')]
if sys.argv[1:] == ['full']:
    suite = unittest.defaultTestLoader.discover(str(root / 'wm/tests'), pattern='test_worker_w*.py')
else:
    suite = unittest.defaultTestLoader.loadTestsFromName('test_worker_wm_c_presenter')
result = unittest.TextTestRunner(verbosity=2 if sys.argv[1:] != ['full'] else 1).run(suite)
sys.exit(not result.wasSuccessful())
