"""Comparison evidence ownership and failure lifecycle regressions."""
from contextlib import closing
import io
import hashlib
import tempfile
import json
from pathlib import Path
import sqlite3
import subprocess
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from app_backend.converter_guard import GuardError
import test_comparison_sessions_v19 as fixtures


class ComparisonLifecycleV21(unittest.TestCase):
    setUp = fixtures.ComparisonSessions.setUp
    cleanup = fixtures.ComparisonSessions.cleanup
    converted = fixtures.ComparisonSessions.converted
    checked = fixtures.ComparisonSessions.checked
    analyze = fixtures.ComparisonSessions.analyze
    recreate = fixtures.ComparisonSessions.recreate

    def test_partial_output_bundle_is_not_a_successful_comparison(self):
        convert = self.adapter.convert
        def bundled(*args, **kwargs):
            paths = convert(*args, **kwargs)
            extra = paths[0].with_name('required-resource.bin')
            extra.write_bytes(b'owned sidecar bytes')
            return [*paths, extra]
        with patch.object(self.adapter, 'convert', side_effect=bundled):
            identity = self.converted(target='csv')
        job = self.guard._jobs[identity]
        self.assertEqual(len(job.artifacts), 2)
        missing = next(a for a in job.artifacts if a.path.name.endswith('.bin'))
        missing.path.unlink()
        self.service.start(identity)
        self.service.jobs[identity].future.result(8)
        report = self.service.snapshot(identity)
        row = report['items'][0]
        self.assertEqual(row['status'], 'not_checked', row)
        self.assertEqual(row['findings'][0]['code'], 'RESULT_UNAVAILABLE')
        self.assertEqual(row['outputScope']['totalFiles'], 2)
        self.assertEqual(row['outputScope']['unavailableFiles'], 1)
        self.assertEqual(row['recipeCandidates'], [])
        self.assertEqual(job.artifacts[0].path.read_bytes(), b'key,value\nA,42\n')
        self.assertEqual(job.io, 0)

    def test_retry_history_failure_stops_its_running_child_and_preserves_parent(self):
        self.adapter.break_csv = True
        identity = self.converted(target='csv')
        self.service.start(identity)
        parent = self.service.jobs[identity]
        parent.future.result(8)
        report = self.service.snapshot(identity)
        self.assertTrue(report['items'][0]['recipeCandidates'])
        original = self.guard._jobs[identity].artifacts[0].path.read_bytes()
        entered = threading.Event()
        convert = self.adapter.convert
        def slow_candidate(*args, **kwargs):
            if self.adapter.recipe is not None:
                entered.set()
                if kwargs['cancel'].wait(4):
                    raise GuardError('CANCELLED', 'controlled child cancellation')
            return convert(*args, **kwargs)
        event = self.service._retry_event
        def failing_event(item, *, strict=False):
            if strict and item.retry.get('state') == 'running':
                self.assertTrue(entered.wait(3))
                raise GuardError('COMPARISON_PERSISTENCE_FAILED', 'controlled full history disk', status=507)
            return event(item, strict=strict)
        with patch.object(self.adapter, 'convert', side_effect=slow_candidate), \
                patch.object(self.service, '_retry_event', side_effect=failing_event), \
                patch.object(self.guard, 'cancel', wraps=self.guard.cancel) as cancelled:
            self.service.retry(identity, report['analysisId'], [identity + ':0'])
            parent.retry_future.result(8)
            child_id = parent.retry_jobs[0]
            self.assertTrue(self.guard._jobs[child_id].cancel.is_set(),
                            'A failed orchestrator left a native child running')
            self.guard._jobs[child_id].future.result(5)
            self.assertIn(child_id, [call.args[0] for call in cancelled.call_args_list])
        self.assertEqual(parent.retry['state'], 'failed')
        self.assertEqual(self.guard._jobs[identity].artifacts[0].path.read_bytes(), original)
        self.assertEqual(parent.retry['verification']['assessment'], 'not_comparable')
        self.assertEqual(self.guard._jobs[identity].io, 0)

    def test_revalidated_rows_refresh_summary_even_when_count_is_unchanged(self):
        identity = self.converted()
        self.analyze(identity)
        item = self.service.jobs[identity]
        with closing(sqlite3.connect(item.db)) as db, db:
            row = db.execute('SELECT task,payload FROM results').fetchone()
            payload = json.loads(row[1])
            payload.update(status='uncertain', findings=[{'code': 'CONTROLLED_RECHECK',
                'category': 'uncertain', 'message': 'Controlled revalidated evidence'}])
            db.execute('UPDATE results SET status=?,payload=? WHERE task=?',
                       ('uncertain', json.dumps(payload), row[0]))
        report = self.service.snapshot(identity)
        self.assertEqual(report['items'][0]['status'], 'uncertain')
        self.assertEqual(report['summary']['uncertain'], 1)
        self.assertEqual(report['summary']['checked'], 0)
        self.assertEqual(sum(report['summary'].values()), report['processed'])

    def test_recovery_rejects_a_catalogue_payload_bound_to_another_analysis_id(self):
        identity = self.converted()
        first = self.analyze(identity)
        first_item = self.service.jobs[identity]
        with patch.object(self.service, '_worker', side_effect=self.checked):
            self.service.start(identity, restart=True, expected_analysis_id=first['analysisId'])
            self.service.jobs[identity].future.result(5)
        second = self.service.jobs[identity]
        with closing(sqlite3.connect(second.catalog)) as db, db:
            other = db.execute('SELECT payload FROM sessions WHERE id=?', (first_item.identity,)).fetchone()[0]
            db.execute('UPDATE sessions SET payload=? WHERE id=?', (other, second.identity))
        self.recreate()
        with self.assertRaises(GuardError) as caught:
            self.service.snapshot(identity)
        self.assertEqual(caught.exception.code, 'COMPARISON_REPORT_UNAVAILABLE')
        self.assertEqual(self.guard.status(identity)['succeeded'], 1)

    def test_stale_preview_token_cannot_open_new_session(self):
        identity = self.converted()
        first = self.analyze(identity)
        detail = self.service.detail(identity, identity + ':0', expected_analysis_id=first['analysisId'])
        query = parse_qs(urlparse(detail['previews']['result']['url']).query)
        self.assertEqual(query.get('analysisId'), [first['analysisId']])
        with patch.object(self.service, '_worker', side_effect=self.checked):
            self.service.start(identity, restart=True, expected_analysis_id=first['analysisId'])
            self.service.jobs[identity].future.result(5)
        with patch.object(self.service, '_text_preview') as decode:
            with self.assertRaises(GuardError) as caught:
                with self.service.preview(identity, identity + ':0', 'result',
                        expected_analysis_id=first['analysisId']):
                    self.fail('stale preview was accepted')
        self.assertEqual(caught.exception.code, 'COMPARISON_STALE')
        self.assertFalse(decode.called)

    def test_control_race_cannot_change_a_superseded_session_even_without_token(self):
        identity = self.converted()
        first = self.analyze(identity)
        old = self.service.jobs[identity]
        get = self.service._get
        replaced = []
        def replace_after_read(job_id):
            current = get(job_id)
            if not replaced:
                replaced.append(True)
                with patch.object(self.service, '_get', side_effect=get), \
                        patch.object(self.service, '_worker', side_effect=self.checked):
                    self.service.start(identity, restart=True, expected_analysis_id=first['analysisId'])
                    self.service.jobs[identity].future.result(5)
            return current
        with patch.object(self.service, '_get', side_effect=replace_after_read):
            with self.assertRaises(GuardError) as caught:
                self.service.control(identity, 'cancel')
        self.assertEqual(caught.exception.code, 'COMPARISON_STALE')
        self.assertFalse(old.cancel.is_set())
        self.assertFalse(self.service.jobs[identity].cancel.is_set())

    def test_release_forgets_settled_indexes_but_detach_retains_recoverable_history(self):
        identity = self.converted(3)
        self.analyze(identity)
        item = self.service.jobs[identity]
        self.assertEqual(len(item.expected_tasks), 3)
        self.guard.release(identity)  # Unsaved recovery-owned results retained.
        self.service.release(identity)
        self.assertIn(identity, self.service.jobs)
        self.assertEqual(self.service.snapshot(identity)['processed'], 3)
        self.guard.release(identity, discard=True)
        self.service.release(identity)
        self.assertNotIn(identity, self.service.jobs)
        self.assertEqual(item.expected_tasks, {})
        self.assertFalse(item.root.exists())

    def test_release_of_running_analysis_drains_lease_before_forgetting_cache(self):
        identity = self.converted(2)
        entered = threading.Event()
        def blocked(item, records, root):
            entered.set()
            self.assertTrue(item.cancel.wait(4))
        with patch.object(self.service, '_worker', side_effect=blocked):
            self.service.start(identity)
            self.assertTrue(entered.wait(3))
            item = self.service.jobs[identity]
            root = self.guard._jobs[identity].root
            self.guard.release(identity, discard=True)
            self.assertTrue(root.exists(), 'Live inspection lease owns the source')
            settled = threading.Event()
            item.future.add_done_callback(lambda future: settled.set())
            self.service.release(identity)
            item.future.result(5)
            self.assertTrue(settled.wait(3))
        self.assertNotIn(identity, self.service.jobs)
        self.assertFalse(root.exists())
        self.assertEqual(item.expected_tasks, {})

    def test_recovered_metadata_cannot_hide_task_count(self):
        identity = self.converted(2)
        self.analyze(identity)
        item = self.service.jobs[identity]
        with closing(sqlite3.connect(item.catalog)) as db, db:
            value = json.loads(db.execute('SELECT payload FROM sessions WHERE id=?', (item.identity,)).fetchone()[0])
            value['total'] = 1
            db.execute('UPDATE sessions SET payload=? WHERE id=?', (json.dumps(value), item.identity))
        with closing(sqlite3.connect(item.db)) as db, db:
            db.execute('DELETE FROM results WHERE task=?', (identity + ':1',))
        self.recreate()
        with self.assertRaises(GuardError) as caught:
            self.service.snapshot(identity)
        self.assertEqual(caught.exception.code, 'COMPARISON_REPORT_UNAVAILABLE')
        self.assertEqual(self.guard.status(identity)['succeeded'], 2)

    def test_real_worker_receives_the_retained_html_companion_identity(self):
        from test_converter_guard_v02 import wait_finished
        content = b'<html><body>Text<img src="images/plot.png"></body></html>'
        identity = self.guard.create([{'id': 'html', 'name': 'index.html', 'size': len(content)}],
                                     prepare_only=True, retain_sources=True)['jobId']
        self.guard.upload(identity, 'html', io.BytesIO(content), len(content))
        self.guard.configure_resources(identity, 'html', [{'path': 'images/plot.png', 'size': 5}])
        self.guard.upload_resource(identity, 'html', 'images/plot.png', io.BytesIO(b'IMAGE'), 5)
        self.guard.preflight(identity)
        self.guard.start(identity, file_ids=['html'], formats=['html'], only_verified=True,
                         archive_mode='on_demand')
        wait_finished(self.guard, identity)
        with self.guard.comparison_inputs(identity) as records:
            resource = records[0]['resources'][0]['source']
        self.service.start(identity)
        self.service.jobs[identity].future.result(8)
        old = self.service.snapshot(identity)
        self.assertEqual(old['items'][0]['status'], 'checked', old)
        resource.write_bytes(b'OTHER')  # Same size; size-only admission still succeeds.
        self.service.start(identity, restart=True, expected_analysis_id=old['analysisId'])
        self.service.jobs[identity].future.result(8)
        report = self.service.snapshot(identity)
        self.assertEqual(report['items'][0]['status'], 'not_checked', report)
        self.assertEqual(report['items'][0]['findings'][0]['code'], 'SOURCE_CHANGED')
        self.assertEqual(self.guard._jobs[identity].artifacts[0].path.read_bytes(), content)
        self.assertEqual(self.guard._jobs[identity].io, 0)


    def test_dangling_catalogue_pointer_requires_explicit_restart(self):
        identity = self.converted()
        original = self.analyze(identity)
        item = self.service.jobs[identity]
        original_report = item.db.read_bytes()
        with closing(sqlite3.connect(item.catalog)) as db, db:
            db.execute('DELETE FROM sessions WHERE id=?', (item.identity,))
        self.recreate()
        with patch.object(self.service, '_worker', side_effect=self.checked) as worker:
            with self.assertRaises(GuardError) as caught:
                self.service.start(identity)
            self.assertEqual(caught.exception.code, 'COMPARISON_REPORT_UNAVAILABLE')
            self.assertFalse(worker.called)
            self.service.start(identity, restart=True, expected_analysis_id=original['analysisId'])
            self.service.jobs[identity].future.result(5)
        self.assertNotEqual(self.service.snapshot(identity)['analysisId'], original['analysisId'])
        self.assertEqual(item.db.read_bytes(), original_report)
        self.assertEqual(self.guard.status(identity)['succeeded'], 1)

    def test_retry_task_history_is_bound_to_its_sql_task_and_original_pair(self):
        self.adapter.break_csv = True
        identity = self.converted(target='csv')
        self.service.start(identity)
        item = self.service.jobs[identity]
        item.future.result(8)
        self.service.retry(identity, item.identity, [identity + ':0'])
        item.retry_future.result(8)
        self.assertEqual(item.retry['state'], 'completed')
        run = item.retry['runId']
        baseline = self.service.retry_history(identity, run_id=run)
        self.assertEqual(baseline['items'][0]['assessment'], 'checked_subset_improved')
        with closing(sqlite3.connect(item.catalog)) as db:
            task, original = db.execute('SELECT task,payload FROM retry_tasks WHERE run=?', (run,)).fetchone()
        for key, replacement in [('parentTaskId', 'f' * 32 + ':999'), ('fileId', 'another-source'),
                                 ('format', 'pdf'), ('name', 'another-name.csv')]:
            with self.subTest(key=key):
                value = json.loads(original)
                value[key] = replacement
                with closing(sqlite3.connect(item.catalog)) as db, db:
                    db.execute('UPDATE retry_tasks SET payload=? WHERE run=? AND task=?',
                               (json.dumps(value), run, task))
                with self.assertRaises(GuardError) as caught:
                    self.service.retry_history(identity, run_id=run)
                self.assertEqual(caught.exception.code, 'COMPARISON_REPORT_UNAVAILABLE')
                with self.assertRaises(GuardError):
                    self.service._finish_verifications(item)
        self.assertEqual(self.guard.status(identity)['succeeded'], 1)

    def test_all_supported_raster_suffixes_are_dispatched_to_image_preview(self):
        from app_backend.converter_guard.media_fidelity import RASTER
        for suffix in sorted(RASTER):
            with self.subTest(suffix=suffix):
                path = self.root / ('metadata.' + suffix)
                path.write_bytes(b'header-not-decoded-by-metadata')
                metadata = self.service._preview_meta('a' * 32, 'a' * 32 + ':0', 'result', path)
                self.assertEqual(metadata['kind'], 'image')
                self.assertIn('исходном разрешении', metadata['note'])
                self.assertIn('доступны страницы и кадры', metadata['note'])

    def test_real_preview_decodes_jpeg_aliases_apng_and_labels_tiff_limit(self):
        from PySide6.QtGui import QImage, QImageReader
        from PySide6.QtCore import QBuffer, QIODevice
        from test_guard_media_fidelity_v16 import png, tiff
        from test_converter_guard_v02 import wait_finished
        image = QImage(12, 8, QImage.Format.Format_RGB32)
        image.fill(0xff3080a0)
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        self.assertTrue(image.save(buffer, 'JPEG'))
        jpeg = bytes(buffer.data())
        buffer.close()
        decoded = 0
        for suffix, content in [('jpe', jpeg), ('jfif', jpeg), ('jif', jpeg),
                                ('apng', png(frames=2)), ('tif', tiff(pages=2)), ('tiff', tiff(pages=2))]:
            with self.subTest(suffix=suffix):
                identity = self.guard.create([{'id': 'picture', 'name': 'photo.' + suffix, 'size': len(content)}],
                                             [suffix], retain_sources=True)['jobId']
                self.guard.upload(identity, 'picture', io.BytesIO(content), len(content))
                self.guard.start(identity, archive_mode='on_demand')
                wait_finished(self.guard, identity)
                report = self.analyze(identity)
                detail = self.service.detail(identity, identity + ':0')
                self.assertEqual(detail['previews']['result']['kind'], 'image')
                output = self.guard._jobs[identity].artifacts[0].path
                tiff_unavailable = suffix in ('tif', 'tiff') and not QImageReader(str(output)).canRead()
                with self.service.preview(identity, identity + ':0', 'result',
                        expected_analysis_id=report['analysisId']) as preview:
                    if tiff_unavailable:
                        self.assertEqual(preview['json']['kind'], 'unavailable')
                        self.assertIn('note', preview['json'])
                    else:
                        decoded += 1
                        self.assertEqual(preview['mime'], 'image/png')
                        shown = QImage.fromData(preview['stream'].read())
                        self.assertFalse(shown.isNull())
                        self.assertLessEqual(shown.width(), 1400)
                        self.assertLessEqual(shown.height(), 1800)
                        self.assertIn('Постраничный просмотр', preview['note'])
                        if suffix in ('jpe','jfif','jif'):
                            self.assertEqual((shown.width(),shown.height()),(12,8))
                self.assertEqual(output.read_bytes(), content)
                self.assertEqual(self.guard._jobs[identity].io, 0)
        self.assertGreaterEqual(decoded, 4)

    def test_worker_exit_wakes_parent_without_fixed_delay_and_drains_final_outcome(self):
        identity = self.converted()
        calls, closed = [], []
        class FinishesOnProcessWait:
            def __init__(inner, command, **kwargs):
                inner.root = Path(command[-1]).parent
                inner.command, inner.process, inner.exited = command, inner, False
            def __enter__(inner): return inner
            def __exit__(inner, *args): closed.append(True)
            def poll(inner): return 0 if inner.exited else None
            def wait(inner, timeout):
                calls.append(timeout)
                if len(calls) == 1:
                    raise subprocess.TimeoutExpired(inner.command, timeout)
                outcome = {'taskId': identity + ':0', 'outcome': {'status': 'checked',
                    'findings': [], 'checkedFeatures': ['identical_file_bytes'], 'recipeCandidates': []}}
                (inner.root / 'outcomes.jsonl').write_text(json.dumps(outcome) + '\n')
                inner.exited = True
                return 0
        with patch('app_backend.converter_guard.comparison_service.ProcessTree', FinishesOnProcessWait), \
                patch('app_backend.converter_guard.comparison_service.time.sleep',
                      side_effect=AssertionError('The parent must wake on process exit, not a blind timer')):
            self.service.start(identity)
            self.service.jobs[identity].future.result(5)
        report = self.service.snapshot(identity)
        self.assertEqual(report['state'], 'completed', report)
        self.assertEqual(report['items'][0]['status'], 'checked')
        self.assertEqual(len(calls), 2, 'Timeout must continue polling, completion must wake promptly')
        self.assertTrue(all(0 < value <= .08 for value in calls))
        self.assertEqual(closed, [True])
        self.assertEqual(self.guard._jobs[identity].io, 0)

    def test_process_wait_timeout_still_observes_cancel_and_closes_worker(self):
        identity = self.converted()
        calls, closed = [], []
        class WaitsUntilCancellation:
            def __init__(inner, command, **kwargs):
                inner.command, inner.process = command, inner
            def __enter__(inner): return inner
            def __exit__(inner, *args): closed.append(True)
            def poll(inner): return None
            def wait(inner, timeout):
                calls.append(timeout)
                self.service.jobs[identity].cancel.set()
                raise subprocess.TimeoutExpired(inner.command, timeout)
        with patch('app_backend.converter_guard.comparison_service.ProcessTree', WaitsUntilCancellation), \
                patch('app_backend.converter_guard.comparison_service.time.sleep',
                      side_effect=AssertionError('Blind timer is not process wait')):
            self.service.start(identity)
            self.service.jobs[identity].future.result(5)
        report = self.service.snapshot(identity)
        self.assertEqual(report['state'], 'cancelled')
        self.assertEqual(report['processed'], 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(closed, [True])
        self.assertEqual(self.guard._jobs[identity].io, 0)

    def test_preview_completion_wakes_without_waiting_for_fixed_timer(self):
        identity = self.converted()
        source = self.guard._jobs[identity].files[0].source
        calls, closed = [], []
        class PreviewOnProcessWait:
            def __init__(inner, command, **kwargs):
                inner.root = Path(command[-1]).parent
                inner.command, inner.process, inner.exited = command, inner, False
            def __enter__(inner): return inner
            def __exit__(inner, *args): closed.append(True)
            def poll(inner): return 0 if inner.exited else None
            def wait(inner, timeout):
                calls.append(timeout)
                if len(calls) == 1:
                    raise subprocess.TimeoutExpired(inner.command, timeout)
                (inner.root / 'preview.json').write_text(json.dumps({'kind': 'text', 'text': 'Completed preview'}))
                inner.exited = True
                return 0
        with patch('app_backend.converter_guard.comparison_service.ProcessTree', PreviewOnProcessWait), \
                patch('app_backend.converter_guard.comparison_service.time.sleep',
                      side_effect=AssertionError('Preview must wake on process completion')):
            result = self.service._text_preview(identity, source)
        self.assertEqual(result, {'kind': 'text', 'text': 'Completed preview'})
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(0 < value <= .06 for value in calls))
        self.assertEqual(closed, [True])
        self.assertFalse(list(self.guard._jobs[identity].root.glob('comparison/preview-*')))
        self.assertTrue(self.service.preview_gate.acquire(blocking=False))
        self.service.preview_gate.release()


class ComparisonResourceIdentityV21(unittest.TestCase):
    def run_worker(self, *, change_during_analysis=False):
        from app_backend.converter_guard import comparison_worker as worker
        from app_backend.converter_guard import comparison_analysis as analysis
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.csv'
            target = root / 'output.csv'
            resource = root / 'input-resource.bin'
            source.write_bytes(b'key,value\nA,42\n')
            target.write_bytes(source.read_bytes())
            resource.write_bytes(b'original resource')
            sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
            request = {'source': str(source), 'sourceSha256': sha(source),
                'sourceResources': [{'path': str(resource), 'sha256': sha(resource)}],
                'pairs': [{'taskId': 'task', 'target': 'csv', 'paths': [str(target)],
                           'sha256': {str(target): sha(target)}}]}
            request_path = root / 'request.json'
            request_path.write_text(json.dumps(request))
            analyze_pair = analysis.analyze_pair
            def inspect(*args, **kwargs):
                value = analyze_pair(*args, **kwargs)
                resource.write_bytes(b'modified resource')
                return value
            if not change_during_analysis:
                resource.write_bytes(b'modified resource')
            with patch.object(worker, '_apply_memory_budget'), \
                    patch.object(analysis, 'analyze_pair', side_effect=inspect if change_during_analysis else analyze_pair):
                worker.main(request_path)
            return json.loads((root / 'outcomes.jsonl').read_text())['outcome']

    def test_changed_linked_input_cannot_be_attributed_to_original_conversion(self):
        outcome = self.run_worker()
        self.assertEqual(outcome['status'], 'not_checked', outcome)
        self.assertEqual(outcome['findings'][0]['code'], 'SOURCE_CHANGED')
        self.assertEqual(outcome['recipeCandidates'], [])

    def test_linked_input_changed_during_decode_invalidates_comparison(self):
        outcome = self.run_worker(change_during_analysis=True)
        self.assertEqual(outcome['status'], 'not_checked', outcome)
        self.assertEqual(outcome['findings'][0]['code'], 'SOURCE_CHANGED')


if __name__ == '__main__':
    unittest.main()
