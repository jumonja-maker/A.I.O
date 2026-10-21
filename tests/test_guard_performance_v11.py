"""Responsiveness, bounded queue work and ownership under a slow local disk."""
import io
import random
import tempfile
import threading
import time
from pathlib import Path
import unittest
from unittest.mock import patch

from app_backend.converter_guard import ConverterGuard
from app_backend.converter_guard.result_store import ResultStore
from test_converter_guard_v02 import FakeAdapter, wait_finished


class TrackingTasks(list):
    def __init__(self, values):
        super().__init__(values)
        self.visits = 0

    def __iter__(self):
        self.visits += 1
        return super().__iter__()


class GuardPerformanceQA(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.guard = ConverterGuard(adapter=FakeAdapter(), recovery_parent=self.root / 'recovery')

    def tearDown(self):
        self.guard.close()
        self.tmp.cleanup()

    def prepared(self, name='one.docx'):
        identity = self.guard.create([{'id': 'one', 'name': name, 'size': 1}], ['pdf'])['jobId']
        self.guard.upload(identity, 'one', io.BytesIO(b'x'), 1)
        return identity

    def assert_reader_responds(self, identity):
        observed, errors = threading.Event(), []

        def read():
            try:
                self.guard.status(identity)
                observed.set()
            except Exception as error:
                errors.append(error)

        reader = threading.Thread(target=read)
        reader.start()
        self.addCleanup(reader.join, 3)
        self.assertTrue(observed.wait(.5), 'Disk journal held the global Guard status lock')
        self.assertFalse(errors)

    def test_terminal_task_journal_does_not_block_status_or_cancellation(self):
        identity = self.prepared()
        store = self.guard._jobs[identity].store
        entered, resume = threading.Event(), threading.Event()
        original = store.task

        def blocked(*args):
            entered.set()
            self.assertTrue(resume.wait(3))
            original(*args)

        with patch.object(store, 'task', blocked):
            self.guard.start(identity)
            try:
                self.assertTrue(entered.wait(2))
                self.assert_reader_responds(identity)
                accepted = threading.Event()
                self.assertEqual(self.guard.status(identity)['succeeded'], 0,
                                 'A ready result must not be published before its durable record')
                cancel = threading.Thread(target=lambda: (self.guard.cancel(identity), accepted.set()))
                cancel.start()
                self.assertTrue(accepted.wait(.5), 'Cancellation cannot wait for an unrelated fsync')
                cancel.join(2)
            finally:
                resume.set()
        wait_finished(self.guard, identity)

    def test_job_initialization_does_not_block_an_existing_job(self):
        existing = self.prepared()
        entered, resume = threading.Event(), threading.Event()
        original = ResultStore.initialize
        errors = []

        def blocked(store, job):
            entered.set()
            self.assertTrue(resume.wait(3))
            original(store, job)

        def create():
            try:
                self.prepared('next.docx')
            except Exception as error:
                errors.append(error)

        with patch.object(ResultStore, 'initialize', blocked):
            worker = threading.Thread(target=create)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assert_reader_responds(existing)
            finally:
                resume.set()
                worker.join(3)
        self.assertFalse(errors)

    def test_finish_journal_does_not_block_status(self):
        identity = self.prepared()
        store = self.guard._jobs[identity].store
        entered, resume = threading.Event(), threading.Event()
        original = store.finish

        def blocked(*args):
            entered.set()
            self.assertTrue(resume.wait(3))
            original(*args)

        with patch.object(store, 'finish', blocked):
            self.guard.start(identity)
            try:
                self.assertTrue(entered.wait(2))
                self.assert_reader_responds(identity)
            finally:
                resume.set()
        wait_finished(self.guard, identity)

    def test_delta_status_does_not_walk_unrelated_task_lists(self):
        # A user can request several outputs per source. A one-file update
        # must not repeatedly scan every target of every other source.
        rows = [{'id': str(i), 'name': f'{i}.docx', 'size': 0} for i in range(100)]
        identity = self.guard.create(rows, ['pdf', 'odt', 'txt'])['jobId']
        job = self.guard._jobs[identity]
        for item in job.files:
            item.targets = TrackingTasks(item.targets)
        revision = job.revision
        item = job.files[0]
        item.targets[0].state = 'converting'
        self.guard._record(job, 'native_stage', file_id=item.id, target='pdf')
        state = self.guard.status(identity, delta=True, since_revision=revision)
        self.assertTrue(state['delta'])
        self.assertEqual(len(state['files']), 1)
        self.assertEqual(state['total'], 300)
        self.assertEqual(state['pending'], 300)
        self.assertEqual(sum(f.targets.visits for f in job.files[1:]), 0)

    def test_cached_counters_match_a_full_scan_after_mixed_task_transitions(self):
        rows = [{'id': str(i), 'name': f'{i}.docx', 'size': 0} for i in range(12)]
        identity = self.guard.create(rows, ['pdf', 'odt', 'txt'])['jobId']
        job = self.guard._jobs[identity]
        generator = random.Random(47)
        states = ('queued', 'converting', 'ready', 'saved', 'error', 'cancelled', 'skipped')
        for _ in range(150):
            item = generator.choice(job.files)
            task = generator.choice(item.targets)
            task.state = generator.choice(states)
            task.lo_started = generator.choice((True, False))
            self.guard._record(job, 'native_stage', file_id=item.id, target=task.format)
            self.assertEqual(self.guard._counts(job), self.guard._scan_counts(job.files))
        self.guard._record(job, 'save_confirmed')
        self.assertEqual(self.guard._counts(job), self.guard._scan_counts(job.files))

    def test_failed_journal_keeps_outputs_and_reports_recovery_error(self):
        identity = self.prepared()
        store = self.guard._jobs[identity].store
        with patch.object(store, 'task', side_effect=OSError('fixture disk failure')):
            self.guard.start(identity)
            state = wait_finished(self.guard, identity)
        self.assertEqual(state['succeeded'], 1)
        self.assertEqual(state['recoveryError'], 'RECOVERY_WRITE_FAILED')
        root = self.guard._jobs[identity].root
        self.guard.release(identity)
        self.assertTrue(root.exists())
        self.assertEqual(len(list(root.glob('work/*/output/*.pdf'))), 1)

    def test_native_timings_survive_result_recovery(self):
        class TimedAdapter(FakeAdapter):
            def native_timings(self):
                return {'source': 'cli_process', 'launchMs': 1, 'processMs': 5, 'adapterMs': 7}
        self.guard.adapter = TimedAdapter()
        identity = self.prepared()
        self.guard.start(identity)
        done = wait_finished(self.guard, identity)
        expected = self.guard.adapter.native_timings()
        self.assertEqual(done['files'][0]['targets'][0]['nativeTimings'], expected)
        self.guard.close()
        resumed = ConverterGuard(adapter=FakeAdapter(), recovery_parent=self.root / 'recovery')
        try:
            recovered = resumed.status(identity)
            self.assertEqual(recovered['files'][0]['targets'][0]['nativeTimings'], expected)
        finally:
            resumed.close()


    def test_bulk_cancellation_keeps_individual_commits_with_one_ledger_flush(self):
        rows = [{'id': str(i), 'name': f'{i}.docx', 'size': 0} for i in range(100)]
        identity = self.guard.create(rows, ['pdf'])['jobId']
        store = self.guard._jobs[identity].store
        with patch.object(store, 'append_many', wraps=store.append_many) as append:
            state = self.guard.cancel(identity)
        self.assertEqual(state['cancelled'], 100)
        self.assertEqual(append.call_count, 1)
        _, records, _, integrity = store.read()
        self.assertEqual(integrity, 'complete')
        self.assertEqual(len(records), 100)
        self.assertTrue(all(record['state'] == 'cancelled' for record in records.values()))

    def test_slow_capability_matrix_keeps_status_and_cancel_available(self):
        identity = self.guard.create([{'id': 'one', 'name': 'one.docx', 'size': 1}],
                                     prepare_only=True)['jobId']
        self.guard.upload(identity, 'one', io.BytesIO(b'x'), 1)
        entered, resume = threading.Event(), threading.Event()
        errors = []
        def matrix(*args, **kwargs):
            entered.set()
            self.assertTrue(resume.wait(3))
            return {'files': [], 'formats': []}
        def inspect():
            try:
                self.guard.preflight(identity)
            except Exception as error:
                errors.append(error)
        with patch.object(self.guard.adapter, 'preflight', return_value={'state':'detected'}, create=True), \
                patch.object(self.guard.adapter, 'capabilities', side_effect=matrix, create=True):
            worker = threading.Thread(target=inspect)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assert_reader_responds(identity)
                cancelled = self.guard.cancel(identity)
                self.assertTrue(self.guard._jobs[identity].cancel.is_set())
            finally:
                resume.set()
                worker.join(3)
        self.assertEqual([error.code for error in errors], ['CANCELLED'])
        self.assertEqual(self.guard.status(identity)['state'], 'cancelled')

    def test_create_racing_shutdown_does_not_return_an_already_deleted_job(self):
        entered, resume = threading.Event(), threading.Event()
        original = ResultStore.initialize
        errors, returned = [], []
        def blocked(store, job):
            entered.set()
            self.assertTrue(resume.wait(3))
            original(store, job)
        def create():
            try:
                returned.append(self.guard.create([
                    {'id':'one', 'name':'one.docx', 'size':1}], ['pdf'])['jobId'])
            except Exception as error:
                errors.append(error)
        with patch.object(ResultStore, 'initialize', blocked):
            creator = threading.Thread(target=create)
            creator.start()
            closer = None
            try:
                self.assertTrue(entered.wait(2))
                closer = threading.Thread(target=self.guard.close)
                closer.start()
                until = time.monotonic() + 1
                while not self.guard._closed and time.monotonic() < until:
                    time.sleep(.001)
                self.assertTrue(self.guard._closed)
            finally:
                resume.set()
                creator.join(3)
                if closer: closer.join(3)
        self.assertFalse(returned)
        self.assertEqual([getattr(error, 'code', None) for error in errors], ['GUARD_CLOSED'])
        self.assertEqual(self.guard.storage_stats()['jobs'], 0)


if __name__ == '__main__':
    unittest.main()
