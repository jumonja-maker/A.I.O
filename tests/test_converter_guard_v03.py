"""Target plans, failed-only retry metadata and cheap revision-aware polling."""
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import zipfile

from save_test_support import verify_download_for_test
from app_backend.converter_guard import ConverterGuard, GuardError
from test_converter_guard_v02 import FakeAdapter, wait_finished, ticket
from test_converter_guard_http_v02 import start_test_server


class PlanAndRevisionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.adapter=FakeAdapter()
        self.guard=ConverterGuard(adapter=self.adapter,spool_parent=self.temp.name)

    def tearDown(self):
        self.guard.close()
        self.assertEqual(self.guard.storage_stats()['jobs'],0)
        self.assertEqual(self.guard.storage_stats()['reservedBytes'],0)
        self.temp.cleanup()

    def create(self,names,plan=None,formats=None):
        files=[{'id':str(i),'name':name,'size':1} for i,name in enumerate(names)]
        return self.guard.create(files,formats,plan=plan)['jobId']

    def run_job(self,names,plan):
        identity=self.create(names,plan)
        for i in range(len(names)): self.guard.upload(identity,str(i),io.BytesIO(b'x'),1)
        self.guard.start(identity)
        return wait_finished(self.guard,identity)

    def test_per_file_targets_do_not_expand_to_a_cartesian_product(self):
        state=self.run_job(['word.docx','image.bmp','sheet.xlsx'],[
            {'fileId':'0','formats':['pdf','odt']},
            {'fileId':'1','formats':['png']},
            {'fileId':'2','formats':['xlsx','csv']}])
        self.assertEqual(state['total'],5)
        self.assertEqual(self.adapter.calls,[('word.docx','pdf'),('word.docx','odt'),
            ('image.bmp','png'),('sheet.xlsx','xlsx'),('sheet.xlsx','csv')])
        with self.guard.open_result(ticket(state['result'])) as (stream,_,_):
            with zipfile.ZipFile(stream) as archive:
                self.assertEqual(archive.namelist()[:-3],[
                    '0001-pdf/word.pdf','0002-odt/word.odt','0003-png/image.png',
                    '0004-xlsx/sheet.xlsx','0005-csv/sheet.csv'])

    def test_plan_entry_order_does_not_change_source_display_order(self):
        state=self.run_job(['one.bmp','two.docx'],[
            {'fileId':'1','formats':['odt']},{'fileId':'0','formats':['png','png']}])
        self.assertEqual(self.adapter.calls,[('one.bmp','png'),('two.docx','odt')])
        self.assertEqual([f['name'] for f in state['files']],['one.bmp','two.docx'])

    def test_incomplete_duplicate_unknown_and_empty_plans_fail_before_spooling(self):
        files=[{'id':'a','name':'x.bmp','size':1}]
        cases=[[],[{'fileId':'b','formats':['png']}],
               [{'fileId':'a','formats':[]}],
               [{'fileId':'a','formats':['png']},{'fileId':'a','formats':['pdf']}],
               [{'fileId':'a','formats':['../pdf']}],{},[None]]
        for plan in cases:
            with self.subTest(plan=plan),self.assertRaises(GuardError):
                self.guard.create(files,plan=plan)
            self.assertEqual(list(Path(self.temp.name).iterdir()),[])
        with self.assertRaises(GuardError):
            self.guard.create(files,['pdf'],plan=[{'fileId':'a','formats':['png']}])

    def test_retry_metadata_contains_only_failed_pairs_and_retains_successes(self):
        first=self.run_job(['word.docx','image.bmp'],[
            {'fileId':'0','formats':['bad','pdf']},{'fileId':'1','formats':['png']}])
        plan=self.guard.retry_plan(first['jobId'])
        self.assertEqual(plan['files'],[{'id':'0','name':'word.docx','size':1}])
        self.assertEqual(plan['plan'],[{'fileId':'0','formats':['bad']}])
        self.assertTrue(plan['requiresUpload'])
        self.assertEqual(plan['retainedOutputs'],2)
        before=len(self.adapter.calls)
        retry=self.guard.create(plan['files'],plan=plan['plan'])['jobId']
        self.guard.upload(retry,'0',io.BytesIO(b'x'),1)
        self.guard.start(retry)
        wait_finished(self.guard,retry)
        self.assertEqual(self.adapter.calls[before:],[('word.docx','bad')])
        with self.guard.open_result(ticket(first['result']['outputs'][0])) as (stream,_,_):
            self.assertEqual(stream.read(),b'data')

    def test_retry_successful_job_is_empty_and_does_not_start_a_conversion(self):
        state=self.run_job(['image.bmp'],[{'fileId':'0','formats':['png']}])
        before=len(self.adapter.calls)
        retry=self.guard.retry_plan(state['jobId'])
        self.assertEqual(retry['files'],[])
        self.assertEqual(retry['plan'],[])
        self.assertFalse(retry['requiresUpload'])
        self.assertEqual(len(self.adapter.calls),before)

    def test_cancelled_pairs_are_not_retried_without_explicit_opt_in(self):
        identity=self.create(['image.bmp'],[{'fileId':'0','formats':['png','pdf']}])
        self.guard.cancel_file(identity,'0')
        self.guard.start(identity)
        wait_finished(self.guard,identity)
        self.assertEqual(self.guard.retry_plan(identity)['plan'],[])
        self.assertEqual(self.guard.retry_plan(identity,include_cancelled=True)['plan'],
                         [{'fileId':'0','formats':['png','pdf']}])

    def test_retry_of_running_or_unstarted_job_is_rejected(self):
        identity=self.create(['hold.bmp'],[{'fileId':'0','formats':['png']}])
        with self.assertRaises(GuardError): self.guard.retry_plan(identity)
        self.guard.upload(identity,'0',io.BytesIO(b'x'),1)
        self.guard.start(identity)
        self.assertTrue(self.adapter.waiting.wait(2))
        with self.assertRaises(GuardError): self.guard.retry_plan(identity)
        self.guard.cancel(identity)
        wait_finished(self.guard,identity)

    def test_revision_changes_on_upload_conversion_and_save(self):
        identity=self.create(['image.bmp'],[{'fileId':'0','formats':['png']}])
        first=self.guard.status(identity)
        self.assertTrue(self.guard.status(identity,since_revision=first['revision'])['unchanged'])
        self.guard.upload(identity,'0',io.BytesIO(b'x'),1)
        second=self.guard.status(identity,since_revision=first['revision'])
        self.assertGreater(second['revision'],first['revision'])
        self.guard.start(identity)
        done=wait_finished(self.guard,identity)
        self.assertGreater(done['revision'],second['revision'])
        verify_download_for_test(self, self.guard, ticket(done['result']))
        saved=self.guard.confirm_saved(identity,ticket(done['result']))
        self.assertGreater(saved['revision'],done['revision'])

    def test_finished_transition_is_never_hidden_by_an_unchanged_revision(self):
        entered,unblock=threading.Event(),threading.Event()
        original=self.guard._make_result
        def pause(job):
            original(job)
            entered.set()
            unblock.wait(2)
        identity=self.create(['image.bmp'],[{'fileId':'0','formats':['png']}])
        self.guard.upload(identity,'0',io.BytesIO(b'x'),1)
        try:
            with patch.object(self.guard,'_make_result',pause):
                self.guard.start(identity)
                self.assertTrue(entered.wait(1))
                before=self.guard.status(identity)
                self.assertFalse(before['finished'])
                unblock.set()
                finished=wait_finished(self.guard,identity)
                self.assertTrue(finished['finished'])
                self.assertGreater(finished['revision'],before['revision'])
                self.assertTrue(self.guard.status(identity,since_revision=before['revision'])['finished'])
        finally:
            unblock.set()

    def test_unchanged_large_job_poll_skips_snapshot_allocation(self):
        files=[{'id':str(i),'name':str(i)+'.bmp','size':1} for i in range(1000)]
        snapshot=self.guard.create(files,['png'])
        with patch.object(self.guard,'_snapshot',side_effect=AssertionError('large snapshot reconstructed')):
            state=self.guard.status(snapshot['jobId'],since_revision=snapshot['revision'])
        self.assertTrue(state['unchanged'])
        self.assertLess(len(json.dumps(state)),120)
        self.assertNotIn('files',state)
        self.assertGreater(len(json.dumps(snapshot)),200000)

    def test_trace_request_remains_explicit_and_ignores_conditional_poll(self):
        identity=self.create(['a.bmp'])
        state=self.guard.status(identity)
        detail=self.guard.status(identity,since_revision=state['revision'],include_trace=True)
        self.assertIn('files',detail)
        self.assertIsNotNone(detail['trace'])

    def test_invalid_revision_and_retry_flags_are_structured_errors(self):
        identity=self.create(['a.bmp'])
        for value in (False,-1,'1',[],{}):
            with self.subTest(value=value),self.assertRaises(GuardError):
                self.guard.status(identity,since_revision=value)
        with self.assertRaises(GuardError): self.guard.retry_plan(identity,include_cancelled='yes')


class PlanHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.guard=ConverterGuard(adapter=FakeAdapter(),spool_parent=self.temp.name)
        self.server,self.thread,self.origin,self.token=start_test_server(self.guard)

    def tearDown(self):
        self.server.api.close()
        self.guard.close()
        self.server.shutdown();self.server.server_close();self.thread.join(2)
        self.temp.cleanup()

    def request(self,path,body=None,headers=None):
        hdr={'X-AIO-Token':self.token,**(headers or {})}
        if isinstance(body,dict):
            body=json.dumps(body).encode()
            hdr['Content-Type']='application/json'
        return urlopen(Request(self.origin+'/api/converter-guard/'+path,data=body,headers=hdr),timeout=5)

    def json(self,*args,**kwargs):
        with self.request(*args,**kwargs) as response:return json.load(response)

    def create(self):
        return self.json('jobs',{'files':[{'id':'a','name':'a.bmp','size':1}],
            'plan':[{'fileId':'a','formats':['png']} ]})['jobId']

    def test_http_conditional_poll_returns_304_then_new_etag_after_upload(self):
        identity=self.create()
        with self.request('jobs?job='+identity) as response:
            tag=response.headers['ETag'];state=json.load(response)
        with self.assertRaises(HTTPError) as error:
            self.request('jobs?job='+identity,headers={'If-None-Match':tag})
        self.assertEqual(error.exception.code,304)
        self.assertEqual(error.exception.read(),b'')
        self.request('input?job='+identity+'&file=a',b'x',
                     headers={'Content-Type':'application/octet-stream'}).close()
        with self.request('jobs?job='+identity,headers={'If-None-Match':tag}) as response:
            self.assertNotEqual(response.headers['ETag'],tag)
            self.assertGreater(json.load(response)['revision'],state['revision'])

    def test_other_jobs_etag_and_malformed_tag_do_not_hide_updates(self):
        first=self.create();second=self.create()
        with self.request('jobs?job='+first) as response:tag=response.headers['ETag']
        for value in (tag,'"bad"', '"'+second+':'+'9'*5000+'"'):
            with self.subTest(value=value),self.request('jobs?job='+second,headers={'If-None-Match':value}) as response:
                self.assertEqual(response.status,200)
                self.assertEqual(json.load(response)['jobId'],second)

    def test_trace_forces_full_response_even_with_current_etag(self):
        identity=self.create()
        with self.request('jobs?job='+identity) as response:tag=response.headers['ETag']
        with self.request('jobs?job='+identity+'&trace=1',headers={'If-None-Match':tag}) as response:
            self.assertEqual(response.status,200)
            self.assertIsNotNone(json.load(response)['trace'])

    def test_ambiguous_and_null_plans_are_not_silently_replaced_by_pdf(self):
        for extra in ({'plan':None},{'plan':[]},{'plan':[{'fileId':'a','formats':['png']}],'formatId':'pdf'}):
            with self.subTest(extra=extra),self.assertRaises(HTTPError):
                self.request('jobs',{'files':[{'id':'a','name':'a.bmp','size':1}],**extra})
        self.assertEqual(self.guard.storage_stats()['jobs'],0)

    def test_http_retry_plan_is_metadata_only_and_preserves_ready_tickets(self):
        identity=self.json('jobs',{'files':[{'id':'a','name':'a.bmp','size':1}],
            'plan':[{'fileId':'a','formats':['bad','png']} ]})['jobId']
        self.request('input?job='+identity+'&file=a',b'x',
                     headers={'Content-Type':'application/octet-stream'}).close()
        self.json('start',{'jobId':identity})
        done=wait_finished(self.guard,identity)
        before=list(self.guard.adapter.calls)
        retry=self.json('retry-plan?job='+identity)
        self.assertEqual(retry['plan'],[{'fileId':'a','formats':['bad']}])
        self.assertEqual(self.guard.adapter.calls,before)
        path=done['result']['outputs'][0]['downloadUrl'].split('ticket=')[1]
        with self.guard.open_result(path) as (stream,_,_):self.assertEqual(stream.read(),b'data')


if __name__=='__main__':
    unittest.main()
