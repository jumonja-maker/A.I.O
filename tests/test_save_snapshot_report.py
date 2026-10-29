"""M30: real folder save + independent report consistency, with fake conversion."""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from app_backend.converter_guard import ConverterGuard, GuardError
from app_backend.converter_guard.save_snapshot import snapshot_report
from test_converter_guard_v02 import FakeAdapter, wait_finished


class SnapshotReportPolicy(unittest.TestCase):
    def setUp(self):
        digest = hashlib.sha256(b'data').hexdigest()
        self.report = {'schemaVersion': 3, 'qualityCompared': False, 'failed': 1,
            'files': [{'fileId': 'f', 'targets': [
                {'format': 'html', 'state': 'ready', 'qualityCompared': False,
                 'outputs': [{'path': 'doc.html', 'size': 4, 'sha256': digest,
                              'saved': False, 'readbackVerified': False}]},
                {'format': 'bad', 'state': 'error', 'code': 'ENGINE_OUTPUT_MISSING', 'outputs': []},
                {'format': 'png', 'state': 'running', 'outputs': []}]}]}
        self.manifest = [{'path': 'doc.html', 'size': 4, 'sha256': digest,
            'fileId': 'f', 'format': 'html', 'readbackSize': 4,
            'readbackSha256': digest, 'readbackVerified': True}]
        self.status = {'jobId': 'a'*32, 'saveId': 'b'*32, 'snapshotRevision': 17,
            'state': 'completed', 'outputsExpected': 1, 'outputsWritten': 1,
            'outputsVerified': 1, 'readbackVerified': True,
            'verificationMethod': 'independent_read_sha256'}
    def make(self):
        return snapshot_report(self.report, self.manifest, self.status,
                               job_state_at_start='running')
    def test_snapshot_changes_no_live_input_and_preserves_conversion_failures(self):
        before = deepcopy(self.report)
        result = self.make()
        targets = result['files'][0]['targets']
        self.assertEqual([t['state'] for t in targets], ['saved','error','running'])
        self.assertTrue(targets[0]['outputs'][0]['saved'])
        self.assertFalse(result['qualityCompared'])
        self.assertEqual(result['failed'],1)
        self.assertEqual(result['saveSnapshot']['jobStateAtStart'],'running')
        self.assertEqual(self.report,before)
    def test_copy_hash_is_not_readback_proof(self):
        self.manifest[0]['readbackVerified'] = False
        with self.assertRaises(GuardError): self.make()
    def test_mismatched_hash_cannot_finalize(self):
        self.manifest[0]['readbackSha256'] = '0'*64
        with self.assertRaises(GuardError): self.make()
    def test_duplicate_manifest_cannot_replace_a_missing_output(self):
        self.manifest.append(deepcopy(self.manifest[0]))
        self.status.update(outputsExpected=2,outputsWritten=2,outputsVerified=2)
        with self.assertRaises(GuardError): self.make()
    def test_wrong_file_or_format_cannot_inherit_save(self):
        for key,value in (('fileId','other'),('format','png')):
            with self.subTest(key=key):
                original=self.manifest[0][key];self.manifest[0][key]=value
                with self.assertRaises(GuardError): self.make()
                self.manifest[0][key]=original
    def test_missing_report_output_cannot_finalize(self):
        self.report['files'][0]['targets'][0]['outputs']=[]
        with self.assertRaises(GuardError): self.make()
    def test_wrong_count_cannot_finalize(self):
        self.status['outputsVerified']=0
        with self.assertRaises(GuardError): self.make()
    def test_failed_attempt_never_turns_ready_into_saved(self):
        self.status['state']='failed'
        result=self.make()
        self.assertEqual(result['files'][0]['targets'][0]['state'],'ready')
        self.assertFalse(result['files'][0]['targets'][0]['outputs'][0]['saved'])
        self.assertEqual(result['saveSnapshot']['state'],'failed')


class FolderSnapshotIntegration(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        self.guard=ConverterGuard(adapter=FakeAdapter(),recovery_parent=self.root/'owned')
        self.addCleanup(self.guard.close)
    def ready(self,formats=('pdf','html')):
        job=self.guard.create([{'id':'f','name':'source.docx','size':4}],list(formats))['jobId']
        self.guard.upload(job,'f',io.BytesIO(b'keep'),4)
        self.guard.start(job,archive_mode='on_demand')
        return job,wait_finished(self.guard,job)
    def saved_documents(self,folder):
        return [json.loads((folder/name).read_text(encoding='utf-8')) for name in
                ('AIO-conversion-report.json','AIO-results-manifest.json','AIO-save-status.json')]
    def test_completed_save_report_matches_verified_manifest(self):
        job,state=self.ready()
        result=self.guard.save_folder(state['result']['ticket'],self.root)
        report,manifest,status=self.saved_documents(self.root/result['name'])
        # Baseline bug: old code freezes this as saved=false before the copy.
        self.assertTrue(all(o['saved'] for f in report['files'] for t in f['targets'] for o in t['outputs']))
        self.assertEqual(report['saveSnapshot']['saveId'],manifest['saveId'])
        self.assertEqual(manifest['saveId'],status['saveId'])
        self.assertEqual(report['saveSnapshot']['snapshotRevision'],status['snapshotRevision'])
        self.assertEqual(report['saveSnapshot']['state'],'completed')
        self.assertEqual(status['outputsVerified'],3) # HTML companion is included.
        self.assertEqual({t['state'] for f in report['files'] for t in f['targets']},{'saved'})
        self.assertFalse(report['qualityCompared'])
    def test_cancelled_readback_reports_cancelled_and_retains_unsaved(self):
        job,state=self.ready()
        cancel=threading.Event()
        with self.assertRaises(GuardError):
            self.guard.save_folder(state['result']['ticket'],self.root,cancel=cancel,on_verifying=cancel.set)
        report,manifest,status=self.saved_documents(next(self.root.glob('AIO_Results_*')))
        self.assertEqual(report['saveSnapshot']['state'],'cancelled')
        self.assertEqual(status['state'],'cancelled')
        self.assertTrue(all(not a.saved and a.path.exists() for a in self.guard._jobs[job].artifacts))
        self.assertTrue(all(not o['saved'] for f in report['files'] for t in f['targets'] for o in t['outputs']))
    def test_metadata_failure_never_acknowledges_outputs(self):
        from app_backend.converter_guard.result_store import atomic_json
        job,state=self.ready()
        def fail_final_report(path,value):
            if path.name=='AIO-conversion-report.json' and value.get('saveSnapshot',{}).get('state')=='completed':
                raise OSError('controlled final metadata failure')
            atomic_json(path,value)
        with patch('app_backend.converter_guard.result_store.atomic_json',side_effect=fail_final_report):
            with self.assertRaises(GuardError): self.guard.save_folder(state['result']['ticket'],self.root)
        report,manifest,status=self.saved_documents(next(self.root.glob('AIO_Results_*')))
        self.assertEqual(status['state'],'failed')
        self.assertEqual(report['saveSnapshot']['state'],'failed')
        self.assertTrue(all(not a.saved for a in self.guard._jobs[job].artifacts))
    def test_partial_conversion_is_not_relabelled_success_by_save(self):
        job,state=self.ready(('pdf','bad'))
        result=self.guard.save_folder(state['result']['ticket'],self.root)
        report,_,status=self.saved_documents(self.root/result['name'])
        self.assertEqual(report['failed'],1)
        self.assertEqual([t['state'] for t in report['files'][0]['targets']],['saved','error'])
        self.assertEqual(report['saveSnapshot']['jobStateAtStart'],'partial')
        self.assertEqual(status['state'],'completed')
    def test_live_queue_completion_does_not_leak_into_saved_snapshot(self):
        from app_backend.converter_guard.core import _Artifact
        job,state=self.ready(('pdf',))
        owned=self.guard._jobs[job]
        before_revision=owned.revision
        late=[]
        def publish_later():
            # Simulate the existing worker publishing another output while copy is in progress.
            with self.guard._lock:
                artifact=owned.artifacts[0]
                path=artifact.path.with_name('later.pdf');path.write_bytes(b'later')
                added=_Artifact(job,path,'later.pdf',5,'z'*43,'f','pdf','later.pdf')
                owned.artifacts.append(added);owned.files[0].targets[0].artifacts.append(added)
                self.guard._tickets[added.ticket]=added;late.append(added)
                self.guard._record(owned,'result_ready',file_id='f',target='pdf')
        result=self.guard.save_folder(state['result']['ticket'],self.root,on_verifying=publish_later)
        report,manifest,status=self.saved_documents(self.root/result['name'])
        self.assertEqual(len(manifest['outputs']),1)
        self.assertEqual(len(report['files'][0]['targets'][0]['outputs']),1)
        self.assertFalse(late[0].saved)
        self.assertEqual(result['remainingOutputs'],1)
        self.assertEqual(status['snapshotRevision'],before_revision)


if __name__=='__main__': unittest.main()
