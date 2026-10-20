"""Batch safety, owned results, recovery and source/target queue identity."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import tracemalloc
import unittest
import zipfile

from app_backend.converter_guard import ConverterGuard, GuardError, GuardLimits
from test_converter_guard_v02 import FakeAdapter, wait_finished


def prepare(guard, names, formats=('pdf','html'), *, prepared=False):
    rows=[{'id':'f'+str(i),'name':name,'size':1} for i,name in enumerate(names)]
    job=guard.create(rows, list(formats), prepare_only=prepared)['jobId']
    for row in rows:
        guard.upload(job,row['id'],io.BytesIO(b'x'),1)
    return job


class SourceFailureAdapter(FakeAdapter):
    def convert(self, source, target, workspace, **options):
        if source.name.startswith(('timeout','unloadable')):
            self.calls.append((source.name,target))
            options['on_started']()
            raise GuardError('ENGINE_TIMEOUT' if source.name.startswith('timeout') else 'SOURCE_IMPORT_FAILED',
                             'Native source failed.',stage='engine')
        return super().convert(source,target,workspace,**options)


class GuardV08Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.adapter=FakeAdapter()
        self.guards=[]

    def guard(self, *, durable=False, adapter=None, limits=None):
        guard=ConverterGuard(adapter=adapter or self.adapter,limits=limits,
            spool_parent=self.root/'spool',recovery_parent=self.root/'recovery' if durable else None)
        self.guards.append(guard)
        return guard

    def tearDown(self):
        for guard in self.guards:
            guard.close()
        self.temp.cleanup()

    def test_cancel_builds_one_partial_zip_with_every_completed_sidecar(self):
        guard=self.guard();job=prepare(guard,['ok.csv','hold.csv'])
        guard.start(job)
        self.assertTrue(self.adapter.waiting.wait(2))
        guard.cancel(job)
        done=wait_finished(guard,job)
        self.assertEqual(done['state'],'cancelled')
        self.assertIsNone(done['packagingError'])
        self.assertEqual(done['result']['name'],'AIO_Results_Partial.zip')
        self.assertEqual(len(done['result']['outputs']),3)
        with guard.open_result(done['result']['ticket']) as (stream,_,_):
            with zipfile.ZipFile(stream) as archive:
                self.assertEqual(set(archive.namelist()),
                    {a['archiveName'] for a in done['result']['outputs']}|{'AIO-conversion-report.json'})
                report=json.loads(archive.read('AIO-conversion-report.json'))
                self.assertEqual((report['succeeded'],report['cancelled']),(2,2))

    def test_timeout_defers_same_source_but_continues_other_files(self):
        adapter=SourceFailureAdapter();guard=self.guard(adapter=adapter)
        job=prepare(guard,['timeout.odc','ok.docx'],('pdf','html','txt'))
        guard.start(job,timeout_seconds=9000)
        done=wait_finished(guard,job)
        self.assertEqual(len([c for c in adapter.calls if c[0]=='timeout.odc']),1)
        self.assertEqual((done['failed'],done['succeeded'],done['attempted']),(3,3,4))
        self.assertEqual(done['files'][0]['targets'][1]['code'],'SOURCE_DEFERRED_AFTER_TIMEOUT')
        self.assertEqual(done['fileTimeoutSeconds'],9000)

    def test_proven_import_failure_stops_redundant_targets_without_suffix_ban(self):
        adapter=SourceFailureAdapter();guard=self.guard(adapter=adapter)
        job=prepare(guard,['unloadable.epub','readable.epub'],('pdf','html','txt'))
        guard.start(job);done=wait_finished(guard,job)
        self.assertEqual((done['attempted'],done['succeeded'],done['failed']),(4,3,3))
        self.assertEqual(len([c for c in adapter.calls if c[0]=='unloadable.epub']),1)
        self.assertEqual(len([c for c in adapter.calls if c[0]=='readable.epub']),3)

    def test_folder_fallback_is_one_operation_with_original_relative_resources(self):
        guard=self.guard(limits=GuardLimits(max_spool_bytes=128))
        job=prepare(guard,['same.csv','same.csv'])
        guard.start(job);done=wait_finished(guard,job)
        self.assertEqual(done['packagingError'],'BUNDLE_QUOTA')
        destination=self.root/'destination';destination.mkdir()
        saved=guard.save_folder(done['result']['ticket'],destination)
        self.assertTrue(saved['savedAll'])
        folder=destination/saved['name']
        manifest=json.loads((folder/'AIO-results-manifest.json').read_text())
        self.assertEqual(len(manifest['outputs']),6)
        for output in manifest['outputs']:
            data=(folder/output['path']).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(),output['sha256'])
        self.assertTrue(all(a['saved'] for a in guard.status(job)['result']['outputs']))
        self.assertEqual(len({a['path'] for a in manifest['outputs']}),6)

    def test_cancel_folder_save_keeps_all_originals_for_one_retry(self):
        guard=self.guard();job=prepare(guard,['a.csv'])
        guard.start(job);done=wait_finished(guard,job)
        token=done['result']['ticket'];cancel=threading.Event()
        destination=self.root/'save';destination.mkdir()
        with self.assertRaises(GuardError) as error:
            guard.save_folder(token,destination,cancel=cancel,on_progress=lambda n,t:cancel.set())
        self.assertEqual(error.exception.code,'SAVE_CANCELLED')
        self.assertTrue(all(not a['saved'] for a in guard.status(job)['result']['outputs']))
        self.assertTrue(guard.save_folder(token,destination)['savedAll'])
        self.assertFalse(list(destination.rglob('*.aio-part-*')))

    def test_recovery_reissues_tickets_without_restarting_native_work(self):
        guard=self.guard(durable=True);job=prepare(guard,['a.csv'])
        guard.start(job);done=wait_finished(guard,job)
        old_ticket=done['result']['outputs'][0]['ticket'];root=guard._jobs[job].root
        guard.close();self.assertTrue(root.exists())
        restored=self.guard(durable=True)
        state=restored.status(job)
        self.assertTrue(state['recovered'])
        self.assertEqual(len(state['result']['outputs']),3)
        self.assertNotEqual(state['result']['outputs'][0]['ticket'],old_ticket)
        self.assertEqual(len(self.adapter.calls),2)
        with self.assertRaises(GuardError):
            with restored.open_result(old_ticket):pass
        restored.release(job);self.assertTrue(root.exists())
        restored.release(job,discard=True);self.assertFalse(root.exists())

    def test_other_instance_cannot_claim_or_remove_live_job(self):
        first=self.guard(durable=True);job=prepare(first,['a.csv'])
        first.start(job);wait_finished(first,job)
        second=self.guard(durable=True)
        self.assertEqual(second.recoverable()['jobs'],[])
        second.close()
        self.assertTrue(first._jobs[job].artifacts[0].path.exists())

    def test_torn_final_record_does_not_erase_earlier_successes(self):
        guard=self.guard(durable=True);job=prepare(guard,['a.csv'])
        guard.start(job);wait_finished(guard,job);root=guard._jobs[job].root
        guard.close()
        with (root/'results.jsonl').open('ab') as stream:stream.write(b'{"kind":"task"')
        restored=self.guard(durable=True)
        self.assertEqual(len(restored.status(job)['result']['outputs']),3)

    def test_pause_delta_and_current_task_belong_to_the_correct_file(self):
        guard=self.guard();job=prepare(guard,['hold.csv','next.csv'])
        guard.start(job);self.assertTrue(self.adapter.waiting.wait(2))
        before=guard.status(job)
        self.assertEqual((before['currentTask']['fileId'],before['currentTask']['format']),('f0','pdf'))
        guard.pause(job)
        delta=guard.status(job,since_revision=before['revision'],delta=True)
        self.assertTrue(delta['delta']);self.assertTrue(delta['pauseRequested'])
        self.assertEqual(delta['files'],[])
        guard.cancel_file(job,'f0')
        deadline=time.monotonic()+2
        while time.monotonic()<deadline and guard.status(job)['state']!='paused':time.sleep(.01)
        paused=guard.status(job)
        self.assertEqual(paused['state'],'paused');self.assertIsNone(paused['currentTask'])
        guard.pause(job,False);done=wait_finished(guard,job)
        self.assertEqual((done['cancelled'],done['succeeded']),(2,2))
        ids=[t['taskId'] for f in done['files'] for t in f['targets']]
        self.assertEqual(len(ids),len(set(ids)))

    def test_thousand_file_queue_is_bounded_and_preserves_every_result(self):
        guard=self.guard();started=time.monotonic();tracemalloc.start()
        try:
            job=prepare(guard,['file'+str(i)+'.csv' for i in range(1000)],('pdf',))
            guard.start(job);done=wait_finished(guard,job,seconds=25)
            _,peak=tracemalloc.get_traced_memory()
        finally:tracemalloc.stop()
        self.assertEqual((done['total'],done['succeeded'],done['failed']),(1000,1000,0))
        self.assertEqual(self.adapter.maximum_active,1)
        self.assertLess(peak,80*1024*1024)
        print(json.dumps({'scope':'1000-file Guard queue; synthetic native boundary',
                          'files':1000,'succeeded':done['succeeded'],
                          'pythonHeapPeakMiB':round(peak/1024/1024,2),
                          'elapsedSecondsWithTracemalloc':round(time.monotonic()-started,3)}))
        with guard.open_result(done['result']['ticket']) as (stream,_,_):
            with zipfile.ZipFile(stream) as archive:self.assertEqual(len(archive.namelist()),1001)

    def test_report_contains_mapping_but_no_download_tokens(self):
        guard=self.guard();job=prepare(guard,['a.csv'])
        guard.start(job);wait_finished(guard,job)
        report=guard.report(job);encoded=json.dumps(report)
        self.assertNotIn('ticket',encoded);self.assertNotIn('downloadUrl',encoded)
        target=report['files'][0]['targets'][0]
        self.assertTrue(target['taskId'].startswith(job+':'))
        self.assertGreaterEqual(target['durationMs'],0)

    def test_save_into_owned_storage_cannot_be_acknowledged_then_deleted(self):
        guard=self.guard(durable=True);job=prepare(guard,['a.csv'])
        guard.start(job);state=wait_finished(guard,job)
        root=guard._jobs[job].root
        with self.assertRaises(GuardError) as error:
            guard.save_folder(state['result']['ticket'],root)
        self.assertEqual(error.exception.code,'SAVE_OWNED_STORAGE')
        self.assertTrue(all(not a['saved'] for a in guard.status(job)['result']['outputs']))

    def test_corrupt_header_is_preserved_and_reported_without_claiming_outputs(self):
        guard=self.guard(durable=True);job=prepare(guard,['a.csv'])
        guard.start(job);wait_finished(guard,job);root=guard._jobs[job].root
        guard.close();header=root/'job.json'
        document=json.loads(header.read_text());document['files'][0]['name']='changed.csv'
        header.write_text(json.dumps(document))
        restored=self.guard(durable=True);found=restored.recoverable()
        self.assertEqual(found['jobs'],[])
        self.assertEqual(found['warnings'][0]['code'],'RECOVERY_METADATA_INVALID')
        self.assertTrue(list(root.rglob('*.pdf')))

    def test_many_sidecars_use_small_committed_ledger_records(self):
        from app_backend.converter_guard.result_store import ResultStore
        from app_backend.converter_guard.core import _Task,_File,_Artifact,_Job
        root=self.root/'many';root.mkdir();store=ResultStore(root)
        task=_Task('html',0,state='ready');item=_File('x','document.docx',1,root/'input',[task])
        for number in range(1500):
            path=root/'work'/'0'/'output'/('resource-'+str(number)+'.png')
            task.artifacts.append(_Artifact('a'*32,path,path.name,1,'ticket',archive_name='0001-html/'+path.name))
        job=_Job('a'*32,root,[item],0);store.initialize(job);store.task(item,task)
        self.assertLess(max(len(line) for line in (root/'results.jsonl').read_bytes().splitlines()),2048)
        _,records,_=store.read()
        self.assertEqual(len(records[0]['outputs']),1500)
