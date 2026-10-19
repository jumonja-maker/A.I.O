"""Run the actual JS Guard bridge against a controlled asynchronous transport."""
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for bridge races')
class GuardBridgeV05Tests(unittest.TestCase):
    def test_actual_bridge_lifecycle_and_complete_plans(self):
        result = subprocess.run(['node', str(Path(__file__).with_name('guard_bridge_contracts.mjs'))],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + '\n' + result.stderr)
        print(result.stdout.strip())
