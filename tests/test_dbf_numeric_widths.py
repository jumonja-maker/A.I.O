"""Numeric DBF schema preserves signed decimals and native column semantics."""
from decimal import Decimal
from pathlib import Path
import struct
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app_backend.converter_guard.dbf_numeric import DbfNumericColumn, verify_dbf_numeric_rows
from app_backend.converter_guard.dbf_snapshot import prepare_numeric_snapshot, verify_numeric_snapshot
from app_backend.converter_guard.errors import GuardError
from test_guard_legacy_native_v17 import Sheet, Model, functions, prop


def formats():
    return SimpleNamespace(getByKey=lambda key: SimpleNamespace(getPropertyValue=lambda _name: key))


def sheet_with_formats(values, kinds):
    sheet = Sheet('data', values)
    original = sheet.getCellByPosition
    def cell(column, row):
        value = original(column, row)
        value.getPropertyValue = lambda _name: kinds[column]
        return value
    sheet.getCellByPosition = cell
    return sheet


class NumericWidths(unittest.TestCase):
    def test_common_scale_accounts_for_sign_magnitude_and_scientific_values(self):
        for values, expected in (([-12.75, .125], (7, 3)),
                                 ([42.0, 7.0], (2, 0)),
                                 ([-999.99, .001], (8, 3)),
                                 ([1e8, .001], (13, 3)),
                                 ([-0.0, .125], (6, 3))):
            profile = DbfNumericColumn()
            for value in values:
                profile.add(value)
            self.assertEqual(profile.schema(), expected)
        self.assertEqual(profile.schema(minimum_width=12, minimum_scale=5), (12, 5))

    def test_unsupported_precision_is_explicit(self):
        for value in ('1e30', '1e19', '0.0000000000000001', 'NaN', 'Infinity'):
            profile = DbfNumericColumn()
            with self.subTest(value=value), self.assertRaises(ValueError):
                profile.add(value)
                profile.schema()

    def test_native_formats_protect_date_logical_and_text_columns(self):
        values = [['ID', 'AMOUNT', 'DATE', 'FLAG', 'PERCENT', 'MIXED'],
                  ['000123', -12.75, 45000, 1, .125, 42],
                  ['000007', .125, 45001, 0, .5, '00042']]
        sheet = sheet_with_formats(values, [16, 16, 2, 1024, 128, 16])
        profile = functions()['dbf_mixed_columns'](sheet, sheet.createCursor().getRangeAddress(), number_formats=formats())
        self.assertEqual(profile['numericFields'], [{'column': 1, 'fieldName': 'AMOUNT', 'width': 7, 'scale': 3}])
        self.assertEqual(profile['exportHeaders'], ['ID', 'AMOUNT,N,7,3', 'DATE', 'FLAG', 'PERCENT', 'MIXED,M'])
        self.assertEqual(values[1][0], '000123')

    def test_later_chunk_and_trusted_native_width_are_preserved(self):
        rows = [['AMOUNT,N,12,5']] + [[.125]]*128 + [[-999.99]]
        sheet = sheet_with_formats(rows, [16])
        profile = functions()['dbf_mixed_columns'](sheet, sheet.createCursor().getRangeAddress(),
                                                 number_formats=formats(), trusted_schema=True)
        self.assertEqual(profile['exportHeaders'], ['AMOUNT,N,12,5'])

    def test_private_numeric_copy_and_headerless_first_record(self):
        for values, expected in (([['AMOUNT'], [-12.75], [.125]], [['AMOUNT,N,7,3'], [-12.75], [.125]]),
                                 ([[-12.75], [.125]], [['FIELD1,N,7,3'], [-12.75], [.125]])):
            source = Model(sheet_with_formats(values, [16]))
            source.getNumberFormats = formats
            copy = Model(Sheet('default', [['']]))
            namespace = functions()
            captured = []
            namespace['verify_dbf_numeric_rows'] = lambda _path, _fields, rows: captured.extend(list(rows))
            with tempfile.TemporaryDirectory() as temporary:
                namespace['export_table_sheets'](source,
                    SimpleNamespace(loadComponentFromURL=lambda *_args: copy), prop,
                    Path(temporary)/'table.dbf', [prop('FilterName', 'dBase'), prop('InteractionHandler', object())])
            self.assertEqual(copy.exports[0][2], expected)
            self.assertEqual(source.sheets.sheets[0].values, values)
            self.assertEqual(captured, [(-12.75,), (.125,)])

    def test_snapshot_changes_only_numeric_header_cells(self):
        raw = (b'<o:document xmlns:o="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
               b'xmlns:t="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
               b'xmlns:x="urn:oasis:names:tc:opendocument:xmlns:text:1.0"><t:table><t:table-row>'
               b'<t:table-cell o:value-type="string"><x:p>ID</x:p></t:table-cell>'
               b'<t:table-cell o:value-type="string"><x:p>AMOUNT</x:p></t:table-cell></t:table-row>'
               b'<t:table-row><t:table-cell o:value-type="string"><x:p>000123</x:p></t:table-cell>'
               b'<t:table-cell o:value-type="float" o:value="-12.75"><x:p>-12.75</x:p></t:table-cell></t:table-row>'
               b'<t:table-row><t:table-cell o:value-type="string"><x:p>000007</x:p></t:table-cell>'
               b'<t:table-cell o:value-type="float" o:value="0.125"><x:p>0.125</x:p></t:table-cell></t:table-row>'
               b'</t:table></o:document>')
        with tempfile.TemporaryDirectory() as temporary:
            source, target = Path(temporary)/'source.fods', Path(temporary)/'copy.fods'
            source.write_bytes(raw)
            prepared, plan = prepare_numeric_snapshot(source, target, cancel=threading.Event())
            self.assertEqual(prepared, target)
            self.assertEqual(plan['numericFields'], [{'column': 1, 'fieldName': 'AMOUNT', 'width': 7, 'scale': 3}])
            self.assertEqual(plan['sourceRows'], 3)
            self.assertIn(b'AMOUNT,N,7,3', target.read_bytes())
            self.assertEqual(target.read_bytes().split(b'</t:table-row>', 1)[1], raw.split(b'</t:table-row>', 1)[1])
            self.assertEqual(source.read_bytes(), raw)

    def test_exact_readback_rejects_rounding_even_when_schema_is_valid(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/'numeric.dbf'
            head, field = bytearray(32), bytearray(32)
            head[0] = 3
            struct.pack_into('<IHH', head, 4, 2, 65, 8)
            field[:6] = b'AMOUNT'
            field[11], field[16], field[17] = ord('N'), 7, 3
            path.write_bytes(head+field+b'\r -12.750   0.125\x1a')
            fields = [{'column': 0, 'fieldName': 'AMOUNT', 'width': 7, 'scale': 3}]
            check = verify_dbf_numeric_rows(path, fields, [[Decimal('-12.750')], [Decimal('.125')]])
            self.assertEqual(check['checkedCells'], 2)
            with self.assertRaisesRegex(ValueError, 'VALUE_CHANGED'):
                verify_dbf_numeric_rows(path, fields, [[Decimal('-12.750')], [Decimal('.1251')]])

    def test_repeated_native_rows_remain_cancellable_during_verification(self):
        raw = ('<o:document xmlns:o="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
               'xmlns:t="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
               'xmlns:x="urn:oasis:names:tc:opendocument:xmlns:text:1.0"><t:table>'
               '<t:table-row><t:table-cell o:value-type="string"><x:p>AMOUNT</x:p>'
               '</t:table-cell></t:table-row><t:table-row t:number-rows-repeated="1000000">'
               '<t:table-cell o:value-type="float" o:value="1"><x:p>1</x:p>'
               '</t:table-cell></t:table-row></t:table></o:document>')
        cancel = threading.Event()
        def consume(_path, _fields, rows):
            self.assertEqual(next(rows), ['1'])
            cancel.set()
            next(rows)
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)/'repeated.fods'
            source.write_text(raw)
            with patch('app_backend.converter_guard.dbf_snapshot.verify_dbf_numeric_rows', consume):
                with self.assertRaises(GuardError) as error:
                    verify_numeric_snapshot(source, None,
                        {'sourceRows':1000001,'fieldNames':['AMOUNT'],'numericFields':[]}, cancel=cancel)
                self.assertEqual(error.exception.code, 'CANCELLED')


if __name__ == '__main__':
    unittest.main()
