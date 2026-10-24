"""Regressions for actual output truncation and bounded optional evidence."""
import io
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard.media_fidelity import inspect_media, check_media_preservation
from app_backend.converter_guard.quality import inspect_document, _Budget, _Limited, _xlsx_values
from app_backend.converter_guard.postconditions import SourceEvidence
from app_backend.converter_guard.content_invariants import check_content_invariants, _source_cells


def jpeg(entropy=b'\x11\xff\0\x22', *, progressive=False):
    frame = b'\xff\xc2' if progressive else b'\xff\xc0'
    sof = frame + struct.pack('>H', 11) + b'\x08\0\x02\0\x02\x01\x01\x11\0'
    sos = b'\xff\xda\0\x08\x01\x01\0\0\x3f\0'
    return b'\xff\xd8' + sof + sos + entropy + b'\xff\xd9'


def bmp():
    pixels = b'\0' * 16  # two padded 24-bit rows, two pixels each
    dib = struct.pack('<IiiHHIIiiII', 40, 2, 2, 1, 24, 0, len(pixels), 0, 0, 0, 0)
    return b'BM' + struct.pack('<IHHI', 54 + len(pixels), 0, 0, 54) + dib + pixels


class OutputIntegrityV18(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.dat'
        self.source.write_bytes(b'not an image')
        self.cancel = threading.Event()

    def check(self, extension, data):
        output = self.root / ('source.' + extension)
        output.write_bytes(data)
        return check_media_preservation([output], extension, self.source, cancel=self.cancel)

    def test_jpeg_with_valid_dimensions_but_missing_scan_or_eoi_cannot_be_ready(self):
        value = jpeg()
        for truncated in (value[:15], value[:-2], value[:-1]):
            with self.subTest(size=len(truncated)):
                with self.assertRaises(GuardError) as caught:
                    self.check('jpg', truncated)
                self.assertEqual(caught.exception.code, 'MEDIA_OUTPUT_INVALID')

    def test_jpeg_metadata_source_probe_still_allows_native_recovery(self):
        path = self.root / 'recoverable.jpg'
        path.write_bytes(jpeg()[:15])
        self.assertEqual(inspect_media(path)['width'], 2)
        with self.assertRaises(GuardError):
            inspect_media(path, validate_container=True)

    def test_jpeg_entropy_stuffing_fill_restart_and_multiple_scans(self):
        entropy = b'x' * 65535 + b'\xff\0' + b'z' * 65534 + b'\xff\xff\xd0more\xff\x01more'
        value = jpeg(entropy, progressive=True)
        # A second scan after a length-bearing marker is valid structure too.
        value = value[:-2] + b'\xff\xc4\0\x02\xff\xda\0\x08\x01\x01\0\0\x3f\0more\xff\xd9'
        self.assertEqual(self.check('jpg', value)['status'], 'checked_subset')

    def test_bmp_dimensions_do_not_certify_missing_pixel_array(self):
        for value in (bmp()[:26], bmp()[:54], bmp()[:-1]):
            with self.subTest(size=len(value)):
                with self.assertRaises(GuardError) as caught:
                    self.check('bmp', value)
                self.assertEqual(caught.exception.code, 'MEDIA_OUTPUT_INVALID')
        self.assertEqual(self.check('bmp', bmp())['status'], 'checked_subset')

    def test_cancelled_complete_jpeg_check_stays_cancelled(self):
        self.cancel.set()
        with self.assertRaises(GuardError) as caught:
            self.check('jpg', jpeg())
        self.assertEqual(caught.exception.code, 'CANCELLED')

    def test_utf16_internal_entity_does_not_become_completed_quality_evidence(self):
        value = ('<?xml version="1.0" encoding="UTF-16"?>'
                 '<!DOCTYPE office:document-content [<!ENTITY x "injected words">]>'
                 '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
                 'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
                 '<office:body><office:text><text:p>&x;</text:p></office:text></office:body>'
                 '</office:document-content>').encode('utf-16')
        path = self.root / 'source.odt'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('mimetype', 'application/vnd.oasis.opendocument.text')
            archive.writestr('content.xml', value)
        evidence = inspect_document(path)
        self.assertFalse(evidence['complete'])
        self.assertEqual(evidence['reason'], 'xml_entities_not_inspected')

    def test_split_utf32_entity_declaration_stays_bounded(self):
        value = ('a' * 16382 + '<!DOCTYPE root>').encode('utf-32-le')
        budget = _Budget(None, 2, 1024 * 1024)
        reader = budget.wrap(io.BytesIO(value))
        reader.read(65536)
        with self.assertRaises(_Limited):
            reader.read(65536)

    def test_eager_xlsx_metadata_budget_is_checked_before_openpyxl(self):
        path = self.root / 'large-shared-strings.xlsx'
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('xl/sharedStrings.xml', b' ' * 2048)
        with patch('openpyxl.load_workbook') as load:
            with self.assertRaisesRegex(_Limited, 'xlsx_metadata_budget'):
                _xlsx_values(path, _Budget(None, 2, 1024))
            load.assert_not_called()

    def test_renamed_opc_shared_strings_cannot_bypass_eager_budget(self):
        path = self.root / 'renamed-shared-strings.xlsx'
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('[Content_Types].xml',
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Override PartName="/xl/custom-strings.dat" '
                'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
                '</Types>')
            archive.writestr('xl/custom-strings.dat', b' ' * 2048)
        with patch('openpyxl.load_workbook') as load:
            with self.assertRaisesRegex(_Limited, 'xlsx_metadata_budget'):
                _xlsx_values(path, _Budget(None, 2, 1024))
            load.assert_not_called()

    def test_reuse_source_summary_still_independently_checks_each_output(self):
        source = self.root / 'source.odt'
        value = ('<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
                 'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
                 'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
                 '<office:body><office:text><table:table><table:table-row>'
                 '<table:table-cell><text:p>ALPHA</text:p></table:table-cell>'
                 '<table:table-cell><text:p>BETA</text:p></table:table-cell>'
                 '</table:table-row></table:table></office:text></office:body></office:document-content>')
        with zipfile.ZipFile(source, 'w') as archive:
            archive.writestr('mimetype', 'application/vnd.oasis.opendocument.text')
            archive.writestr('content.xml', value)
        output = self.root / 'source.txt'
        output.write_text('ALPHA BETA', encoding='utf-8')
        evidence = SourceEvidence(source)
        with patch('app_backend.converter_guard.content_invariants._source_cells', wraps=_source_cells) as inspect:
            for _ in range(2):
                result = check_content_invariants([output], 'txt', source,
                    cancel=self.cancel, source_evidence=evidence)
                self.assertEqual(result['status'], 'checked_subset')
            self.assertEqual(inspect.call_count, 1)
            output.write_text('ALPHA', encoding='utf-8')
            with self.assertRaises(GuardError) as caught:
                check_content_invariants([output], 'txt', source,
                    cancel=self.cancel, source_evidence=evidence)
            self.assertEqual(caught.exception.code, 'ENGINE_OUTPUT_CONTENT_LOSS')
            self.assertEqual(inspect.call_count, 1)

    def test_source_summary_invalidation_and_change_during_read(self):
        evidence = SourceEvidence(self.source)
        loader = lambda: self.source.read_bytes()
        self.assertEqual(evidence.get('media_profile', self.source, loader), b'not an image')
        self.source.write_bytes(b'a different source')
        self.assertEqual(evidence.get('media_profile', self.source, loader), b'a different source')
        other = self.root / 'other.dat'
        other.write_bytes(b'other')
        self.assertEqual(evidence.get('media_profile', other, lambda: other.read_bytes()), b'other')
        self.assertEqual(len(evidence.values), 1)
        def changes_while_reading():
            other.write_bytes(b'changed during inspection')
            return {'width': 2}
        with self.assertRaises(GuardError) as caught:
            evidence.get('odf_table_text', other, changes_while_reading)
        self.assertEqual(caught.exception.code, 'SOURCE_CHANGED_DURING_VALIDATION')
        self.assertEqual(evidence.values, {})


if __name__ == '__main__':
    unittest.main()
