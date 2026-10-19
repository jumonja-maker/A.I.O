"""Direct LibreOffice adapter regressions: no Guard or legacy converter imports."""
from __future__ import annotations

import io
import json
import subprocess
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from zipfile import ZipFile

from app_backend import libreoffice_engine as lo
from app_backend.server import start_server


def multipart(name, content, *, target=None):
    boundary = 'aio-converter-test-boundary-42'
    fields = [('ids', '["test-1"]'.encode('utf-8'))]
    if target:
        fields.append(('format', target.encode('utf-8')))
    result = bytearray()
    for key, value in fields:
        result.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
        result.extend(value + b'\r\n')
    result.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{name}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode())
    result.extend(content + b'\r\n')
    result.extend(f'--{boundary}--\r\n'.encode())
    return bytes(result), f'multipart/form-data; boundary={boundary}'


class LibreOfficeDirectTests(unittest.TestCase):
    def test_native_families_only(self):
        self.assertTrue(lo.compatible('docx', 'pdf'))
        self.assertTrue(lo.compatible('xlsx', 'pdf'))
        self.assertTrue(lo.compatible('pptx', 'pdf'))
        self.assertFalse(lo.compatible('docx', 'xlsx'))
        self.assertFalse(lo.compatible('xlsx', 'docx'))
        self.assertFalse(lo.compatible('docx', 'docx'))
        self.assertFalse(lo.compatible('pdf', 'docx'))

    def test_reject_untrusted_filename_and_signature(self):
        with self.assertRaises(lo.LibreOfficeError) as error:
            lo.validate_source('../file.docx', b'fake')
        self.assertEqual(error.exception.code, 'INVALID_FILENAME')
        with self.assertRaises(lo.LibreOfficeError) as error:
            lo.validate_source('file.docx', b'not a zip')
        self.assertEqual(error.exception.code, 'INVALID_PACKAGE')

    def test_inspection_uses_actual_executable_presence(self):
        with patch.object(lo, 'engine_binary', return_value=Path('/fake/soffice')):
            report = lo.inspect([('file.txt', b'hello')], ['id1'])
        self.assertTrue(next(x for x in report['formats'] if x['id']=='pdf')['enabled'])
        self.assertFalse(next(x for x in report['formats'] if x['id']=='xlsx')['enabled'])
        self.assertEqual(report['files'][0]['id'], 'id1')
        with patch.object(lo, 'engine_binary', return_value=None):
            report = lo.inspect([('file.txt', b'hello')], ['id1'])
        self.assertFalse(next(x for x in report['formats'] if x['id']=='pdf')['enabled'])

    def test_process_writes_valid_pdf(self):
        def pretend(command, **kwargs):
            destination = Path(command[command.index('--outdir') + 1])
            (destination/'input.pdf').write_bytes(b'%PDF-1.7\n% AIO test')
            self.assertEqual(kwargs['timeout'], lo.TIMEOUT_SECONDS)
            self.assertTrue(any(item.startswith('-env:UserInstallation=file:') for item in command))
            return subprocess.CompletedProcess(command, 0, stdout=b'convert', stderr=b'')
        with patch.object(lo.subprocess, 'run', side_effect=pretend):
            output=lo.convert('demo.txt', b'Hello AIO', 'pdf', executable=Path(__file__))
        self.assertTrue(output.startswith(b'%PDF-'))

    def test_missing_and_false_output_never_success(self):
        with patch.object(lo.subprocess, 'run',
                          return_value=subprocess.CompletedProcess([], 0)):
            with self.assertRaises(lo.LibreOfficeError) as error:
                lo.convert('demo.txt', b'Hello', 'pdf', executable=Path(__file__))
            self.assertEqual(error.exception.code, 'ENGINE_FAILED')
        def malicious(command, **kwargs):
            destination = Path(command[command.index('--outdir') + 1])
            (destination/'input.pdf').write_bytes(b'not a pdf')
            return subprocess.CompletedProcess(command, 0)
        with patch.object(lo.subprocess, 'run', side_effect=malicious):
            with self.assertRaises(lo.LibreOfficeError) as error:
                lo.convert('demo.txt', b'Hello', 'pdf', executable=Path(__file__))
            self.assertEqual(error.exception.code, 'INVALID_OUTPUT')

    def test_live_writer_calc_pdf_with_local_engine(self):
        """True LO CLI smoke, required when AIO_LO_QA_MUST_HAVE_ENGINE=1."""
        import os
        from openpyxl import Workbook
        binary = lo.engine_binary()
        if binary is None:
            if os.environ.get('AIO_LO_QA_MUST_HAVE_ENGINE') == '1':
                self.fail('QA requires an installed complete LibreOffice engine.')
            self.skipTest('LibreOffice binary not installed on this host.')
        writer = lo.convert('smoke.txt', b'A.I.O LibreOffice direct engine smoke', 'pdf')
        self.assertTrue(writer.startswith(b'%PDF-'))
        table = Workbook()
        table.active.append(['Name','Qty'])
        table.active.append(['Sample',3])
        buffer = io.BytesIO()
        table.save(buffer)
        table.close()
        calc = lo.convert('spreadsheet.xlsx',buffer.getvalue(),'pdf')
        self.assertTrue(calc.startswith(b'%PDF-'))
        self.assertGreater(len(calc), 500)

    def test_loopback_real_converter_api(self):
        server, token = start_server()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = 'http://127.0.0.1:' + str(server.server_port)
            payload, content_type = multipart('test.txt', b'AIO', target='pdf')
            headers={'X-AIO-Token':token,'Content-Type':content_type}
            with patch('app_backend.server.run_libreoffice',
                       return_value=b'%PDF-1.7\nAIO'):
                request=Request(base+'/api/converter/convert', data=payload, headers=headers)
                with urlopen(request,timeout=10) as reply:
                    self.assertEqual(reply.status,200)
                    self.assertEqual(reply.headers.get('Content-Type'),'application/pdf')
                    self.assertEqual(reply.read(),b'%PDF-1.7\nAIO')
            request=Request(base+'/api/converter/inspect',data=payload,
                            headers=headers)
            with urlopen(request,timeout=10) as reply:
                result=json.load(reply)
            self.assertEqual(result['files'][0]['id'],'test-1')
            self.assertTrue(any(item['id']=='pdf' for item in result['formats']))
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(base+'/api/converter/inspect', data=payload,
                                headers={'Content-Type':content_type}), timeout=10)
            self.assertEqual(error.exception.code,403)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
