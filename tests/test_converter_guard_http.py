"""Real loopback HTTP, disk streaming and authorization; fake LO worker."""
import io
import http.client
import json
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import zipfile

from app_backend.converter_guard import ConverterGuard
from app_backend.server import start_server
from test_converter_guard import FakeAdapter, PDF


class GuardHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.guard = ConverterGuard(adapter=FakeAdapter(), spool_parent=self.temp.name)
        self.server, self.token = start_server(converter_guard=self.guard)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.assertEqual(self.guard.storage_stats()['jobs'], 0)
        self.temp.cleanup()

    def request(self, path, body=None, *, auth=True, headers=None):
        hdr = {'X-AIO-Token': self.token} if auth else {}
        hdr.update(headers or {})
        if isinstance(body, dict):
            body = json.dumps(body).encode()
            hdr['Content-Type'] = 'application/json'
        return urlopen(Request(self.base + '/api/converter/' + path, data=body, headers=hdr), timeout=5)

    def job(self, files):
        meta = [{'id': str(i), 'name': name, 'size': len(data)} for i, (name, data) in enumerate(files)]
        job = json.loads(self.request('jobs', {'files': meta}).read())['jobId']
        for i, (_, data) in enumerate(files):
            self.request('input?job=' + job + '&file=' + str(i), data,
                         headers={'Content-Type': 'application/octet-stream'}).close()
        self.request('start', {'jobId': job}).close()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = json.loads(self.request('jobs?job=' + job).read())
            if result['finished']: return result
            time.sleep(.01)
        raise AssertionError('HTTP job did not finish')

    def test_inspect_requires_token_and_never_uploads_document_content(self):
        body = {'files': [{'id': 'f', 'name': 'a.docx', 'size': 5}]}
        with self.assertRaises(HTTPError) as error: self.request('inspect', body, auth=False)
        self.assertEqual(error.exception.code, 403)
        result = json.loads(self.request('inspect', body).read())
        self.assertEqual(result['formats'][0]['id'], 'pdf')
        self.assertEqual(self.guard.storage_stats()['jobs'], 0)

    def test_convert_download_cancel_save_retry_release_full_cycle(self):
        result = self.job([('Счёт.docx', b'good')])
        url = result['result']['downloadUrl'].removeprefix('/api/converter/')
        # GET download initiation/cancel never deletes the retained result.
        first = self.request(url, auth=False)
        self.assertIn("filename*=UTF-8''", first.headers['Content-Disposition'])
        first.close()
        with self.request(url, auth=False) as saved:
            self.assertEqual(saved.read(), PDF)
        self.assertTrue(json.loads(self.request('jobs?job=' + result['jobId']).read())['finished'])
        self.request('release', {'jobId': result['jobId']}).close()
        with self.assertRaises(HTTPError) as error: self.request(url, auth=False)
        self.assertEqual(error.exception.code, 404)
        repeat = self.job([('Счёт.docx', b'good')])
        self.assertEqual(repeat['state'], 'ready')

    def test_partial_batch_returns_only_successful_documents(self):
        state = self.job([('bad.docx', b'bad!'), ('ok.odt', b'good'), ('ok.xlsx', b'good')])
        self.assertEqual(state['state'], 'partial')
        link = state['result']['downloadUrl'].removeprefix('/api/converter/')
        with self.request(link, auth=False) as response:
            with zipfile.ZipFile(io.BytesIO(response.read())) as archive:
                self.assertEqual(len(archive.namelist()), 2)
        self.assertEqual(state['files'][0]['code'], 'ENGINE_OUTPUT_MISSING')

    def test_external_origin_and_rebinding_host_are_rejected(self):
        for headers in ({'Origin': 'https://other.invalid'}, {'Host': 'other.invalid'}):
            with self.subTest(headers=headers), self.assertRaises(HTTPError) as error:
                self.request('status', headers=headers)
            self.assertEqual(error.exception.code, 403)

    def test_download_ticket_cannot_resolve_a_path_or_another_session(self):
        for ticket in ('../app_backend/server.py', 'a'*43):
            with self.subTest(ticket=ticket), self.assertRaises(HTTPError) as error:
                self.request('result?ticket=' + ticket, auth=False)
            self.assertEqual(error.exception.code, 404)

    def test_unconnected_output_is_not_silently_changed_to_pdf(self):
        with self.assertRaises(HTTPError) as error:
            self.request('jobs', {'files': [{'id': 'f', 'name': 'a.docx', 'size': 5}], 'formatId': 'docx'})
        self.assertEqual(json.loads(error.exception.read())['code'], 'OUTPUT_NOT_CONNECTED')
        self.assertEqual(self.guard.storage_stats()['jobs'], 0)

    def test_wrong_upload_length_is_rejected_before_file_read(self):
        job = json.loads(self.request('jobs', {'files': [{'id': 'f', 'name': 'a.docx', 'size': 5}]}).read())['jobId']
        with self.assertRaises(HTTPError) as error:
            self.request('input?job=' + job + '&file=f', b'xx', headers={'Content-Type': 'application/octet-stream'})
        self.assertEqual(json.loads(error.exception.read())['code'], 'UPLOAD_LENGTH')

    def test_bad_json_and_unknown_job_produce_structured_errors(self):
        with self.assertRaises(HTTPError) as error:
            self.request('jobs', b'{', headers={'Content-Type': 'application/json'})
        self.assertEqual(json.loads(error.exception.read())['code'], 'REQUEST_JSON')
        with self.assertRaises(HTTPError) as error: self.request('cancel', {'jobId': 'x'})
        self.assertEqual(json.loads(error.exception.read())['code'], 'JOB_ID')

    def test_server_close_unblocks_incomplete_upload_and_cleans_ownership(self):
        job = json.loads(self.request('jobs', {'files': [{'id':'f','name':'a.docx','size':100}]}).read())['jobId']
        client = http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=2)
        client.putrequest('POST','/api/converter/input?job='+job+'&file=f')
        client.putheader('X-AIO-Token',self.token)
        client.putheader('Content-Type','application/octet-stream')
        client.putheader('Content-Length','100')
        client.endheaders(b'xx')
        try:
            deadline=time.monotonic()+2
            while not self.guard._jobs[job].io and time.monotonic()<deadline: time.sleep(.01)
            self.assertEqual(self.guard._jobs[job].io,1)
            started=time.monotonic()
            self.server.shutdown()
            self.server.server_close()
            self.assertLess(time.monotonic()-started,2)
            self.assertEqual(self.guard.storage_stats()['jobs'],0)
        finally:
            client.close()


if __name__ == '__main__':
    unittest.main()
