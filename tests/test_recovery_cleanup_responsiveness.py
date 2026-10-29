"""M02: filesystem cleanup may be slow; UI acknowledgement must not wait for it."""
import io
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app_backend.converter_guard import ConverterGuard, GuardError
from app_backend.converter_guard.http_api import GuardAPI, PREFIX
from test_converter_guard_v02 import FakeAdapter, wait_finished


class RecoveryCleanupTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        self.guard=ConverterGuard(adapter=FakeAdapter(),recovery_parent=self.root/'owned')
        self.addCleanup(self.guard.close)
    def ready(self):
        identity=self.guard.create([{'id':'f','name':'source.docx','size':4}],['pdf'])['jobId']
        self.guard.upload(identity,'f',io.BytesIO(b'keep'),4)
        self.guard.start(identity,archive_mode='on_demand')
        state=wait_finished(self.guard,identity)
        return identity,self.guard._jobs[identity],state
    def drain(self):
        self.guard._cleanup_executor.submit(lambda:None).result(timeout=5)
    def test_disappearing_child_does_not_release_live_storage_accounting(self):
        identity,job,state=self.ready()
        self.addCleanup(job.store.close)
        reserved=self.guard.storage_stats()['reservedBytes']
        with patch.object(job.store,'cleanup_retired',side_effect=FileNotFoundError('vanished child')):
            self.guard.release(identity,discard=True)
        # Baseline swallowed FileNotFoundError and forgot a still-existing root.
        self.assertIn(identity,self.guard._jobs)
        self.assertTrue(job.root.exists())
        self.assertEqual(self.guard.storage_stats()['reservedBytes'],reserved)
        self.guard._cleanup(job)
        self.assertNotIn(identity,self.guard._jobs)
    def test_slow_delete_returns_before_disk_cleanup_and_stays_accounted(self):
        identity,job,state=self.ready()
        entered,allow=threading.Event(),threading.Event()
        original=job.store.cleanup_retired
        def slow():
            entered.set()
            if not allow.wait(5): raise OSError('test cleanup barrier timed out')
            original()
        with patch.object(job.store,'cleanup_retired',side_effect=slow) as cleanup:
            try:
                result=self.guard.release(identity,discard=True,defer_cleanup=True)
                self.assertTrue(entered.wait(2))
                self.assertTrue(result['cleanupPending'])
                self.assertTrue(job.root.exists())
                self.assertGreater(self.guard.storage_stats()['reservedBytes'],0)
                self.assertEqual(self.guard.recoverable()['jobs'],[])
                for _ in range(5): self.guard._prune()
                self.assertEqual(cleanup.call_count,1)
            finally:
                allow.set();self.drain()
        self.assertFalse(job.root.exists())
        self.assertEqual(self.guard.storage_stats()['reservedBytes'],0)
    def test_recovery_scan_cannot_reenter_an_owned_retiring_root(self):
        identity,job,state=self.ready()
        entered,allow=threading.Event(),threading.Event()
        original=job.store.cleanup_retired
        def after_unlock():
            job.store.close()  # Windows removal begins after owner.lock closes.
            entered.set()
            if not allow.wait(5): raise OSError('test cleanup barrier timed out')
            original()
        from app_backend.converter_guard.result_store import ResultStore
        with patch.object(job.store,'cleanup_retired',side_effect=after_unlock):
            try:
                self.guard.release(identity,discard=True,defer_cleanup=True)
                self.assertTrue(entered.wait(2))
                with patch.object(ResultStore,'acquire',side_effect=AssertionError('duplicate recovery ownership')):
                    self.assertEqual(self.guard.recoverable()['jobs'],[])
                self.assertIn(identity,self.guard._jobs)
            finally:
                allow.set();self.drain()
        self.assertFalse(job.root.exists())
    def test_unsaved_release_without_discard_retains_outputs(self):
        identity,job,state=self.ready()
        result=self.guard.release(identity,defer_cleanup=True)
        self.assertTrue(result['retained'])
        self.assertFalse(result['released'])
        self.assertTrue(all(a.path.exists() for a in job.artifacts))
        self.assertFalse(job.cleaning)
    def test_close_still_requires_confirmation(self):
        identity,job,state=self.ready()
        result=self.guard.close_conversion(identity,defer_cleanup=True)
        self.assertTrue(result['confirmationRequired'])
        self.assertFalse(job.released)
        self.assertTrue(job.root.exists())
    def test_active_reader_keeps_lease_until_exit(self):
        identity,job,state=self.ready()
        with self.guard.open_result(state['result']['ticket']) as (stream,_,size):
            result=self.guard.release(identity,discard=True,defer_cleanup=True)
            self.assertTrue(result['cleanupPending'])
            self.assertFalse(job.cleaning)
            self.assertEqual(stream.read(),b'data')
        self.drain()
        self.assertFalse(job.root.exists())
    def test_failed_background_cleanup_has_bounded_retry(self):
        identity,job,state=self.ready()
        with patch.object(job.store,'cleanup_retired',side_effect=PermissionError('busy')) as cleanup:
            self.guard.release(identity,discard=True,defer_cleanup=True);self.drain()
            for _ in range(20): self.guard._prune()
            self.drain()
            self.assertEqual(cleanup.call_count,1)
            self.assertEqual(self.guard.storage_stats()['cleanupRetryJobs'],1)
            self.assertIn(identity,self.guard._jobs)
        job.cleanup_retry_at=0
        self.guard._cleanup(job);self.drain()
        self.assertNotIn(identity,self.guard._jobs)
    def test_http_release_and_close_select_nonblocking_cleanup(self):
        for route in ('release','close'):
            with self.subTest(route=route):
                identity,job,state=self.ready()
                api=GuardAPI(self.guard,origin='http://127.0.0.1:8877',token='token')
                self.addCleanup(api.close)
                handler=SimpleNamespace(connection=Mock(),_json=lambda code,body:(code,body))
                method='release' if route=='release' else 'close_conversion'
                with patch.object(api,'_authorize'),patch.object(api,'_body',return_value={'jobId':identity,'discard':True}),patch.object(self.guard,method,wraps=getattr(self.guard,method)) as operation:
                    code,body=api.post(handler,PREFIX+'/'+route)
                    self.assertEqual(code,200)
                    self.assertTrue(operation.call_args.kwargs['defer_cleanup'])
                self.drain()
    def test_invalid_defer_flag_rejected_before_release(self):
        identity,job,state=self.ready()
        with self.assertRaises(GuardError): self.guard.release(identity,discard=True,defer_cleanup='yes')
        self.assertFalse(job.released)


if __name__=='__main__': unittest.main()
