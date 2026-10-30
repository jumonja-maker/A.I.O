"""Actual loopback sockets for the optional, independently mounted Guard API."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import secrets
import socket
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import zipfile

from save_test_support import verify_download_for_test
from app_backend.converter_guard import ConverterGuard
from app_backend.converter_guard.http_api import GuardAPI, PREFIX
from test_converter_guard_v02 import FakeAdapter


class Handler(BaseHTTPRequestHandler):
    # Guard's cooperative socket uploader requires the same unbuffered body
    # reader as the real AIO host, otherwise headers can prefetch input bytes.
    rbufsize = 0

    def _json(self, status, body):
        data = json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except OSError:
            pass

    def do_GET(self):
        return self.server.api.get(self,urlsplit(self.path).path)

    def do_POST(self):
        return self.server.api.post(self,urlsplit(self.path).path)

    def log_message(self,*args):
        pass


def start_test_server(guard):
    server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    token = secrets.token_urlsafe(32)
    origin = 'http://127.0.0.1:' + str(server.server_port)
    server.api = GuardAPI(guard,origin=origin,token=token)
    thread = threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True)
    thread.start()
    return server,thread,origin,token


class GuardHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.guard = ConverterGuard(adapter=FakeAdapter(),spool_parent=self.temp.name)
        self.server,self.thread,self.origin,self.token = start_test_server(self.guard)

    def tearDown(self):
        self.server.api.close()
        self.guard.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.assertEqual(self.guard.storage_stats()['jobs'],0)
        self.temp.cleanup()

    def request(self,path,body=None,*,auth=True,headers=None):
        hdr = {'X-AIO-Token':self.token} if auth else {}
        hdr.update(headers or {})
        if isinstance(body,dict):
            body=json.dumps(body).encode('utf-8')
            hdr['Content-Type']='application/json'
        return urlopen(Request(self.origin+PREFIX+'/'+path,data=body,headers=hdr),timeout=5)

    def json_request(self,*args,**kwargs):
        with self.request(*args,**kwargs) as response:
            return json.loads(response.read())

    def job(self,files,formats=None):
        rows=[{'id':str(i),'name':name,'size':len(data)} for i,(name,data) in enumerate(files)]
        body={'files':rows}
        if formats is not None: body['formats']=formats
        identity=self.json_request('jobs',body)['jobId']
        for i,(_,data) in enumerate(files):
            self.request('input?job='+identity+'&file='+str(i),data,
                         headers={'Content-Type':'application/octet-stream'}).close()
        self.json_request('start',{'jobId':identity})
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            state=self.json_request('jobs?job='+identity)
            if state['finished']: return state
            time.sleep(.005)
        raise AssertionError('HTTP job did not finish')

    def test_missing_token_external_origin_and_rebinding_host_denied(self):
        for kwargs in ({'auth':False},{'headers':{'Origin':'https://evil.invalid'}},
                       {'headers':{'Host':'evil.invalid'}}):
            with self.subTest(kwargs=kwargs),self.assertRaises(HTTPError) as error:
                self.request('status',**kwargs)
            self.assertEqual(error.exception.code,403)

    def test_status_reports_unbounded_document_policy_without_old_allowlist(self):
        state=self.json_request('status')
        self.assertIsNone(state['limits']['max_file_bytes'])
        self.assertEqual(state['limits']['file_timeout'], 1800)
        self.assertEqual(state['acceptedExtensions'],'')
        self.assertNotIn('inputs',state)

    def test_multi_format_job_reaches_native_boundary_and_returns_archive(self):
        state=self.job([('image.bmp',b'x'),('source.anything',b'x')],['pdf','png'])
        self.assertEqual(state['state'],'ready')
        self.assertEqual(state['total'],4)
        with self.request(state['result']['downloadUrl'].removeprefix(PREFIX+'/'),auth=False) as response:
            self.assertEqual(response.headers['Content-Type'],'application/zip')
            with zipfile.ZipFile(io.BytesIO(response.read())) as archive:
                self.assertEqual(len(archive.namelist()),7)  # four outputs + report + manifest + packaging status

    def test_actual_download_does_not_confirm_saved_and_can_be_retried(self):
        state=self.job([('Договор.docx',b'x')],['txt'])
        path=state['result']['downloadUrl'].removeprefix(PREFIX+'/')
        with self.request(path,auth=False) as response:
            self.assertTrue(response.headers['Content-Type'].startswith('text/plain'))
            self.assertIn("filename*=UTF-8''",response.headers['Content-Disposition'])
            self.assertEqual(response.read(),b'')
        self.assertEqual(self.json_request('jobs?job='+state['jobId'])['files'][0]['state'],'ready')
        with self.request(path,auth=False) as response: self.assertEqual(response.read(),b'')
        ticket=path.split('ticket=')[1]
        with self.assertRaises(HTTPError) as missing:
            self.json_request('saved',{'jobId':state['jobId'],'ticket':ticket})
        self.assertEqual(missing.exception.code,409)
        verify_download_for_test(self, self.guard, ticket)
        saved=self.json_request('saved',{'jobId':state['jobId'],'ticket':ticket})
        self.assertEqual(saved['files'][0]['state'],'saved')

    def test_compact_save_acknowledgement_is_explicit_and_bounded(self):
        state=self.job([('first.docx',b'x'),('second.docx',b'x')],['pdf','png'])
        body={'jobId':state['jobId'],'ticket':state['result']['ticket'],'compact':True}
        verify_download_for_test(self, self.guard, body['ticket'])
        saved=self.json_request('saved',body)
        self.assertTrue(saved['savedAll'])
        self.assertEqual(saved['outputs'],4)
        self.assertEqual(saved['remainingOutputs'],0)
        self.assertNotIn('files',saved)
        self.assertLess(len(json.dumps(saved)),600)
        with self.assertRaises(HTTPError) as error:
            self.request('saved',{**body,'compact':'true'})
        self.assertEqual(json.loads(error.exception.read())['code'],'SAVE_REQUEST')

    def test_partial_result_includes_failures_without_blocking_other_files(self):
        state=self.job([('bad.docx',b'x'),('okay.bmp',b'x')],['pdf','bad','png'])
        self.assertEqual(state['state'],'partial')
        self.assertEqual(state['completed'],6)
        self.assertEqual(len(state['result']['outputs']),2)

    def test_html_sidecars_have_complete_archive_hierarchy(self):
        state=self.job([('web.docx',b'x')],['html'])
        path=state['result']['downloadUrl'].removeprefix(PREFIX+'/')
        with self.request(path,auth=False) as response,zipfile.ZipFile(io.BytesIO(response.read())) as archive:
            self.assertIn('0001-html/images/plot.png',archive.namelist())
            self.assertIn(b'images/plot.png',archive.read('0001-html/web.html'))

    def test_release_removes_tickets_but_never_touches_other_jobs(self):
        first=self.job([('first.bmp',b'x')])
        second=self.job([('second.bmp',b'x')])
        path=first['result']['downloadUrl'].removeprefix(PREFIX+'/')
        self.json_request('release',{'jobId':first['jobId']})
        with self.assertRaises(HTTPError) as error: self.request(path,auth=False)
        self.assertEqual(error.exception.code,404)
        self.assertEqual(self.json_request('jobs?job='+second['jobId'])['state'],'ready')

    def test_download_rejects_external_origin_even_with_valid_ticket(self):
        state=self.job([('first.bmp',b'x')])
        path=state['result']['downloadUrl'].removeprefix(PREFIX+'/')
        with self.assertRaises(HTTPError) as error:
            self.request(path,auth=False,headers={'Origin':'https://evil.invalid'})
        self.assertEqual(error.exception.code,403)

    def test_empty_inspection_is_idle_and_never_creates_a_job(self):
        state=self.json_request('inspect',{'files':[]})
        self.assertEqual(state['compatibility'],'native_decision')
        self.assertEqual(self.guard.storage_stats()['jobs'],0)
        self.assertEqual(self.guard.adapter.calls,[])

    def test_bad_json_options_and_ambiguous_formats_are_explicit_errors(self):
        values=[(b'{',{'Content-Type':'application/json'},'REQUEST_JSON'),
                ({'files':[{'id':'f','name':'a.bmp','size':1}],'options':{'Quality':1}},None,'OPTIONS_NOT_CONNECTED'),
                ({'files':[{'id':'f','name':'a.bmp','size':1}],'formats':['pdf'],'formatId':'png'},None,'OUTPUT_FORMAT')]
        for body,headers,code in values:
            with self.subTest(code=code),self.assertRaises(HTTPError) as error:
                self.request('jobs',body,headers=headers)
            self.assertEqual(json.loads(error.exception.read())['code'],code)
        self.assertEqual(self.guard.storage_stats()['jobs'],0)

    def test_wrong_input_length_never_reads_the_document_body(self):
        identity=self.json_request('jobs',{'files':[{'id':'f','name':'a.bmp','size':100}]})['jobId']
        with self.assertRaises(HTTPError) as error:
            self.request('input?job='+identity+'&file=f',b'xx',
                         headers={'Content-Type':'application/octet-stream'})
        self.assertEqual(json.loads(error.exception.read())['code'],'UPLOAD_LENGTH')

    def test_cancel_unblocks_stalled_real_upload_socket(self):
        identity=self.json_request('jobs',{'files':[{'id':'f','name':'a.bmp','size':100}]})['jobId']
        client=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        client.putrequest('POST',PREFIX+'/input?job='+identity+'&file=f')
        client.putheader('X-AIO-Token',self.token)
        client.putheader('Content-Type','application/octet-stream')
        client.putheader('Content-Length','100')
        client.endheaders(b'xx')
        try:
            deadline=time.monotonic()+2
            while not self.guard._jobs[identity].io and time.monotonic()<deadline: time.sleep(.005)
            self.assertEqual(self.guard._jobs[identity].io,1)
            started=time.monotonic()
            self.json_request('cancel',{'jobId':identity})
            response=client.getresponse()
            self.assertEqual(json.loads(response.read())['code'],'CANCELLED')
            self.assertLess(time.monotonic()-started,1)
        finally:
            client.close()

    def test_close_unblocks_stalled_json_body_before_waiting_for_guard(self):
        client=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        client.putrequest('POST',PREFIX+'/jobs')
        client.putheader('X-AIO-Token',self.token)
        client.putheader('Content-Type','application/json')
        client.putheader('Content-Length','100')
        client.endheaders(b'{')
        try:
            deadline=time.monotonic()+2
            while not self.server.api._connections and time.monotonic()<deadline: time.sleep(.005)
            self.assertTrue(self.server.api._connections)
            started=time.monotonic()
            self.server.api.close()
            self.guard.close()
            self.assertLess(time.monotonic()-started,1)
        finally:
            client.close()

    def test_duplicate_content_length_is_not_accepted(self):
        client=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        client.putrequest('POST',PREFIX+'/jobs')
        client.putheader('X-AIO-Token',self.token)
        client.putheader('Content-Type','application/json')
        client.putheader('Content-Length','2')
        client.putheader('Content-Length','2')
        client.endheaders(b'{}')
        try:
            response=client.getresponse()
            self.assertEqual(json.loads(response.read())['code'],'REQUEST_SIZE')
        finally:
            client.close()

    def test_wrong_save_ticket_type_is_structured_error(self):
        state=self.job([('first.bmp',b'x')])
        with self.assertRaises(HTTPError) as error:
            self.request('saved',{'jobId':state['jobId'],'ticket':[]})
        self.assertEqual(json.loads(error.exception.read())['code'],'RESULT_NOT_FOUND')

    def test_skip_one_file_over_http_continues_other_files(self):
        identity=self.json_request('jobs',{'files':[
            {'id':'hold','name':'hold.docx','size':1},
            {'id':'next','name':'next.bmp','size':1}]})['jobId']
        for key in ('hold','next'):
            self.request('input?job='+identity+'&file='+key,b'x',
                         headers={'Content-Type':'application/octet-stream'}).close()
        self.json_request('start',{'jobId':identity})
        self.assertTrue(self.guard.adapter.waiting.wait(2))
        self.json_request('cancel-file',{'jobId':identity,'fileId':'hold'})
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            state=self.json_request('jobs?job='+identity)
            if state['finished']:break
            time.sleep(.005)
        self.assertEqual(state['state'],'partial')
        self.assertEqual(state['files'][1]['state'],'ready')


if __name__=='__main__':
    unittest.main()
