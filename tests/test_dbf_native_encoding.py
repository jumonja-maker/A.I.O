"""DBF encoding is selected from complete text, never from a locale guess."""
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest

from app_backend.converter_guard.dbf_codepage import attach_native_codepages, declare_native_cp1251
from app_backend.converter_guard.dbf_schema import cli_dbf_encoding
from app_backend.converter_guard.errors import GuardError
from test_dbf_codepage import sample_dbf
from test_guard_legacy_native_v17 import Sheet, Model, functions, prop


class NativeDbfEncoding(unittest.TestCase):
    def profile(self, values):
        sheet = Sheet('data', values)
        return functions()['dbf_mixed_columns'](sheet, sheet.createCursor().getRangeAddress())

    def test_cyrillic_and_all_rows_are_checked_including_beyond_first_chunk(self):
        rows = [['NAME']] + [['Семён Ёлкин']] * 128
        self.assertTrue(self.profile(rows)['cp1251'])
        rows.append(['漢字 😀'])
        self.assertFalse(self.profile(rows)['cp1251'])

    def test_unrepresentable_literal_heading_keeps_utf8_even_when_mapping_renames_it(self):
        profile = self.profile([['Имя 😀'], ['Семён']])
        self.assertFalse(profile['cp1251'])
        self.assertEqual(profile['exportHeaders'], ['FIELD00001'])

    def test_actual_displayed_formula_error_is_checked(self):
        sheet = Sheet('data', [['NAME'], [None]])
        original = sheet.getCellByPosition
        sheet.getCellByPosition = lambda c, r: (SimpleNamespace(getString=lambda: '漢字')
                                              if r == 1 else original(c, r))
        profile = functions()['dbf_mixed_columns'](sheet, sheet.createCursor().getRangeAddress())
        self.assertFalse(profile['cp1251'])

    def test_memo_and_mixed_columns_remain_on_supported_utf8_import_contract(self):
        self.assertFalse(self.profile([['NOTE'], ['длинная строка ' * 30]])['cp1251'])
        self.assertFalse(self.profile([['MIXED'], [42], ['текст']])['cp1251'])
        declared = Sheet('data', [['NOTE,M'], ['короткий текст']])
        self.assertFalse(functions()['dbf_mixed_columns'](
            declared, declared.createCursor().getRangeAddress(), trusted_schema=True)['cp1251'])
        sheet = Sheet('data', [['MIXED'], [42], ['текст']])
        original = sheet.getCellByPosition
        sheet.getCellByPosition = lambda c, r: (SimpleNamespace(getString=lambda: '42漢字')
                                              if r == 1 else original(c, r))
        source, copy = Model(sheet), Model(Sheet('default', [['']]))
        with tempfile.TemporaryDirectory() as temporary:
            functions()['export_table_sheets'](source,
                SimpleNamespace(loadComponentFromURL=lambda *_args: copy), prop,
                Path(temporary)/'mixed.dbf', [prop('FilterName', 'dBase'),
                prop('FilterOptions', '76'), prop('InteractionHandler', object())])
        self.assertEqual({p.Name:p.Value for p in copy.exports[0][1]}['FilterOptions'], '76')
        self.assertEqual(copy.exports[0][2][1][0], '42漢字')

    def test_native_options_are_per_sheet_and_other_filters_are_unchanged(self):
        source = Model(Sheet('Cyrillic', [['NAME'], ['Ёлка']]),
                       Sheet('Unicode', [['NAME'], ['漢字 😀']]))
        copies = []
        def factory(*_args):
            copy = Model(Sheet('default', [['']]))
            copies.append(copy)
            return copy
        with tempfile.TemporaryDirectory() as temporary:
            functions()['export_table_sheets'](source,
                SimpleNamespace(loadComponentFromURL=factory), prop, Path(temporary)/'table.dbf',
                [prop('FilterName', 'dBase'), prop('FilterOptions', '76'),
                 prop('InteractionHandler', object())])
        self.assertEqual([{p.Name: p.Value for p in copy.exports[0][1]}['FilterOptions']
                          for copy in copies], ['34', '76'])
        self.assertEqual(source.sheets.sheets[0].values, [['NAME'], ['Ёлка']])
        self.assertEqual(source.sheets.sheets[1].values, [['NAME'], ['漢字 😀']])

    def test_encoding_declaration_changes_only_ldid_and_adds_matching_companion(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = sample_dbf(Path(temporary)/'native.dbf')
            before = path.read_bytes()
            declare_native_cp1251(path)
            after = path.read_bytes()
            self.assertEqual([i for i, (a, b) in enumerate(zip(before, after)) if a != b], [29])
            self.assertEqual(after[29], 0xC9)
            self.assertEqual(attach_native_codepages([path], filter_name='dBase'), [path.with_suffix('.cpg')])
            self.assertEqual(path.with_suffix('.cpg').read_bytes(), b'1251\n')
            self.assertEqual(path.read_bytes(), after)

    def test_conflicting_driver_is_not_relabelled(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = sample_dbf(Path(temporary)/'native.dbf', ldid=0x57)
            before = path.read_bytes()
            with self.assertRaises(GuardError):
                declare_native_cp1251(path)
            self.assertEqual(path.read_bytes(), before)

    def test_metadata_write_preserves_terminal_dbase_eof_in_both_native_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            for index, declare in enumerate((declare_native_cp1251,
                    lambda p: functions()['declare_native_dbf_encoding'](p, {'cp1251': True}))):
                path = sample_dbf(Path(temporary)/f'eof-{index}.dbf')
                with path.open('ab') as stream:
                    stream.write(b'\x1a')
                before = path.read_bytes()
                declare(path)
                expected = bytearray(before)
                expected[29] = 0xC9
                self.assertEqual(path.read_bytes(), bytes(expected))
                self.assertEqual(path.read_bytes()[-1:], b'\x1a')

    def test_link_rejection_precedes_metadata_write_for_both_native_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            victim = sample_dbf(Path(temporary)/'victim.dbf')
            link = Path(temporary)/'link.dbf'
            try:
                link.symlink_to(victim)
            except OSError:
                self.skipTest('symlink creation is unavailable')
            before = victim.read_bytes()
            with self.assertRaises(GuardError):
                declare_native_cp1251(link)
            with self.assertRaises(RuntimeError):
                functions()['declare_native_dbf_encoding'](link, {'cp1251': True})
            self.assertEqual(victim.read_bytes(), before)

    def test_cli_snapshot_scans_all_text_and_string_attributes_without_rewriting(self):
        prefix = ('<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
                  'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
                  'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0">'
                  '<office:styles><text:p>漢字 😀</text:p><text:tab/></office:styles>'
                  '<table:table><table:table-row>')
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/'private.fods'
            for suffix, expected in (('<text:p>Ёлка</text:p>', '34'),
                                     ('<text:p>😀</text:p>', '76'),
                                     ('<cell office:string-value="漢字"/>', '76'),
                                     ('<text:p>'+'длинная строка '*30+'</text:p>', '76'),
                                     ('<text:p>NOTE,M</text:p>', '76')):
                raw = (prefix + '<table:table-cell><text:p>Семён</text:p></table:table-cell>' * 1024
                       + '<table:table-cell>'+suffix+'</table:table-cell></table:table-row>'
                       '</table:table></office:document>').encode()
                path.write_bytes(raw)
                self.assertEqual(cli_dbf_encoding(path), expected)
                self.assertEqual(path.read_bytes(), raw)
            cancel = threading.Event()
            cancel.set()
            with self.assertRaises(GuardError) as error:
                cli_dbf_encoding(path, cancel=cancel)
            self.assertEqual(error.exception.code, 'CANCELLED')

    def test_cli_nested_whitespace_and_split_text_cannot_bypass_memo_exclusion(self):
        prefix = ('<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
                  'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
                  'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
                  '<table:table><table:table-row><table:table-cell office:value-type="string"><text:p>')
        suffix = '</text:p></table:table-cell></table:table-row></table:table></office:document>'
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/'private.fods'
            for body in ('Ёлка<text:s text:c="300"/>', 'Ёлка<text:tab/>',
                         'Ёлка<text:line-break/>',
                         '<text:span>'+'a'*130+'</text:span><text:span>'+'b'*130+'</text:span>'):
                raw = (prefix+body+suffix).encode()
                path.write_bytes(raw)
                self.assertEqual(cli_dbf_encoding(path), '76')
                self.assertEqual(path.read_bytes(), raw)


if __name__ == '__main__':
    unittest.main()
