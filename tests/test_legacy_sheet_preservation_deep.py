"""Known workbook sheets cannot disappear through the unprepared CLI path."""
import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET
from zipfile import ZipFile, ZIP_DEFLATED

from openpyxl import Workbook

from app_backend.converter_transport import ConverterTransport
from app_backend.libreoffice_engine import LibreOfficeError


class LegacySheetPreservation(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root/'output'
        self.output.mkdir()
        self.cancel = threading.Event()

    def workbook(self, count=2, hidden=False):
        book = Workbook()
        first = book.active
        first.title = 'First'
        first.append(['Marker'])
        first.append(['FIRST'])
        for index in range(1, count):
            sheet = book.create_sheet('Sheet' + str(index + 1))
            if hidden:
                sheet.sheet_state = 'hidden'
            else:
                sheet.append(['SECOND'])
        path = self.root/'source.renamed'
        book.save(path)
        book.close()
        return path

    def files(self, count=1, target='csv'):
        paths = []
        for index in range(count):
            path = self.output/('source' + ('' if index == 0 else '__sheet_%d' % index) + '.' + target)
            path.write_text('Marker\nFIRST\n')
            paths.append(path)
        return paths

    def check(self, source, paths, target='csv', cancel=None):
        from app_backend.legacy_preservation import check_legacy_sheet_preservation
        return check_legacy_sheet_preservation(source, target, paths, self.output,
                                               cancel=cancel or self.cancel)

    def rewrite(self, path, change):
        result = io.BytesIO()
        with ZipFile(path) as source, ZipFile(result, 'w', ZIP_DEFLATED) as target:
            for name in source.namelist():
                data = source.read(name)
                name, data = change(name, data)
                target.writestr(name, data)
        path.write_bytes(result.getvalue())

    def test_legacy_transport_does_not_publish_only_first_sheet(self):
        source = self.workbook()
        original = source.read_bytes()
        transport = ConverterTransport(recovery_parent=self.root/'legacy')
        self.addCleanup(transport.close)
        request = transport.begin('f' * 32, None)
        self.addCleanup(transport.finish, request)
        def native(path, target, work, **kwargs):
            output = work/'output'
            output.mkdir(parents=True)
            result = output/'source.csv'
            result.write_text('Marker\nFIRST\n')
            return [result]
        with patch('app_backend.converter_transport.convert_path', native):
            with self.assertRaises(LibreOfficeError) as error:
                transport.execute(request, [{'path':source, 'warnings':[]}], ['file'], ['csv'])
        self.assertEqual(error.exception.batch_report['files'][0]['targets'][0]['code'],
                         'LEGACY_OUTPUT_CONTENT_LOSS')
        self.assertFalse(transport.results)
        self.assertFalse(request.published)
        self.assertTrue((request.root/'native/0/0/output/source.csv').is_file())
        self.assertEqual(source.read_bytes(), original)

    def test_one_sheet_and_all_sheet_outputs_have_explicit_count_only_receipts(self):
        for count in (1, 2):
            with self.subTest(count=count):
                result = self.check(self.workbook(count), self.files(count))
                self.assertEqual(result['status'], 'checked_subset')
                self.assertEqual(result['sourceSheets'], count)
                self.assertEqual(result['observedOutputs'], count)
                self.assertFalse(result['valuesCompared'])
                self.assertFalse(result['qualityCompared'])

    def test_target_report_exposes_checked_and_unknown_inventory_scope(self):
        source = self.workbook(1)
        transport = ConverterTransport(recovery_parent=self.root/'legacy')
        self.addCleanup(transport.close)
        def native(path, target, work, **kwargs):
            output = work/'output'
            output.mkdir(parents=True)
            result = output/'source.csv'
            result.write_text('Marker\nFIRST\n')
            return [result]
        for index, input_file in enumerate((source, self.root/'unknown-source.bin')):
            if index:
                input_file.write_bytes(b'unknown native source')
            request = transport.begin(str(index + 1) * 32, None)
            self.addCleanup(transport.finish, request, retained=True)
            with patch('app_backend.converter_transport.convert_path', native):
                result = transport.execute(request, [{'path':input_file, 'warnings':[]}], ['file'], ['csv'])
            row = result['result']['targetReport']['files'][0]['targets'][0]
            check = row['resultChecks'][0]
            self.assertEqual(check['status'], 'checked_subset' if index == 0 else 'not_checked')
            self.assertFalse(check['qualityCompared'])

    def test_empty_additional_primary_is_not_a_sheet_content_certificate(self):
        paths = self.files(2)
        paths[-1].write_bytes(b'')
        with self.assertRaises(LibreOfficeError) as error:
            self.check(self.workbook(2), paths)
        self.assertEqual(error.exception.code, 'LEGACY_OUTPUT_VALIDATION_INCOMPLETE')

    def test_hidden_empty_sheet_is_still_part_of_source_inventory(self):
        with self.assertRaises(LibreOfficeError) as error:
            self.check(self.workbook(2, hidden=True), self.files(1))
        self.assertEqual(error.exception.code, 'LEGACY_OUTPUT_CONTENT_LOSS')

    def test_nested_primary_cannot_replace_missing_top_level_sheet(self):
        paths = self.files(1)
        nested = self.output/'sidecars'/'second.csv'
        nested.parent.mkdir()
        nested.write_text('SECOND\n')
        with self.assertRaises(LibreOfficeError) as error:
            self.check(self.workbook(2), paths + [nested])
        self.assertEqual(error.exception.code, 'LEGACY_OUTPUT_CONTENT_LOSS')

    def test_companions_are_not_sheet_primaries_and_sylk_alias_is_counted(self):
        paths = self.files(1, 'slk')
        companion = self.output/'source.cpg'
        companion.write_text('UTF-8')
        result = self.check(self.workbook(1), paths + [companion], 'sylk')
        self.assertEqual(result['observedOutputs'], 1)

    def test_moved_opc_workbook_main_part_and_strict_namespaces_are_followed(self):
        source = self.workbook(2)
        old = b'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
        strict = b'http://purl.oclc.org/ooxml/spreadsheetml/main'
        old_rel = b'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
        strict_rel = b'http://purl.oclc.org/ooxml/officeDocument/relationships'
        def change(name, data):
            if name == '[Content_Types].xml':
                data = data.replace(b'/xl/workbook.xml', b'/xl/moved.xml')
            elif name == '_rels/.rels':
                data = data.replace(b'xl/workbook.xml', b'xl/moved.xml')
            elif name == 'xl/workbook.xml':
                name = 'xl/moved.xml'
            elif name == 'xl/_rels/workbook.xml.rels':
                name = 'xl/_rels/moved.xml.rels'
            return name, data.replace(old, strict).replace(old_rel, strict_rel)
        self.rewrite(source, change)
        result = self.check(source, self.files(2))
        self.assertEqual(result['sourceSheets'], 2)

    def test_missing_workbook_sheet_relationship_cannot_be_certified(self):
        source = self.workbook(1)
        def change(name, data):
            if name == 'xl/_rels/workbook.xml.rels':
                data = b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'
            return name, data
        self.rewrite(source, change)
        with self.assertRaises(LibreOfficeError) as error:
            self.check(source, self.files(1))
        self.assertEqual(error.exception.code, 'LEGACY_OUTPUT_VALIDATION_INCOMPLETE')

    def test_known_calc_xml_budget_exhaustion_is_incomplete(self):
        with patch('app_backend.legacy_preservation.MAX_XML_BYTES', 128):
            with self.assertRaises(LibreOfficeError) as error:
                self.check(self.workbook(1), self.files(1))
        self.assertEqual(error.exception.code, 'LEGACY_OUTPUT_VALIDATION_INCOMPLETE')

    def test_cancel_and_mid_inspection_storage_codes_are_not_reclassified(self):
        source = self.workbook(1)
        paths = self.files(1)
        self.cancel.set()
        with self.assertRaises(LibreOfficeError) as error:
            self.check(source, paths)
        self.assertEqual(error.exception.code, 'CANCELLED')
        class StorageControl:
            calls = 0
            def is_set(control):
                control.calls += 1
                if control.calls == 3:
                    raise LibreOfficeError('Storage fixture', 'LEGACY_STORAGE_BYTES')
                return False
        with self.assertRaises(LibreOfficeError) as error:
            self.check(source, paths, cancel=StorageControl())
        self.assertEqual(error.exception.code, 'LEGACY_STORAGE_BYTES')

    def test_binary_unknown_source_is_explicitly_not_checked(self):
        source = self.root/'source.xls'
        source.write_bytes(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1unknown binary fixture')
        result = self.check(source, self.files(1))
        self.assertEqual(result['status'], 'not_checked')
        self.assertEqual(result['reason'], 'source_sheet_inventory_not_available')
        self.assertFalse(result['qualityCompared'])

    def test_source_extension_does_not_choose_workbook_admission(self):
        source = self.root/'document.xlsx'
        source.write_text('This is ordinary text, not a workbook.')
        result = self.check(source, self.files(1))
        self.assertEqual(result['status'], 'not_checked')

    def test_flat_odf_counts_only_direct_sheets_and_reports_missing_output(self):
        source = self.root/'table.unknown'
        source.write_text('<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
            'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
            'office:mimetype="application/vnd.oasis.opendocument.spreadsheet">'
            '<office:body><office:spreadsheet><table:table table:name="One"/>'
            '<table:table table:name="Two"/></office:spreadsheet></office:body></office:document>')
        with self.assertRaises(LibreOfficeError) as error:
            self.check(source, self.files(1))
        self.assertEqual(error.exception.code, 'LEGACY_OUTPUT_CONTENT_LOSS')
        result = self.check(source, self.files(2))
        self.assertEqual(result['sourceSheets'], 2)

    def test_ods_package_inventory_is_supported(self):
        flat = self.root/'source.fods'
        flat.write_text('<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
            'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0">'
            '<office:body><office:spreadsheet><table:table table:name="One"/>'
            '</office:spreadsheet></office:body></office:document-content>')
        packed = self.root/'source.bin'
        with ZipFile(packed, 'w') as archive:
            archive.writestr('mimetype', 'application/vnd.oasis.opendocument.spreadsheet')
            archive.writestr('content.xml', flat.read_bytes())
        result = self.check(packed, self.files(1))
        self.assertEqual(result['sourceSheets'], 1)


if __name__ == '__main__':
    unittest.main()
