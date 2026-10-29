"""M02: asynchronous discovery keeps the existing recovery validation intact."""
import io
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app_backend.converter_guard import ConverterGuard, GuardError
from app_backend.converter_guard.http_api import GuardAPI, PREFIX
from app_backend.converter_guard.result_store import ResultStore
from test_converter_guard_v02 import FakeAdapter, wait_finished


class RecoveryScanTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / 'owned'
        self.guard = self.new_guard()

    def new_guard(self):
        guard = ConverterGuard(adapter=FakeAdapter(), recovery_parent=self.root)
        self.addCleanup(guard.close)
        return guard

    def ready(self, guard=None):
        guard = guard or self.guard
        identity = guard.create([{'id': 'f', 'name': 'original.docx', 'size': 4}], ['pdf'])['jobId']
        guard.upload(identity, 'f', io.BytesIO(b'keep'), 4)
        guard.start(identity, archive_mode='on_demand')
        state = wait_finished(guard, identity)
        return identity, guard._jobs[identity], state

    def old_result(self):
        identity, job, state = self.ready()
        path = job.artifacts[0].path
        self.guard.close()
        self.guard = self.new_guard()
        return identity, path

    def drain(self):
        future = self.guard._recovery_scanner.future
        if future is not None:
            future.result(timeout=5)

    def http(self, query):
        api = GuardAPI(self.guard, origin='http://127.0.0.1:8877', token='token')
        self.addCleanup(api.close)
        handler = SimpleNamespace(path=PREFIX+'/recoverable'+query,
                                  _json=lambda code, body: (code, body))
        with patch.object(api, '_authorize'):
            return api.get(handler, PREFIX+'/recoverable')

    def test_http_background_returns_before_scan(self):
        # Runs unchanged against the baseline first: old HTTP waits for disk.
        entered, allow, replied = threading.Event(), threading.Event(), threading.Event()
        answer = []
        def slow(*args):
            entered.set()
            if not allow.wait(5): raise OSError('test scan barrier timed out')
        def request():
            try: answer.append(self.http('?background=1'))
            finally: replied.set()
        with patch.object(self.guard, '_recover_results', side_effect=slow):
            worker = threading.Thread(target=request); worker.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertTrue(replied.wait(1), 'HTTP waited for the recovery scan')
                self.assertEqual(answer[0][0], 200)
                self.assertEqual(answer[0][1]['recoveryScan']['state'], 'scanning')
                self.assertFalse(answer[0][1]['recoveryScan']['complete'])
            finally:
                allow.set(); worker.join(5)
                scanner = getattr(self.guard, '_recovery_scanner', None)
                if scanner and scanner.future: scanner.future.result(timeout=5)

    def test_many_polls_share_one_worker_and_keep_metadata_available(self):
        entered, allow = threading.Event(), threading.Event()
        def slow(*args): entered.set(); allow.wait(5)
        with patch.object(self.guard, '_recover_results', side_effect=slow) as load:
            try:
                self.guard.recoverable(background=True)
                self.assertTrue(entered.wait(2))
                for _ in range(30):
                    self.assertEqual(self.guard.recoverable(background=True)['recoveryScan']['state'], 'scanning')
                    self.guard.storage_stats()
                self.assertEqual(load.call_count, 1)
                self.assertEqual(self.guard._recovery_scanner.snapshot()['attempts'], 1)
            finally: allow.set(); self.drain()
        self.assertTrue(self.guard.recoverable(background=True)['recoveryScan']['complete'])

    def test_error_is_explicit_and_polling_does_not_retry(self):
        with patch.object(self.guard, '_recover_results', side_effect=OSError('private path')) as load:
            self.guard.recoverable(background=True); self.drain()
            for _ in range(10):
                scan = self.guard.recoverable(background=True)['recoveryScan']
                self.assertEqual(scan['state'], 'error')
                self.assertEqual(scan['code'], 'RECOVERY_SCAN_FAILED')
                self.assertNotIn('private path', str(scan))
                self.assertFalse(scan['complete'])
            self.assertEqual(load.call_count, 1)
        self.guard.recoverable(background=True, retry_scan=True); self.drain()
        self.assertTrue(self.guard.recoverable(background=True)['recoveryScan']['complete'])

    def test_synchronous_default_remains_synchronous(self):
        entered, allow, done = threading.Event(), threading.Event(), threading.Event()
        def slow(*args): entered.set(); allow.wait(5)
        def request(): self.guard.recoverable(); done.set()
        with patch.object(self.guard, '_recover_results', side_effect=slow):
            worker = threading.Thread(target=request); worker.start()
            try:
                self.assertTrue(entered.wait(2)); self.assertFalse(done.is_set())
            finally: allow.set(); worker.join(5)
        self.assertTrue(done.is_set())
        self.assertIsNone(self.guard._recovery_scanner.future)
        self.assertNotIn('recoveryScan', self.guard.recoverable())

    def test_restored_output_is_not_published_before_digest_validation(self):
        identity, path = self.old_result()
        from app_backend.converter_guard.save_verification import sha256_stream
        entered, allow = threading.Event(), threading.Event()
        def slow(stream, **kwargs):
            entered.set(); allow.wait(5)
            return sha256_stream(stream, **kwargs)
        with patch('app_backend.converter_guard.save_verification.sha256_stream', side_effect=slow):
            try:
                result = self.guard.recoverable(background=True)
                self.assertTrue(entered.wait(2))
                self.assertNotIn(identity, self.guard._jobs)
                self.assertEqual(result['jobs'], [])
                self.assertTrue(path.exists())
                created = self.guard.create([{'id':'new','name':'new.txt','size':0}])
                self.assertIn(created['jobId'], self.guard._jobs)
            finally: allow.set(); self.drain()
        self.assertIn(identity, self.guard._jobs)
        self.assertEqual(self.guard._jobs[identity].artifacts[0].path.read_bytes(), b'data')

    def test_same_size_corruption_is_still_detected(self):
        identity, path = self.old_result(); path.write_bytes(b'evil')
        self.guard.recoverable(background=True); self.drain()
        result = self.guard.recoverable(background=True)
        self.assertTrue(result['recoveryScan']['complete'])
        self.assertTrue(result['warnings'])
        self.assertTrue(all(not a.readback_verified for j in self.guard._jobs.values() for a in j.artifacts))
        self.assertTrue(all(j.salvage or j.recovery_error for j in self.guard._jobs.values()))
        self.assertEqual(path.read_bytes(), b'evil')

    def test_capacity_race_defers_root_instead_of_overfilling_jobs(self):
        identity, path = self.old_result()
        from dataclasses import replace
        self.guard.limits = replace(self.guard.limits, max_jobs=1)
        entered, allow = threading.Event(), threading.Event()
        original = ResultStore.read
        def slow(store): entered.set(); allow.wait(5); return original(store)
        with patch.object(ResultStore, 'read', slow):
            try:
                self.guard.recoverable(background=True)
                self.assertTrue(entered.wait(2))
                current = self.guard.create([{'id':'new','name':'new.txt','size':0}])['jobId']
            finally: allow.set(); self.drain()
        self.assertEqual(len(self.guard._jobs), 1)
        self.assertNotIn(identity, self.guard._jobs)
        self.assertTrue(path.exists())
        self.assertEqual(self.guard.recoverable(background=True)['recoveryScan']['state'], 'waiting_for_slot')
        self.guard.release(current, discard=True)
        self.guard.recoverable(background=True); self.drain()
        self.assertIn(identity, self.guard._jobs)
        self.assertEqual(len(self.guard._jobs), 1)

    def test_shutdown_cancels_scan_without_salvaging_or_removing_output(self):
        identity, path = self.old_result()
        from app_backend.converter_guard.save_verification import sha256_stream
        entered, allow = threading.Event(), threading.Event()
        def slow(stream, **kwargs):
            entered.set(); allow.wait(5); return sha256_stream(stream, **kwargs)
        with patch('app_backend.converter_guard.save_verification.sha256_stream', side_effect=slow):
            self.guard.recoverable(background=True)
            self.assertTrue(entered.wait(2))
            closer = threading.Thread(target=self.guard.close); closer.start()
            try:
                self.assertTrue(self.guard._recovery_scanner.cancel.wait(2))
            finally: allow.set(); closer.join(5)
            self.assertFalse(closer.is_alive())
        self.assertEqual(path.read_bytes(), b'data')
        self.assertNotIn(identity, self.guard._jobs)
        self.assertEqual(self.guard._recovery_issues, [])
        reopened = self.new_guard()
        self.assertIn(identity, [j['jobId'] for j in reopened.recoverable()['jobs']])

    def test_owned_live_root_is_not_scanned_again(self):
        identity, job, _ = self.ready()
        with patch.object(ResultStore, 'read', side_effect=AssertionError('read live job')):
            self.guard.recoverable(background=True); self.drain()
        self.assertTrue(self.guard.recoverable(background=True)['recoveryScan']['complete'])
        self.assertIn(identity, self.guard._jobs)

    def test_no_recovery_parent_starts_no_scan_worker(self):
        guard = ConverterGuard(adapter=FakeAdapter()); self.addCleanup(guard.close)
        result = guard.recoverable(background=True)
        self.assertTrue(result['recoveryScan']['complete'])
        self.assertIsNone(guard._recovery_scanner.future)

    def test_legacy_http_request_keeps_synchronous_contract(self):
        with patch.object(self.guard, 'recoverable', return_value={'jobs': []}) as operation:
            self.assertEqual(self.http(''), (200, {'jobs': []}))
            operation.assert_called_once_with()

    def test_invalid_background_flags_are_rejected_without_scanning(self):
        with patch.object(self.guard, '_recover_results') as load:
            for query in ('?background=maybe', '?background=1&background=0', '?background=1&retry=bad'):
                status, body = self.http(query)
                self.assertEqual(status, 400)
                self.assertEqual(body['code'], 'RECOVERY_REQUEST')
            for kwargs in ({'background': 1}, {'background': True, 'retry_scan': 'yes'}):
                with self.assertRaises(GuardError): self.guard.recoverable(**kwargs)
            load.assert_not_called()

    def test_saved_cleanup_is_not_waited_on_by_async_list(self):
        identity, job, _ = self.ready()
        for artifact in job.artifacts: artifact.saved = True
        entered, allow = threading.Event(), threading.Event()
        original = job.store.cleanup_retired
        def slow(): entered.set(); allow.wait(5); original()
        with patch.object(job.store, 'cleanup_retired', side_effect=slow):
            try:
                result = self.guard.recoverable(background=True)
                self.assertTrue(entered.wait(2))
                self.assertEqual(result['jobs'], [])
                self.assertTrue(job.root.exists())
                self.assertGreater(self.guard.storage_stats()['reservedBytes'], 0)
            finally:
                allow.set(); self.drain()
                self.guard._cleanup_executor.submit(lambda: None).result(timeout=5)
        self.assertFalse(job.root.exists())


if __name__ == '__main__': unittest.main()
