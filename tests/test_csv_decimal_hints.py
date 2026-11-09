"""M24: locale-safe decimal hints must not disable the original ID contract."""
import builtins
import csv
import io
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app_backend.converter_guard.csv_numeric_columns import decimal_column_hints
from app_backend.converter_guard.csv_import import csv_import_hint
from app_backend.converter_guard.csv_id_contract import (
    CALC_TEXT, _import_dialect, check_csv_text_ids, text_columns_in_sample)
from app_backend.converter_guard.errors import GuardError


class DecimalHintTests(unittest.TestCase):
    def test_money_columns_and_ids_are_separate(self):
        rows = [['ID', 'Name', 'Quantity', 'Price', 'Total'],
                ['00001', 'A', '3', '14.50', '43.50'],
                ['00002', 'B', '2', '7.25', '14.50']]
        text = text_columns_in_sample(rows, 5)
        self.assertEqual(text, '1/2')
        self.assertEqual(decimal_column_hints(rows, 5, text), '1/2/4/10/5/10')

    def test_headerless_and_blank_numbers(self):
        rows = [['00001', '-14.50'], ['00002', ''], ['00003', '+7.25']]
        self.assertEqual(decimal_column_hints(rows, 2, '1/2'), '1/2/2/10')

    def test_ambiguous_columns_keep_native_behavior(self):
        for values in (['1.234', '2.567'], ['14,50', '7,25'],
                       ['14.50', '7,25'], ['2026-10-01', '2026-10-02'],
                       ['1e3', '2e3'], ['=1+2', '=2+3'], ['001.20', '002.40'],
                       ['1234567890123456.50', '2.25'], ['14.50', 'unavailable'],
                       ['14.50', '2'], ['14.50 ', '7.25']):
            with self.subTest(values=values):
                rows = [['ID', 'Value'], ['001', values[0]], ['002', values[1]]]
                self.assertEqual(decimal_column_hints(rows, 2, '1/2'), '1/2')

    def test_explicit_text_hint_has_priority(self):
        rows = [['ID', 'Price'], ['001', '14.50'], ['002', '7.25']]
        self.assertEqual(decimal_column_hints(rows, 2, '1/2/2/2'), '1/2/2/2')

    def test_ragged_oversized_and_tiny_samples_are_not_reinterpreted(self):
        for rows, width in (([['A', 'B'], ['1']], 2),
                            ([['14.50'], ['7.25']], True),
                            ([['14.50']], 1),
                            ([['14.50']] * 257, 1),
                            ([['14.50'] * 257] * 2, 257)):
            with self.subTest(width=width, rows=len(rows)):
                self.assertEqual(decimal_column_hints(rows, width, '1/2'), '1/2')

    def test_hint_count_remains_inside_id_validation_budget(self):
        rows = [['Price'] * 100, ['14.50'] * 100, ['7.25'] * 100]
        hints = decimal_column_hints(rows, 100)
        self.assertEqual(len(hints.split('/')), 128)
        self.assertEqual(_import_dialect(CALC_TEXT, '44,34,76,1,' + hints), ',')

    def test_dialect_accepts_numeric_hints_not_skip_or_date_codes(self):
        for sep, expected in (('44', ','), ('9', '\t'), ('59', ';'), ('124', '|')):
            self.assertEqual(_import_dialect(CALC_TEXT, sep + ',34,76,1,1/2/4/10'), expected)
        for columns in ('1/9', '1/5', '1/10/1/2', '257/10', '1/10/0/2', '1/10/2'):
            with self.subTest(columns=columns):
                self.assertIsNone(_import_dialect(CALC_TEXT, '44,34,76,1,' + columns))

    def test_hint_has_no_io(self):
        rows = [['Price'], ['14.50'], ['7.25']]
        with patch.object(builtins, 'open', side_effect=AssertionError('extra IO')), \
             patch.object(Path, 'open', side_effect=AssertionError('extra IO')):
            self.assertEqual(decimal_column_hints(rows, 1), '1/10')

    def test_csv_tab_tsv_keep_single_bounded_source_read(self):
        native = SimpleNamespace(valid=True, preferred_filters={},
            filters={CALC_TEXT: ('com.sun.star.sheet.SpreadsheetDocument', {'csv', 'tab', 'tsv'}, {'IMPORT', 'EXPORT'})})
        class Counted(io.BytesIO):
            calls = 0
            largest = 0
            def read(self, size=-1):
                self.calls += 1
                self.largest = max(self.largest, size)
                return super().read(size)
        for delimiter in (',', '\t', ';', '|'):
            content = io.StringIO(newline='')
            writer = csv.writer(content, delimiter=delimiter)
            writer.writerows([['ID', 'Price'], ['00001', '14.50'], ['00002', '7.25']])
            stream = Counted(content.getvalue().encode('utf-8'))
            with patch.object(Path, 'open', return_value=stream) as opened:
                result = csv_import_hint(Path('unlisted.data'), native)
            self.assertEqual(opened.call_count, 1)
            self.assertEqual((stream.calls, stream.largest), (1, 65537))
            self.assertEqual(result['options'], f'{ord(delimiter)},34,76,1,1/2/2/10')

    def test_new_hints_cannot_bypass_id_loss_detection(self):
        from openpyxl import Workbook
        with TemporaryDirectory() as temp:
            source, output = Path(temp)/'source.csv', Path(temp)/'result.xlsx'
            source.write_text('ID,Price\n00001,14.50\n00002,7.25\n', encoding='utf-8')
            book = Workbook(); sheet = book.active
            sheet.append(['ID', 'Price']); sheet.append([1, 14.5]); sheet.append([2, 7.25])
            book.save(output); book.close()
            with self.assertRaises(GuardError) as caught:
                check_csv_text_ids([output], 'xlsx', source, import_filter=CALC_TEXT,
                                   import_options='44,34,76,1,1/2/2/10')
            self.assertEqual(caught.exception.code, 'ENGINE_OUTPUT_CONTENT_LOSS')
            book = Workbook(); sheet = book.active
            sheet.append(['ID', 'Price']); sheet.append(['00001', 14.5]); sheet.append(['00002', 7.25])
            book.save(output); book.close()
            result = check_csv_text_ids([output], 'xlsx', source, import_filter=CALC_TEXT,
                                       import_options='44,34,76,1,1/2/2/10')
            self.assertEqual(result['status'], 'checked_subset')
            self.assertEqual(result['idsCompared'], 2)


if __name__ == '__main__':
    unittest.main()
