"""A04: Guard cancellation before start and after terminal must be truthful."""
import io
import tempfile
import unittest

from app_backend.converter_guard import ConverterGuard
from test_converter_guard_v02 import FakeAdapter, wait_finished


class AuditGuardCancel(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.guard = ConverterGuard(adapter=FakeAdapter(), spool_parent=self.temp.name)

    def tearDown(self):
        self.guard.close()
        self.assertEqual(self.guard.storage_stats()['jobs'], 0)
        self.temp.cleanup()

    def test_cancel_before_upload_never_launches_a_native_job(self):
        created = self.guard.create([{'id': 'source', 'name': 'image.bmp', 'size': 2}],
                                    ['pdf', 'png'])
        cancelled = self.guard.cancel(created['jobId'])
        self.assertTrue(cancelled['finished'])
        self.assertEqual(cancelled['state'], 'cancelled')
        self.assertEqual(cancelled['succeeded'], 0)
        self.assertEqual(cancelled['cancelled'], 2)
        self.assertEqual(len(self.guard.adapter.calls), 0)

    def test_late_cancel_does_not_poison_finished_success_or_its_ticket(self):
        created = self.guard.create([{'id': 'source', 'name': 'image.bmp', 'size': 2}], ['pdf'])
        identity = created['jobId']
        self.guard.upload(identity, 'source', io.BytesIO(b'BM'), 2)
        self.guard.start(identity)
        done = wait_finished(self.guard, identity)
        self.assertEqual(done['state'], 'ready')
        self.assertEqual(done['succeeded'], 1)
        ticket = done['result']['ticket']
        late = self.guard.cancel(identity)
        self.assertEqual(late['state'], 'ready')
        self.assertEqual(late['succeeded'], 1)
        with self.guard.open_result(ticket) as (stream, _, _):
            self.assertEqual(stream.read(), b'data')
