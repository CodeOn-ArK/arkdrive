import os
from pathlib import Path
import sys
import tempfile
import unittest

root = Path.cwd()
tmp = root/'wm/tests/.tmp'
os.environ['TMPDIR'] = str(tmp)
tempfile.tempdir = str(tmp)
original = tempfile.TemporaryDirectory
def redirected(*args, **kw):
    if kw.get('dir') == root:
        kw['dir'] = tmp
    return original(*args, **kw)
tempfile.TemporaryDirectory = redirected
sys.path[:0] = [str(root/'wm'), str(root/'wm/tests')]
if sys.argv[1:] == ['full']:
    suite = unittest.defaultTestLoader.discover(str(root/'wm/tests'), pattern='test_worker_w*.py')
else:
    suite = unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:] or [
        'test_worker_w_memory_step5_fix1', 'test_worker_w_memory_step5'])
result = unittest.TextTestRunner(verbosity=1).run(suite)
sys.exit(not result.wasSuccessful())
