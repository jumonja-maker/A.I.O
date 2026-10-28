"""Actual loopback QA transport must be local, bounded and diagnosable."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
import threading
import unittest
from unittest.mock import patch

from tools.qa.guard_v19_comparison_acceptance import LoopbackClient


class ComparisonQATransport(unittest.TestCase):
    def test_machine_proxy_is_bypassed_for_real_loopback_qa(self):
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append((self.path, self.headers.get('X-AIO-Token'),
                    json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                payload = b'{"ok":true}'
                self.send_response(200)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers(); self.wfile.write(payload)
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        evidence = {}
        try:
            with patch.dict(os.environ, {'HTTP_PROXY': 'http://127.0.0.1:1',
                    'http_proxy': 'http://127.0.0.1:1', 'NO_PROXY': '', 'no_proxy': ''}):
                client = LoopbackClient('http://127.0.0.1:' + str(server.server_port), 'private-token', evidence)
                result = client.call('prepare', {'files': []})
            self.assertEqual(result, {'ok': True})
            self.assertEqual(seen, [('/api/converter-guard/prepare', 'private-token', {'files': []})])
            self.assertEqual(evidence['lastRequest']['httpStatus'], 200)
            self.assertNotIn('private-token', json.dumps(evidence))
        finally:
            server.shutdown(); server.server_close(); thread.join(2)

    def test_preflight_budget_opt_in_does_not_extend_normal_status_requests(self):
        evidence = {}
        client = LoopbackClient('http://127.0.0.1:1', 'token', evidence)
        def response(*args, **kwargs):
            stream = io.BytesIO(b'{"ok":true}')
            stream.status = 200
            return stream
        with patch.object(client.opener, 'open', side_effect=response) as requests:
            client.call('preflight', {'jobId': 'a' * 32}, timeout=180)
            client.call('jobs?job=' + 'a' * 32)
        self.assertEqual([call.kwargs['timeout'] for call in requests.call_args_list], [180, 15])
        self.assertEqual([row['endpoint'] for row in evidence['requestTrace']], ['preflight', 'jobs'])
        self.assertNotIn('a' * 32, json.dumps(evidence))

    def test_timeout_retains_exact_failing_stage_without_hiding_failure(self):
        evidence = {}
        client = LoopbackClient('http://127.0.0.1:1', 'token', evidence)
        with patch.object(client.opener, 'open', side_effect=TimeoutError('controlled timeout')):
            with self.assertRaisesRegex(TimeoutError, 'controlled timeout'):
                client.call('input?job=private-identity&file=private-filename', b'secret contents', binary=True)
        row = evidence['lastRequest']
        self.assertEqual(evidence['stage'], 'request:input')
        self.assertEqual((row['endpoint'], row['method'], row['state']), ('input', 'POST', 'failed'))
        self.assertEqual(row['timeoutSeconds'], 15)
        self.assertEqual(row['error'], 'TimeoutError')
        self.assertGreaterEqual(row['elapsedSeconds'], 0)
        self.assertNotIn('secret contents', json.dumps(evidence))
        self.assertNotIn('private-filename', json.dumps(evidence))


if __name__ == '__main__':
    unittest.main()
