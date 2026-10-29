"""Real production JS/DOM with controlled asynchronous transport, not GUI pixels."""
import os
from pathlib import Path
import subprocess
import unittest


class RecoveryScanDOM(unittest.TestCase):
    def test_progress_retry_and_view_lifecycle(self):
        env = dict(os.environ)
        if not env.get('AIO_DOM_MODULE'):
            candidate = Path('node_modules/linkedom/esm/index.js').resolve()
            if not candidate.is_file():
                self.skipTest('AIO_DOM_MODULE is required for DOM acceptance')
            env['AIO_DOM_MODULE'] = str(candidate)
        result = subprocess.run(['node','tests/recovery_scan_dom.mjs'], env=env,
                                capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout+'\n'+result.stderr)
        self.assertIn('AIO_RECOVERY_SCAN_DOM=', result.stdout)
        print(result.stdout.strip())
