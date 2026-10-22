import os
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(os.environ.get('AIO_DOM_MODULE') and shutil.which('node'),
                     'Set AIO_DOM_MODULE to an existing linkedom package')
class LargeUI(unittest.TestCase):
    def test_real_ui_modules_keep_large_queues_bounded_and_show_native_risks(self):
        result=subprocess.run(['node',str(Path(__file__).with_name('guard_large_ui_v13.mjs'))],
                              capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+'\n'+result.stderr)
        print(result.stdout.strip())
