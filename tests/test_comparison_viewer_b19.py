import os
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which('node') and os.environ.get('AIO_DOM_MODULE'),'Pinned offline DOM dependency required')
class ComparisonViewerDOM(unittest.TestCase):
    def test_real_two_pane_viewer_zoom_pages_security_and_lifetime(self):
        result=subprocess.run(['node',str(Path(__file__).with_name('guard_comparison_viewer_b19.mjs'))],capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+'\n'+result.stderr)
        print(result.stdout.strip())
