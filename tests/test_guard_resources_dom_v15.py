"""Preserved UI + authenticated HTTP; controlled native import, no Windows GUI claim."""
import os
from pathlib import Path
import shutil
import subprocess
import unittest
import test_converter_native_stream as native_cases
from test_converter_native_dom import GuardDOMAdapter
from test_converter_guard_v04 import write_registry


@unittest.skipUnless(os.environ.get('AIO_DOM_MODULE') and shutil.which('node'), 'Set AIO_DOM_MODULE')
class HtmlResourceDOM(unittest.TestCase):
    def test_explicit_companion_upload_rechecks_without_selecting_or_closing(self):
        fixture=native_cases.NativeTransportHTTPTests('runTest')
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        executable,_=write_registry(fixture.work.name)
        fixture.server.guard.adapter=GuardDOMAdapter(executable,fixture.fake_native)
        result=subprocess.run(['node',str(Path(__file__).with_name('guard_resources_ui_v15.mjs'))],
            env=dict(os.environ,AIO_TEST_SERVER=fixture.base),capture_output=True,text=True,timeout=40)
        self.assertEqual(result.returncode,0,result.stdout+'\n'+result.stderr)
        print(result.stdout.strip())
