"""Integrated policy/queue tests, including failed-output ownership."""
import io
from pathlib import Path
import tempfile
import time
import unittest

from app_backend.converter_guard import ConverterGuard, GuardLimits
from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard.adapter import LibreOfficeAdapter
from app_backend.converter_guard.routes import Registry


class FailedNative:
    available = True
    def capabilities(self, files, **_):
        return {'files': files, 'formats': []}
    def convert(self, source, target, workspace, **_):
        output = workspace / 'output'
        output.mkdir(parents=True)
        (output / 'damaged.html').write_text('<html><body></body></html>')
        raise GuardError('ENGINE_OUTPUT_CONTENT_LOSS', 'Controlled proven missing content', stage='result')


class IntegratedV20(unittest.TestCase):
    def test_failed_outputs_remain_in_bounded_diagnostics_without_any_ready_ticket(self):
        with tempfile.TemporaryDirectory() as folder:
            guard = ConverterGuard(adapter=FailedNative(), spool_parent=folder,
                limits=GuardLimits(rejected_output_bytes=8192, max_spool_bytes=16384))
            try:
                identity = guard.create([{'id': 'a', 'name': 'source.odp', 'size': 3}], formats=['html'])['jobId']
                guard.upload(identity, 'a', io.BytesIO(b'abc'), 3)
                guard.start(identity)
                until = time.monotonic() + 5
                while not guard.status(identity)['finished']:
                    self.assertLess(time.monotonic(), until)
                    time.sleep(.01)
                report = guard.report(identity)
                task = report['files'][0]['targets'][0]
                self.assertEqual(task['state'], 'error')
                self.assertEqual(task['code'], 'ENGINE_OUTPUT_CONTENT_LOSS')
                self.assertEqual(task['outputs'], [])
                diagnostic = next(row for row in task['resultChecks'] if row['check'] == 'rejected_output_diagnostic')
                self.assertFalse(diagnostic['ready'])
                self.assertEqual(diagnostic['receipt']['status'], 'captured')
                job = guard._jobs[identity]
                sample = job.root / diagnostic['receipt']['files'][0]['relativePath']
                self.assertTrue(sample.is_file())
                self.assertEqual(guard._tickets, {})
                self.assertEqual(job.diagnostic_quota_reserved, 8192)
                self.assertLessEqual(guard.storage_stats()['reservedBytes'], 16384)
                guard.release(identity, discard=True)
                self.assertFalse(sample.exists())
                self.assertEqual(guard.storage_stats()['reservedBytes'], 0)
            finally:
                guard.close()

    def test_confirmed_bad_dif_import_runs_once_and_does_not_block_the_next_source(self):
        class ImportFailure(FailedNative):
            calls = []
            def convert(self, source, target, workspace, **_):
                self.calls.append((source.read_bytes(), target))
                if source.read_bytes() == b'bad':
                    raise GuardError('DIF_NUMERIC_IMPORT_MISMATCH', 'controlled wrong numeric import', stage='import')
                output = workspace / 'output'
                output.mkdir(parents=True)
                path = output / ('good.' + target)
                path.write_bytes(b'controlled native boundary')
                return [path]
        adapter = ImportFailure()
        with tempfile.TemporaryDirectory() as folder:
            guard = ConverterGuard(adapter=adapter, spool_parent=folder)
            try:
                identity = guard.create([{'id': name, 'name': name + '.dif', 'size': 3}
                    for name in ('first', 'next')], formats=['pdf', 'odt', 'html'])['jobId']
                guard.upload(identity, 'first', io.BytesIO(b'bad'), 3)
                guard.upload(identity, 'next', io.BytesIO(b'yes'), 3)
                guard.start(identity)
                until = time.monotonic() + 5
                while not guard.status(identity)['finished']:
                    self.assertLess(time.monotonic(), until)
                    time.sleep(.01)
                report = guard.report(identity)
                self.assertEqual(len(adapter.calls), 4)
                self.assertEqual(report['failed'], 3)
                self.assertEqual(report['succeeded'], 3)
                self.assertTrue(all(task['code'] == 'DIF_NUMERIC_IMPORT_MISMATCH'
                    for task in report['files'][0]['targets']))
                self.assertEqual(report['buildIdentity']['scope'], 'report_runtime')
                guard.release(identity, discard=True)
            finally:
                guard.close()

    def test_windows_link_exceptions_are_exact_scoped_and_do_not_hide_other_skips(self):
        from tools.qa.guard_v17_selfhosted_acceptance import windows_fixture_unavailable
        name = 'test_comparison_recovery_v20.ComparisonRecoverySafety.test_dangling_catalogue_link_cannot_create_an_external_database'
        self.assertTrue(windows_fixture_unavailable(name, 'Host cannot create the controlled symlink'))
        self.assertFalse(windows_fixture_unavailable('other.test', 'Host cannot create the controlled symlink'))
        self.assertFalse(windows_fixture_unavailable(name, 'Qt dependency missing'))

    def test_diagnostic_policy_is_off_by_default_and_never_reintroduces_conversion_mb_limit(self):
        limits = GuardLimits()
        self.assertEqual(limits.rejected_output_bytes, 0)
        self.assertIsNone(limits.max_file_bytes)
        self.assertIsNone(limits.max_spool_bytes)
        for value in (-1, True, 8 * 1024 * 1024 + 1):
            with self.assertRaises(ValueError):
                GuardLimits(rejected_output_bytes=value)

    def test_unknown_dif_inspection_warning_remains_visible_in_task_report(self):
        from app_backend.converter_guard.export_recipe import warning_details
        rows = warning_details(['DIF_NUMERIC_CONTENT_NOT_CHECKED'])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['code'], 'DIF_NUMERIC_CONTENT_NOT_CHECKED')
        self.assertIn('не подтверждено', rows[0]['message'])

    def test_native_apng_registry_cannot_bypass_the_actual_encoder_contract(self):
        class Native:
            fingerprint = 'installed'
            def observed_decision(self, target, proof):
                return 'registered'
        native = Native()
        proof = {'state': 'detected', 'registryFingerprint': 'installed'}
        self.assertEqual(LibreOfficeAdapter._route_with_registry(native, 'apng', proof), 'unavailable')
        # No blanket source/target blocking: PNG is still delegated to LO.
        self.assertEqual(LibreOfficeAdapter._route_with_registry(native, 'png', proof), 'registered')
        proof.update(mediaProfile={'format': 'png'}, qtMediaTargets=['apng'])
        self.assertEqual(LibreOfficeAdapter._route_with_registry(native, 'apng', proof), 'registered')
