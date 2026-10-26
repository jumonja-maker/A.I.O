"""HTTP and streaming regressions. Native execution is isolated behind a test adapter."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zipfile import ZipFile

from app_backend import libreoffice_engine as lo
from app_backend.converter_upload_stream import MultipartUpload, CHUNK
from app_backend.converter_guard import GuardError
from app_backend.server import start_server


def multipart(files, formats=('pdf',), ids=None, boundary='aio-stream-test-boundary'):
    stream = tempfile.TemporaryFile()
    ids = ids or ['f' + str(i) for i in range(len(files))]
    for key, value in [('ids', json.dumps(ids)), ('formats', json.dumps(list(formats)))]:
        stream.write(('--' + boundary + '\r\nContent-Disposition: form-data; name="' +
                      key + '"\r\n\r\n' + value + '\r\n').encode())
    for name, source in files:
        stream.write(('--' + boundary +
            '\r\nContent-Disposition: form-data; name="files"; filename="' + name +
            '"\r\nContent-Type: application/octet-stream\r\n\r\n').encode())
        if isinstance(source, bytes):
            stream.write(source)
        else:
            with Path(source).open('rb') as incoming:
                while chunk := incoming.read(CHUNK):
                    stream.write(chunk)
        stream.write(b'\r\n')
    stream.write(('--' + boundary + '--\r\n').encode())
    size = stream.tell()
    stream.seek(0)
    return stream, {'Content-Type': 'multipart/form-data; boundary=' + boundary,
                    'Content-Length': str(size)}


class StreamingParserTests(unittest.TestCase):
    def test_boundary_like_bytes_and_chunk_splits_preserve_original_content(self):
        boundary = 'aio-stream-test-boundary'
        content = (b'x' * (CHUNK - 8) + b'\r\n--' + boundary.encode() +
                   b'XXnot-a-delimiter' + b'\x00\xff' * CHUNK)
        wire, headers = multipart([('Картинка.bmp', content)], boundary=boundary)
        class SmallReader:
            def read(self, size):
                self.max_requested = max(getattr(self, 'max_requested', 0), size)
                return wire.read(min(size, 17))
        reader = SmallReader()
        try:
            with tempfile.TemporaryDirectory() as directory:
                files, fields = MultipartUpload(reader, int(headers['Content-Length']),
                    headers['Content-Type'], threading.Event()).read(Path(directory))
                self.assertEqual(files[0]['path'].read_bytes(), content)
                self.assertEqual(files[0]['name'], 'Картинка.bmp')
                self.assertEqual(json.loads(fields['ids']), ['f0'])
                self.assertLessEqual(reader.max_requested, CHUNK)
        finally:
            wire.close()

    def test_truncated_body_is_not_a_successful_upload(self):
        wire, headers = multipart([('file.bmp', b'BMtest')])
        payload = wire.read()[:-20]
        wire.close()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(lo.LibreOfficeError) as error:
                MultipartUpload(io.BytesIO(payload), int(headers['Content-Length']),
                    headers['Content-Type'], threading.Event()).read(Path(directory))
        self.assertEqual(error.exception.code, 'UPLOAD_INCOMPLETE')


class NativeTransportHTTPTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.calls = []
        self.running = threading.Event()
        self.output_bytes = None
        self.engine_patch = patch.object(lo, 'engine_binary', return_value=Path(__file__))
        self.engine_patch.start()
        self.native_patch = patch('app_backend.converter_transport.convert_path',
                                  side_effect=self.fake_native)
        self.native_patch.start()
        self.server, self.token = start_server(guard_recovery_parent=Path(self.work.name)/'guard-results')
        # Track requests before their daemon workers start, including a client
        # which dies in the middle of upload/preflight. A bounded fixture drain
        # is independent of ThreadingMixIn's unbounded/non-daemon join policy.
        self.http_drained = threading.Condition()
        self.http_requests = set()
        dispatch = self.server.process_request
        handle = self.server.process_request_thread
        def forget(request):
            with self.http_drained:
                self.http_requests.discard(request)
                self.http_drained.notify_all()
        def tracked_dispatch(request, address):
            with self.http_drained:
                self.http_requests.add(request)
            try:
                dispatch(request, address)
            except BaseException:
                forget(request)
                raise
        def tracked_handle(request, address):
            try:
                handle(request, address)
            finally:
                forget(request)
        self.server.process_request = tracked_dispatch
        self.server.process_request_thread = tracked_handle
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        try:
            # Stop accepting transfers and cancel every fixture-owned job before
            # draining HTTP workers. Guard.close must run after those workers,
            # not concurrently with a late preflight finishing its ownership.
            self.server.guard_api.close()
            self.server.converter.close()
            with self.server.guard._lock:
                job_ids = list(self.server.guard._jobs)
            for job_id in job_ids:
                try:
                    self.server.guard.cancel(job_id)
                except GuardError as error:
                    self.assertEqual(error.code, 'JOB_NOT_FOUND')
            with self.http_drained:
                drained = self.http_drained.wait_for(lambda: not self.http_requests, timeout=15)
            self.assertTrue(drained, 'Fixture HTTP workers did not drain before Guard/spool cleanup')
        finally:
            self.server.server_close()
            self.thread.join(timeout=5)
            self.native_patch.stop()
            self.engine_patch.stop()
            self.work.cleanup()

    def fake_native(self, source, target, workspace, *, cancel, on_started):
        on_started()
        with source.open('rb') as stream:
            head = stream.read(8)
            stream.seek(0)
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        self.calls.append((source.name, target, source.stat().st_size, digest))
        if head.startswith(b'BROKEN'):
            raise lo.LibreOfficeError('Native import failed', 'ENGINE_OUTPUT_MISSING')
        if head.startswith(b'HOLD'):
            self.running.set()
            if cancel.wait(10):
                raise lo.LibreOfficeError('Cancelled native request', 'CANCELLED')
            raise AssertionError('Expected explicit cancellation')
        output = workspace / 'output'
        output.mkdir(parents=True)
        result = output / (source.stem + '.' + target)
        if self.output_bytes is not None:
            with result.open('wb') as stream:
                if self.output_bytes:
                    stream.seek(self.output_bytes - 1)
                    stream.write(b'x')
        else:
            result.write_bytes(b'%PDF-1.7\nnative-test-output\n%%EOF')
        return [result]

    def post(self, path, data, headers=None):
        return urlopen(Request(self.base + '/api/converter/' + path, data=data,
            headers={'X-AIO-Token': self.token, **(headers or {})}), timeout=20)

    def json(self, path, value):
        with self.post(path, json.dumps(value).encode(),
                       {'Content-Type': 'application/json'}) as response:
            return json.load(response)

    def convert(self, files, formats=('pdf',), request=None):
        wire, headers = multipart(files, formats)
        try:
            with self.post('convert' + ('?request=' + request if request else ''),
                           wire, headers) as response:
                return json.load(response)
        finally:
            wire.close()

    def test_inspect_is_metadata_only_and_does_not_execute_libreoffice(self):
        report = self.json('inspect', {'files': [
            {'id': 'a', 'name': 'image.bmp', 'size': 2 ** 40},
            {'id': 'b', 'name': 'unknown.custom', 'size': 0}]})
        self.assertEqual(self.calls, [])
        self.assertTrue(next(f for f in report['formats'] if f['id'] == 'png')['enabled'])
        self.assertEqual(len(self.server.converter.active), 0)

    def test_inspect_reads_fragmented_json_body_exactly(self):
        # A SocketIO reader can return a short body even with more bytes
        # promised by Content-Length. Simulate separated TCP packets to
        # protect metadata inspection and the unchanged Excel endpoint.
        files = [{'id': 'f' + str(i), 'name': 'image.bmp', 'size': i}
                 for i in range(120)]
        body = json.dumps({'files': files}).encode('utf-8')
        headers = ('POST /api/converter/inspect HTTP/1.0\r\n' +
            'Host: 127.0.0.1:' + str(self.server.server_port) + '\r\n' +
            'X-AIO-Token: ' + self.token + '\r\n' +
            'Content-Type: application/json\r\n' +
            'Content-Length: ' + str(len(body)) + '\r\n\r\n').encode()
        with socket.create_connection(('127.0.0.1', self.server.server_port)) as connection:
            connection.settimeout(5)
            connection.sendall(headers + body[:13])
            time.sleep(.1)
            connection.sendall(body[13:])
            data = bytearray()
            while chunk := connection.recv(64 * 1024):
                data.extend(chunk)
        self.assertTrue(data.startswith(b'HTTP/1.0 200'), data[:150])
        payload = json.loads(bytes(data).split(b'\r\n\r\n', 1)[1])
        self.assertEqual(len(payload['files']), len(files))

    def test_bmp_and_unknown_extension_are_delivered_unchanged(self):
        content = b'BM' + b'\x00\xff' * 100
        outcome = self.convert([('Картинка.bmp', content), ('unknown.custom', b'custom')])
        self.assertEqual(outcome['result']['targetReport']['succeeded'], 2)
        self.assertEqual(self.calls[0][0], 'Картинка.bmp')
        self.assertEqual(self.calls[0][3], hashlib.sha256(content).hexdigest())

    def test_files_above_20_mib_and_batch_above_50_mib_are_streamed(self):
        sources = []
        for i in range(2):
            path = Path(self.work.name) / (str(i) + '.bmp')
            with path.open('wb') as stream:
                for _ in range(26 * 16): stream.write(b'x' * CHUNK)
            sources.append((path.name, path))
        outcome = self.convert(sources)
        self.assertEqual(outcome['result']['targetReport']['succeeded'], 2)
        self.assertTrue(all(call[2] == 26 * 1024 * 1024 for call in self.calls))

    def test_more_than_100_inputs_and_more_than_12_targets_are_not_ui_policy(self):
        outcome = self.convert([('file' + str(i) + '.bmp', b'BM') for i in range(103)])
        self.assertEqual(outcome['result']['targetReport']['succeeded'], 103)
        self.json('release', {'ticket': outcome['result']['ticket']})
        outcome = self.convert([('file.bmp', b'BM')], formats=tuple('native' + str(i) for i in range(14)))
        self.assertEqual(outcome['result']['targetReport']['succeeded'], 14)

    def test_native_output_above_100_mib_is_retained_without_reading_it_into_bytes(self):
        self.output_bytes = 105 * 1024 * 1024
        outcome = self.convert([('file.bmp', b'BM')])
        self.assertEqual(outcome['result']['size'], self.output_bytes)
        self.assertEqual(len(self.server.converter.results), 1)

    def test_zero_byte_native_text_result_is_valid_transport_data(self):
        self.output_bytes = 0
        outcome = self.convert([('blank.odt', b'empty-native-document')], formats=('txt',))
        self.assertEqual(outcome['result']['size'], 0)
        self.assertEqual(outcome['result']['targetReport']['succeeded'], 1)
        with urlopen(self.base + outcome['result']['downloadUrl']) as response:
            self.assertEqual(response.read(), b'')

    def test_cleanup_failure_keeps_ownership_for_a_retry(self):
        request = self.server.converter.begin('c' * 32, None)
        cleanup = request.temporary.cleanup
        with patch.object(request.temporary, 'cleanup', side_effect=[PermissionError('busy'), None]):
            self.server.converter.finish(request)
            self.assertFalse(self.server.converter.active)
            self.assertEqual(len(self.server.converter.cleanup_pending), 1)
            self.server.converter.close()
            self.assertFalse(self.server.converter.cleanup_pending)
        cleanup()

    def test_first_failure_does_not_discard_remaining_outputs(self):
        outcome = self.convert([('broken.docx', b'BROKEN'), ('good.bmp', b'BM'), ('table.csv', b'a,b')])
        report = outcome['result']['targetReport']
        self.assertEqual((report['succeeded'], report['failed']), (2, 1))
        with urlopen(self.base + outcome['result']['downloadUrl']) as response:
            content = response.read()
        with ZipFile(io.BytesIO(content)) as archive:
            self.assertEqual(len(archive.namelist()), 3)
            details = json.loads(archive.read('AIO-conversion-report.json'))
            self.assertEqual(details['files'][0]['targets'][0]['code'], 'ENGINE_OUTPUT_MISSING')

    def test_save_retry_and_explicit_release(self):
        outcome = self.convert([('image.bmp', b'BM')])
        url = self.base + outcome['result']['downloadUrl']
        for _ in range(2):
            with urlopen(url) as response:
                self.assertTrue(response.read().startswith(b'%PDF'))
        self.json('release', {'ticket': outcome['result']['ticket']})
        with self.assertRaises(HTTPError) as error:
            urlopen(url)
        self.assertEqual(error.exception.code, 404)
        self.assertFalse(self.server.converter.results)

    def test_release_waits_for_an_active_download_lease(self):
        outcome = self.convert([('image.bmp', b'BM')])
        ticket = outcome['result']['ticket']
        with self.server.converter.lease(ticket) as result:
            path = result.path
            self.json('release', {'ticket': ticket})
            self.assertTrue(path.exists())
        self.assertFalse(path.exists())
        self.assertFalse(self.server.converter.results)

    def test_cancel_preserves_finished_outputs_and_next_request_can_run(self):
        identity = 'a' * 32
        reply, errors = [], []
        def run():
            try:
                reply.append(self.convert([('first.bmp', b'BM'), ('second.bmp', b'HOLD'),
                                           ('third.bmp', b'BM')], request=identity))
            except BaseException as exc:
                errors.append(exc)
        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(self.running.wait(5))
        self.assertTrue(self.json('cancel', {'requestId': identity})['cancelled'])
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        report = reply[0]['result']['targetReport']
        self.assertEqual((report['succeeded'], report['failed']), (1, 2))
        self.assertEqual(report['files'][2]['targets'][0]['code'], 'CANCELLED')
        self.assertEqual(self.convert([('retry.bmp', b'BM')])['result']['targetReport']['succeeded'], 1)

    def test_external_origin_and_traversal_are_rejected_as_protocol_errors(self):
        data = json.dumps({'files': []}).encode()
        with self.assertRaises(HTTPError) as error:
            self.post('inspect', data, {'Content-Type': 'application/json', 'Origin': 'https://example.com'})
        self.assertEqual(error.exception.code, 403)
        with self.assertRaises(HTTPError) as error:
            self.convert([('../escape.bmp', b'BM')])
        self.assertEqual(error.exception.code, 422)
        self.assertEqual(self.calls, [])

    def test_cancel_interrupts_a_stalled_upload_without_closing_the_application(self):
        identity = 'd' * 32
        connection = socket.create_connection(('127.0.0.1', self.server.server_port))
        headers = ('POST /api/converter/convert?request=' + identity +
            ' HTTP/1.1\r\nHost: 127.0.0.1:' + str(self.server.server_port) +
            '\r\nX-AIO-Token: ' + self.token +
            '\r\nContent-Type: multipart/form-data; boundary=stalled\r\nContent-Length: 1048576\r\n\r\n')
        connection.sendall(headers.encode())
        deadline = time.monotonic() + 5
        while not self.server.converter.active and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(self.json('cancel', {'requestId': identity})['cancelled'])
        while self.server.converter.active and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(self.server.converter.active)
        connection.settimeout(5)
        self.assertIn(b'422', connection.recv(8192))
        connection.close()
        self.assertEqual(self.convert([('retry.bmp', b'BM')])['result']['targetReport']['succeeded'], 1)

    def test_close_interrupts_incomplete_upload_and_releases_its_owner(self):
        connection = socket.create_connection(('127.0.0.1', self.server.server_port))
        request = ('POST /api/converter/convert?request=' + 'b' * 32 +
            ' HTTP/1.1\r\nHost: 127.0.0.1:' + str(self.server.server_port) +
            '\r\nX-AIO-Token: ' + self.token +
            '\r\nContent-Type: multipart/form-data; boundary=missing\r\nContent-Length: 1048576\r\n\r\n')
        connection.sendall(request.encode())
        deadline = time.monotonic() + 5
        while not self.server.converter.active and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(self.server.converter.active)
        self.server.converter.close()
        connection.close()
        self.assertFalse(self.server.converter.active)
