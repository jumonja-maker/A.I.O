import os
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(os.environ.get('AIO_DOM_MODULE') and shutil.which('node'),
                     'Set AIO_DOM_MODULE to the pinned offline DOM package')
class LegacyRecoveryUi(unittest.TestCase):
    def test_actual_discovery_save_discard_pagination_and_late_reply(self):
        result = subprocess.run(['node', str(Path(__file__).with_name('legacy_recovery_ui.mjs'))],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + '\n' + result.stderr)
        print(result.stdout.strip())
