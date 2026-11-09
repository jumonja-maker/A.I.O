"""Direct-converter protocol memory and cancellable ZIP ownership regressions."""
import hashlib
import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

from app_backend import converter_upload_stream as upload
from app_backend.converter_transport import ConverterTransport, ARCHIVE_CHUNK
from app_backend.libreoffice_engine import LibreOfficeError
from app_backend.server import start_server


def multipart(fields, data=b'file bytes'):
    parts = []
    for name, value in fields:
        parts.append(b'--b\r\nContent-Disposition: form-data; name="' +
                     name.encode() + b'"\r\n\r\n' + value + b'\r\n')
    parts.append(b'--b\r\nContent-Disposition: form-data; name="files"; '
                 b'filename="source.txt"\r\n\r\n' + data + b'\r\n--b--\r\n')
    return b''.join(parts)


class MetadataBudget(unittest.TestCase):
    def parse(self, wire, directory):
        return upload.MultipartUpload(io.BytesIO(wire), len(wire),
            'multipart/form-data; boundary=b', threading.Event()).read(Path(directory))

    def test_metadata_limit_is_aggregate_across_fields(self):
        wire = multipart([('ids', b'1234'), ('formats', b'5678')])
        with tempfile.TemporaryDirectory() as directory, patch.object(upload, 'MAX_METADATA_BYTES', 7):
            with self.assertRaises(LibreOfficeError) as error:
                self.parse(wire, directory)
            self.assertEqual(error.exception.code, 'INVALID_REQUEST')
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_file_bytes_remain_unlimited_by_the_metadata_budget(self):
        data = b'unchanged\x00\xff' * 20000
        wire = multipart([('ids', b'1234'), ('formats', b'567')], data)
        with tempfile.TemporaryDirectory() as directory, patch.object(upload, 'MAX_METADATA_BYTES', 7):
            files, fields = self.parse(wire, directory)
            self.assertEqual(fields, {'ids':'1234', 'formats':'567'})
            self.assertEqual(files[0]['path'].read_bytes(), data)

    def test_oversized_metadata_is_refused_during_streaming_before_full_body(self):
        wire = multipart([('ids', b'x' * (upload.CHUNK * 8))])
        stream = io.BytesIO(wire)
        parser = upload.MultipartUpload(stream, len(wire),
            'multipart/form-data; boundary=b', threading.Event())
        with tempfile.TemporaryDirectory() as directory, patch.object(upload, 'MAX_METADATA_BYTES', 128):
            with self.assertRaises(LibreOfficeError):
                parser.read(Path(directory))
        self.assertLess(stream.tell(), len(wire))
        self.assertLessEqual(parser.metadata_bytes, 128)


class ControlRequestFraming(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.server, self.token = start_server(guard_recovery_parent=Path(self.folder.name)/'guard')
        # Exercise explicit close behavior, even under persistent HTTP.
        self.server.RequestHandlerClass.protocol_version = 'HTTP/1.1'
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval':.01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.folder.cleanup()

    def reject_header_only(self, headers, endpoint='inspect'):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        try:
            with patch.object(self.server.RequestHandlerClass, '_read_exact',
                              side_effect=AssertionError('body must not be read')):
                connection.putrequest('POST', '/api/converter/' + endpoint)
                connection.putheader('X-AIO-Token', self.token)
                connection.putheader('Content-Type', 'application/json')
                connection.putheader('Connection', 'keep-alive')
                for name, value in headers:
                    connection.putheader(name, str(value))
                connection.endheaders()
                response = connection.getresponse()
                self.assertEqual(response.status, 422)
                self.assertEqual(json.loads(response.read())['code'], 'INVALID_REQUEST')
                self.assertTrue(response.will_close)
                self.assertIsNone(connection.sock)
            self.assertFalse(self.server.converter.active)
            self.assertFalse(self.server.converter.results)
        finally:
            connection.close()

    def test_large_json_is_refused_from_headers_without_reading_its_body(self):
        for endpoint in ('inspect', 'release', 'cancel'):
            with self.subTest(endpoint=endpoint):
                self.reject_header_only([('Content-Length', upload.MAX_METADATA_BYTES + 1)], endpoint)

    def test_duplicate_content_length_is_refused_without_body_or_desynchronization(self):
        for endpoint in ('inspect', 'convert'):
            with self.subTest(endpoint=endpoint):
                self.reject_header_only([('Content-Length', 2), ('Content-Length', 2)], endpoint)

    def test_transfer_encoding_is_refused_even_with_content_length(self):
        for encoding in ('chunked', 'identity', ''):
            for endpoint in ('inspect', 'convert'):
                with self.subTest(encoding=encoding, endpoint=endpoint):
                    self.reject_header_only([('Content-Length', 2), ('Transfer-Encoding', encoding)], endpoint)

    def test_valid_control_json_still_works_after_rejected_connection(self):
        self.reject_header_only([('Content-Length', upload.MAX_METADATA_BYTES + 1)])
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        try:
            connection.request('POST', '/api/converter/cancel', body=b'{"requestId":"unknown"}',
                headers={'X-AIO-Token':self.token, 'Content-Type':'application/json'})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read()), {'ok':True, 'cancelled':False})
        finally:
            connection.close()


class PackagingCancellation(unittest.TestCase):
    def setUp(self):
        self.transport = ConverterTransport()
        self.request = self.transport.begin('request', object())
        self.data = bytes(range(256)) * (ARCHIVE_CHUNK // 256 * 4)

    def tearDown(self):
        if not self.request.done.is_set():
            self.transport.finish(self.request, retained=bool(self.transport.results))
        self.transport.close()

    def convert(self, source, target, workspace, **kwargs):
        output = workspace / 'output'
        output.mkdir(parents=True)
        path = output / ('source.' + target)
        path.write_bytes(self.data)
        return [path]

    def execute(self):
        return self.transport.execute(self.request,
            [{'path':Path('source.txt'), 'warnings':[]}], ['one'], ['pdf', 'docx'])

    def test_zip_bytes_and_names_are_preserved_without_cancel(self):
        with patch('app_backend.converter_transport.convert_path', side_effect=self.convert):
            result = self.execute()
        with self.transport.lease(result['result']['ticket']) as retained:
            with zipfile.ZipFile(retained.path) as archive:
                self.assertEqual(archive.namelist(), ['001_pdf/source.pdf', '001_docx/source.docx'])
                for name in archive.namelist():
                    self.assertEqual(hashlib.sha256(archive.read(name)).digest(), hashlib.sha256(self.data).digest())
                    self.assertEqual(archive.getinfo(name).compress_type, zipfile.ZIP_STORED)
                    self.assertIsNone(archive.testzip())

    def test_cancel_during_large_file_stops_at_chunk_without_retaining_partial_zip(self):
        original = zipfile._ZipWriteFile.write
        writes = []
        def cancel_after_first_chunk(stream, data):
            writes.append(len(data))
            result = original(stream, data)
            self.assertTrue(self.request.packaging)
            self.transport.cancel(self.request.identity)
            return result
        with patch('app_backend.converter_transport.convert_path', side_effect=self.convert), \
                patch.object(zipfile._ZipWriteFile, 'write', cancel_after_first_chunk):
            with self.assertRaises(LibreOfficeError) as error:
                self.execute()
        self.assertEqual(error.exception.code, 'CANCELLED')
        self.assertEqual(writes, [ARCHIVE_CHUNK])
        self.assertFalse(self.transport.results)
        self.assertFalse(self.request.packaging)
        root = self.request.root
        self.transport.finish(self.request)
        self.assertFalse(root.exists())
        next_request = self.transport.begin('next-request', object())
        self.transport.finish(next_request)

    def test_cancel_when_packaging_starts_prevents_first_archive_write(self):
        transport = self.transport
        original = transport._check_packaging_cancel
        def at_packaging(request):
            if request.packaging:
                transport.cancel(request.identity)
            return original(request)
        with patch('app_backend.converter_transport.convert_path', side_effect=self.convert), \
                patch.object(transport, '_check_packaging_cancel', side_effect=at_packaging), \
                patch.object(zipfile._ZipWriteFile, 'write', side_effect=AssertionError('No archive bytes may be written')):
            with self.assertRaises(LibreOfficeError) as error:
                self.execute()
        self.assertEqual(error.exception.code, 'CANCELLED')
        self.assertFalse(transport.results)

    def test_conversion_cancel_still_preserves_previously_completed_results(self):
        def partial(source, target, workspace, **kwargs):
            if target == 'docx':
                self.transport.cancel(self.request.identity)
                raise LibreOfficeError('cancel', 'CANCELLED')
            return self.convert(source, target, workspace, **kwargs)
        with patch('app_backend.converter_transport.convert_path', side_effect=partial):
            result = self.execute()
        self.assertTrue(result['ok'])
        self.assertEqual(result['result']['targetReport']['succeeded'], 1)
        self.assertEqual(result['result']['targetReport']['failed'], 1)
        with self.transport.lease(result['result']['ticket']) as retained:
            with zipfile.ZipFile(retained.path) as archive:
                self.assertEqual(archive.read('001_pdf/source.pdf'), self.data)
                self.assertIn('AIO-conversion-report.json', archive.namelist())
