"""Guard v18.2: 510-file preflight responsiveness and native DBF recovery gates."""
import io
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app_backend.converter_guard import ConverterGuard, GuardError, GuardLimits
from app_backend.converter_guard.adapter import LibreOfficeAdapter, _native_dbf_structure
from app_backend.converter_guard.export_recipe import ExportRecipe
from test_converter_guard_v02 import FakeAdapter

CALC = 'com.sun.star.sheet.SpreadsheetDocument'
CSV_FILTER = 'Text - txt - csv (StarCalc)'


def native_dbf_sample():
    """Small deterministic test container; production always uses LibreOffice."""
    header = bytearray(32)
    header[0] = 0x03
    header[4:8] = (2).to_bytes(4, 'little')
    header[8:10] = (33).to_bytes(2, 'little')
    header[10:12] = (4).to_bytes(2, 'little')
    return bytes(header) + b'\x0d' + b'abc ' + b'def '


class BulkPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.guard = ConverterGuard(adapter=FakeAdapter(),
                                    limits=GuardLimits(preflight_timeout=120),
                                    recovery_parent=Path(self.temp.name) / 'owned')

    def tearDown(self):
        self.guard.close()
        self.temp.cleanup()

    def create(self, count):
        files = [{'id': 'f' + str(i), 'name': 'table-' + str(i) + '.csv', 'size': 1}
                 for i in range(count)]
        job = self.guard.create(files, prepare_only=True)['jobId']
        for item in files:
            self.guard.upload(job, item['id'], io.BytesIO(b'x'), 1)
        return job

    def test_large_batch_bounds_each_default_inspection_and_exposes_count(self):
        job = self.create(32)
        budgets = []
        def observed(source, workspace, *, cancel, timeout):
            budgets.append(timeout)
            return {'state': 'detected'}
        with patch.object(self.guard.adapter, 'preflight', side_effect=observed, create=True), \
             patch.object(self.guard.adapter, 'capabilities',
                          return_value={'files': [], 'formats': []}, create=True):
            self.guard.preflight(job)
            state = self.guard.status(job)
            self.assertEqual(state['preflightCompleted'], 32)
            self.assertEqual(state['preflightTotal'], 32)
            self.assertEqual(state['preflightTimeoutSeconds'], 15)
            self.assertEqual(budgets, [15] * 32)
            budgets.clear()
            self.guard.preflight(job, recheck=True, timeout_seconds=300)
            self.assertEqual(budgets, [300] * 32)
            self.assertEqual(self.guard.status(job)['preflightTimeoutSeconds'], 300)

    def test_single_file_keeps_normal_native_budget(self):
        job = self.create(1)
        budgets = []
        with patch.object(self.guard.adapter, 'preflight',
                          side_effect=lambda source, workspace, *, cancel, timeout:
                              budgets.append(timeout) or {'state': 'detected'}, create=True), \
             patch.object(self.guard.adapter, 'capabilities',
                          return_value={'files': [], 'formats': []}, create=True):
            self.guard.preflight(job)
        self.assertEqual(budgets, [120])


    def test_live_bulk_cancel_releases_preflight_gate_for_next_job(self):
        """A cancelled 510-file verification must not poison a fresh run."""
        job = self.create(510)
        entered = threading.Event()
        outcomes = []
        def blocking(source, workspace, *, cancel, timeout):
            entered.set()
            while not cancel.wait(.01):
                pass
            raise GuardError('CANCELLED', 'Interrupted native source inspection',
                             stage='preflight')
        def verify():
            try:
                self.guard.preflight(job)
            except GuardError as error:
                outcomes.append(error.code)
        with patch.object(self.guard.adapter, 'preflight', side_effect=blocking, create=True), \
             patch.object(self.guard.adapter, 'capabilities',
                          return_value={'files': [], 'formats': []}, create=True):
            worker = threading.Thread(target=verify, name='bulk-preflight-cancel')
            worker.start()
            self.assertTrue(entered.wait(3), 'Expected a live native preflight')
            before = self.guard.status(job)
            self.assertEqual(before['state'], 'checking')
            self.assertEqual(before['preflightTotal'], 510)
            self.guard.cancel(job)
            worker.join(3)
            self.assertFalse(worker.is_alive(), 'Cancelled preflight still owns global gate')
            self.assertEqual(outcomes, ['CANCELLED'])
            self.assertEqual(self.guard.status(job)['state'], 'cancelled')
        second = self.create(1)
        with patch.object(self.guard.adapter, 'preflight',
                          return_value={'state': 'detected'}, create=True), \
             patch.object(self.guard.adapter, 'capabilities',
                          return_value={'files': [], 'formats': []}, create=True):
            self.guard.preflight(second)
        self.assertEqual(self.guard.status(second)['state'], 'prepared')



class MissingDbfRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'table.csv'
        self.source.write_bytes(b'name,amount\nalpha,123\nbeta,456\n')
        self.engine = self.root / 'soffice.com'
        self.engine.write_bytes(b'not-executed')
        self.adapter = LibreOfficeAdapter(self.engine)
        self.native = SimpleNamespace(fingerprint='verified-native', import_services={CALC})
        self.adapter._local.batch = (self.native, self.office())
        self.adapter._local.proof = {
            'state': 'detected', 'registryFingerprint': 'verified-native',
            'documentServices': [CALC], 'selectedImportFilter': CSV_FILTER}
        self.adapter._local.import_filter = CSV_FILTER
        self.adapter._local.import_options = '44,34,76,1'
        self.recipe = ExportRecipe('dBase', '76')
        self.cancel = threading.Event()
        self.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def office(self, code='ENGINE_OUTPUT_MISSING', create_partial=False):
        class FakeUno:
            disabled = False
            import_timeout = 60
            def ensure_started(self, control, timeout):
                pass
            def request(self, value, *, cancel, timeout, on_stage):
                if create_partial:
                    (Path(value['output']) / 'table.dbf').write_bytes(b'existing')
                return {'ok': False, 'code': code, 'stage': 'export',
                        'timings': {'source': 'uno_document', 'importMs': 3, 'exportMs': 2}}
            def recycle(self):
                pass
        return FakeUno()

    def cli(self, source, target, workspace, **kwargs):
        self.calls.append((target, kwargs.get('convert_spec'), kwargs.get('import_filter')))
        output = workspace / 'output'
        output.mkdir(parents=True, exist_ok=True)
        path = output / 'table.dbf'
        path.write_bytes(native_dbf_sample())
        return [path]

    def convert(self):
        with patch.object(self.adapter, '_registry', return_value=self.native), \
             patch('app_backend.converter_guard.adapter.native_export_recipe',
                   return_value=self.recipe), \
             patch('app_backend.converter_guard.adapter.convert_path', side_effect=self.cli):
            return self.adapter._convert_native(
                self.source, 'dbf', self.root / 'work', cancel=self.cancel, timeout=90,
                on_started=lambda: None)

    def test_only_one_exact_native_cli_fallback_with_nonempty_header_and_warning(self):
        expected_source = self.source.read_bytes()
        output = self.convert()
        self.assertEqual(len(output), 1)
        self.assertTrue(_native_dbf_structure(output[0]))
        self.assertEqual(output[0].read_bytes(), native_dbf_sample())
        self.assertEqual(self.calls, [('dbf', 'dbf:dBase:76', CSV_FILTER + ':44,34,76,1')])
        self.assertEqual(self.adapter.execution_mode(), 'native_cli')
        info = self.adapter.export_info()
        self.assertEqual(info['executionReason'], 'native_cli_dbase_recovery')
        self.assertIn('NATIVE_DBF_CLI_RECOVERY', info['warnings'])
        self.assertEqual(info['resultChecks'][0]['status'], 'checked_subset')
        self.assertEqual(self.source.read_bytes(), expected_source)

    def test_unverified_import_never_retries_and_keeps_original_error(self):
        self.adapter._local.proof['registryFingerprint'] = 'other-native'
        with self.assertRaises(GuardError) as error:
            self.convert()
        self.assertEqual(error.exception.code, 'ENGINE_OUTPUT_MISSING')
        self.assertEqual(self.calls, [])

    def test_interaction_does_not_retry(self):
        self.adapter._local.batch = (self.native, self.office('EXPORT_INTERACTION_REQUIRED'))
        with self.assertRaises(GuardError) as error:
            self.convert()
        self.assertEqual(error.exception.code, 'EXPORT_INTERACTION_REQUIRED')
        self.assertEqual(self.calls, [])

    def test_existing_partial_output_is_never_replaced(self):
        self.adapter._local.batch = (self.native, self.office(create_partial=True))
        with self.assertRaises(GuardError) as error:
            self.convert()
        self.assertEqual(error.exception.code, 'ENGINE_OUTPUT_MISSING')
        self.assertEqual((self.root / 'work' / 'output' / 'table.dbf').read_bytes(), b'existing')
        self.assertEqual(self.calls, [])

    def test_invalid_native_cli_container_still_fails(self):
        def corrupt(source, target, workspace, **kwargs):
            self.calls.append('cli')
            output = workspace / 'output'
            output.mkdir(parents=True, exist_ok=True)
            path = output / 'table.dbf'
            path.write_bytes(b'garbage')
            return [path]
        with patch.object(self.adapter, '_registry', return_value=self.native), \
             patch('app_backend.converter_guard.adapter.native_export_recipe',
                   return_value=self.recipe), \
             patch('app_backend.converter_guard.adapter.convert_path', side_effect=corrupt):
            with self.assertRaises(GuardError) as error:
                self.adapter._convert_native(self.source, 'dbf', self.root / 'work',
                                             cancel=self.cancel, timeout=90,
                                             on_started=lambda: None)
        self.assertEqual(error.exception.code, 'ENGINE_OUTPUT_MISSING')
        self.assertFalse((self.root / 'work' / 'output' / 'table.dbf').exists())
        self.assertEqual(self.calls, ['cli'])


if __name__ == '__main__':
    unittest.main()
