"""Opt-in DOM/HTTP acceptance using an existing linkedom installation."""
from contextlib import nullcontext
import os
from pathlib import Path
import shutil
import subprocess
import unittest

import test_converter_native_stream as cases
from app_backend.converter_guard import GuardError, LibreOfficeAdapter
from app_backend.converter_guard.routes import registry
from app_backend.libreoffice_engine import LibreOfficeError
from test_converter_guard_v04 import write_registry, D, W, C


class GuardDOMAdapter(LibreOfficeAdapter):
    """Synthetic native boundary; the real host, API and UI remain in use."""
    def __init__(self, executable, native):
        super().__init__(executable)
        self.native = native
        # This adapter owns a frozen synthetic installation. Resolve it once:
        # production capabilities evaluates thousands of model/target pairs,
        # which must not repeatedly discover the fixture's Windows filesystem.
        self.fixture_registry = registry(self.executable)

    def _registry(self):
        return self.fixture_registry

    def batch(self, workspace, **kwargs):
        # Import/export are the controlled boundary in this test; do not create
        # an OfficeSession simply because the runner has LibreOffice installed.
        return nullcontext()

    def preflight(self, source, workspace, *, cancel, timeout):
        head=source.read_bytes()[:8]
        if head.startswith((b'BROKEN',b'UNKNOWN!')):
            return {'state':'undetermined','code':'PREFLIGHT_IMPORT_UNDETERMINED'}
        service = D if head.startswith(b'BM') else C if head.startswith(b'a,b') else W
        return {'state':'detected','basis':'native_preflight_document_kind',
                'documentServices':[service],
                'registryFingerprint':self._registry().fingerprint,'exported':False}

    def convert(self, source, target, workspace, *, on_document_service=None, **kwargs):
        native = self._registry()
        head = source.read_bytes()[:8]
        service = D if head.startswith(b'BM') else C if head.startswith(b'a,b') else W
        try:
            if native.decide(target, {service}) == 'unavailable':
                kwargs['on_started']()
                raise LibreOfficeError('No native model route.', 'ENGINE_OUTPUT_MISSING')
            paths = self.native(source, target, workspace, cancel=kwargs['cancel'],
                                on_started=kwargs['on_started'])
        except LibreOfficeError as error:
            raise GuardError(error.code, str(error), stage='engine') from error
        if on_document_service:
            for name, (model, extensions, flags) in native.filters.items():
                if model == service and target in extensions and 'EXPORT' in flags:
                    on_document_service(native.proof(name, target))
                    break
        return paths


@unittest.skipUnless(os.environ.get('AIO_DOM_MODULE') and shutil.which('node'),
                     'Set AIO_DOM_MODULE to an existing linkedom package')
class NativeUIDOMAcceptance(cases.NativeTransportHTTPTests):
    def setUp(self):
        super().setUp()
        executable, _ = write_registry(self.work.name)
        self.server.guard.adapter = GuardDOMAdapter(executable, self.fake_native)

    # The inherited HTTP cases are already collected in their own module.
    def test_ui_contract(self):
        result = subprocess.run(['node', str(Path(__file__).with_name('native_ui_dom_smoke.mjs'))],
            env=dict(os.environ, AIO_TEST_SERVER=self.base), capture_output=True,
            text=True, timeout=40)
        self.assertEqual(result.returncode, 0, result.stdout + '\n' + result.stderr)
        print(result.stdout.strip())

    def test_thousand_file_queue_controls(self):
        result = subprocess.run(['node', str(Path(__file__).with_name('guard_queue_dom_v08.mjs'))],
            env=dict(os.environ, AIO_TEST_SERVER=self.base), capture_output=True,
            text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + '\n' + result.stderr)
        print(result.stdout.strip())

    def test_guard_unknown_and_renamed_file_selection(self):
        result = subprocess.run(['node', str(Path(__file__).with_name('guard_ui_dom_smoke.mjs'))],
            env=dict(os.environ, AIO_TEST_SERVER=self.base), capture_output=True,
            text=True, timeout=40)
        self.assertEqual(result.returncode, 0, result.stdout + '\n' + result.stderr)
        self.assertFalse(self.server.guard.storage_stats()['jobs'])
        print(result.stdout.strip())


# Reuse fixture helpers without collecting their HTTP tests twice.
for _name in list(vars(cases.NativeTransportHTTPTests)):
    if _name.startswith('test_'):
        setattr(NativeUIDOMAcceptance, _name, None)
