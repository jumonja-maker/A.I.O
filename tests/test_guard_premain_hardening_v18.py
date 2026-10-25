"""Independent negative gates for pre-main Guard v18 quality/security hardening."""
from http.client import HTTPConnection
from pathlib import Path
import tempfile
import threading
import unittest
import zipfile

from openpyxl import Workbook

from app_backend.server import start_server
from app_backend.converter_guard.content_invariants import check_content_invariants
from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard.quality import compare_documents, inspect_document

O = 'urn:oasis:names:tc:opendocument:xmlns:office:1.0'
T = 'urn:oasis:names:tc:opendocument:xmlns:table:1.0'
P = 'urn:oasis:names:tc:opendocument:xmlns:text:1.0'
W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'


class LocalHostBoundary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.token = start_server()
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()
        cls.port = cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join(timeout=5)

    def request(self, hosts, *, origin=None, method='GET', path='/'):
        connection = HTTPConnection('127.0.0.1', self.port, timeout=10)
        try:
            connection.putrequest(method, path, skip_host=True)
            for host in hosts:
                connection.putheader('Host', host)
            if origin is not None:
                connection.putheader('Origin', origin)
            if method == 'POST':
                connection.putheader('X-AIO-Token', self.token)
                connection.putheader('Content-Type', 'application/json')
                connection.putheader('Content-Length', '2')
            connection.endheaders()
            if method == 'POST':
                connection.send(b'{}')
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_trusted_loopback_bootstrap_still_serves_app(self):
        host = '127.0.0.1:' + str(self.port)
        status, body = self.request([host])
        self.assertEqual(status, 200)
        self.assertIn(b'data-aio-token=', body)
        status, _ = self.request([host], origin='http://' + host)
        self.assertEqual(status, 200)

    def test_mismatched_or_ambiguous_host_never_exposes_api_token(self):
        host = '127.0.0.1:' + str(self.port)
        for hosts in (['foreign.example.invalid:' + str(self.port)],
                      ['localhost:' + str(self.port)],
                      [host, host], []):
            with self.subTest(hosts=hosts):
                status, body = self.request(hosts)
                self.assertNotEqual(status, 200)
                self.assertNotIn(b'data-aio-token=', body)
                self.assertNotIn(self.token.encode('ascii'), body)

    def test_untrusted_origin_blocks_bootstrap_and_authenticated_post(self):
        host = '127.0.0.1:' + str(self.port)
        status, body = self.request([host], origin='http://foreign.example.invalid')
        self.assertEqual(status, 403)
        self.assertNotIn(b'data-aio-token=', body)
        status, _ = self.request(['foreign.example.invalid:' + str(self.port)],
                                 method='POST', path='/api/icon-theme')
        self.assertEqual(status, 403)


class OdfCellAccuracy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cancel = threading.Event()

    def odf(self, rows):
        path = self.root / 'source.odt'
        body = ''.join('<t:table-row>' + ''.join(
            '<t:table-cell><p:p>' + cell + '</p:p></t:table-cell>'
            for cell in row) + '</t:table-row>' for row in rows)
        xml = (f'<o:document-content xmlns:o="{O}" xmlns:t="{T}" xmlns:p="{P}">'
               '<o:body><o:text><t:table>' + body +
               '</t:table></o:text></o:body></o:document-content>')
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('mimetype', 'application/vnd.oasis.opendocument.text')
            archive.writestr('content.xml', xml)
        return path

    def docx(self, rows, extra_paragraph=''):
        path = self.root / 'source.docx'
        cells = ''.join('<w:tr>' + ''.join(
            '<w:tc><w:p><w:r><w:t>' + value +
            '</w:t></w:r></w:p></w:tc>' for value in row) +
            '</w:tr>' for row in rows)
        xml = (f'<w:document xmlns:w="{W}"><w:body>' +
               ('<w:p><w:r><w:t>' + extra_paragraph + '</w:t></w:r></w:p>'
                if extra_paragraph else '') +
               ('<w:tbl>' + cells + '</w:tbl>' if rows else '') +
               '</w:body></w:document>')
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('word/document.xml', xml)
        return path

    def check(self, source, output, target='docx'):
        return check_content_invariants([output], target, source, cancel=self.cancel)

    def test_exact_structured_values_pass_and_mark_table_scope(self):
        source = self.odf([['42', 'A'], ['B', 'A']])
        result = self.check(source, self.docx([['42', 'A'], ['B', 'A']]))
        self.assertEqual(result['status'], 'checked_subset')
        self.assertTrue(result['tableStructureCompared'])
        self.assertEqual(result['checkedCellOccurrences'], 4)

    def test_substring_collision_cannot_hide_missing_cell(self):
        source = self.odf([['42', 'B']])
        with self.assertRaises(GuardError) as error:
            self.check(source, self.docx([['1420', 'B']]))
        self.assertEqual(error.exception.code, 'ENGINE_OUTPUT_CONTENT_LOSS')

    def test_repeated_cell_values_cannot_hide_missing_occurrences(self):
        source = self.odf([['A', 'A'], ['A', 'A']])
        with self.assertRaises(GuardError) as error:
            self.check(source, self.docx([['A']]))
        self.assertEqual(error.exception.code, 'ENGINE_OUTPUT_CONTENT_LOSS')
        self.assertIn('duplicate_cells', error.exception.diagnostic)

    def test_unrelated_paragraph_cannot_hide_lost_table(self):
        source = self.odf([['ALPHA', 'BETA']])
        with self.assertRaises(GuardError) as error:
            self.check(source, self.docx([], extra_paragraph='ALPHA BETA'))
        self.assertEqual(error.exception.code, 'ENGINE_OUTPUT_CONTENT_LOSS')

    def test_equal_cell_values_but_swapped_positions_raise_structure_changed(self):
        source = self.odf([['A', 'B'], ['C', 'D']])
        with self.assertRaises(GuardError) as error:
            self.check(source, self.docx([['B', 'A'], ['C', 'D']]))
        self.assertEqual(error.exception.code, 'ENGINE_OUTPUT_TABLE_STRUCTURE_CHANGED')

    def test_plaintext_requires_full_values_and_multiplicity(self):
        source = self.odf([['42', '42'], ['B', 'A']])
        txt = self.root / 'source.txt'
        txt.write_text('42\t42\nB\tA', encoding='utf-8')
        result = self.check(source, txt, 'txt')
        self.assertEqual(result['comparedScope'], 'text_value_multiplicity')
        self.assertFalse(result['tableStructureCompared'])
        for changed in ('1420\t42\nB\tA', '42\nB\tA'):
            txt.write_text(changed, encoding='utf-8')
            with self.assertRaises(GuardError) as error:
                self.check(source, txt, 'txt')
            self.assertEqual(error.exception.code, 'ENGINE_OUTPUT_CONTENT_LOSS')

    def test_html_values_outside_table_are_not_counted(self):
        source = self.odf([['A', 'B']])
        html = self.root / 'source.html'
        html.write_text('<html><body><table><tr><td>A</td><td>B</td></tr></table></body></html>')
        self.assertEqual(self.check(source, html, 'html')['status'], 'checked_subset')
        html.write_text('<html><body><p>A B</p><table><tr><td>A</td></tr></table></body></html>')
        with self.assertRaises(GuardError) as error:
            self.check(source, html, 'html')
        self.assertEqual(error.exception.code, 'ENGINE_OUTPUT_CONTENT_LOSS')


class SpreadsheetEvidenceAccuracy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def book(self, name, values):
        path = self.root / name
        book = Workbook()
        book.active.title = 'Sheet A'
        book.active['B2'] = values[0]
        if len(values) > 1:
            sheet = book.create_sheet('Sheet B')
            sheet['B2'] = values[1]
        book.save(path)
        book.close()
        return path

    def test_multisheet_value_changes_never_count_as_checked_values(self):
        before = inspect_document(self.book('before.xlsx', (42, 99)))
        after = inspect_document(self.book('after.xlsx', (888, 777)))
        self.assertTrue(before['complete'])
        self.assertTrue(after['complete'])
        self.assertIsNone(before['cellValues']['valuesDigest'])
        result = compare_documents(before, after)
        self.assertTrue(result['structureCompared'])
        self.assertFalse(result['valuesCompared'])
        self.assertFalse(result['qualityCompared'])
        self.assertEqual(result['comparisonCoverage']['values'], 'not_checked')
        self.assertIn('spreadsheet_values', result['unknown'])
        self.assertEqual(result['status'], 'not_checked')

    def test_single_sheet_value_changes_are_detected(self):
        before = inspect_document(self.book('single-a.xlsx', (42,)))
        after = inspect_document(self.book('single-b.xlsx', (888,)))
        result = compare_documents(before, after)
        self.assertTrue(result['valuesCompared'])
        self.assertTrue(result['qualityCompared'])
        self.assertEqual(result['status'], 'risks_found')
        self.assertIn('CELL_VALUES_DIFFER', [r['code'] for r in result['risks']])


if __name__ == '__main__':
    unittest.main()
