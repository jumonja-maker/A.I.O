"""Hosted Qt source GUI; installed Windows/system DPI explicitly NOT_TESTED."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import unittest


@unittest.skipUnless(os.environ.get('AIO_QT_SAVE_QA')=='1' and os.environ.get('AIO_DOM_MODULE')
                     and importlib.util.find_spec('PySide6'), 'Requires hosted Qt/DOM QA environment')
class RecoverySourceGUI(unittest.TestCase):
    def test_manual_recovery_geometry_keyboard_folder_and_external_zip(self):
        result=subprocess.run([sys.executable,str(Path(__file__).resolve().parents[1]/'tools/qa/recovery_save_webengine.py')],
                              capture_output=True,text=True,encoding='utf-8',timeout=150)
        self.assertEqual(result.returncode,0,result.stdout+'\n'+result.stderr)
        self.assertIn('"status": "PASS"',result.stdout)
        print(result.stdout.strip())
