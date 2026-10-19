"""Guard lifecycle contracts. FakeAdapter tests ownership, never PDF fidelity."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

from app_backend.converter_guard import ConverterGuard, GuardError, GuardTrace, LibreOfficeAdapter
from app_backend.converter_guard.core import GuardLimits, CHUNK
from app_backend.converter_guard.process import ProcessTree
from app_backend.downloads import download_filter

PDF = b'%PDF-1.7\nfixture for lifecycle tests only\n%%EOF\n'


class FakeAdapter:
    available = True

    def __init__(self):
        self.calls = []
        self.names = []
        self.entered = threading.Event()

    def export_pdf(self, source, output, profile, *, cancel, timeout, on_started, output_limit):
        on_started()
        self.names.append(source.name)
        with source.open('rb') as stream:
            marker = stream.read(4)
            stream.seek(0)
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        self.calls.append((source.suffix, source.stat().st_size, digest))
        if marker == b'bad!':
            raise GuardError('ENGINE_OUTPUT_MISSING', 'Fixture conversion failed.', stage='result')
        if marker == b'wait':
            self.entered.set()
            if cancel.wait(timeout):
                raise GuardError('CANCELLED', 'Cancelled.', stage='engine')
            raise GuardError('ENGINE_TIMEOUT', 'Timed out.', stage='engine')
        output.mkdir(parents=True)
        result = output / 'source.pdf'
        result.write_bytes(PDF)
        return result


def wait_finished(guard, id, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = guard.status(id)
        if state['finished']:
            return state
        time.sleep(.01)
    raise AssertionError('Guard job did not finish')


class GuardLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.adapter = FakeAdapter()
        self.guard = ConverterGuard(adapter=self.adapter, spool_parent=self.temp.name)

    def tearDown(self):
        self.guard.close()
        self.assertEqual(self.guard.storage_stats()['reservedBytes'], 0)
        self.assertEqual(self.guard.storage_stats()['jobs'], 0)
        self.temp.cleanup()

    def prepare(self, files, guard=None):
        guard = guard or self.guard
        job = guard.create([{'id': str(i), 'name': name, 'size': len(data)}
                            for i, (name, data) in enumerate(files)])
        for i, (_, data) in enumerate(files):
            guard.upload(job['jobId'], str(i), io.BytesIO(data), len(data))
        return job['jobId']

    def test_metadata_inspection_does_not_spawn_or_create_spool(self):
        result = self.guard.capabilities([{'id': 'f1', 'name': 'footnote.docx', 'size': 123}])
        self.assertEqual(result['formats'][0]['compatibleFileIds'], ['f1'])
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])
        self.assertEqual(self.adapter.calls, [])

    def test_failed_first_file_does_not_block_remaining_and_zip_is_readable(self):
        id = self.prepare([('first.docx', b'bad!'), ('second.xlsx', b'good'), ('third.pptx', b'good')])
        self.guard.start(id)
        state = wait_finished(self.guard, id)
        self.assertEqual([f['state'] for f in state['files']], ['error', 'ready', 'ready'])
        self.assertEqual(state['state'], 'partial')
        self.assertEqual(len(self.adapter.calls), 3)
        ticket = state['result']['downloadUrl'].split('ticket=')[1]
        with self.guard.open_result(ticket) as (stream, name, size):
            self.assertEqual(name, 'AIO_Results.zip')
            with zipfile.ZipFile(stream) as archive:
                self.assertEqual(archive.namelist(), ['001-second.pdf', '002-third.pdf'])
                self.assertTrue(all(archive.read(n) == PDF for n in archive.namelist()))

    def test_original_bytes_are_handed_to_engine_unchanged(self):
        content = b'good\x00\xff\x80' * 1000
        id = self.prepare([('Формулы и сноски.DOCX', content)])
        self.guard.start(id)
        state = wait_finished(self.guard, id)
        self.assertEqual(state['state'], 'ready')
        self.assertEqual(self.adapter.calls[0], ('.DOCX', len(content), hashlib.sha256(content).hexdigest()))
        self.assertNotIn(self.temp.name, json.dumps(state['trace']))
        self.assertNotIn('Формулы', json.dumps(state['trace'], ensure_ascii=False))

    def test_original_basename_at_filesystem_limit_is_preserved(self):
        name = 'n'*250+'.docx'
        id = self.prepare([(name,b'good')])
        self.guard.start(id)
        state = wait_finished(self.guard,id)
        self.assertEqual(state['state'],'ready')
        self.assertEqual(self.adapter.names,[name])
        self.assertEqual(state['files'][0]['warnings'],[])

    def test_unknown_route_is_explicit_and_other_files_continue(self):
        id = self.prepare([('future.html', b'good'), ('valid.odt', b'good')])
        self.guard.start(id)
        state = wait_finished(self.guard, id)
        self.assertEqual(state['files'][0]['code'], 'INPUT_NOT_CONNECTED')
        self.assertFalse(state['files'][0]['libreOfficeStarted'])
        self.assertTrue(state['files'][1]['libreOfficeStarted'])

    def test_empty_input_is_not_sent_to_engine(self):
        id = self.prepare([('empty.docx', b''), ('valid.docx', b'good')])
        self.guard.start(id)
        state = wait_finished(self.guard, id)
        self.assertEqual(state['files'][0]['code'], 'INPUT_EMPTY')
        self.assertEqual(len(self.adapter.calls), 1)

    def test_more_than_16_results_share_one_owner(self):
        id = self.prepare([(f'{i}.docx', b'good') for i in range(24)])
        self.guard.start(id)
        state = wait_finished(self.guard, id)
        self.assertEqual(state['state'], 'ready')
        self.assertEqual(len(state['result']['outputs']), 24)
        self.assertEqual(self.guard.storage_stats()['jobs'], 1)

    def test_long_unicode_display_names_do_not_gate_conversion(self):
        id = self.prepare([('я'*140+'.docx', b'good'), ('я'*139+'ю.docx', b'good')])
        self.guard.start(id)
        state = wait_finished(self.guard,id)
        self.assertEqual(state['state'],'ready')
        with self.guard.open_result(state['result']['downloadUrl'].split('ticket=')[1]) as (stream,_,_):
            with zipfile.ZipFile(stream) as archive:
                names=archive.namelist()
                self.assertEqual(len(set(names)),2)
                self.assertTrue(all(len(name.encode('utf-8'))<=240 for name in names))
                self.assertTrue(all(archive.read(name)==PDF for name in names))

    def test_release_waits_for_download_lease_then_removes_files(self):
        id = self.prepare([('one.docx', b'good')])
        self.guard.start(id)
        state = wait_finished(self.guard, id)
        ticket = state['result']['downloadUrl'].split('ticket=')[1]
        with self.guard.open_result(ticket) as (stream, _, _):
            self.guard.release(id)
            self.assertEqual(stream.read(), PDF)
            self.assertEqual(self.guard.storage_stats()['jobs'], 1)
            with self.assertRaises(GuardError):
                self.guard.open_result(ticket).__enter__()
        self.assertEqual(self.guard.storage_stats()['jobs'], 0)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_active_cancel_and_subsequent_retry_need_no_restart(self):
        id = self.prepare([('slow.docx', b'wait'), ('later.docx', b'good')])
        self.guard.start(id)
        self.assertTrue(self.adapter.entered.wait(2))
        self.guard.cancel(id)
        state = wait_finished(self.guard, id)
        self.assertEqual([f['state'] for f in state['files']], ['cancelled', 'cancelled'])
        self.assertEqual(len(self.adapter.calls), 1)
        self.guard.release(id)
        retry = self.prepare([('retry.docx', b'good')])
        self.guard.start(retry)
        self.assertEqual(wait_finished(self.guard, retry)['state'], 'ready')

    def test_queued_job_can_be_cancelled_without_waiting_for_active_job(self):
        first = self.prepare([('slow.docx', b'wait')])
        self.guard.start(first)
        self.assertTrue(self.adapter.entered.wait(2))
        second = self.prepare([('queued.docx', b'good')])
        self.guard.start(second)
        state = self.guard.cancel(second)
        self.assertTrue(state['finished'])
        self.assertEqual(state['files'][0]['code'], 'CANCELLED')
        self.assertFalse(state['files'][0]['libreOfficeStarted'])
        self.guard.release(second)
        self.guard.cancel(first)

    def test_timeout_is_per_file_and_next_file_runs(self):
        self.guard.close()
        self.guard = ConverterGuard(adapter=self.adapter, limits=GuardLimits(file_timeout=.05), spool_parent=self.temp.name)
        id = self.prepare([('slow.docx', b'wait'), ('next.docx', b'good')])
        self.guard.start(id)
        state = wait_finished(self.guard, id)
        self.assertEqual(state['files'][0]['code'], 'ENGINE_TIMEOUT')
        self.assertEqual(state['files'][1]['state'], 'ready')

    def test_upload_can_be_retried_after_truncation(self):
        id = self.guard.create([{'id': 'f', 'name': 'one.docx', 'size': 4}])['jobId']
        with self.assertRaises(GuardError) as error:
            self.guard.upload(id, 'f', io.BytesIO(b'go'), 4)
        self.assertEqual(error.exception.code, 'UPLOAD_TRUNCATED')
        self.guard.upload(id, 'f', io.BytesIO(b'good'), 4)
        self.guard.start(id)
        self.assertEqual(wait_finished(self.guard, id)['state'], 'ready')

    def test_cannot_start_with_missing_upload_or_upload_twice(self):
        id = self.guard.create([{'id': 'f', 'name': 'one.docx', 'size': 4}])['jobId']
        with self.assertRaises(GuardError): self.guard.start(id)
        self.guard.upload(id, 'f', io.BytesIO(b'good'), 4)
        with self.assertRaises(GuardError): self.guard.upload(id, 'f', io.BytesIO(b'good'), 4)

    def test_large_upload_is_streamed_and_has_no_20mb_barrier(self):
        class RepeatStream:
            remaining = 21 * 1024 * 1024
            largest_read = 0
            def read(self, count):
                self.largest_read = max(self.largest_read, count)
                size = min(count, self.remaining)
                self.remaining -= size
                return b'x' * size
        stream = RepeatStream()
        id = self.guard.create([{'id': 'f', 'name': 'large.docx', 'size': stream.remaining}])['jobId']
        self.guard.upload(id, 'f', stream, stream.remaining)
        self.assertLessEqual(stream.largest_read, CHUNK)
        self.assertEqual(self.guard.status(id)['files'][0]['state'], 'queued')
        self.guard.release(id)

    def test_bundle_quota_keeps_individual_results_available(self):
        self.guard.close()
        self.guard = ConverterGuard(adapter=self.adapter,
            limits=GuardLimits(max_spool_bytes=1024), spool_parent=self.temp.name)
        id = self.prepare([('a.docx', b'good'), ('b.docx', b'good')])
        self.guard.start(id)
        state = wait_finished(self.guard, id)
        self.assertEqual(state['state'], 'ready')
        self.assertEqual(state['packagingError'], 'BUNDLE_QUOTA')
        self.assertNotIn('downloadUrl', state['result'])
        for output in state['result']['outputs']:
            with self.guard.open_result(output['downloadUrl'].split('ticket=')[1]) as (s, _, _):
                self.assertEqual(s.read(), PDF)

    def test_quota_and_job_count_are_reclaimed_after_release(self):
        self.guard.close()
        self.guard = ConverterGuard(adapter=self.adapter,
            limits=GuardLimits(max_jobs=1, max_spool_bytes=8), spool_parent=self.temp.name)
        id = self.prepare([('a.docx', b'good')])
        with self.assertRaises(GuardError) as error: self.prepare([('b.docx', b'good')])
        self.assertEqual(error.exception.code, 'JOB_LIMIT')
        self.guard.release(id)
        other = self.prepare([('b.docx', b'good')])
        self.assertEqual(self.guard.status(other)['state'], 'uploading')

    def test_expired_inactive_job_is_reclaimed(self):
        id = self.prepare([('a.docx', b'good')])
        future = time.monotonic() + self.guard.limits.retention_seconds + 1
        with patch('app_backend.converter_guard.core.time.monotonic', return_value=future):
            with self.assertRaises(GuardError) as error: self.guard.status(id)
        self.assertEqual(error.exception.code, 'JOB_NOT_FOUND')
        self.assertEqual(self.guard.storage_stats()['jobs'], 0)

    def test_cleanup_failure_retains_ownership_until_retry(self):
        id = self.prepare([('a.docx', b'good')])
        with patch('app_backend.converter_guard.core.shutil.rmtree', side_effect=PermissionError()):
            self.guard.release(id)
        self.assertEqual(self.guard.storage_stats()['jobs'], 1)
        self.guard._prune()
        self.assertEqual(self.guard.storage_stats()['jobs'], 0)

    def test_ready_result_does_not_expire_while_waiting_for_user_save(self):
        id = self.prepare([('a.docx', b'good')])
        self.guard.start(id)
        state = wait_finished(self.guard,id)
        ticket = state['result']['downloadUrl'].split('ticket=')[1]
        future = time.monotonic() + self.guard.limits.retention_seconds + 1
        with patch('app_backend.converter_guard.core.time.monotonic',return_value=future):
            with self.guard.open_result(ticket) as (stream,_,_):
                self.assertEqual(stream.read(),PDF)

    def test_missing_engine_is_not_misreported_as_document_corruption(self):
        self.guard.close()
        self.guard = ConverterGuard(adapter=LibreOfficeAdapter('/missing/aio/soffice'), spool_parent=self.temp.name)
        id = self.prepare([('a.docx', b'good')])
        self.guard.start(id)
        state = wait_finished(self.guard, id)
        self.assertEqual(state['files'][0]['code'], 'ENGINE_NOT_FOUND')
        self.assertFalse(state['files'][0]['libreOfficeStarted'])

    def test_bad_metadata_cannot_choose_a_path(self):
        for name in ('../x.docx', 'C:\\x.docx', '\x00.docx', 'a\n.docx'):
            with self.subTest(name=name), self.assertRaises(GuardError):
                self.guard.create([{'id': 'f', 'name': name, 'size': 1}])
        with self.assertRaises(GuardError):
            self.guard.create([{'id': 'same', 'name': 'a.docx', 'size': 1}] * 2)

    def test_close_only_removes_owned_temporary_directories(self):
        sentinel = Path(self.temp.name) / 'unrelated.txt'
        sentinel.write_text('keep')
        self.prepare([('a.docx', b'good')])
        self.guard.close()
        self.assertEqual(sentinel.read_text(), 'keep')


class TraceAndNativeBoundaryTests(unittest.TestCase):
    def test_trace_is_bounded_and_reports_truncation(self):
        trace = GuardTrace()
        for _ in range(600): trace.add('fixture')
        self.assertEqual(len(trace.snapshot()['events']), 512)
        self.assertEqual(trace.snapshot()['droppedEvents'], 88)

    def test_process_context_terminates_its_own_child(self):
        with tempfile.TemporaryDirectory() as root:
            marker = Path(root) / 'started'
            code = 'from pathlib import Path;import time;Path(' + repr(str(marker)) + ').write_text("ok");time.sleep(60)'
            with ProcessTree([sys.executable, '-c', code], cwd=root) as tree:
                deadline = time.monotonic() + 3
                while not marker.exists() and time.monotonic() < deadline: time.sleep(.01)
                self.assertTrue(marker.exists())
                self.assertIsNone(tree.process.poll())
            self.assertIsNotNone(tree.process.poll())
            tree.close()  # Idempotent teardown.

    def test_native_save_requires_exact_session_origin_and_ticket(self):
        origin = 'http://127.0.0.1:5000'
        good = origin + '/api/converter/result?ticket=' + 'a'*43 + '&aioSave=1'
        self.assertEqual(download_filter(good, 'file.pdf', 5000), 'PDF (*.pdf)')
        self.assertEqual(download_filter(good, 'files.zip', 5000), 'ZIP (*.zip)')
        self.assertEqual(download_filter('blob:' + origin + '/uuid', 'merge.xlsx', 5000), 'Excel (*.xlsx)')
        for url, name in [(good.replace(':5000', ':5001'), 'a.pdf'),
                          (good.replace('127.0.0.1', 'evil.test'), 'a.pdf'),
                          (good + '&ticket=' + 'b'*43, 'a.pdf'),
                          (good, 'a.exe'), (origin + '/assets/a.pdf', 'a.pdf')]:
            with self.subTest(url=url): self.assertIsNone(download_filter(url, name, 5000))


if __name__ == '__main__':
    unittest.main()
