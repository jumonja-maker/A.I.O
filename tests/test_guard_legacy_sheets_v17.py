"""All-sheet legacy fallback preserves native cached values and global styles."""
from pathlib import Path
import tempfile
import threading
import unittest
from xml.etree import ElementTree as ET

from app_backend.converter_guard import GuardError
from app_backend.converter_guard.legacy_sheets import prepare_calc_sheet_snapshots

O='urn:oasis:names:tc:opendocument:xmlns:office:1.0'
T='urn:oasis:names:tc:opendocument:xmlns:table:1.0'
S='urn:oasis:names:tc:opendocument:xmlns:style:1.0'
X='urn:oasis:names:tc:opendocument:xmlns:text:1.0'


def native_flat(*sheets):
    return ('<?xml version="1.0" encoding="UTF-8"?>'
        '<o:document xmlns:o="%s" xmlns:t="%s" xmlns:s="%s" xmlns:x="%s" '
        'o:mimetype="application/vnd.oasis.opendocument.spreadsheet">'
        '<o:automatic-styles><s:style s:name="DateFormat" s:family="table-cell" '
        's:data-style-name="N37"/></o:automatic-styles>'
        '<o:body><o:spreadsheet>%s</o:spreadsheet></o:body></o:document>'
        % (O,T,S,X,''.join(sheets))).encode('utf-8')


def sheet(name, value, *, formula='of:=SUM([other.A1:.A3])', kind='float'):
    attrs = 'o:value-type="%s" o:value="%s"' % (kind, value)
    return ('<t:table t:name="%s"><t:table-column t:default-cell-style-name="DateFormat"/>'
        '<t:table-row><t:table-cell t:style-name="DateFormat" t:formula="%s" %s>'
        '<x:p>%s</x:p></t:table-cell></t:table-row></t:table>' % (name,formula,attrs,value))


class LegacySheetSnapshots(unittest.TestCase):
    def test_all_native_sheets_keep_cached_values_styles_and_order(self):
        original = native_flat(sheet('First','42.5'), sheet('Second','00123',kind='string'),
                               sheet('Third','45000'))
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'native.fods';source.write_bytes(original)
            paths,check=prepare_calc_sheet_snapshots(source,root/'copies',stem='book',cancel=threading.Event())
            self.assertEqual([p.name for p in paths],['book.fods','book__sheet_002.fods','book__sheet_003.fods'])
            self.assertEqual(check['sheetCount'],3)
            self.assertEqual(check['cachedFormulaCells'],3)
            for i,path in enumerate(paths):
                doc=ET.parse(path).getroot()
                tables=doc.findall('{%s}body/{%s}spreadsheet/{%s}table' % (O,O,T))
                self.assertEqual(len(tables),1)
                self.assertEqual(tables[0].get('{%s}name'%T),('First','Second','Third')[i])
                cell=tables[0].find('.//{%s}table-cell'%T)
                self.assertIsNone(cell.get('{%s}formula'%T))
                self.assertEqual(cell.get('{%s}value'%O),('42.5','00123','45000')[i])
                self.assertEqual(cell.get('{%s}style-name'%T),'DateFormat')
                self.assertEqual(doc.find('{%s}automatic-styles/{%s}style'%(O,S)).get('{%s}data-style-name'%S),'N37')
            self.assertEqual(source.read_bytes(),original)

    def test_native_error_cache_keeps_displayed_error_instead_of_empty_string(self):
        error = ('<t:table t:name="Error"><t:table-row><t:table-cell '
            'xmlns:c="urn:org:documentfoundation:names:experimental:calc:xmlns:calcext:1.0" '
            't:formula="of:=1/0" o:value-type="string" o:string-value="" c:value-type="error">'
            '<x:p>#DIV/0!</x:p></t:table-cell></t:table-row></t:table>')
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'native.fods';source.write_bytes(native_flat(error))
            paths,check=prepare_calc_sheet_snapshots(source,root/'copies',cancel=threading.Event())
            cell=ET.parse(paths[0]).getroot().find('.//{%s}table-cell'%T)
            self.assertEqual(cell.get('{%s}value-type'%O),'string')
            self.assertIsNone(cell.get('{%s}string-value'%O))
            self.assertEqual(''.join(cell.itertext()),'#DIV/0!')
            self.assertEqual(check['cachedFormulaCells'],1)

    def test_empty_self_closing_sheet_is_retained_in_its_own_file(self):
        original=native_flat('<t:table t:name="Blank"/>',sheet('Data','7'))
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'native.fods';source.write_bytes(original)
            paths,check=prepare_calc_sheet_snapshots(source,root/'copies',cancel=threading.Event())
            self.assertEqual(check['sheetCount'],2)
            first=ET.parse(paths[0]).getroot().find('.//{%s}table'%T)
            self.assertEqual(first.get('{%s}name'%T),'Blank')
            self.assertIsNone(first.find('.//{%s}table-cell'%T))

    def test_unknown_or_missing_formula_cache_is_rejected_and_partial_output_removed(self):
        invalid=native_flat(sheet('Data','3').replace('o:value="3"',''))
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'native.fods';source.write_bytes(invalid)
            with self.assertRaises(GuardError) as error:
                prepare_calc_sheet_snapshots(source,root/'copies',cancel=threading.Event())
            self.assertEqual(error.exception.code,'NATIVE_TABLE_SNAPSHOT_INVALID')
            self.assertFalse((root/'copies').exists())
            self.assertEqual(source.read_bytes(),invalid)

    def test_entities_are_refused_before_any_snapshot_can_become_a_result(self):
        original=native_flat(sheet('Data','3'))
        original=original.replace(b'<o:document',b'<!DOCTYPE o:document [<!ENTITY x "untrusted">]><o:document',1)
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'native.fods';source.write_bytes(original)
            with self.assertRaises(GuardError):
                prepare_calc_sheet_snapshots(source,root/'copies',cancel=threading.Event())
            self.assertFalse((root/'copies').exists())

    def test_cancellation_and_existing_paths_never_overwrite_sources_or_other_work(self):
        original=native_flat(sheet('Data','3'))
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'native.fods';source.write_bytes(original)
            cancel=threading.Event();cancel.set()
            with self.assertRaises(GuardError) as error:
                prepare_calc_sheet_snapshots(source,root/'cancelled',cancel=cancel)
            self.assertEqual(error.exception.code,'CANCELLED')
            self.assertFalse((root/'cancelled').exists())
            output=root/'copies';output.mkdir();sentinel=output/'keep.txt';sentinel.write_text('keep')
            with self.assertRaises(FileExistsError):
                prepare_calc_sheet_snapshots(source,output,cancel=threading.Event())
            self.assertEqual(sentinel.read_text(),'keep')
            self.assertEqual(source.read_bytes(),original)


if __name__=='__main__':
    unittest.main()
