import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
import zipfile

from app_backend.converter_guard import ConverterGuard, GuardError, GuardLimits
from test_converter_guard_v02 import FakeAdapter, wait_finished


class SessionLifecycleContracts(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.guards=[]
    def tearDown(self):
        for guard in self.guards:guard.close()
        self.temp.cleanup()
    def ready(self, name='source.docx', targets=('pdf','html')):
        guard=ConverterGuard(adapter=FakeAdapter(),recovery_parent=self.root/'owned')
        self.guards.append(guard)
        job=guard.create([{'id':'f','name':name,'size':4}],list(targets),retain_sources=True)['jobId']
        guard.upload(job,'f',io.BytesIO(b'keep'),4);guard.start(job,archive_mode='on_demand')
        return guard,job,wait_finished(guard,job)
    def test_saved_session_stays_available_with_zero_unsaved_outputs(self):
        guard,job,state=self.ready()
        saved=guard.save_folder(state['result']['ticket'],self.root)
        self.assertTrue(saved['savedAll'])
        retained=guard.recoverable()['jobs']
        self.assertEqual([r['jobId'] for r in retained],[job])
        self.assertEqual(retained[0]['unsavedOutputs'],0)
        self.assertFalse(guard.status(job)['comparisonAvailable'])
        guard.close_conversion(job)
        with self.assertRaises(GuardError):guard.status(job)
        self.assertTrue((self.root/saved['name']).is_dir())
    def test_close_warns_for_active_or_unfinished_zero_output_session(self):
        guard=ConverterGuard(adapter=FakeAdapter(),recovery_parent=self.root/'owned-zero')
        self.guards.append(guard)
        job=guard.create([{'id':'f','name':'pending.docx','size':4}],['pdf'],retain_sources=True)['jobId']
        self.assertEqual(guard.status(job)['state'],'uploading')
        result=guard.close_conversion(job)
        self.assertTrue(result['confirmationRequired'])
        self.assertEqual(result['unsavedOutputs'],0)
        self.assertTrue(result['activeConversion'])
        self.assertEqual(guard.status(job)['state'],'uploading')
        self.assertTrue(guard.close_conversion(job,discard=True)['closed'])

    def test_close_warns_if_recovery_incomplete_even_after_all_known_outputs_saved(self):
        guard,job,state=self.ready()
        saved=guard.save_folder(state['result']['ticket'],self.root)
        self.assertTrue(saved['savedAll'])
        with guard._lock:
            guard._jobs[job].recovery_error='RECOVERY_LEDGER_INCOMPLETE'
        result=guard.close_conversion(job)
        self.assertTrue(result['confirmationRequired'])
        self.assertEqual(result['unsavedOutputs'],0)
        self.assertTrue(result['recoveryWarning'])
        self.assertEqual(guard.status(job)['recoveryError'],'RECOVERY_LEDGER_INCOMPLETE')
        self.assertTrue(guard.close_conversion(job,discard=True)['closed'])
        self.assertTrue((self.root/saved['name']).exists())

    def test_close_without_confirmation_is_a_read_only_decision(self):
        guard,job,state=self.ready();root=guard._jobs[job].root
        def inventory():
            files={}
            for path in root.rglob('*'):
                if not path.is_file():continue
                if path==root/'owner.lock':
                    # Windows denies a second reader over the locked byte.
                    # Hash through the existing owner handle; do not omit it.
                    owner=guard._jobs[job].store.owner;position=owner.tell()
                    try:owner.seek(0);data=owner.read()
                    finally:owner.seek(position)
                else:data=path.read_bytes()
                files[path.relative_to(root).as_posix()]=hashlib.sha256(data).hexdigest()
            return files
        before=inventory()
        result=guard.close_conversion(job)
        self.assertFalse(result['closed']);self.assertTrue(result['confirmationRequired'])
        self.assertFalse(guard._jobs[job].cancel.is_set())
        self.assertEqual(before,inventory())
    def test_confirmed_close_removes_only_own_spool_and_preserves_saved_copy_and_other_job(self):
        guard,job,state=self.ready();artifact=state['result']['outputs'][0]
        saved_path=self.root/'user-saved.pdf'
        with guard.open_result(artifact['ticket']) as (stream,_,__):saved_path.write_bytes(stream.read())
        guard.verify_saved_download(artifact['ticket'],saved_path);guard.confirm_saved(job,artifact['ticket'])
        other=guard.create([{'id':'other','name':'other.docx','size':4}],['pdf'])['jobId']
        guard.upload(other,'other',io.BytesIO(b'keep'),4);guard.start(other);wait_finished(guard,other)
        source=guard._jobs[job].files[0].source;source_hash=hashlib.sha256(source.read_bytes()).hexdigest()
        self.assertTrue(guard.close_conversion(job,discard=True)['closed'])
        self.assertEqual(saved_path.read_bytes(),b'data')
        self.assertTrue(guard.status(other)['finished'])
        self.assertEqual(source_hash,hashlib.sha256(b'keep').hexdigest())
    def test_close_is_blocked_during_save_and_holds_data_until_native_cancel_drains(self):
        guard,job,state=self.ready()
        guard._jobs[job].saving=True
        try:
            with self.assertRaises(GuardError) as err:guard.close_conversion(job,discard=True)
            self.assertEqual(err.exception.code,'SAVE_BUSY')
            self.assertFalse(guard._jobs[job].released)
        finally:guard._jobs[job].saving=False
        live=guard.create([{'id':'live','name':'hold.docx','size':4}],['pdf'])['jobId']
        guard.upload(live,'live',io.BytesIO(b'keep'),4);guard.start(live)
        self.assertTrue(guard.adapter.waiting.wait(2))
        self.assertTrue(guard.close_conversion(live,discard=True)['closed'])
        guard._jobs.get(live) and guard._jobs[live].future.result(timeout=3)
        self.assertEqual(guard.adapter.active,0)


if __name__=='__main__':unittest.main()
