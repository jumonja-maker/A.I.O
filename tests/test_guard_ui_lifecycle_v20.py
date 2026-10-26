import os
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(os.environ.get('AIO_DOM_MODULE') and shutil.which('node'),
                     'Set AIO_DOM_MODULE to the pinned offline DOM package')
class QueueLifecycleV20(unittest.TestCase):
    def test_large_snapshot_cleanup_save_clear_discard_and_stale_requests(self):
        result = subprocess.run(['node', str(Path(__file__).with_name('guard_ui_lifecycle_v20.mjs'))],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + '\n' + result.stderr)
        print(result.stdout.strip())
