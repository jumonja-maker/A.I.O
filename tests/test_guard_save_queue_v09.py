"""Save snapshots must not stop a live queue or acknowledge later outputs."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from app_backend.converter_guard import ConverterGuard, GuardError
from test_converter_guard_v02 import FakeAdapter, wait_finished


class SaveQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.adapter = FakeAdapter()
        self.guard = ConverterGuard(adapter=self.adapter, spool_parent=self.root/'spool',
                                    recovery_parent=self.root/'recovery')
        self.destination = self.root/'saved'
        self.destination.mkdir()

    def tearDown(self):
        self.adapter.resume.set()
        self.guard.close()
        self.temp.cleanup()

    def job(self, names=('first.csv', 'hold.csv', 'last.csv')):
        rows = [{'id': 'f'+str(i), 'name': name, 'size': 1} for i, name in enumerate(names)]
        identity = self.guard.create(rows, ['pdf'])['jobId']
        for row in rows:
            self.guard.upload(identity, row['id'], io.BytesIO(b'x'), 1)
        self.guard.start(identity)
        return identity

    def test_live_snapshot_is_saveable_and_listed_without_stopping_queue(self):
        identity = self.job()
        self.assertTrue(self.adapter.waiting.wait(2))
        before = self.guard.status(identity)
        self.assertEqual(before['partialResult']['jobId'], identity)
        self.assertEqual(before['partialResult']['outputsCount'], 1)
        listed = self.guard.recoverable()['jobs']
        self.assertTrue(next(j for j in listed if j['jobId'] == identity)['running'])
        saved = self.guard.save_folder(before['partialResult']['ticket'], self.destination)
        self.assertTrue(saved['saved'])
        self.assertFalse(saved['savedAll'])
        self.assertTrue(saved['snapshotComplete'])
        self.assertEqual(saved['outputs'], 1)
        self.assertTrue(saved['jobRunning'])
        self.assertFalse(self.guard._jobs[identity].cancel.is_set())
        self.assertEqual(self.guard.status(identity)['currentTask']['fileId'], 'f1')
        self.adapter.resume.set()
        after = wait_finished(self.guard, identity)
        self.assertEqual((after['succeeded'], after['cancelled']), (3, 0))
        self.assertEqual(sum(a['saved'] for a in after['result']['outputs']), 1)

    def test_outputs_arriving_during_copy_are_not_acknowledged(self):
        identity = self.job()
        self.assertTrue(self.adapter.waiting.wait(2))
        before = self.guard.status(identity)
        def advance(_current, _total):
            self.adapter.resume.set()
            wait_finished(self.guard, identity)
        saved = self.guard.save_folder(before['partialResult']['ticket'], self.destination,
                                       on_progress=advance)
        self.assertFalse(saved['savedAll'])
        self.assertEqual((saved['outputs'], saved['remainingOutputs']), (1, 2))
        final = self.guard.status(identity)
        self.assertEqual(sum(a['saved'] for a in final['result']['outputs']), 1)
        manifest = json.loads((self.destination/saved['name']/'AIO-results-manifest.json').read_text())
        self.assertEqual(manifest['jobId'], identity)
        self.assertEqual(manifest['saveId'], saved['saveId'])
        self.assertEqual(len(manifest['outputs']), 1)
        self.assertTrue(manifest['readbackVerified'])
        self.assertEqual(manifest['outputsVerified'],1)
        copied = manifest['outputs'][0]
        self.assertEqual(hashlib.sha256((self.destination/saved['name']/copied['path']).read_bytes()).hexdigest(),
                         copied['sha256'])

    def test_second_save_is_rejected_while_first_copy_is_in_progress(self):
        identity = self.job(('first.csv', 'second.csv'))
        done = wait_finished(self.guard, identity)
        entered, resume = threading.Event(), threading.Event()
        result = []
        def progress(_n, _total):
            entered.set()
            resume.wait(3)
        def save():
            result.append(self.guard.save_folder(done['result']['ticket'], self.destination,
                                                 on_progress=progress))
        worker = threading.Thread(target=save)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            with self.assertRaises(GuardError) as error:
                self.guard.save_folder(done['result']['ticket'], self.destination)
            self.assertEqual(error.exception.code, 'SAVE_BUSY')
            self.assertTrue(self.guard.status(identity)['saveInProgress'])
        finally:
            resume.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertTrue(result[0]['savedAll'])

    def test_cancelled_live_save_keeps_originals_and_does_not_cancel_native(self):
        identity = self.job()
        self.assertTrue(self.adapter.waiting.wait(2))
        before = self.guard.status(identity)
        cancel = threading.Event()
        with self.assertRaises(GuardError) as error:
            self.guard.save_folder(before['partialResult']['ticket'], self.destination, cancel=cancel,
                                   on_progress=lambda _n, _t: cancel.set())
        self.assertEqual(error.exception.code, 'SAVE_CANCELLED')
        self.assertFalse(self.guard._jobs[identity].cancel.is_set())
        self.assertTrue(all(not a.saved and a.path.exists() for a in self.guard._jobs[identity].artifacts))
        self.assertFalse(self.guard.status(identity)['saveInProgress'])
        self.assertFalse(list(self.destination.rglob('*.aio-part-*')))
        status = json.loads(next(self.destination.glob('*/AIO-save-status.json')).read_text())
        self.assertEqual(status['state'], 'cancelled')
        self.assertEqual(status['code'], 'SAVE_CANCELLED')

    def test_completed_folder_ack_contains_exact_counts_and_save_id(self):
        identity = self.job(('first.csv', 'second.csv'))
        done = wait_finished(self.guard, identity)
        saved = self.guard.save_folder(done['result']['ticket'], self.destination)
        self.assertTrue(saved['savedAll'])
        self.assertEqual(saved['outputs'], 2)
        self.assertEqual(saved['bytesWritten'], 8)
        self.assertEqual(saved['remainingOutputs'], 0)
        self.assertFalse(saved['jobRunning'])
        status = json.loads((self.destination/saved['name']/'AIO-save-status.json').read_text())
        self.assertEqual((status['state'], status['saveId']), ('completed', saved['saveId']))

    def test_storage_failure_does_not_acknowledge_a_partially_copied_snapshot(self):
        identity = self.job(('first.csv', 'second.csv'))
        done = wait_finished(self.guard, identity)
        original_open, writes = Path.open, 0
        def fail_second_part(path, mode='r', *args, **kwargs):
            nonlocal writes
            if mode == 'xb' and '.aio-part-' in path.name:
                writes += 1
                if writes == 2:
                    raise OSError(28, 'No space left on device')
            return original_open(path, mode, *args, **kwargs)
        with patch.object(Path, 'open', fail_second_part), self.assertRaises(GuardError) as error:
            self.guard.save_folder(done['result']['ticket'], self.destination)
        self.assertEqual(error.exception.code, 'SAVE_STORAGE_FAILED')
        status = json.loads(next(self.destination.glob('*/AIO-save-status.json')).read_text())
        self.assertEqual((status['state'], status['outputsWritten']), ('failed', 1))
        self.assertTrue(all(not a.saved and a.path.exists() for a in self.guard._jobs[identity].artifacts))
        self.assertFalse(list(self.destination.rglob('*.aio-part-*')))
        self.assertEqual(len(self.guard.recoverable()['jobs']), 1)

    def test_journal_write_does_not_hold_global_status_lock(self):
        identity = self.job(('first.csv',))
        done = wait_finished(self.guard, identity)
        store = self.guard._jobs[identity].store
        entered, resume, observed = threading.Event(), threading.Event(), threading.Event()
        original = store.saved
        def blocked(artifacts):
            entered.set()
            resume.wait(3)
            original(artifacts)
        errors = []
        def acknowledge():
            try:
                self.guard.confirm_saved(identity, done['result']['ticket'], compact=True)
            except Exception as error:
                errors.append(error)
        with patch.object(store, 'saved', blocked):
            worker = threading.Thread(target=acknowledge)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                reader = threading.Thread(target=lambda: (self.guard.status(identity), observed.set()))
                reader.start()
                self.assertTrue(observed.wait(.5), 'Save journal I/O blocked queue/status requests')
            finally:
                resume.set()
                worker.join(3)
                if 'reader' in locals(): reader.join(3)
        self.assertFalse(errors)

    def test_compact_ack_does_not_reserialize_every_task_and_result(self):
        identity = self.job(('first.csv', 'second.csv'))
        done = wait_finished(self.guard, identity)
        with patch.object(self.guard, '_snapshot', side_effect=AssertionError('Full snapshot is unnecessary')):
            ack = self.guard.confirm_saved(identity, done['result']['ticket'], compact=True)
        self.assertTrue(ack['savedAll'])
        self.assertNotIn('files', ack)
        self.assertLess(len(json.dumps(ack)), 600)
        with self.assertRaises(GuardError):
            self.guard.confirm_saved(identity, done['result']['ticket'], compact='yes')


if __name__ == '__main__':
    unittest.main()

