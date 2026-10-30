"""M31: user headings and native dBase field declarations have distinct provenance."""
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest

from test_guard_legacy_native_v17 import Sheet, Model, functions, prop
from app_backend.converter_guard.dbf_schema import validate_cli_dbf_headers
from app_backend.converter_guard.errors import GuardError


class LiteralDbfHeaders(unittest.TestCase):
    def profile(self, values, **options):
        sheet = Sheet('data', values)
        return functions()['dbf_mixed_columns'](sheet, sheet.createCursor().getRangeAddress(), **options)

    def test_metadata_looking_user_name_is_literal_even_when_all_values_are_text(self):
        profile = self.profile([['ID,C,8'], ['000123'], ['000007']])
        self.assertEqual(profile['originalHeaders'], ['ID,C,8'])
        self.assertEqual(profile['mixed'], [])
        self.assertEqual(profile['exportHeaders'], ['FIELD00001'])
        self.assertEqual(profile['renamedFields'][0]['originalName'], 'ID,C,8')

    def test_literal_numeric_suffix_does_not_authorize_numeric_coercion(self):
        profile = self.profile([['ID,N,2'], ['000123'], ['000007']])
        self.assertEqual(profile['mixed'], [])
        self.assertNotIn(',', profile['exportHeaders'][0])

    def test_native_import_retains_typed_declarations(self):
        profile = self.profile([['ID,C,8', 'AMOUNT,N,12,3', 'DATE,D', 'FLAG,L', 'NOTE,M'],
                                ['000123', -12.75, 45000, 1, 'текст']], trusted_schema=True)
        self.assertEqual(profile['renamedFields'], [])
        self.assertEqual(profile['exportHeaders'], profile['originalHeaders'])
        self.assertTrue(profile['trustedSchema'])

    def test_duplicate_long_cyrillic_and_reserved_generated_names_stay_unique(self):
        names = ['ID', 'id', 'Русское название', 'long name longer than ten', 'FIELD00002']
        profile = self.profile([names, ['a'] * len(names)])
        exported = profile['exportHeaders']
        self.assertEqual(len({v.upper() for v in exported}), len(names))
        self.assertEqual(exported[4], 'FIELD00002')
        self.assertTrue(all(len(v.encode('ascii')) <= 10 for v in exported))
        self.assertEqual(profile['originalHeaders'], names)

    def test_unconfirmed_native_metadata_fails_instead_of_removing_a_suffix(self):
        with self.assertRaisesRegex(RuntimeError, 'DBF_SCHEMA_HEADER_UNCONFIRMED'):
            self.profile([['ID,C,8,literal'], ['000123']], trusted_schema=True)

    def test_private_copy_and_mapping_keep_entire_literal_name_and_source_values(self):
        values = [['ID,C,8', 'Mixed,N,2'], ['000123', 42], ['000007', '00042']]
        source = Model(Sheet('data', values))
        copy = Model(Sheet('default', [['']]))
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'literal.dbf'
            functions()['export_table_sheets'](source,
                SimpleNamespace(loadComponentFromURL=lambda *args: copy), prop, target,
                [prop('FilterName', 'dBase'), prop('InteractionHandler', object())])
            metadata = json.loads(target.with_name(target.name + '.aio-columns.jsonl').read_text().splitlines()[0])
        self.assertEqual(source.sheets.sheets[0].values, values)
        self.assertEqual(copy.exports[0][2], [['FIELD00001', 'FIELD00002,M'],
                                             ['000123', '42'], ['000007', '00042']])
        self.assertEqual(metadata['originalHeaders'], ['ID,C,8', 'Mixed,N,2'])
        self.assertEqual(metadata['fieldNames'], ['FIELD00001', 'FIELD00002'])
        self.assertIn('NATIVE_DBF_FIELD_NAME_MAPPING', metadata['warnings'])
        self.assertTrue(copy.closed)

    def cli(self, names, **options):
        from xml.sax.saxutils import escape
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'snapshot.fods'
            raw = ('<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
                   'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
                   'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"><table:table><table:table-row>' +
                   ''.join('<table:table-cell office:value-type="string"><text:p>'+escape(name)+
                           '</text:p></table:table-cell>' for name in names) +
                   '</table:table-row></table:table></office:document>')
            path.write_text(raw, encoding='utf-8')
            before = path.read_bytes()
            try:
                return validate_cli_dbf_headers(path, **options)
            finally:
                self.assertEqual(path.read_bytes(), before)

    def test_cli_literal_metadata_duplicate_and_unrepresentable_names_fail_closed(self):
        for names in (['ID,C,8'], ['ID', 'id'], ['Фамилия'], ['Long field name']):
            with self.subTest(names=names), self.assertRaises(GuardError) as error:
                self.cli(names)
            self.assertEqual(error.exception.code, 'ENGINE_OUTPUT_VALIDATION_INCOMPLETE')

    def test_cli_safe_names_and_confirmed_native_schema_are_admitted(self):
        self.cli(['ID', 'Amount'])
        self.cli(['ID,C,8', 'AMOUNT,N,12,3', 'DATE,D', 'FLAG,L', 'MEMO,M'], trusted_schema=True)

    def test_cli_cancellation_does_not_rewrite_snapshot(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(GuardError) as error:
            self.cli(['ID'], cancel=cancel)
        self.assertEqual(error.exception.code, 'CANCELLED')


if __name__ == '__main__':
    unittest.main()
