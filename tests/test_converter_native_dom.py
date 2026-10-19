"""Opt-in DOM/HTTP acceptance using an existing linkedom installation."""
import os
from pathlib import Path
import shutil
import subprocess
import unittest

import test_converter_native_stream as cases


@unittest.skipUnless(os.environ.get('AIO_DOM_MODULE') and shutil.which('node'),
                     'Set AIO_DOM_MODULE to an existing linkedom package')
class NativeUIDOMAcceptance(cases.NativeTransportHTTPTests):
    # The inherited HTTP cases are already collected in their own module.
    def test_ui_contract(self):
        result = subprocess.run(['node', str(Path(__file__).with_name('native_ui_dom_smoke.mjs'))],
            env=dict(os.environ, AIO_TEST_SERVER=self.base), capture_output=True,
            text=True, timeout=40)
        self.assertEqual(result.returncode, 0, result.stdout + '\n' + result.stderr)
        print(result.stdout.strip())


# Reuse fixture helpers without collecting their HTTP tests twice.
for _name in list(vars(cases.NativeTransportHTTPTests)):
    if _name.startswith('test_'):
        setattr(NativeUIDOMAcceptance, _name, None)
