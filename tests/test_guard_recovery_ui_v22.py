import os
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(os.environ.get('AIO_DOM_MODULE') and shutil.which('node'),
                     'Set AIO_DOM_MODULE to the pinned offline DOM package')
class RecoveryMessagesV22(unittest.TestCase):
    def test_recovery_reasons_do_not_claim_corrupt_results(self):
        result = subprocess.run(['node', str(Path(__file__).with_name('guard_recovery_ui_v22.mjs'))],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + '\n' + result.stderr)
        print(result.stdout.strip())
