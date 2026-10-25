"""Durable session limits, explicit resume and failure isolation regressions."""
from contextlib import closing
from concurrent.futures import Future
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app_backend.converter_guard import ConverterGuard, GuardError
from app_backend.converter_guard.comparison_service import ComparisonService
from test_comparison_service_v19 import ControlledNativeAdapter
from test_converter_guard_v02 import wait_finished


class ComparisonSessions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.adapter = ControlledNativeAdapter()
        self.guard = ConverterGuard(adapter=self.adapter, recovery_parent=self.root / 'owned')
        self.service = ComparisonService(self.guard)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        try:
            self.service.close()
        finally:
            self.guard.close()
            self.temp.cleanup()

    def converted(self, count=1, *, target='txt'):
        data = b'key,value\nA,42\n'
        rows = [{'id': str(index), 'name': str(index) + '.csv', 'size': len(data)}
                for index in range(count)]
        identity = self.guard.create(rows, [target], retain_sources=True)['jobId']
        for row in rows:
            self.guard.upload(identity, row['id'], io.BytesIO(data), len(data))
        self.guard.start(identity, archive_mode='on_demand')
        wait_finished(self.guard, identity)
        return identity

    def checked(self, item, records, root):
        for record in records:
            self.service._store(item, record, {'status': 'checked', 'findings': [],
                'checkedFeatures': ['identical_file_bytes'], 'recipeCandidates': []})

    def analyze(self, identity):
        with patch.object(self.service, '_worker', side_effect=self.checked):
            self.service.start(identity)
            self.service.jobs[identity].future.result(5)
        return self.service.snapshot(identity)

    def recreate(self):
        self.service.close()
        self.service = ComparisonService(self.guard)

    def test_completed_report_recovery_is_frozen_and_does_not_export(self):
        identity = self.converted(3)
        before = self.analyze(identity)
        exports = list(self.adapter.calls)
        self.recreate()
        recovered = self.service.start(identity)
        self.assertEqual(recovered['analysisId'], before['analysisId'])
        self.assertEqual((recovered['state'], recovered['processed']), ('completed', 3))
        self.assertTrue(recovered['recovered'])
        self.assertIsNone(self.service.jobs[identity].future)
        self.assertEqual(self.adapter.calls, exports)

    def test_interrupted_metadata_with_complete_rows_resumes_without_rescanning(self):
        identity = self.converted(2)
        before = self.analyze(identity)
        item = self.service.jobs[identity]
        with closing(sqlite3.connect(item.catalog)) as db, db:
            value = json.loads(db.execute('SELECT payload FROM sessions WHERE id=?', (item.identity,)).fetchone()[0])
            value['state'] = 'running'  # Crash after row commit, before terminal checkpoint.
            db.execute('UPDATE sessions SET payload=? WHERE id=?', (json.dumps(value), item.identity))
        self.recreate()
        recovered = self.service.start(identity)
        self.assertEqual((recovered['state'], recovered['processed']), ('interrupted', 2))
        self.assertTrue(recovered['canResume'])
        with patch.object(self.service, '_worker', side_effect=AssertionError('Must reuse committed rows')):
            self.service.control(identity, 'resume', expected_analysis_id=before['analysisId'])
            self.service.jobs[identity].future.result(5)
        resumed = self.service.snapshot(identity)
        self.assertEqual((resumed['analysisId'], resumed['state'], resumed['processed']),
                         (before['analysisId'], 'completed', 2))

    def test_missing_committed_rows_cannot_recover_as_complete(self):
        identity = self.converted(3)
        before = self.analyze(identity)
        item = self.service.jobs[identity]
        with closing(sqlite3.connect(item.db)) as db, db:
            db.execute('DELETE FROM results WHERE task=?', (identity + ':1',))
        cached = self.service.snapshot(identity)
        self.assertEqual((cached['state'], cached['processed']), ('interrupted', 2))
        self.recreate()
        recovered = self.service.snapshot(identity)
        self.assertEqual((recovered['state'], recovered['processed']), ('interrupted', 2))
        inspected = []
        def worker(item, records, root):
            inspected.extend(row['taskId'] for row in records)
            self.checked(item, records, root)
        with patch.object(self.service, '_worker', side_effect=worker):
            self.service.control(identity, 'resume')
            self.service.jobs[identity].future.result(5)
        self.assertEqual(inspected, [identity + ':1'])
        self.assertEqual(self.service.snapshot(identity)['analysisId'], before['analysisId'])

    def test_session_limit_preserves_original_report_and_conversion(self):
        identity = self.converted()
        original = self.analyze(identity)
        with patch('app_backend.converter_guard.comparison_service.MAX_SESSIONS', 1):
            with self.assertRaises(GuardError) as caught:
                self.service.start(identity, restart=True)
        self.assertEqual(caught.exception.code, 'COMPARISON_SESSION_LIMIT')
        self.assertEqual(self.service.snapshot(identity)['analysisId'], original['analysisId'])
        self.assertEqual(self.guard.status(identity)['succeeded'], 1)
        self.assertEqual(self.guard._jobs[identity].io, 0)

    def test_mutable_report_growth_between_stats_is_not_foreign_path_detection(self):
        identity = self.converted()
        self.analyze(identity)
        item = self.service.jobs[identity]
        comparison_path = self.guard._comparison_path
        changed = []
        def grows_after_size_snapshot(job, path, expected_size):
            if Path(path) == item.db and not changed:
                changed.append(True)
                with closing(sqlite3.connect(item.db)) as db, db:
                    db.execute('CREATE TABLE controlled_page_commit(value TEXT)')
                self.assertNotEqual(item.db.stat().st_size, expected_size)
            return comparison_path(job, path, expected_size)
        with patch.object(self.guard, '_comparison_path', side_effect=grows_after_size_snapshot):
            report = self.service.snapshot(identity)
        self.assertEqual((report['state'], report['processed']), ('completed', 1))
        self.assertEqual(changed, [True])

    def test_persistence_failure_does_not_damage_outputs_or_hold_an_input_lease(self):
        identity = self.converted()
        connect = sqlite3.connect
        def unavailable(path, *args, **kwargs):
            if 'sessions.sqlite' in str(path):
                raise sqlite3.OperationalError('controlled write failure')
            return connect(path, *args, **kwargs)
        with patch('app_backend.converter_guard.comparison_service.sqlite3.connect', side_effect=unavailable):
            with self.assertRaises(GuardError):
                self.service.start(identity)
        self.assertEqual(self.guard._jobs[identity].io, 0)
        artifact = self.guard._jobs[identity].artifacts[0]
        with self.guard.open_result(artifact.ticket) as (stream, _, __):
            self.assertTrue(stream.read())
        self.assertEqual(self.analyze(identity)['processed'], 1)

    def test_stale_session_commands_cannot_cancel_or_restart_current_analysis(self):
        identity = self.converted()
        first = self.analyze(identity)
        with patch.object(self.service, '_worker', side_effect=self.checked):
            self.service.start(identity, restart=True, expected_analysis_id=first['analysisId'])
            self.service.jobs[identity].future.result(5)
        second = self.service.snapshot(identity)
        self.assertNotEqual(second['analysisId'], first['analysisId'])
        for operation in (lambda: self.service.control(identity, 'cancel', expected_analysis_id=first['analysisId']),
                          lambda: self.service.start(identity, restart=True, expected_analysis_id=first['analysisId']),
                          lambda: self.service.detail(identity, identity + ':0', expected_analysis_id=first['analysisId'])):
            with self.assertRaises(GuardError) as caught:
                operation()
            self.assertEqual(caught.exception.code, 'COMPARISON_STALE')
        self.assertEqual(self.service.snapshot(identity)['analysisId'], second['analysisId'])
        self.assertFalse(self.service.jobs[identity].cancel.is_set())

    def test_corrupt_result_payload_returns_structured_error_and_keeps_outputs(self):
        identity = self.converted()
        self.analyze(identity)
        item = self.service.jobs[identity]
        with closing(sqlite3.connect(item.db)) as db, db:
            db.execute('UPDATE results SET payload=?', ('[]',))
        with self.assertRaises(GuardError) as caught:
            self.service.snapshot(identity)
        self.assertEqual(caught.exception.code, 'COMPARISON_REPORT_UNAVAILABLE')
        self.assertEqual(self.guard.status(identity)['succeeded'], 1)
        previous_identity = item.identity
        self.recreate()
        with patch.object(self.service, '_worker', side_effect=self.checked):
            restored = self.service.start(identity, restart=True, expected_analysis_id=previous_identity)
            self.service.jobs[identity].future.result(5)
        self.assertNotEqual(restored['analysisId'], previous_identity)
        self.assertEqual(self.service.snapshot(identity)['state'], 'completed')

    def test_retry_checks_selected_old_bytes_before_creating_candidate(self):
        self.adapter.break_csv = True
        identity = self.converted(target='csv')
        self.service.start(identity)
        self.service.jobs[identity].future.result(5)
        report = self.service.snapshot(identity)
        self.assertEqual(report['items'][0]['status'], 'confirmed_loss')
        artifact = self.guard._jobs[identity].artifacts[0]
        artifact.path.write_bytes(artifact.path.read_bytes().replace(b',0\n', b',9\n'))
        count = len(self.guard._jobs)
        self.service.retry(identity, report['analysisId'], [identity + ':0'])
        self.service.jobs[identity].retry_future.result(5)
        after = self.service.snapshot(identity)
        self.assertEqual(after['retry']['state'], 'failed')
        self.assertEqual(after['retry']['jobIds'], [])
        self.assertEqual(after['retry']['verification']['assessment'], 'not_comparable')
        self.assertEqual(len(self.guard._jobs), count)
        history = self.service.retry_history(identity, run_id=after['retry']['runId'])
        self.assertEqual(history['total'], 1)
        self.assertEqual(history['items'][0]['assessment'], 'not_comparable')
        self.assertEqual(self.guard._jobs[identity].io, 0)

    def test_worker_drains_final_flush_after_process_exit(self):
        self._assert_final_worker_flush(overlong=False)

    def test_queued_retry_cancel_releases_lease_and_records_every_selected_pair(self):
        self.adapter.break_csv = True
        identity = self.converted(target='csv')
        self.service.start(identity)
        self.service.jobs[identity].future.result(5)
        report = self.service.snapshot(identity)
        queued = Future()
        with patch.object(self.service.retry_executor, 'submit', return_value=queued):
            accepted = self.service.retry(identity, report['analysisId'], [identity + ':0'])
        self.assertGreater(self.guard._jobs[identity].io, 0)
        cancelled = self.service.control(identity, 'cancel')
        self.assertTrue(queued.cancelled())
        self.assertEqual(cancelled['retry']['state'], 'cancelled')
        self.assertEqual(self.guard._jobs[identity].io, 0)
        rows = self.service.retry_history(identity, run_id=accepted['retry']['runId'])
        self.assertEqual(rows['total'], 1)
        self.assertEqual(rows['items'][0]['parentTaskId'], identity + ':0')
        self.assertEqual(rows['items'][0]['assessment'], 'not_comparable')
        self.assertEqual(len(self.guard._jobs), 1)

    def test_worker_drops_overlong_line_and_keeps_following_final_outcome(self):
        self._assert_final_worker_flush(overlong=True)

    def test_only_one_retry_batch_holds_a_lease_and_next_parent_can_retry_after_cancel(self):
        self.adapter.break_csv = True
        reports = []
        for _ in range(2):
            identity = self.converted(target='csv')
            self.service.start(identity)
            self.service.jobs[identity].future.result(5)
            reports.append(self.service.snapshot(identity))
        first, second = reports
        pending = Future()
        with patch.object(self.service.retry_executor, 'submit', return_value=pending):
            self.service.retry(first['jobId'], first['analysisId'], [first['jobId'] + ':0'])
        before_jobs = set(self.guard._jobs)
        with patch.object(self.service, '_lease', wraps=self.service._lease) as leases:
            with self.assertRaises(GuardError) as caught:
                self.service.retry(second['jobId'], second['analysisId'], [second['jobId'] + ':0'])
            self.assertEqual(caught.exception.code, 'COMPARISON_RETRY_BUSY')
            self.assertEqual(leases.call_count, 0)
        self.assertEqual(set(self.guard._jobs), before_jobs)
        self.assertEqual(self.guard._jobs[second['jobId']].io, 0)
        self.assertEqual(self.service.retry_history(second['jobId'])['total'], 0)
        self.service.control(first['jobId'], 'cancel')
        self.assertTrue(pending.cancelled())
        next_pending = Future()
        with patch.object(self.service.retry_executor, 'submit', return_value=next_pending):
            accepted = self.service.retry(second['jobId'], second['analysisId'], [second['jobId'] + ':0'])
        self.assertEqual(accepted['retry']['state'], 'queued')
        self.assertEqual(accepted['activeRetryJobId'], second['jobId'])
        self.service.control(second['jobId'], 'cancel')
        self.assertTrue(next_pending.cancelled())
        self.assertEqual(self.guard._jobs[second['jobId']].io, 0)

    def _assert_final_worker_flush(self, *, overlong):
        identity = self.converted()
        class ExitsWithFinalFlush:
            def __init__(inner, command, **kwargs):
                inner.root = Path(command[-1]).parent
                inner.process = inner
            def __enter__(inner): return inner
            def __exit__(inner, *args): return False
            def poll(inner):
                # The writer exits after the parent's preceding EOF read.
                outcome = {'taskId': identity + ':0', 'outcome': {'status': 'checked',
                    'findings': [], 'checkedFeatures': ['identical_file_bytes'], 'recipeCandidates': []}}
                prefix = b'x' * (256 * 1024 + 20) + b'\n' if overlong else b''
                (inner.root / 'outcomes.jsonl').write_bytes(prefix + json.dumps(outcome).encode() + b'\n')
                return 0
        with patch('app_backend.converter_guard.comparison_service.ProcessTree', ExitsWithFinalFlush):
            self.service.start(identity)
            self.service.jobs[identity].future.result(5)
        report = self.service.snapshot(identity)
        self.assertEqual((report['processed'], report['summary']['checked']), (1, 1))
        self.assertEqual(report['items'][0]['checkedFeatures'], ['identical_file_bytes'])
