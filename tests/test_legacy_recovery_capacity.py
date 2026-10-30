"""A06: current-process discovery and bounded ownership; native boundary stubbed."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from app_backend.converter_transport import ConverterTransport
from app_backend.libreoffice_engine import LibreOfficeError


def convert_fixture(source, target, workspace, **kwargs):
    output = workspace / 'output'
    output.mkdir(parents=True)
    path = output / ('Результат.' + target)
    path.write_bytes(b'%PDF-1.7\nunchanged-fixture\n')
    return [path]


def retain(transport, identity, *, finish=True):
    request = transport.begin(identity, object())
    with patch('app_backend.converter_transport.convert_path', side_effect=convert_fixture):
        outcome = transport.execute(request, [{'path':Path('source.txt'), 'warnings':[]}], ['file'], ['pdf'])
    if finish:
        transport.finish(request, retained=True)
    return request, outcome['result']['ticket']


class LegacyOwnershipCapacity(unittest.TestCase):
    def setUp(self):
        self.transport = ConverterTransport(max_owned_roots=2)
        self.addCleanup(self.transport.close)

    def full(self):
        with self.assertRaises(LibreOfficeError) as error:
            self.transport.begin('f' * 32, object())
        self.assertEqual(error.exception.code, 'LEGACY_STORAGE_LIMIT')

    def test_published_active_owner_counts_once_and_unsaved_is_not_evicted(self):
        request, ticket = retain(self.transport, 'a' * 32, finish=False)
        self.assertEqual(self.transport.storage_stats()['ownedRoots'], 1)
        other = self.transport.begin('b' * 32, object())
        try:
            self.full()
            self.assertEqual(self.transport.recover(request.identity)['state'], 'ready')
            with self.transport.lease(ticket) as result:
                self.assertEqual(result.path.read_bytes(), b'%PDF-1.7\nunchanged-fixture\n')
        finally:
            self.transport.finish(other)
            self.transport.finish(request, retained=True)

    def test_released_but_leased_result_holds_capacity_until_reader_exits(self):
        _, first = retain(self.transport, 'a' * 32)
        _, second = retain(self.transport, 'b' * 32)
        with self.transport.lease(first) as result:
            root = Path(result.temporary.name)
            self.assertFalse(self.transport.release(first))
            self.full()
            self.assertTrue(root.exists())
            self.assertEqual(self.transport.inventory()['total'], 1)
            self.assertEqual(self.transport.recover('a' * 32)['state'], 'not_found')
        self.assertFalse(root.exists())
        next_request = self.transport.begin('c' * 32, object())
        self.transport.finish(next_request)
        self.assertTrue(self.transport.release(second))
        self.assertEqual(self.transport.storage_stats()['ownedRoots'], 0)

    def test_failed_result_cleanup_stays_counted_and_can_be_retried(self):
        request, ticket = retain(self.transport, 'a' * 32)
        other, _ = retain(self.transport, 'b' * 32)
        with patch.object(request.temporary, 'cleanup', side_effect=PermissionError('sharing violation')), \
                patch.object(self.transport, '_schedule_cleanup'):
            self.assertFalse(self.transport.release(ticket))
            self.full()
            self.assertTrue(request.root.exists())
            self.assertTrue(other.root.exists())
            self.assertEqual(self.transport.storage_stats()['cleanupPending'], 1)
        self.transport.retry_cleanup()
        self.assertFalse(request.root.exists())
        self.assertEqual(self.transport.storage_stats()['ownedRoots'], 1)

    def test_failed_unpublished_cleanup_remains_owned(self):
        request = self.transport.begin('a' * 32, object())
        with patch.object(request.temporary, 'cleanup', side_effect=PermissionError('busy')), \
                patch.object(self.transport, '_schedule_cleanup'):
            self.transport.finish(request)
            self.assertEqual(self.transport.storage_stats()['ownedRoots'], 1)
            other = self.transport.begin('b' * 32, object())
            try:
                self.full()
            finally:
                self.transport.finish(other)
        self.transport.retry_cleanup()
        self.assertEqual(self.transport.storage_stats()['ownedRoots'], 0)
        self.assertFalse(request.root.exists())

    def test_inflight_pending_cleanup_is_not_duplicated_or_uncounted(self):
        request = self.transport.begin('a' * 32, object())
        other, _ = retain(self.transport, 'b' * 32)
        with patch.object(request.temporary, 'cleanup', side_effect=PermissionError('busy')), \
                patch.object(self.transport, '_schedule_cleanup'):
            self.transport.finish(request)
        entered, unblock = threading.Event(), threading.Event()
        original = request.temporary.cleanup
        calls, failures = [], []
        def slow():
            calls.append(True)
            entered.set()
            if not unblock.wait(5):
                raise AssertionError('cleanup test was not released')
            original()
        def retry():
            try:
                self.transport.retry_cleanup()
            except BaseException as error:
                failures.append(error)
        with patch.object(request.temporary, 'cleanup', side_effect=slow):
            worker = threading.Thread(target=retry, daemon=True)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                self.full()
                self.transport.retry_cleanup()
                self.assertEqual(len(calls), 1)
                self.assertEqual(self.transport.storage_stats()['ownedRoots'], 2)
                self.assertEqual(self.transport.inventory()['items'][0]['requestId'], other.identity)
            finally:
                unblock.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertFalse(failures)
        self.assertFalse(request.root.exists())

    def test_twenty_release_cycles_leave_no_owned_directories(self):
        roots = []
        for number in range(20):
            request, ticket = retain(self.transport, f'{number:032x}')
            roots.append(request.root)
            self.assertTrue(self.transport.release(ticket))
        self.assertEqual(self.transport.storage_stats()['ownedRoots'], 0)
        self.assertFalse(any(path.exists() for path in roots))
        self.assertFalse(self.transport.results_by_request)

    def test_inventory_is_paginated_metadata_not_reports_or_disk_scan(self):
        first, _ = retain(self.transport, 'a' * 32)
        retain(self.transport, 'b' * 32)
        with patch.object(Path, 'stat', side_effect=AssertionError('no disk scans')):
            page = self.transport.inventory(limit=1)
            tail = self.transport.inventory(offset=1, limit=1)
            empty = self.transport.inventory(offset=20)
        self.assertTrue(page['more'])
        self.assertFalse(tail['more'])
        self.assertEqual(page['total'], 2)
        self.assertEqual(page['items'][0]['requestId'], first.identity)
        self.assertNotIn('targetReport', page['items'][0])
        self.assertNotIn('ticket', page['items'][0])
        self.assertNotIn(str(first.root), json.dumps(page))
        self.assertEqual(empty['items'], [])
        self.assertFalse(page['restartRecovery'])
        page['items'][0]['formatIds'].append('fake')
        self.assertEqual(self.transport.inventory()['items'][0]['formatIds'], ['pdf'])

    def test_invalid_limits_and_inventory_arguments_are_rejected(self):
        for value in (0, -1, True, 1.5, 257):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ConverterTransport(max_owned_roots=value)
        for args in ({'limit':0}, {'limit':51}, {'offset':-1}, {'offset':True}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.transport.inventory(**args)


class LegacyRecoveryHttp(unittest.TestCase):
    def setUp(self):
        from app_backend.server import start_server
        self.temp = tempfile.TemporaryDirectory()
        self.server, self.token = start_server(guard_recovery_parent=Path(self.temp.name)/'guard')
        self.server.converter.close()
        self.server.converter = ConverterTransport(max_owned_roots=2)
        self.server.RequestHandlerClass.protocol_version = 'HTTP/1.1'
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval':.01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.temp.cleanup()

    def get(self, path, *, token=True, origin=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        headers = {'X-AIO-Token':self.token} if token else {}
        if origin:
            headers['Origin'] = origin
        try:
            connection.request('GET', path, headers=headers)
            response = connection.getresponse()
            data = response.read()
            return response.status, data, dict(response.getheaders())
        finally:
            connection.close()

    def test_result_is_discoverable_after_reply_and_generated_id_are_discarded(self):
        wire = (b'--b\r\nContent-Disposition: form-data; name="ids"\r\n\r\n["one"]\r\n'
                b'--b\r\nContent-Disposition: form-data; name="formats"\r\n\r\n["pdf"]\r\n'
                b'--b\r\nContent-Disposition: form-data; name="files"; filename="source.txt"\r\n\r\noriginal\r\n--b--\r\n')
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            with patch('app_backend.converter_transport.convert_path', side_effect=convert_fixture):
                # No request ID is supplied. Discard the successful response,
                # exactly the information unavailable after a lost reply.
                connection.request('POST', '/api/converter/convert', body=wire,
                    headers={'X-AIO-Token':self.token, 'Content-Type':'multipart/form-data; boundary=b'})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                response.read()
        finally:
            connection.close()
        status, data, headers = self.get('/api/converter/recover')
        self.assertEqual(status, 200)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        inventory = json.loads(data)
        self.assertEqual(inventory['total'], 1)
        self.assertFalse(inventory['restartRecovery'])
        status, data, _ = self.get(inventory['items'][0]['recoverUrl'])
        self.assertEqual(status, 200)
        result = json.loads(data)['result']
        status, content, _ = self.get(result['downloadUrl'])
        self.assertEqual(status, 200)
        self.assertEqual(content, b'%PDF-1.7\nunchanged-fixture\n')
        self.assertTrue(self.server.converter.release(result['ticket']))
        self.assertEqual(json.loads(self.get('/api/converter/recover')[1])['total'], 0)

    def test_discovery_requires_token_and_original_loopback_origin(self):
        retain(self.server.converter, 'a' * 32)
        self.assertEqual(self.get('/api/converter/recover', token=False)[0], 403)
        self.assertEqual(self.get('/api/converter/recover', origin='https://example.invalid')[0], 403)
        status, data, _ = self.get('/api/converter/recover?limit=1&offset=0')
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(data)['items']), 1)

    def test_invalid_pagination_does_not_fall_back_to_discovery(self):
        for query in ('request=', 'request=x', 'request='+'a'*32+'&limit=1',
                      'limit=0', 'limit=51', 'offset=-1', 'offset=1.0',
                      'limit=1&limit=2', 'offset=0&offset=1', 'unknown=1',
                      'offset='+'9'*100):
            with self.subTest(query=query):
                self.assertEqual(self.get('/api/converter/recover?'+query)[0], 400)

    def test_capacity_rejection_does_not_read_body_and_releases_engine_gate(self):
        _, ticket = retain(self.server.converter, 'a' * 32)
        retain(self.server.converter, 'b' * 32)
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        try:
            with patch('app_backend.server.MultipartUpload', side_effect=AssertionError('no upload at capacity')):
                connection.putrequest('POST', '/api/converter/convert?request='+'c'*32)
                connection.putheader('X-AIO-Token', self.token)
                connection.putheader('Content-Length', '100000')
                connection.endheaders()
                response = connection.getresponse()
                self.assertEqual(response.status, 429)
                self.assertEqual(json.loads(response.read())['code'], 'LEGACY_STORAGE_LIMIT')
                self.assertTrue(response.will_close)
            self.assertTrue(self.server.converter_gate.acquire(blocking=False))
            self.server.converter_gate.release()
            self.assertTrue(self.server.converter.release(ticket))
            request = self.server.converter.begin('c' * 32, object())
            self.server.converter.finish(request)
        finally:
            connection.close()
