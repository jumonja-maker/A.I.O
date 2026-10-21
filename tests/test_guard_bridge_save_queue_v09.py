import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which('node'), 'Node is required for the actual JS bridge contract')
class BridgeSaveQueueTests(unittest.TestCase):
    def test_actual_bridge_save_and_control_races(self):
        completed = subprocess.run(['node', str(Path(__file__).with_name('guard_save_queue_v09.mjs'))],
                                   capture_output=True, text=True, timeout=25)
        self.assertEqual(completed.returncode, 0, completed.stdout+'\n'+completed.stderr)
        print(completed.stdout.strip())
