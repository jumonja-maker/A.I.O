"""Live large-queue route matrix and cancellation regressions for Guard v18.2.

Synthetic import proofs are intentionally distinct from native conversion QA.
The installed-registry model used for route decisions is fixture-produced.
"""
import io
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from app_backend.converter_assets import read_designer_asset
from app_backend.converter_guard import ConverterGuard, GuardError
from app_backend.converter_guard.adapter import LibreOfficeAdapter
from test_converter_guard_v04 import W, write_registry


class NativeMatrixCancellationQA(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        executable, _ = write_registry(self.temp.name)
        self.adapter = LibreOfficeAdapter(executable)
        self.guard = ConverterGuard(adapter=self.adapter,
                                    recovery_parent=self.root / 'recovery')
        native = self.adapter._registry()
        self.proof = {
            'state': 'detected',
            'basis': 'native_preflight_document_kind',
            'documentServices': [W],
            'registryFingerprint': native.fingerprint,
            'exported': False,
        }

    def tearDown(self):
        self.guard.close()
        self.temp.cleanup()

    def inputs(self, count):
        metadata = [{'id': 'file_%04d' % i,
                     'name': 'sample-%04d.docx' % i, 'size': 1}
                    for i in range(count)]
        job = self.guard.create(metadata, prepare_only=True)['jobId']
        for file in metadata:
            self.guard.upload(job, file['id'], io.BytesIO(b'x'), 1)
        return job

    def test_matrix_completion_is_separately_visible_after_all_imports(self):
        job = self.inputs(32)
        with patch.object(self.adapter, 'preflight', return_value=self.proof):
            result = self.guard.preflight(job)
        status = self.guard.status(job)
        self.assertEqual(status['preflightCompleted'], 32)
        self.assertEqual(status['preflightTotal'], 32)
        self.assertEqual(status['preflightPhase'], 'done')
        self.assertGreater(status['preflightMatrixTotal'], 0)
        self.assertEqual(status['preflightMatrixCompleted'],
                         status['preflightMatrixTotal'])
        self.assertFalse(status['cancellationRequested'])
        self.assertTrue(result['preflightComplete'])

    def test_cancel_breaks_long_route_matrix_after_imports_finished(self):
        job = self.inputs(128)
        original = self.adapter._bound_route_decider
        entered = threading.Event()
        errors = []

        def delayed(native):
            decision = original(native)
            def slow(target, proof):
                entered.set()
                time.sleep(.001)
                return decision(target, proof)
            return slow

        def worker():
            try:
                self.guard.preflight(job)
            except GuardError as error:
                errors.append(error.code)

        with patch.object(self.adapter, 'preflight', return_value=self.proof), \
             patch.object(self.adapter, '_bound_route_decider', side_effect=delayed):
            thread = threading.Thread(target=worker, daemon=True)
            thread.start()
            self.assertTrue(entered.wait(8), 'Expected matrix evaluation after native imports')
            snapshot = self.guard.status(job)
            self.assertEqual(snapshot['state'], 'checking')
            self.assertEqual(snapshot['preflightPhase'], 'routes')
            self.assertEqual(snapshot['preflightCompleted'], 128)
            self.assertGreater(snapshot['preflightMatrixTotal'], 0)
            tick = time.monotonic()
            acknowledged = self.guard.cancel(job)
            self.assertTrue(acknowledged['cancellationRequested'])
            thread.join(4)
            self.assertFalse(thread.is_alive(), 'Matrix ignored the cancellation lease')
            self.assertLess(time.monotonic() - tick, 4.0)
        self.assertEqual(errors, ['CANCELLED'])
        final = self.guard.status(job)
        self.assertEqual(final['state'], 'cancelled')
        self.assertEqual(final['preflightPhase'], 'cancelled')
        self.assertTrue(final['cancellationRequested'])
        self.assertLess(final['preflightMatrixCompleted'], final['preflightMatrixTotal'])

        # Global preflight slot must be reusable immediately, not after the
        # old 510-file worker's full timeout.
        next_job = self.inputs(1)
        with patch.object(self.adapter, 'preflight', return_value=self.proof):
            self.guard.preflight(next_job)
        self.assertEqual(self.guard.status(next_job)['state'], 'prepared')

    def test_explicit_matrix_cancellation_never_returns_verified_routes(self):
        native = self.adapter._registry()
        files = [{'id': 'id%04d' % i, 'name': 'sample-%04d.docx' % i, 'size': 1}
                 for i in range(510)]
        proofs = {file['id']: self.proof for file in files}
        cancel = threading.Event()
        visited = []

        def after_target(done, total):
            visited.append(done)
            if done == 2:
                cancel.set()
        with self.assertRaises(GuardError) as error:
            self.adapter.capabilities(files, proofs=proofs, cancel=cancel,
                                      on_progress=after_target)
        self.assertEqual(error.exception.code, 'CANCELLED')
        self.assertEqual(visited, [0, 1, 2])

    def test_installed_route_proofs_remain_identical_without_progress_callback(self):
        files = [{'id': 'one', 'name': 'one.docx', 'size': 1}]
        proof = {'one': self.proof}
        baseline = self.adapter.capabilities(files, proofs=proof)
        reported = []
        after = self.adapter.capabilities(files, proofs=proof, cancel=threading.Event(),
                                          on_progress=lambda done, total: reported.append((done, total)))
        self.assertEqual(after, baseline)
        self.assertEqual(reported[0][0], 0)
        self.assertEqual(reported[-1][0], reported[-1][1])

    def test_preserved_designer_build_contains_immediate_cancel_feedback(self):
        from app_backend import converter_assets
        root = Path(converter_assets.__file__).resolve().parent.parent
        text = read_designer_asset(str(root), 'file-converter.js').decode('utf-8')
        self.assertIn('Отменить проверку', text)
        self.assertIn('Отменяем проверку', text)


if __name__ == '__main__':
    unittest.main()
