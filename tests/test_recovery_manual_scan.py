"""Manual recovery: real retained bytes, controlled disk barriers, no GUI claim."""
import io
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app_backend.converter_guard import ConverterGuard, GuardError, GuardLimits
from app_backend.converter_guard.http_api import GuardAPI, PREFIX
from app_backend.converter_guard.result_store import ResultStore
from test_converter_guard_v02 import FakeAdapter, wait_finished


class ManualRecovery(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.guard = self.make_guard()

    def make_guard(self, **kwargs):
        guard = ConverterGuard(adapter=FakeAdapter(), recovery_parent=self.root/'owned', **kwargs)
        self.addCleanup(guard.close)
        return guard

    def old_job(self, *, retain_sources=False):
        producer = self.make_guard()
        identity = producer.create([{'id':'f','name':'source.docx','size':4}], ['pdf'],
                                   retain_sources=retain_sources)['jobId']
        producer.upload(identity, 'f', io.BytesIO(b'keep'), 4)
        producer.start(identity, archive_mode='on_demand')
        wait_finished(producer, identity)
        job = producer._jobs[identity]
        path = job.artifacts[0].path
        producer.close()
        return identity, job.root, path

    def drain(self, guard=None):
        guard = guard or self.guard
        guard._recovery_executor.submit(lambda: None).result(timeout=8)

    def http(self, route, *, body=None):
        api = GuardAPI(self.guard, origin='http://127.0.0.1:8877', token='token')
        self.addCleanup(api.close)
        handler = SimpleNamespace(path=PREFIX+route, connection=Mock(),
                                  _json=lambda code,value:(code,value))
        with patch.object(api, '_authorize'), patch.object(api, '_body', return_value=body or {}):
            return (api.get(handler, PREFIX+route.split('?')[0]) if body is None else
                    api.post(handler, PREFIX+route.split('?')[0]))

    def test_snapshot_http_does_not_start_old_recovery(self):
        with patch.object(self.guard, '_load_recovery') as loader:
            status, value = self.http('/recoverable?mode=snapshot')
        self.assertEqual(status, 200)
        self.assertEqual(loader.call_count, 0, 'SNAPSHOT_MUST_NOT_SCAN_OLD_DISK')
        self.assertEqual(value['recoveryScan']['state'], 'idle')
        self.assertFalse(value['recoveryScan']['loaded'])

    def test_legacy_get_remains_synchronous(self):
        with patch.object(self.guard, '_load_recovery') as loader:
            self.assertEqual(self.http('/recoverable')[0], 200)
        loader.assert_called_once_with()

    def test_invalid_modes_fail_before_disk_scan(self):
        with patch.object(self.guard, '_load_recovery') as loader:
            for query in ('mode=', 'mode=bad', 'mode=snapshot&mode=snapshot', 'extra=1'):
                self.assertEqual(self.http('/recoverable?'+query)[0], 400)
        loader.assert_not_called()

    def test_scan_body_is_validated_before_start(self):
        with patch.object(self.guard, 'start_recovery_scan') as start:
            self.assertEqual(self.http('/recovery-scan', body={'discard': True})[0], 400)
        start.assert_not_called()

    def test_one_scan_returns_before_blocked_disk_and_does_not_block_conversion(self):
        identity, root, path = self.old_job()
        entered, allow = threading.Event(), threading.Event()
        original = ResultStore.read
        def blocked(store, **kwargs):
            entered.set()
            if not allow.wait(5): raise AssertionError('disk barrier timed out')
            return original(store, **kwargs)
        with patch.object(ResultStore, 'read', blocked):
            try:
                status, first = self.http('/recovery-scan', body={})
                self.assertEqual(status, 202)
                self.assertTrue(entered.wait(2))
                request = first['recoveryScan']['requestId']
                for _ in range(5):
                    current = self.guard.start_recovery_scan()
                    self.assertEqual(current['recoveryScan']['requestId'], request)
                    self.assertEqual(current['recoveryScan']['state'], 'scanning')
                    self.assertNotIn(identity, [j['jobId'] for j in current['jobs']])
                current_id = self.guard.create([{'id':'new','name':'new.docx','size':0}], ['pdf'])['jobId']
                self.guard.upload(current_id, 'new', io.BytesIO(b''), 0)
                self.guard.start(current_id, archive_mode='on_demand')
                self.assertEqual(wait_finished(self.guard, current_id)['succeeded'], 1)
            finally:
                allow.set(); self.drain()
        found = self.guard.recoverable(scan=False)
        self.assertEqual(found['recoveryScan']['state'], 'completed')
        self.assertIn(identity, [j['jobId'] for j in found['jobs']])
        self.assertEqual(path.read_bytes(), b'data')

    def test_repeated_snapshot_never_reads_or_hashes_retained_files(self):
        identity, root, path = self.old_job(retain_sources=True)
        with patch.object(ResultStore, 'read', side_effect=AssertionError('unexpected disk read')):
            for _ in range(10):
                self.assertEqual(self.guard.recoverable(scan=False)['jobs'], [])
        self.assertTrue(path.exists())
        self.guard.start_recovery_scan(); self.drain()
        with patch.object(ResultStore, 'read', side_effect=AssertionError('reread')):
            for _ in range(5): self.guard.recoverable(scan=False)
        self.assertTrue(self.guard._jobs[identity].files[0].source.exists())

    def test_corrupted_output_is_not_restored_as_verified_conversion(self):
        identity, root, path = self.old_job()
        path.write_bytes(b'evil')  # Same size, different SHA-256.
        self.guard.start_recovery_scan(); self.drain()
        recovered = self.guard._jobs[identity]
        self.assertTrue(recovered.recovery_error)
        self.assertEqual(recovered.files[0].targets[0].state, 'error')
        self.assertTrue(all(not a.readback_verified for a in recovered.artifacts))
        self.assertEqual(path.read_bytes(), b'evil')

    def test_current_creation_cannot_be_overwritten_or_exceed_slot_limit(self):
        identity, root, path = self.old_job()
        guard = self.make_guard(limits=GuardLimits(max_jobs=1))
        entered, allow = threading.Event(), threading.Event()
        original = ResultStore.read
        def blocked(store, **kwargs):
            entered.set()
            if not allow.wait(5): raise AssertionError('slot barrier timed out')
            return original(store, **kwargs)
        with patch.object(ResultStore, 'read', blocked):
            try:
                guard.start_recovery_scan(); self.assertTrue(entered.wait(2))
                live = guard.create([{'id':'live','name':'live.txt','size':0}])['jobId']
            finally:
                allow.set(); self.drain(guard)
        self.assertEqual(list(guard._jobs), [live])
        self.assertEqual(guard.recoverable(scan=False)['recoveryScan']['state'], 'waiting_for_slot')
        self.assertTrue(path.exists())
        guard.release(live, discard=True)
        guard.start_recovery_scan(); self.drain(guard)
        self.assertEqual(list(guard._jobs), [identity])
        self.assertEqual(guard.recoverable(scan=False)['recoveryScan']['state'], 'completed')

    def test_shutdown_during_read_keeps_bytes_and_never_salvages(self):
        identity, root, path = self.old_job()
        entered, allow = threading.Event(), threading.Event()
        original = ResultStore.read
        def blocked(store, **kwargs):
            entered.set()
            if not allow.wait(5): raise AssertionError('close barrier timed out')
            return original(store, **kwargs)
        with patch.object(ResultStore, 'read', blocked), patch.object(self.guard, '_salvage_orphan') as salvage:
            self.guard.start_recovery_scan(); self.assertTrue(entered.wait(2))
            closer = threading.Thread(target=self.guard.close)
            try:
                closer.start()
                self.assertTrue(self.guard._recovery_stop.wait(2))
            finally:
                allow.set(); closer.join(5)
            self.assertFalse(closer.is_alive())
            salvage.assert_not_called()
        self.assertEqual(self.guard.storage_stats()['jobs'], 0)
        self.assertEqual(path.read_bytes(), b'data')
        probe = ResultStore(root)
        try: self.assertTrue(probe.acquire())
        finally: probe.close()

    def test_failed_scan_is_visible_and_retryable_without_private_error_text(self):
        with patch.object(self.guard, '_recover_results', side_effect=OSError('/private/path')):
            self.guard.start_recovery_scan(); self.drain()
        state = self.guard.recoverable(scan=False)['recoveryScan']
        self.assertEqual((state['state'], state['errorCode']), ('failed', 'RECOVERY_SCAN_FAILED'))
        self.assertNotIn('/private', repr(state))
        self.guard.start_recovery_scan(); self.drain()
        self.assertEqual(self.guard.recoverable(scan=False)['recoveryScan']['state'], 'completed')

    def test_read_and_input_hash_cooperate_with_shutdown(self):
        from app_backend.converter_guard.recovery_control import RecoveryStopped
        identity, root, path = self.old_job(retain_sources=True)
        store = ResultStore(root)
        header, _, _, _ = store.read()
        cancel = threading.Event(); cancel.set()
        with self.assertRaises(RecoveryStopped): store.read(cancel=cancel)
        row = header['files'][0]
        with self.assertRaises(RecoveryStopped):
            store.input_path(row['sourcePath'], row['size'], row['sourceSha256'], cancel=cancel)
        self.assertEqual(path.read_bytes(), b'data')

    def test_snapshot_pruning_defers_slow_cleanup(self):
        identity = self.guard.create([{'id':'f','name':'source.docx','size':0}], ['pdf'])['jobId']
        self.guard.upload(identity, 'f', io.BytesIO(b''), 0)
        self.guard.start(identity, archive_mode='on_demand'); wait_finished(self.guard, identity)
        job = self.guard._jobs[identity]
        for artifact in job.artifacts: artifact.saved = True
        entered, allow = threading.Event(), threading.Event()
        original = job.store.cleanup_retired
        def blocked():
            entered.set()
            if not allow.wait(5): raise AssertionError('cleanup barrier timed out')
            original()
        with patch.object(job.store, 'cleanup_retired', blocked):
            try:
                snapshot = self.guard.recoverable(scan=False)
                self.assertTrue(entered.wait(2))
                self.assertEqual(snapshot['jobs'], [])
                self.assertGreater(self.guard.storage_stats()['reservedBytes'], 0)
            finally:
                allow.set(); self.guard._cleanup_executor.submit(lambda:None).result(timeout=5)
        self.assertNotIn(identity, self.guard._jobs)

    def test_closed_guard_cannot_start_new_scan(self):
        self.guard.close()
        with self.assertRaises(GuardError) as error: self.guard.start_recovery_scan()
        self.assertEqual(error.exception.code, 'GUARD_CLOSED')

    def test_snapshot_flag_is_boolean(self):
        for value in (None, 0, 1, 'false'):
            with self.assertRaises(GuardError): self.guard.recoverable(scan=value)

    def test_manual_recovery_dom(self):
        module = os.environ.get('AIO_DOM_MODULE')
        if not module or not shutil.which('node'):
            self.skipTest('DOM acceptance requires Node and AIO_DOM_MODULE')
        result = subprocess.run(['node','tools/qa/recovery_manual_dom.mjs'],
            text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout+'\n'+result.stderr)
        print(result.stdout.strip())


if __name__ == '__main__': unittest.main()
