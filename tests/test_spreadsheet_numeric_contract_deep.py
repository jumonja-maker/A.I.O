"""Independent exact-number controls for declared OOXML numeric literals."""
import io
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

from openpyxl import Workbook

from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard.postconditions import SourceEvidence, check_native_output
from app_backend.converter_guard import spreadsheet_numeric_contract as numbers
from app_backend.converter_transport import ConverterTransport
from app_backend.libreoffice_engine import LibreOfficeError


NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'


def workbook(path, sheets, *, formulas=None, types=None):
    """Set raw numeric XML independently of openpyxl's numeric serializer."""
    book = Workbook(); book.remove(book.active)
    for name, cells in sheets.items():
        sheet = book.create_sheet(name)
        for coordinate in cells:
            sheet[coordinate] = 1
    stream = io.BytesIO(); book.save(stream); book.close()
    with zipfile.ZipFile(io.BytesIO(stream.getvalue())) as original, zipfile.ZipFile(path, 'w') as target:
        for item in original.infolist():
            payload = original.read(item)
            for index, (name, cells) in enumerate(sheets.items(), 1):
                if item.filename == f'xl/worksheets/sheet{index}.xml':
                    root = ET.fromstring(payload)
                    for cell in root.findall('.//{' + NS + '}c'):
                        coordinate = cell.get('r')
                        cell.set('t', (types or {}).get((name, coordinate), 'n'))
                        cell.find('{' + NS + '}v').text = cells[coordinate]
                        if (name, coordinate) in (formulas or {}):
                            attrs = formulas[name, coordinate]
                            formula = ET.Element('{' + NS + '}f', attrs)
                            formula.text = '1+1'
                            cell.insert(0, formula)
                    payload = ET.tostring(root)
            target.writestr(item, payload)
    return path


class NumericLiterals(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root/'source.xlsx'; self.output = self.root/'output.xlsx'
        self.sheets = {'Data': {'B2': '10000000000000002', 'B3': '12345678901234567'}}
        workbook(self.source, self.sheets)
        self.cancel = threading.Event()

    def check(self, sheets=None, **kwargs):
        workbook(self.output, self.sheets if sheets is None else sheets, **kwargs.pop('book_options', {}))
        return numbers.check_spreadsheet_numeric_literals([self.output], 'xlsx', self.source,
                                                         cancel=self.cancel, **kwargs)

    def test_changed_exact_integers_are_rejected_without_private_values(self):
        with self.assertRaises(GuardError) as caught:
            self.check({'Data': {'B2': '10000000000000000', 'B3': '12345678901234600'}})
        self.assertEqual(caught.exception.code, 'ENGINE_OUTPUT_CONTENT_LOSS')
        self.assertNotIn('10000000000000002', caught.exception.diagnostic)
        self.assertNotIn('Data', caught.exception.diagnostic)

    def test_exact_integer_semantics_accept_equivalent_exponent_tokens(self):
        result = self.check({'Data': {'B2': '1.0000000000000002e16', 'B3': '12345678901234567.0'}})
        self.assertEqual(result['status'], 'checked_subset')
        self.assertEqual(result['cellsCompared'], 2)
        self.assertFalse(result['fullWorkbookFidelity'])
        self.assertFalse(result['formulaCachesCompared'])

    def test_nonintegral_lexical_roundtrip_uses_same_ieee_double(self):
        workbook(self.source, {'Data': {'B2': '0.10000000000000001'}})
        self.assertEqual(self.check({'Data': {'B2': '0.1'}})['cellsCompared'], 1)
        workbook(self.source, {'Data': {'B2': '0.10000000000000002'}})
        with self.assertRaises(GuardError):
            self.check({'Data': {'B2': '0.1'}})

    def test_string_or_formula_is_not_the_original_numeric_cell_type(self):
        for options in ({'types': {('Data', 'B2'): 'str'}},
                        {'formulas': {('Data', 'B2'): {}}}):
            with self.subTest(options=options), self.assertRaises(GuardError):
                self.check(book_options=options)

    def test_missing_coordinate_or_changed_sheet_identity_is_rejected(self):
        for sheets in ({'Data': {'B3': '12345678901234567'}},
                       {'Renamed': self.sheets['Data']}):
            with self.subTest(sheets=sheets), self.assertRaises(GuardError):
                self.check(sheets)

    def test_all_worksheets_are_mapped_by_name_not_active_sheet(self):
        source = {'Data': {'B2': '10000000000000002'}, 'Hidden': {'B2': '12345678901234567'}}
        workbook(self.source, source)
        with self.assertRaises(GuardError):
            self.check({'Hidden': {'B2': '12345678901234600'}, 'Data': {'B2': '10000000000000002'}})
        self.assertEqual(self.check({'Hidden': source['Hidden'], 'Data': source['Data']})['cellsCompared'], 2)

    def test_source_formulas_and_array_cache_followers_are_not_literal_evidence(self):
        source = {'Data': {'A1': '12345678901234567', 'A2': '12345678901234567',
                           'B2': '10000000000000002'}}
        for kind in ('array', 'shared', 'dataTable'):
            with self.subTest(kind=kind):
                workbook(self.source, source, formulas={('Data', 'A1'): {'t': kind, 'ref': 'A1:A2'}})
                result = self.check({'Data': {'A1': '2', 'A2': '2', 'B2': '10000000000000002'}})
                self.assertEqual(result['cellsCompared'], 1)

    def test_formula_ranges_do_not_expand_million_row_grids(self):
        source = {'Data': {'A1': '12345678901234567', 'A2': '12345678901234567'}}
        workbook(self.source, source, formulas={('Data', 'A1'): {'t': 'array', 'ref': 'A1:XFD1048576'}})
        self.assertIsNone(self.check({'Data': {'A1': '2', 'A2': '2'}}))

    def test_nonstandard_sheet_part_relationship_is_resolved(self):
        with zipfile.ZipFile(self.source) as archive:
            files = {name: archive.read(name) for name in archive.namelist()}
        files['xl/custom/numeric.xml'] = files.pop('xl/worksheets/sheet1.xml')
        files['xl/_rels/workbook.xml.rels'] = files['xl/_rels/workbook.xml.rels'].replace(
            b'/xl/worksheets/sheet1.xml', b'custom/numeric.xml')
        files['[Content_Types].xml'] = files['[Content_Types].xml'].replace(
            b'/xl/worksheets/sheet1.xml', b'/xl/custom/numeric.xml')
        with zipfile.ZipFile(self.source, 'w') as archive:
            for name, payload in files.items():
                archive.writestr(name, payload)
        self.assertEqual(self.check()['cellsCompared'], 2)

    def test_malformed_formula_range_stays_unverified(self):
        workbook(self.source, self.sheets,
                 formulas={('Data', 'B2'): {'t': 'array', 'ref': 'not-a-cell-range'}})
        self.assertEqual(self.check()['status'], 'not_checked')

    def test_output_formula_range_cannot_replace_an_original_literal(self):
        workbook(self.source, {'Data': {'A2': '10000000000000002'}})
        with self.assertRaises(GuardError):
            self.check({'Data': {'A1': '10000000000000002', 'A2': '10000000000000002'}},
                       book_options={'formulas': {('Data', 'A1'): {'t': 'array', 'ref': 'A1:A2'}}})

    def test_filename_is_not_source_admission_and_other_targets_remain_unconstrained(self):
        renamed = self.root/'numeric.bin'; shutil.copy2(self.source, renamed); self.source = renamed
        self.assertEqual(self.check()['cellsCompared'], 2)
        self.assertIsNone(numbers.check_spreadsheet_numeric_literals([], 'pdf', self.source))

    def test_budget_is_explicitly_unknown_and_cancel_or_storage_errors_propagate(self):
        self.assertEqual(self.check(seconds=0)['status'], 'not_checked')
        self.cancel.set()
        with self.assertRaises(GuardError) as caught:
            self.check()
        self.assertEqual(caught.exception.code, 'CANCELLED')
        class StorageControl:
            def is_set(self):
                raise LibreOfficeError('Quota', 'LEGACY_STORAGE_BYTES')
        with self.assertRaises(LibreOfficeError) as storage:
            numbers.check_spreadsheet_numeric_literals([], 'xlsx', self.source, cancel=StorageControl())
        self.assertEqual(storage.exception.code, 'LEGACY_STORAGE_BYTES')

    def test_source_subset_is_cached_but_every_output_is_reread(self):
        evidence = SourceEvidence(self.source)
        with patch.object(numbers, '_source', wraps=numbers._source) as source_scan:
            self.assertEqual(self.check(source_evidence=evidence)['cellsCompared'], 2)
            with self.assertRaises(GuardError):
                self.check({'Data': {'B2': '10000000000000000', 'B3': '12345678901234567'}},
                           source_evidence=evidence)
            self.assertEqual(source_scan.call_count, 1)

    def test_guard_postconditions_reject_the_known_numeric_change(self):
        workbook(self.output, {'Data': {'B2': '10000000000000000', 'B3': '12345678901234600'}})
        with self.assertRaises(GuardError) as caught:
            check_native_output([self.output], 'xlsx', self.source, cancel=self.cancel)
        self.assertEqual(caught.exception.code, 'ENGINE_OUTPUT_CONTENT_LOSS')

    def test_legacy_cannot_publish_the_known_numeric_change(self):
        workbook(self.output, {'Data': {'B2': '10000000000000000', 'B3': '12345678901234600'}})
        output_bytes = self.output.read_bytes()
        transport = ConverterTransport(recovery_parent=self.root/'legacy')
        self.addCleanup(transport.close)
        request = transport.begin('a'*32, None)
        def native(_source, _target, workspace, **kwargs):
            folder = workspace/'output'; folder.mkdir(parents=True)
            path = folder/'result.xlsx'; path.write_bytes(output_bytes)
            return [path]
        try:
            with patch('app_backend.converter_transport.convert_path', side_effect=native):
                with self.assertRaises(LibreOfficeError) as caught:
                    transport.execute(request, [{'path': self.source, 'warnings': []}], ['file'], ['xlsx'])
            self.assertEqual(caught.exception.batch_report['files'][0]['targets'][0]['code'],
                             'LEGACY_OUTPUT_CONTENT_LOSS')
            self.assertFalse(transport.results)
            self.assertFalse(request.published)
            self.assertTrue(self.source.exists())
        finally:
            transport.finish(request, retained=request.published)


if __name__ == '__main__':
    unittest.main()
