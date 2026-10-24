"""Exercise negotiated output inventories through actual authenticated HTTP."""
import io
import json
import tempfile
import unittest
from urllib.request import Request, urlopen

from app_backend.converter_guard import ConverterGuard
from test_converter_guard_http_v02 import start_test_server
from test_converter_guard_v02 import FakeAdapter, wait_finished


class NegotiatedHTTPDelta(unittest.TestCase):
    def test_delta_payload_legacy_etag_and_full_resync_preserve_saved_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            guard = ConverterGuard(adapter=FakeAdapter(), spool_parent=directory)
            server, thread, origin, token = start_test_server(guard)
            try:
                identity = guard.create([
                    {'id': str(i), 'name': str(i)+'.docx', 'size': 1}
                    for i in range(3)], ['pdf'])['jobId']
                for i in range(3):
                    guard.upload(identity, str(i), io.BytesIO(b'x'), 1)
                guard.start(identity, archive_mode='on_demand')
                wait_finished(guard, identity)

                def get(query='', etag=None):
                    headers = {'X-AIO-Token': token}
                    if etag:
                        headers['If-None-Match'] = etag
                    with urlopen(Request(origin+'/api/converter-guard/jobs?job='+identity+query,
                                         headers=headers), timeout=5) as response:
                        return json.loads(response.read()), response.headers['ETag']

                before, etag = get()
                self.assertEqual(len(before['result']['outputs']), 3)
                with guard._lock:
                    job = guard._jobs[identity]
                    row = job.files[1]
                    row.targets[0].artifacts[0].saved = True
                    row.targets[0].state = 'saved'
                    guard._record(job, 'native_stage', file_id=row.id, target='pdf')
                change, next_etag = get('&delta=1&resultDelta=1', etag)
                self.assertTrue(change['resultOutputsDelta'])
                self.assertEqual(change['baseRevision'], before['revision'])
                self.assertEqual(len(change['result']['outputs']), 1)
                self.assertTrue(change['result']['outputs'][0]['saved'])
                legacy, legacy_etag = get('&delta=1', etag)
                full, full_etag = get('&resultDelta=1')
                self.assertEqual(next_etag, legacy_etag)
                self.assertEqual(next_etag, full_etag)
                self.assertEqual(legacy['result']['outputs'], full['result']['outputs'])
                merged = {output['ticket']: output for output in before['result']['outputs']}
                merged.update({output['ticket']: output for output in change['result']['outputs']})
                self.assertEqual(list(merged.values()), full['result']['outputs'])
                self.assertNotIn('resultOutputsDelta', full)
            finally:
                server.api.close()
                guard.close()
                server.shutdown()
                server.server_close()
                thread.join(3)
