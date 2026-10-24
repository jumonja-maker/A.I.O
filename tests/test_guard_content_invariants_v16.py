"""Risky ODF tables must not lose inspectable cell text silently."""
from pathlib import Path
import tempfile
import threading
import unittest
import zipfile
from unittest.mock import patch

from app_backend.converter_guard.content_invariants import check_content_invariants
from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard.postconditions import check_native_output

O = 'urn:oasis:names:tc:opendocument:xmlns:office:1.0'
T = 'urn:oasis:names:tc:opendocument:xmlns:table:1.0'
P = 'urn:oasis:names:tc:opendocument:xmlns:text:1.0'
W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
ROWS = [['R0 C0 value 2', 'R0 C1 value 2'], ['R1 C0 value 2', 'R1 C1 value 3']]


class ContentInvariants(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.odf(self.root / 'source.odt', ROWS)

    def odf(self, path, rows, columns=False):
        table = '<table:table>' + ('<table:table-column table:number-columns-repeated="2"/>' if columns else '')
        for row in rows:
            table += '<table:table-row>'
            for text in row:
                # ODF whitespace elements must match normal spaces in OOXML.
                table += '<table:table-cell><text:p>' + text.replace(' ', '<text:s/>') + '</text:p></table:table-cell>'
            table += '</table:table-row>'
        table += '</table:table>'
        xml = f'<office:document-content xmlns:office="{O}" xmlns:table="{T}" xmlns:text="{P}">'
        xml += '<office:body><office:text>' + table + '</office:text></office:body></office:document-content>'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('mimetype', 'application/vnd.oasis.opendocument.text')
            archive.writestr('content.xml', xml)
        return path

    def docx(self, rows):
        path = self.root / 'source.docx'
        xml = f'<w:document xmlns:w="{W}"><w:body><w:tbl>'
        for row in rows:
            xml += '<w:tr>'
            for text in row:
                pieces = text.split(' ', 1)
                xml += '<w:tc><w:p><w:r><w:t>' + pieces[0] + '</w:t></w:r>'
                xml += '<w:r><w:t xml:space="preserve"> ' + pieces[1] + '</w:t></w:r></w:p></w:tc>'
            xml += '</w:tr>'
        xml += '</w:tbl></w:body></w:document>'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('word/document.xml', xml)
        return path

    def check(self, output, target, source=None):
        return check_content_invariants([output], target, source or self.source,
                                        cancel=threading.Event())

    def test_docx_preserves_tokens_across_runs_and_odf_space_elements(self):
        result = self.check(self.docx(ROWS), 'docx')
        self.assertEqual(result['status'], 'checked_subset')
        self.assertEqual(result['sourceNonemptyCells'], 4)
        self.assertEqual(result['missingUniqueCellTexts'], 0)
        self.assertFalse(result['qualityCompared'])

    def test_missing_columns_in_valid_docx_are_detected(self):
        with self.assertRaises(GuardError) as error:
            self.check(self.docx([[r[0]] for r in ROWS]), 'docx')
        self.assertEqual(error.exception.code, 'ENGINE_OUTPUT_CONTENT_LOSS')
        self.assertEqual(error.exception.diagnostic, 'odf_table_missing_unique_cell_texts:2')

    def test_odf_and_plain_text_targets_are_inspected(self):
        output = self.odf(self.root / 'source.ott', ROWS, columns=True)
        self.assertEqual(self.check(output, 'ott')['status'], 'checked_subset')
        output = self.root / 'source.txt'
        output.write_text('\n'.join('\t'.join(r) for r in ROWS), encoding='utf-8')
        self.assertEqual(self.check(output, 'txt')['status'], 'checked_subset')
        output.write_text('\n'.join(r[0] for r in ROWS), encoding='utf-8')
        with self.assertRaises(GuardError) as error:
            self.check(output, 'txt')
        self.assertEqual(error.exception.code, 'ENGINE_OUTPUT_CONTENT_LOSS')

    def test_html_ignores_script_and_style_when_checking_presence(self):
        output = self.root / 'source.html'
        cells = ''.join('<td>' + t + '</td>' for row in ROWS for t in row)
        output.write_text('<html><body><table><tr>' + cells + '</tr></table></body></html>')
        result = check_native_output([output], 'html', self.source, cancel=threading.Event())
        self.assertEqual(result['status'], 'content_present')
        self.assertEqual(result['additionalChecks'][0]['status'], 'checked_subset')
        output.write_text('<html><head><script>"' + ' '.join(t for row in ROWS for t in row) +
                          '"</script></head><body>unrelated</body></html>')
        with self.assertRaises(GuardError) as error:
            self.check(output, 'html')
        self.assertEqual(error.exception.code, 'ENGINE_OUTPUT_CONTENT_LOSS')

    def test_source_names_do_not_determine_actual_odf_content(self):
        renamed = self.root / 'renamed.bin'
        renamed.write_bytes(self.source.read_bytes())
        self.assertEqual(self.check(self.docx(ROWS), 'docx', renamed)['status'], 'checked_subset')

    def test_declared_tables_are_outside_this_conservative_risk_check(self):
        source = self.odf(self.root / 'declared.odt', ROWS, columns=True)
        self.assertIsNone(self.check(self.docx([[r[0]] for r in ROWS]), 'docx', source))

    def test_uninspectable_output_does_not_claim_quality_or_loss(self):
        for target in ('pdf', 'doc', 'rtf', 'emz'):
            result = self.check(self.root / ('not-read.' + target), target)
            self.assertEqual(result['status'], 'not_checked')
            self.assertEqual(result['reason'], 'target_text_not_inspectable')
            self.assertFalse(result['qualityCompared'])

    def test_broken_inspectable_output_fails_closed(self):
        output = self.root / 'source.docx'
        output.write_bytes(b'not zip')
        with self.assertRaises(GuardError) as error:
            self.check(output, 'docx')
        self.assertEqual(error.exception.code, 'ENGINE_OUTPUT_VALIDATION_INCOMPLETE')

    def test_cancellation_does_not_turn_into_a_skipped_check(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(GuardError) as error:
            check_content_invariants([self.docx(ROWS)], 'docx', self.source, cancel=cancel)
        self.assertEqual(error.exception.code, 'CANCELLED')

    def test_cell_text_split_across_plain_file_read_chunks_is_preserved(self):
        output = self.root / 'source.txt'
        prefix = b' ' * (65536 - 2)
        output.write_bytes(prefix + '\n'.join(t for row in ROWS for t in row).encode())
        self.assertEqual(self.check(output, 'txt')['status'], 'checked_subset')

    def test_source_inspection_budgets_are_explicit_without_vetoing_file_size(self):
        for key, value, reason in (
                ('MAX_UNIQUE_CELL_TEXTS', 1, 'cell_text_budget'),
                ('MAX_XML_READ', 64, 'xml_read_budget'),
                ('MAX_XML_DEPTH', 3, 'xml_depth_budget')):
            with self.subTest(reason=reason), patch(
                    'app_backend.converter_guard.content_invariants.' + key, value):
                result = self.check(self.docx(ROWS), 'docx')
                self.assertEqual(result['status'], 'not_checked')
                self.assertEqual(result['reason'], 'source_inspection_budget')
                self.assertEqual(result['inspectionLimit'], reason)

    def test_xml_entities_are_not_expanded_by_the_optional_inspection(self):
        with zipfile.ZipFile(self.source) as archive:
            xml = archive.read('content.xml')
        with zipfile.ZipFile(self.source, 'w') as archive:
            archive.writestr('mimetype', 'application/vnd.oasis.opendocument.text')
            archive.writestr('content.xml', b'<!DOCTYPE example [<!ENTITY x "private">]>' + xml)
        result = self.check(self.docx(ROWS), 'docx')
        self.assertEqual(result['status'], 'not_checked')
        self.assertEqual(result['inspectionLimit'], 'xml_declarations_not_inspected')

    def test_known_risk_with_output_inspection_budget_fails_incomplete(self):
        output = self.docx(ROWS)
        with zipfile.ZipFile(output) as archive:
            xml = archive.read('word/document.xml')
        with zipfile.ZipFile(output, 'w') as archive:
            archive.writestr('word/document.xml', xml.replace(b'<w:body>', b'<w:body>' + b' ' * 5000))
        with patch('app_backend.converter_guard.content_invariants.MAX_XML_READ', 4096):
            with self.assertRaises(GuardError) as error:
                self.check(output, 'docx')
        self.assertEqual(error.exception.code, 'ENGINE_OUTPUT_VALIDATION_INCOMPLETE')


if __name__ == '__main__':
    unittest.main()
