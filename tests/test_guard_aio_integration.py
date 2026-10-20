"""Integration contracts for opt-in A.I.O + Converter Guard branch.

No real conversion, no installer, no Windows runner. Real native smoke is a
separate NIKITA acceptance gate after source contracts succeed.
"""
from http.client import HTTPConnection
import json
from pathlib import Path
import socket
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app_backend.downloads import download_filter
from app_backend.server import start_server

ROOT = Path(__file__).resolve().parents[1]


class AioGuardIntegrationContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.token = start_server()
        cls.worker = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.worker.start()
        cls.origin = 'http://127.0.0.1:' + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join(5)

    def request(self, path, body=None, *, token=True, origin=None):
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['X-AIO-Token'] = self.token
        if origin is not None:
            headers['Origin'] = origin
        data = json.dumps(body).encode() if body is not None else None
        return urlopen(Request(self.origin + path, data=data, headers=headers),
                       timeout=10)

    def test_status_is_schema_three_and_guard_is_available_to_local_host(self):
        with self.request('/api/converter-guard/status') as response:
            status = json.load(response)
        self.assertEqual(status['schemaVersion'], 3)
        self.assertEqual(status['routeFiltering'],
                         'native_registry_after_model_confirmation')
        self.assertFalse(status['qualityCompared'])
        self.assertIsInstance(status['storage'], dict)

    def test_guard_requires_token_and_loopback_origin(self):
        with self.assertRaises(HTTPError) as ctx:
            self.request('/api/converter-guard/status', token=False)
        self.assertEqual(ctx.exception.code, 403)
        with self.assertRaises(HTTPError) as ctx:
            self.request('/api/converter-guard/status',
                         origin='https://outside.invalid')
        self.assertEqual(ctx.exception.code, 403)

    def test_fragmented_metadata_reads_complete_body(self):
        files = [{'id':'item'+str(i), 'name':'sample.bmp', 'size':1}
                 for i in range(64)]
        body = json.dumps({'files':files}).encode()
        port = self.server.server_port
        request = (
            'POST /api/converter-guard/inspect HTTP/1.0\r\n'
            'Host: 127.0.0.1:' + str(port) + '\r\n'
            'X-AIO-Token: ' + self.token + '\r\n'
            'Content-Type: application/json\r\n'
            'Content-Length: ' + str(len(body)) + '\r\n\r\n'
        ).encode()
        with socket.create_connection(('127.0.0.1', port), timeout=5) as conn:
            conn.settimeout(5)
            conn.sendall(request + body[:9])
            time.sleep(.05)
            conn.sendall(body[9:])
            raw = bytearray()
            while data := conn.recv(65536):
                raw.extend(data)
        self.assertTrue(raw.startswith(b'HTTP/1.0 200'), raw[:180])
        result = json.loads(bytes(raw).split(b'\r\n\r\n', 1)[1])
        self.assertEqual(len(result['files']), len(files))
        self.assertFalse(result['qualityCompared'])

    def test_jobs_use_explicit_per_file_plans_and_release(self):
        data = {'files':[{'id':'bmp1','name':'photo.bmp','size':3},
                         {'id':'xlsx1','name':'sheet.xlsx','size':4}],
                'plan':[{'fileId':'bmp1','formats':['pdf','png']},
                        {'fileId':'xlsx1','formats':['pdf','dbf']}]}
        with self.request('/api/converter-guard/jobs', data) as res:
            job = json.load(res)
        identity = job['jobId']
        self.assertEqual(job['schemaVersion'],3)
        self.assertEqual(job['total'],4)
        try:
            with self.request('/api/converter-guard/jobs?job=' + identity) as res:
                status = json.load(res)
            self.assertEqual(status['schemaVersion'],3)
            self.assertEqual([len(f['targets']) for f in status['files']],[2,2])
        finally:
            with self.request('/api/converter-guard/release', {'jobId':identity}) as res:
                self.assertTrue(json.load(res)['ok'])

    def test_native_save_accepts_guard_tickets_only_from_loopback(self):
        url=self.origin + '/api/converter-guard/result?ticket=' + 'a'*43 + '&aioSave=2'
        self.assertEqual(download_filter(url,'AIO_Results.zip',self.server.server_port),
                         'ZIP (*.zip)')
        self.assertIsNone(download_filter(
            url.replace('/api/converter-guard/result','/api/unsafe/result'),
            'AIO_Results.zip',self.server.server_port))

    def test_frontend_integrates_v3_and_does_not_replace_excel(self):
        js=(ROOT/'src/converter-bridge.js').read_text(encoding='utf-8')
        ui=(ROOT/'addons/file-converter/aio-converter-issues.js').read_text(encoding='utf-8')
        server=(ROOT/'app_backend/server.py').read_text(encoding='utf-8')
        self.assertIn("prefix = '/api/converter-guard'",js)
        self.assertIn("'skipped'",js)
        self.assertIn("api('/preflight'",js)
        self.assertIn('onlyVerified',js)
        self.assertIn("'/saved'",js)
        self.assertIn("target.state === 'skipped'",ui)
        self.assertIn('self.server.guard_api.get(self, path)',server)
        self.assertIn('self.server.guard_api.post(self, path)',server)
        self.assertIn('/api/merge',server)


if __name__ == '__main__':
    unittest.main()
