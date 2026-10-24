"""Private explicit-column import must preserve data and all source bytes."""
import hashlib
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard.odf_table_import import prepare_odf_table_source

O = 'urn:oasis:names:tc:opendocument:xmlns:office:1.0'
T = 'urn:oasis:names:tc:opendocument:xmlns:table:1.0'
P = 'urn:oasis:names:tc:opendocument:xmlns:text:1.0'


def table(rows, columns='', name='table'):
    return ('<t:table t:name="' + name + '">' + columns + ''.join(
        '<t:table-row>' + ''.join('<t:table-cell><p:p>' + value +
                                 '</p:p></t:table-cell>' for value in row) +
        '</t:table-row>' for row in rows) + '</t:table>')


class OdfTableImport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cancel = threading.Event()

    def xml(self, body):
        return ('<?xml version="1.0" encoding="UTF-8"?>'
                '<o:document-content xmlns:o="%s" xmlns:t="%s" xmlns:p="%s">'
                '<o:automatic-styles/><o:body><o:text>%s</o:text></o:body>'
                '</o:document-content>' % (O, T, P, body)).encode()

    def source(self, body, extra=None):
        path = self.root / 'original.odt'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('mimetype', 'application/vnd.oasis.opendocument.text')
            archive.writestr('content.xml', self.xml(body))
            archive.writestr('Pictures/source.png', b'picture-original')
            archive.comment = b'archive-comment'
            for name, content in (extra or {}).items():
                archive.writestr(name, content)
        return path

    def prepare(self, path):
        return prepare_odf_table_source(path, self.root / 'work', cancel=self.cancel)

    def test_private_copy_inserts_exact_declaration_without_reserializing_xml(self):
        path = self.source(table([['A', 'B', 'C'], ['D', 'E', 'F']]))
        original = path.read_bytes()
        prepared, proof = self.prepare(path)
        self.assertNotEqual(prepared, path)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(proof['columns'], [3])
        self.assertTrue(proof['originalPreserved'])
        with zipfile.ZipFile(prepared) as archive, zipfile.ZipFile(path) as original_archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(archive.namelist(), original_archive.namelist())
            self.assertEqual(archive.comment, original_archive.comment)
            for name in archive.namelist():
                if name != 'content.xml':
                    self.assertEqual(archive.read(name), original_archive.read(name))
            prepared_xml = archive.read('content.xml')
            declaration = b'<aio_guard_columns:table-column xmlns:aio_guard_columns="' + T.encode() + b'" aio_guard_columns:number-columns-repeated="3"/>'
            self.assertEqual(prepared_xml.replace(declaration, b''), original_archive.read('content.xml'))

    def test_declared_and_ragged_tables_do_not_change(self):
        for rows, columns in (([['A','B']], '<t:table-column t:number-columns-repeated="2"/>'),
                              ([['A','B'], ['C']], '')):
            with self.subTest(rows=rows):
                source = self.source(table(rows, columns))
                self.assertEqual(self.prepare(source), (source, None))

    def test_repeated_cells_have_exact_logical_width(self):
        body = '<t:table><t:table-row><t:table-cell t:number-columns-repeated="4"><p:p>A</p:p></t:table-cell></t:table-row></t:table>'
        prepared, proof = self.prepare(self.source(body))
        self.assertEqual(proof['columns'], [4])

    def test_spans_and_covered_cells_are_not_guessed(self):
        for cell in ('<t:table-cell t:number-columns-spanned="2"/>',
                     '<t:covered-table-cell/>', '<t:table-cell t:number-rows-spanned="2"/>'):
            with self.subTest(cell=cell):
                source = self.source('<t:table><t:table-row>' + cell + '</t:table-row></t:table>')
                self.assertEqual(self.prepare(source), (source, None))

    def test_nested_table_columns_do_not_hide_parent_missing_columns(self):
        inner = table([['nested-1','nested-2']], '<t:table-column t:number-columns-repeated="2"/>')
        outer = '<t:table><t:table-row><t:table-cell>' + inner + '</t:table-cell><t:table-cell><p:p>outer-2</p:p></t:table-cell></t:table-row></t:table>'
        _, proof = self.prepare(self.source(outer))
        self.assertEqual(proof['tables'], 1)
        self.assertEqual(proof['columns'], [2])

    def test_row_groups_insert_declaration_before_group(self):
        body = '<t:table><t:table-header-rows><t:table-row><t:table-cell/><t:table-cell/></t:table-row></t:table-header-rows></t:table>'
        prepared, _ = self.prepare(self.source(body))
        with zipfile.ZipFile(prepared) as archive:
            data = archive.read('content.xml')
        self.assertLess(data.index(b'aio_guard_columns:table-column'), data.index(b'<t:table-header-rows>'))

    def test_xml_entities_are_never_expanded(self):
        source = self.source(table([['&private;']]))
        with zipfile.ZipFile(source) as archive:
            xml = archive.read('content.xml')
        with zipfile.ZipFile(source, 'w') as archive:
            archive.writestr('mimetype', 'application/vnd.oasis.opendocument.text')
            archive.writestr('content.xml', b'<!DOCTYPE o:document-content [<!ENTITY private "SECRET">]>' + xml)
        prepared, proof = self.prepare(source)
        self.assertEqual(prepared, source)
        self.assertEqual(proof['reason'], 'xml_declarations')

    def test_source_inspection_budget_does_not_veto_conversion(self):
        source = self.source(table([['A','B']]))
        with patch('app_backend.converter_guard.odf_table_import.MAX_XML_BYTES', 5):
            prepared, proof = self.prepare(source)
        self.assertEqual(prepared, source)
        self.assertEqual(proof['reason'], 'xml_budget')

    def test_signed_container_is_not_invalidated_in_private_copy(self):
        source = self.source(table([['A','B']]), {'META-INF/documentsignatures.xml':b'signature'})
        prepared, proof = self.prepare(source)
        self.assertEqual(prepared, source)
        self.assertEqual(proof['reason'], 'signed_package')

    def test_cancel_prevents_output(self):
        self.cancel.set()
        with self.assertRaises(GuardError) as error:
            self.prepare(self.source(table([['A','B']])))
        self.assertEqual(error.exception.code, 'CANCELLED')
        self.assertFalse((self.root / 'work').exists())

    def test_non_writer_packages_are_untouched(self):
        source = self.source(table([['A','B']]))
        with zipfile.ZipFile(source, 'w') as archive:
            archive.writestr('mimetype','application/vnd.oasis.opendocument.spreadsheet')
            archive.writestr('content.xml', self.xml(table([['A','B']])))
        self.assertEqual(self.prepare(source), (source,None))

    def test_native_mimetype_header_remains_first_uncompressed_without_extra(self):
        prepared, _ = self.prepare(self.source(table([['A','B']])))
        with zipfile.ZipFile(prepared) as archive:
            self.assertEqual(archive.namelist()[0], 'mimetype')
            info = archive.getinfo('mimetype')
            self.assertEqual(info.compress_type, zipfile.ZIP_STORED)
            self.assertEqual(info.extra, b'')

    def test_default_table_namespace_and_unicode_content_are_preserved(self):
        source = self.source('')
        data = ('<?xml version="1.0" encoding="UTF-8"?>'
                '<o:document-content xmlns:o="%s" xmlns="%s" xmlns:p="%s">'
                '<o:body><o:text><table><table-row>'
                '<table-cell><p:p>Контроль</p:p></table-cell>'
                '<table-cell><p:p>Ёж &amp; tree</p:p></table-cell>'
                '</table-row></table></o:text></o:body></o:document-content>' % (O,T,P)).encode()
        with zipfile.ZipFile(source, 'w') as archive:
            archive.writestr('mimetype', 'application/vnd.oasis.opendocument.text')
            archive.writestr('content.xml', data)
        prepared, proof = self.prepare(source)
        self.assertEqual(proof['columns'], [2])
        with zipfile.ZipFile(prepared) as archive:
            rewritten = archive.read('content.xml')
        self.assertIn('Контроль'.encode(), rewritten)
        self.assertIn(b'&amp; tree', rewritten)

    def test_utf16_is_left_to_native_import_without_byte_offset_guess(self):
        source = self.source('')
        xml = self.xml(table([['A','B']])).decode().replace('UTF-8','UTF-16').encode('utf-16')
        with zipfile.ZipFile(source, 'w') as archive:
            archive.writestr('mimetype', 'application/vnd.oasis.opendocument.text')
            archive.writestr('content.xml', xml)
        prepared, proof = self.prepare(source)
        self.assertEqual(prepared, source)
        self.assertEqual(proof['reason'], 'xml_encoding')

    def test_path_collision_does_not_overwrite_prior_preparation(self):
        source = self.source(table([['A','B']]))
        prepared, _ = self.prepare(source)
        data = prepared.read_bytes()
        with self.assertRaises(OSError):
            self.prepare(source)
        self.assertEqual(prepared.read_bytes(), data)


if __name__ == '__main__':
    unittest.main()
