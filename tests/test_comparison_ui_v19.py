"""Exercise the actual comparison UI; no browser or network dependency."""
import os
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which('node') and os.environ.get('AIO_DOM_MODULE'),
                     'Node and the pinned offline AIO_DOM_MODULE are required')
class ComparisonUI(unittest.TestCase):
    def test_optional_analysis_preview_selection_retry_and_lifetime(self):
        process = subprocess.run(['node', str(Path(__file__).with_name('guard_comparison_v19.mjs'))],
                                 capture_output=True, text=True, timeout=30)
        self.assertEqual(process.returncode, 0, process.stdout+'\n'+process.stderr)
        print(process.stdout.strip())
