"""Opt-in DOM/HTTP acceptance using an existing linkedom installation."""
from contextlib import nullcontext, contextmanager
import os
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch
from save_test_support import verify_download_for_test

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

    @contextmanager
    def external_save_host(self):
        # These DOM tests already simulate the native picker/event. Give that
        # controlled host a real external copy/readback before /saved; never
        # weaken the production acknowledgement requirement for the test.
        original = self.server.guard.confirm_saved
        def confirmed(job_id, ticket, **kwargs):
            verify_download_for_test(self, self.server.guard, ticket)
            return original(job_id, ticket, **kwargs)
        with patch.object(self.server.guard, 'confirm_saved', side_effect=confirmed):
            yield

    def run_with_external_save_host(self, script):
        with self.external_save_host():
            return subprocess.run(['node', str(Path(__file__).with_name(script))],
                env=dict(os.environ, AIO_TEST_SERVER=self.base), capture_output=True,
                text=True, timeout=40)

    # The inherited HTTP cases are already collected in their own module.
    def test_ui_contract(self):
        result = self.run_with_external_save_host('native_ui_dom_smoke.mjs')
        self.assertEqual(result.returncode, 0, result.stdout + '\n' + result.stderr)
        print(result.stdout.strip())

    def test_thousand_file_queue_controls(self):
        result = subprocess.run(['node', str(Path(__file__).with_name('guard_queue_dom_v08.mjs'))],
            env=dict(os.environ, AIO_TEST_SERVER=self.base), capture_output=True,
            text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + '\n' + result.stderr)
        print(result.stdout.strip())

    def test_guard_unknown_and_renamed_file_selection(self):
        result = self.run_with_external_save_host('guard_ui_dom_smoke.mjs')
        self.assertEqual(result.returncode, 0, result.stdout + '\n' + result.stderr)
        self.assertFalse(self.server.guard.storage_stats()['jobs'])
        print(result.stdout.strip())

    def test_510_source_cancel_during_route_matrix_http_dom(self):
        # Full preserved Designer + authenticated HTTP. Slow only the
        # synthetic registry decision after 510 fake native import proofs
        # so the user-facing blue cancel button can be clicked at 510/510.
        import time
        adapter = self.server.guard.adapter
        original = adapter._bound_route_decider
        def delayed(native):
            selected = original(native)
            def check(target, proof):
                time.sleep(.001)
                return selected(target, proof)
            return check
        adapter._bound_route_decider = delayed
        result = subprocess.run(
            ['node', str(Path(__file__).with_name('guard_matrix_cancel_ui_v182.mjs'))],
            env=dict(os.environ, AIO_TEST_SERVER=self.base),
            capture_output=True, text=True, timeout=80)
        self.assertEqual(result.returncode, 0, result.stdout + '\n' + result.stderr)
        print(result.stdout.strip())


# Reuse fixture helpers without collecting their HTTP tests twice.
for _name in list(vars(cases.NativeTransportHTTPTests)):
    if _name.startswith('test_'):
        setattr(NativeUIDOMAcceptance, _name, None)
