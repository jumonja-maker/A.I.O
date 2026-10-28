"""Race and pair-identity regressions in the production before/after UI."""
import os
from pathlib import Path
import shutil
import subprocess
import unittest

@unittest.skipUnless(shutil.which('node') and os.environ.get('AIO_DOM_MODULE'),
                     'Node and pinned offline AIO_DOM_MODULE are required')
class ComparisonUIV21(unittest.TestCase):
    def test_deferred_requests_and_pair_identity(self):
        result = subprocess.run(['node', str(Path(__file__).with_name('guard_comparison_v21.mjs'))],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + '\n' + result.stderr)
        print(result.stdout.strip())
