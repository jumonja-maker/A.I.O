"""Opt-in full approved UI + loopback contract with an existing DOM QA package."""
import os
from pathlib import Path
import shutil
import subprocess
import unittest

import test_converter_guard_http as http_cases


@unittest.skipUnless(os.environ.get('AIO_DOM_MODULE') and shutil.which('node'),
                     'Opt-in: set AIO_DOM_MODULE to an existing linkedom package')
class GuardDOMAcceptance(unittest.TestCase):
    setUp = http_cases.GuardHTTPTests.setUp
    tearDown = http_cases.GuardHTTPTests.tearDown

    def test_full_converter_ui_against_real_loopback(self):
        env = dict(os.environ, AIO_TEST_SERVER=self.base)
        result = subprocess.run(['node', str(Path(__file__).with_name('guard_ui_dom_smoke.mjs'))],
            env=env,capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+'\n'+result.stderr)
        print(result.stdout.strip())

    def test_missing_engine_is_explained_in_existing_status_area(self):
        self.guard.adapter.available = False
        env = dict(os.environ, AIO_TEST_SERVER=self.base, AIO_UI_EXPECT_UNAVAILABLE='1')
        result = subprocess.run(['node',str(Path(__file__).with_name('guard_ui_dom_smoke.mjs'))],
            env=env,capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+'\n'+result.stderr)
        self.assertEqual(self.guard.adapter.calls,[])
        print(result.stdout.strip())
