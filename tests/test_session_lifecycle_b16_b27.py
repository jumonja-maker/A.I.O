import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
import zipfile

from app_backend.converter_guard import ConverterGuard, GuardError, GuardLimits
from app_backend.converter_guard.diagnostic_export import diagnostic_package
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
        self.assertTrue(guard.status(job)['comparisonAvailable'])
        guard.close_conversion(job)
        with self.assertRaises(GuardError):guard.status(job)
        self.assertTrue((self.root/saved['name']).is_dir())
    def test_close_without_confirmation_is_a_read_only_decision(self):
        guard,job,state=self.ready();root=guard._jobs[job].root
        before={p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                for p in root.rglob('*') if p.is_file()}
        result=guard.close_conversion(job)
        self.assertFalse(result['closed']);self.assertTrue(result['confirmationRequired'])
        self.assertFalse(guard._jobs[job].cancel.is_set())
        self.assertEqual(before,{p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                for p in root.rglob('*') if p.is_file()})
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
    def test_selected_diagnostic_never_saves_or_adds_required_outputs(self):
        guard,job,state=self.ready();before=guard.storage_stats()['unsavedOutputs']
        package=diagnostic_package(guard,job,job+':1',{'status':'confirmed_loss','findings':[]},output_index=0)
        self.assertTrue(package['optionalDiagnostic']);self.assertFalse(package['requiredForSave'])
        self.assertEqual(guard.storage_stats()['unsavedOutputs'],before)
        with guard.open_result(package['ticket']) as (stream,_,__):data=stream.read()
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names=archive.namelist();self.assertEqual(len([n for n in names if n.startswith('result/')]),1)
            self.assertFalse(any(n.startswith('source/') for n in names))
            self.assertFalse(any(n.endswith('.pdf') for n in names))
        guard.confirm_saved(job,package['ticket'])
        self.assertEqual(guard.storage_stats()['unsavedOutputs'],before)
        self.assertFalse(any(o['saved'] for o in guard.status(job)['result']['outputs']))
    def test_failed_without_output_exports_report_only_unless_source_explicitly_chosen(self):
        guard,job,state=self.ready(name='bad.docx',targets=('pdf',))
        self.assertEqual(state['failed'],1)
        for include in (False,True):
            package=diagnostic_package(guard,job,job+':0',{'status':'not_checked'},include_source=include)
            with guard.open_result(package['ticket']) as (stream,_,__):data=stream.read()
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names=archive.namelist();self.assertFalse(any(n.startswith('result/') for n in names))
                self.assertEqual(any(n.startswith('source/') for n in names),include)
                self.assertFalse(json.loads(archive.read('AIO-diagnostic.json'))['outputPresent'])
        self.assertEqual(guard.storage_stats()['unsavedOutputs'],0)
    def test_diagnostic_rejects_other_task_and_changed_or_symlinked_output(self):
        guard,job,state=self.ready()
        with self.assertRaises(GuardError):diagnostic_package(guard,job,'f'*32+':0',{},output_index=0)
        artifact=guard._jobs[job].artifacts[0];artifact.path.write_bytes(b'evil')
        with self.assertRaises(GuardError) as err:diagnostic_package(guard,job,job+':0',{},output_index=0)
        self.assertEqual(err.exception.code,'SAVE_CHANGED')
        self.assertEqual(guard.storage_stats()['unsavedOutputs'],3)


if __name__=='__main__':unittest.main()
