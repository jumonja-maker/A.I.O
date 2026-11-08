"""CSV high-precision numbers and formula-like literals must survive as text."""
import csv
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from openpyxl import Workbook

from app_backend.converter_guard.csv_id_contract import CALC_TEXT, check_csv_text_ids
from app_backend.converter_guard.csv_import import CALC, csv_import_hint
from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard.export_recipe import WARNING_TEXT


NATIVE = SimpleNamespace(valid=True, preferred_filters={}, filters={
    CALC_TEXT: (CALC, {'csv', 'tab', 'tsv'}, {'IMPORT', 'EXPORT'})})


class CsvPreciseLiterals(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root/'source.csv'

    def source_rows(self, rows, delimiter=',', quoting=csv.QUOTE_MINIMAL):
        with self.source.open('w', encoding='utf-8', newline='') as stream:
            csv.writer(stream, delimiter=delimiter, quoting=quoting).writerows(rows)
        return self.source

    def workbook(self, rows, *, literals=False, target='xlsx'):
        workbook = Workbook()
        for row in rows:
            workbook.active.append(row)
        if literals:
            for row in workbook.active:
                for cell in row:
                    if isinstance(cell.value, str):
                        cell.data_type = 's'
        output = self.root/('output.'+target)
        workbook.save(output)
        workbook.close()
        return output

    def check(self, rows, *, literals=False, target='xlsx', **kwargs):
        output = self.workbook(rows, literals=literals, target=target)
        return check_csv_text_ids([output], target, self.source,
            import_filter=CALC_TEXT, import_options='44,34,76,1', **kwargs)

    def test_high_precision_numeric_tokens_receive_only_their_column_text_hint(self):
        for value in ('12345678901234567', '-12345678901234567',
                      '+12345678901234567', '123456789012345.67',
                      '.00012345678901234567', '1.2345678901234567e20'):
            with self.subTest(value=value):
                self.source_rows([['CustomerID', 'Name', 'Price'],
                                  [value, 'Alice', '14.50'], ['2', 'Борис', '7.25']])
                hint = csv_import_hint(self.source, NATIVE)
                self.assertEqual(hint['options'], '44,34,76,1,1/2/3/10')

    def test_formula_like_source_fields_are_not_enabled_as_formulas(self):
        for value in ('=1+1', ' =SUM(A1:A2)', '\t=42'):
            with self.subTest(value=value):
                self.source_rows([['Name', 'Literal'], ['Alice', value], ['Борис', 'plain']])
                self.assertEqual(csv_import_hint(self.source, NATIVE)['options'],
                                 '44,34,76,1,2/2')

    def test_normal_dates_scientific_and_short_numbers_keep_existing_native_inference(self):
        for value in ('123456789012345', '1E10', '01/02/2025', '2025-03-04',
                      '12345678901234567X', '12345678901234567 кг'):
            with self.subTest(value=value):
                self.source_rows([['Name', 'Value'], ['Alice', value], ['Борис', 'ordinary']])
                self.assertEqual(csv_import_hint(self.source, NATIVE)['options'], '44,34,76,1')

    def test_validly_quoted_fields_still_receive_precision_protection(self):
        self.source_rows([['CustomerID', 'Value'], ['12345678901234567', '001234'],
                          ['99999999999999999', '000005']], quoting=csv.QUOTE_ALL)
        self.assertEqual(csv_import_hint(self.source, NATIVE)['options'],
                         '44,34,76,1,1/2/2/2')

    def test_all_delimiters_preserve_the_same_precision_hint(self):
        for delimiter in (',', ';', '\t', '|'):
            with self.subTest(delimiter=delimiter):
                self.source_rows([['CustomerID', 'Name'], ['12345678901234567', 'Alice'],
                                  ['99999999999999999', 'Борис']], delimiter=delimiter)
                self.assertEqual(csv_import_hint(self.source, NATIVE)['options'],
                                 f'{ord(delimiter)},34,76,1,1/2')

    def test_numeric_readback_of_a_precision_sensitive_source_field_is_rejected(self):
        self.source_rows([['CustomerID', 'Name'], ['12345678901234567', 'Alice']])
        with self.assertRaises(GuardError) as caught:
            self.check([['CustomerID', 'Name'], [12345678901234600, 'Alice']])
        self.assertEqual(caught.exception.code, 'ENGINE_OUTPUT_CONTENT_LOSS')
        self.assertNotIn('12345678901234567', caught.exception.diagnostic)

    def test_unchanged_formula_string_with_formula_cell_type_is_rejected(self):
        self.source_rows([['Name', 'Literal'], ['Alice', '=1+1']])
        with self.assertRaises(GuardError) as caught:
            self.check([['Name', 'Literal'], ['Alice', '=1+1']])
        self.assertEqual(caught.exception.code, 'ENGINE_OUTPUT_CONTENT_LOSS')

    def test_sensitive_fields_after_the_bounded_sample_cannot_bypass_readback(self):
        prefix = [['CustomerID', 'Name']] + [[str(i), 'item'] for i in range(300)]
        for literal, changed in (('12345678901234567', 12345678901234600),
                                 ('=1+1', '=1+1')):
            with self.subTest(literal=literal):
                self.source_rows(prefix+[[literal, 'tail']])
                self.assertEqual(csv_import_hint(self.source, NATIVE)['options'], '44,34,76,1')
                with self.assertRaises(GuardError) as caught:
                    self.check(prefix+[[changed, 'tail']])
                self.assertEqual(caught.exception.code, 'ENGINE_OUTPUT_CONTENT_LOSS')

    def test_all_supported_excel_targets_accept_exact_literal_strings_and_explain_the_policy(self):
        rows = [['CustomerID', 'Literal', 'ID'],
                ['12345678901234567', '=1+1', '0001'],
                ['99999999999999999', 'ordinary', '0002']]
        self.source_rows(rows)
        for target in ('xlsx', 'xlsm', 'xltx', 'xltm'):
            with self.subTest(target=target):
                result = self.check(rows, literals=True, target=target)
                self.assertEqual(result['status'], 'checked_subset')
                self.assertEqual(result['candidateKinds'],
                                 {'leading_zero': 2, 'high_precision': 2, 'formula_literal': 1})
                self.assertEqual(result['idsCompared'], 5)
                self.assertFalse(result['qualityCompared'])
                self.assertFalse(result['fullDocumentFidelity'])
                self.assertIn('CSV_HIGH_PRECISION_AS_TEXT', result['warnings'])
                self.assertIn('CSV_FORMULA_LITERALS_AS_TEXT', result['warnings'])
                for warning in result['warnings']:
                    self.assertIn(warning, WARNING_TEXT)

    def test_budget_exhaustion_remains_explicitly_unverified(self):
        rows = [['CustomerID', 'Name'], ['12345678901234567', 'Alice']]
        self.source_rows(rows)
        result = self.check(rows, literals=True, seconds=0)
        self.assertEqual(result['status'], 'not_checked')
        self.assertEqual(result['warning'], 'CSV_TEXT_IDS_NOT_CHECKED')


if __name__ == '__main__':
    unittest.main()
