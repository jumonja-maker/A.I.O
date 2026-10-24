"""Independent QA handles native memo companions and locale-formatted text."""
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from xml.etree import ElementTree as ET

FILE = Path(__file__).resolve().parents[1] / 'tools/qa/guard_v17_native_acceptance.py'
SPEC = importlib.util.spec_from_file_location('guard_v17_native_qa_checker', FILE)
qa = importlib.util.module_from_spec(SPEC)
sys.path.insert(0, str(FILE.parent))
try:
    SPEC.loader.exec_module(qa)
finally:
    sys.path.remove(str(FILE.parent))


class NativeQAInspectorV17(unittest.TestCase):
    def make_readback(self, directory, *, value='5622.13', value_type='float', display='5\u202f622,13'):
        root = ET.Element('{%s}document' % qa.OFFICE)
        body = ET.SubElement(root, '{%s}body' % qa.OFFICE)
        spreadsheet = ET.SubElement(body, '{%s}spreadsheet' % qa.OFFICE)
        table = ET.SubElement(spreadsheet, '{%s}table' % qa.TABLE)
        row = ET.SubElement(table, '{%s}table-row' % qa.TABLE)
        label = ET.SubElement(row, '{%s}table-cell' % qa.TABLE,
                              {'{%s}value-type' % qa.OFFICE: 'string'})
        ET.SubElement(label, '{urn:oasis:names:tc:opendocument:xmlns:text:1.0}p').text = 'Сумма'
        attributes = {'{%s}value-type' % qa.OFFICE: value_type}
        if value is not None:
            attributes['{%s}value' % qa.OFFICE] = value
        numeric = ET.SubElement(row, '{%s}table-cell' % qa.TABLE, attributes)
        ET.SubElement(numeric, '{urn:oasis:names:tc:opendocument:xmlns:text:1.0}p').text = display
        path = Path(directory) / 'readback.fods'
        ET.ElementTree(root).write(path, encoding='utf-8', xml_declaration=True)
        return path

    def test_dbf_memo_companions_are_retained_without_counting_as_sheets(self):
        outputs = [Path('first.dbf'), Path('first.dbt'), Path('first__sheet_002.dbf'),
                   Path('first__sheet_002.dbt')]
        primaries, companions = qa.primary_table_outputs(outputs, 'dbf', 2)
        self.assertEqual(primaries, [outputs[0], outputs[2]])
        self.assertEqual(companions, [outputs[1], outputs[3]])

    def test_companion_cannot_hide_a_missing_actual_sheet(self):
        with self.assertRaisesRegex(AssertionError, 'sheet primaries') as failure:
            qa.primary_table_outputs([Path('one.dbf'), Path('one.dbt')], 'dbf', 2)
        self.assertIn('one.dbt', str(failure.exception))

    def test_locale_display_does_not_change_canonical_numeric_readback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.make_readback(directory)
            result = qa.check_native_table_readback(path, ['Сумма'], [('Сумма', Decimal('5622.13'))])
        self.assertEqual(result['numericValues'], 1)
        self.assertEqual(result['numericReadback'][0]['displayText'], '5\u202f622,13')
        self.assertEqual(result['numericReadback'][0]['actual'], '5622.13')

    def test_zeroed_cached_formula_is_a_failure_with_the_actual_value(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.make_readback(directory, value='0', display='0')
            with self.assertRaises(AssertionError) as failure:
                qa.check_native_table_readback(path, ['Сумма'], [('Сумма', Decimal('5622.13'))])
        self.assertIn("observedOfficeValue='0'", str(failure.exception))
        self.assertIn("observedDisplayText='0'", str(failure.exception))

    def test_number_changed_to_text_or_formula_error_is_rejected(self):
        for display in ('5622,13', '#DIV/0!'):
            with self.subTest(display=display), tempfile.TemporaryDirectory() as directory:
                path = self.make_readback(directory, value=None, value_type='string', display=display)
                with self.assertRaises(AssertionError) as failure:
                    qa.check_native_table_readback(path, ['Сумма'], [('Сумма', Decimal('5622.13'))])
                self.assertIn("observedType='string'", str(failure.exception))
                self.assertIn(display, str(failure.exception))

    def test_locale_in_machine_numeric_attribute_is_not_guessed_or_silenced(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.make_readback(directory, value='5622,13')
            with self.assertRaisesRegex(AssertionError, 'not canonical') as failure:
                qa.check_native_table_readback(path, ['Сумма'], [('Сумма', Decimal('5622.13'))])
        self.assertIn("observedOfficeValue='5622,13'", str(failure.exception))

    def test_proven_mixed_dbf_column_retains_exact_numeric_text_and_reports_coercion(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.make_readback(directory, value=None, value_type='string', display='5622.13')
            result = qa.check_native_table_readback(path, ['Сумма'], [('Сумма', Decimal('5622.13'))],
                allow_dbf_mixed_numeric_text=True)
        self.assertEqual(result['numericReadback'][0]['actual'], '5622.13')
        self.assertEqual(result['numericReadback'][0]['typeCoercion'], 'native_DBF_mixed_column_numeric_as_text')

    def test_mixed_dbf_exception_never_accepts_zero_errors_or_ambiguous_locale_text(self):
        for value in ('0', '#DIV/0!', '5.622,13', '5622,13'):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                path = self.make_readback(directory, value=None, value_type='string', display=value)
                with self.assertRaises(AssertionError) as failure:
                    qa.check_native_table_readback(path, ['Сумма'], [('Сумма', Decimal('5622.13'))],
                        allow_dbf_mixed_numeric_text=True)
                self.assertIn(value, str(failure.exception))

    def test_localized_dbf_memo_requires_matching_typed_recovery_and_text(self):
        localized = '5\u00a0622,13 ₽'
        control = {'Сумма': {'sourceValue': '5622.13', 'exportedText': localized,
                             'sidecar': 'table.dbf.aio-columns.jsonl', 'row': 4, 'column': 1}}
        with tempfile.TemporaryDirectory() as directory:
            path = self.make_readback(directory, value=None, value_type='string', display=localized)
            result = qa.check_native_table_readback(path, ['Сумма'], [('Сумма', Decimal('5622.13'))],
                allow_dbf_mixed_numeric_text=True, dbf_memo_controls=control)
            self.assertEqual(result['numericReadback'][0]['actual'], '5622.13')
            self.assertEqual(result['numericReadback'][0]['typedRecovery']['exportedText'], localized)
            changed = self.make_readback(directory, value=None, value_type='string', display='0')
            with self.assertRaisesRegex(AssertionError, 'differs from typed recovery'):
                qa.check_native_table_readback(changed, ['Сумма'], [('Сумма', Decimal('5622.13'))],
                    allow_dbf_mixed_numeric_text=True, dbf_memo_controls=control)

    def test_dbf_recovery_source_value_is_compared_with_original_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.make_readback(directory, display='5622.13')
            primary = root / 'table.dbf'
            sidecar = root / 'table.dbf.aio-columns.jsonl'
            header = {'schemaVersion': 1, 'format': 'dBase', 'memoColumns': [1]}
            row = {'row': 0, 'column': 1, 'sourceType': 'number', 'sourceValue': 0, 'exportedText': '0'}
            sidecar.write_text(json.dumps(header) + '\n' + json.dumps(row) + '\n', encoding='utf-8')
            with self.assertRaisesRegex(AssertionError, 'changed source value'):
                qa.dbf_numeric_controls(source, 0, primary)


if __name__ == '__main__':
    unittest.main()
