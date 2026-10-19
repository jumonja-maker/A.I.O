"""Complete user plans, native-attempt eligibility and save/result contracts."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from app_backend.converter_guard import ConverterGuard, GuardLimits, LibreOfficeAdapter
from test_converter_guard_v02 import FakeAdapter, wait_finished
from test_converter_guard_v04 import RoutedAdapter, write_registry


class GuardV05Contracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.exe, _ = write_registry(self.temp.name)
        self.guard = ConverterGuard(adapter=RoutedAdapter(self.exe),
                                    spool_parent=self.temp.name)

    def tearDown(self):
        self.guard.close()
        self.assertEqual(self.guard.storage_stats()['jobs'], 0)
        self.assertEqual(self.guard.storage_stats()['reservedBytes'], 0)
        self.temp.cleanup()

    def run_job(self, name='picture.bmp', content=b'D', formats=('pdf',)):
        job = self.guard.create([{'id': 'one', 'name': name, 'size': len(content)}],
                                list(formats))
        self.guard.upload(job['jobId'], 'one', io.BytesIO(content), len(content))
        self.guard.start(job['jobId'])
        return wait_finished(self.guard, job['jobId'])

    def assert_counts(self, report, **expected):
        self.assertEqual(report['total'], report['succeeded'] + report['failed'] +
                         report['cancelled'] + report['filtered'] + report['pending'])
        self.assertEqual(report['completed'], report['total'] - report['pending'])
        for key, value in expected.items():
            self.assertEqual(report[key], value, key)

    def test_filename_warning_keeps_explicit_native_attempt_available_without_probe(self):
        adapter = LibreOfficeAdapter(self.exe)
        with patch('app_backend.converter_guard.adapter.convert_path',
                   side_effect=AssertionError('metadata must not launch LO')):
            report = adapter.capabilities([{'id': 'one', 'name': 'x.bmp', 'size': 2**40}])
        dbf = next(row for row in report['formats'] if row['id'] == 'dbf')
        self.assertFalse(dbf['enabled'])  # still an honest filename recommendation
        self.assertEqual(dbf['compatibleFileIds'], [])
        self.assertEqual(dbf['incompatibleFileIds'], ['one'])
        self.assertTrue(dbf['nativeAttemptAvailable'])
        self.assertEqual(dbf['selectableFileIds'], ['one'])
        self.assertFalse(report['qualityCompared'])

    def test_unknown_file_can_be_selected_for_every_native_target(self):
        formats = LibreOfficeAdapter(self.exe).capabilities([
            {'id': 'unknown', 'name': 'document.unlisted', 'size': 1}])['formats']
        self.assertTrue(all(f['nativeAttemptAvailable'] for f in formats))
        self.assertTrue(all(f['selectableFileIds'] == ['unknown'] for f in formats))

    def test_missing_engine_cannot_claim_native_attempt_available(self):
        formats = LibreOfficeAdapter(Path(self.temp.name) / 'absent').capabilities([
            {'id': 'one', 'name': 'x.bmp', 'size': 1}])['formats']
        self.assertTrue(all(not f['nativeAttemptAvailable'] for f in formats))
        self.assertTrue(all(f['selectableFileIds'] == [] for f in formats))

    def test_single_result_has_explicit_ticket_for_frontend_and_confirmed_save(self):
        done = self.run_job()
        result = done['result']
        self.assertEqual(result['ticket'], result['downloadUrl'].split('ticket=')[1])
        with self.guard.open_result(result['ticket']) as (stream, _, _):
            self.assertEqual(stream.read(), b'data')
        saved = self.guard.confirm_saved(done['jobId'], result['ticket'])
        self.assertEqual(saved['files'][0]['targets'][0]['state'], 'saved')
        self.assert_counts(saved, succeeded=1, failed=0, cancelled=0, filtered=0, pending=0)

    def test_all_requested_pairs_and_reasons_survive_in_status_and_zip_report(self):
        done = self.run_job(formats=('pdf', 'dbf', 'docx', 'png'))
        self.assert_counts(done, total=4, attempted=2, succeeded=2, filtered=2,
                           failed=0, cancelled=0, pending=0)
        self.assertEqual(done['state'], 'ready')
        self.assertEqual([r['format'] for r in done['files'][0]['targets']],
                         ['pdf', 'dbf', 'docx', 'png'])
        with self.guard.open_result(done['result']['ticket']) as (stream, _, _):
            with zipfile.ZipFile(stream) as archive:
                report = json.loads(archive.read('AIO-conversion-report.json'))
                self.assert_counts(report, total=4, attempted=2, succeeded=2, filtered=2)
                self.assertNotIn('ticket', json.dumps(report))
                self.assertEqual(report['files'][0]['targets'][1]['stage'], 'route')

    def test_broad_plan_for_renamed_document_uses_actual_model_and_never_drops_pairs(self):
        done = self.run_job(name='renamed.bmp', content=b'W',
                            formats=('pdf', 'docx', 'odt', 'dbf', 'png', 'custom'))
        self.assertEqual([r['format'] for r in done['files'][0]['targets']],
                         ['pdf', 'docx', 'odt', 'dbf', 'png', 'custom'])
        self.assert_counts(done, total=6, attempted=4, succeeded=4, filtered=2)
        self.assertIn(('renamed.bmp', 'docx'), self.guard.adapter.calls)
        self.assertIn(('renamed.bmp', 'custom'), self.guard.adapter.calls)

    def test_counts_separate_real_native_failure_from_unattempted_routes(self):
        done = self.run_job(formats=('dbf', 'pdf', 'docx', 'png'))
        self.assert_counts(done, total=4, attempted=3, succeeded=2, failed=1,
                           filtered=1, pending=0)
        self.assertEqual(self.guard.retry_plan(done['jobId'])['plan'],
                         [{'fileId': 'one', 'formats': ['dbf']}])

    def test_pending_and_cancelled_are_disjoint_from_errors_and_skips(self):
        created = self.guard.create([{'id': 'one', 'name': 'x.bmp', 'size': 1}],
                                    ['pdf', 'dbf'])
        self.assert_counts(created, total=2, pending=2, completed=0, attempted=0)
        cancelled = self.guard.cancel(created['jobId'])
        self.assert_counts(cancelled, total=2, pending=0, completed=2,
                           cancelled=2, failed=0, filtered=0)

    def test_packaging_quota_keeps_first_artifact_saveable_with_explicit_ticket(self):
        self.guard.close()
        self.guard = ConverterGuard(adapter=FakeAdapter(), spool_parent=self.temp.name,
                                    limits=GuardLimits(max_spool_bytes=20))
        done = self.run_job(formats=('pdf', 'png'))
        self.assertEqual(done['packagingError'], 'BUNDLE_QUOTA')
        self.assertEqual(done['result']['ticket'], done['result']['outputs'][0]['ticket'])
        with self.guard.open_result(done['result']['ticket']) as (stream, _, _):
            self.assertEqual(stream.read(), b'data')


if __name__ == '__main__':
    unittest.main()
