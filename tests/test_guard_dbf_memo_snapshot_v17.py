"""Private CLI Memo preparation retains values, types, repeats and styles."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET

from app_backend.converter_guard.dbf_memo_snapshot import prepare_dbf_memo_snapshot
from app_backend.converter_guard.errors import GuardError

O='urn:oasis:names:tc:opendocument:xmlns:office:1.0'
T='urn:oasis:names:tc:opendocument:xmlns:table:1.0'
X='urn:oasis:names:tc:opendocument:xmlns:text:1.0'


def cell(value, repeat=1):
    from xml.sax.saxutils import escape
    attrs = ('o:value-type="string"' if isinstance(value, str) else
             'o:value-type="float" o:value="'+str(value)+'"')
    return '<t:table-cell t:style-name="KeepStyle" t:number-columns-repeated="'+str(repeat)+'" '+attrs+'><x:p>'+escape(str(value))+'</x:p></t:table-cell>'


def row(*values, repeat=1):
    return '<t:table-row t:number-rows-repeated="'+str(repeat)+'">'+''.join(values)+'</t:table-row>'


def document(*rows):
    return ('<?xml version="1.0" encoding="UTF-8"?><o:document xmlns:o="'+O+'" xmlns:t="'+T+'" xmlns:x="'+X+'" '
        'o:mimetype="application/vnd.oasis.opendocument.spreadsheet"><o:automatic-styles/>'
        '<o:body><o:spreadsheet><t:table t:name="Data">'+''.join(rows)+'</t:table></o:spreadsheet></o:body></o:document>')


class MemoSnapshots(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.cancel=threading.Event()

    def prepare(self, text):
        source=self.root/'source.fods';source.write_text(text,encoding='utf-8')
        before=source.read_bytes()
        prepared,sidecar,check=prepare_dbf_memo_snapshot(source,self.root/'prepared',cancel=self.cancel)
        self.assertEqual(source.read_bytes(),before)
        return prepared,sidecar,check

    def rows(self, path):
        return ET.parse(path).getroot().findall('.//{'+T+'}table-row')

    def test_only_mixed_column_becomes_memo_and_numeric_styles_stay(self):
        prepared,sidecar,check=self.prepare(document(row(cell('Mixed'),cell('Amount')),
            row(cell(7),cell(3.5)),row(cell('Я🙂'*200),cell(5))))
        rows=self.rows(prepared)
        self.assertEqual(''.join(rows[0][0].itertext()),'Mixed,M')
        self.assertEqual(rows[1][0].get('{'+O+'}value-type'),'string')
        self.assertEqual(rows[1][1].get('{'+O+'}value-type'),'float')
        self.assertEqual(rows[1][1].get('{'+O+'}value'),'3.5')
        self.assertEqual(rows[1][1].get('{'+T+'}style-name'),'KeepStyle')
        self.assertEqual(rows[2][0].get('{'+O+'}string-value'),'Я🙂'*200)
        records=[json.loads(line) for line in sidecar.read_text().splitlines()]
        self.assertEqual(records[0]['memoColumns'],[0])
        self.assertEqual(records[1]['sourceValue'],'7')
        self.assertEqual(records[2]['sourceValue'],'Я🙂'*200)
        self.assertEqual(check['memoColumnCount'],1)

    def test_headerless_first_record_is_preserved_after_generated_schema(self):
        prepared,sidecar,_=self.prepare(document(row(cell(7),cell(3.5)),row(cell('text'),cell(5))))
        rows=self.rows(prepared)
        self.assertEqual([''.join(c.itertext()) for c in rows[0]],['FIELD1,M','FIELD2'])
        self.assertEqual([''.join(c.itertext()) for c in rows[1]],['7','3.5'])
        self.assertFalse(json.loads(sidecar.read_text().splitlines()[0])['sourceHasHeader'])

    def test_repeated_cells_are_split_only_where_column_types_differ(self):
        prepared,_,_=self.prepare(document(row(cell('Header',repeat=2)),
            row(cell(7,repeat=2),repeat=2),row(cell('text'),cell(8))))
        rows=self.rows(prepared)
        self.assertEqual(rows[1].get('{'+T+'}number-rows-repeated'),'2')
        self.assertEqual(len(rows[1]),2)
        self.assertEqual(rows[1][0].get('{'+O+'}value-type'),'string')
        self.assertEqual(rows[1][1].get('{'+O+'}value-type'),'float')

    def test_repeated_header_keeps_remaining_header_copies_as_real_records(self):
        prepared,_,_=self.prepare(document(row(cell('Header'),repeat=2),row(cell(7)),row(cell('text'))))
        rows=self.rows(prepared)
        self.assertEqual(''.join(rows[0][0].itertext()),'Header,M')
        self.assertEqual(''.join(rows[1][0].itertext()),'Header')
        self.assertEqual(rows[1].get('{'+T+'}number-rows-repeated'),'1')

    def test_native_pure_numeric_columns_need_no_preparation(self):
        prepared,sidecar,check=self.prepare(document(row(cell('Amount')),row(cell(7)),row(cell(8))))
        self.assertEqual(prepared,self.root/'source.fods')
        self.assertIsNone(sidecar);self.assertIsNone(check)
        self.assertFalse((self.root/'prepared').exists())

    def test_unsafe_xml_and_cancellation_do_not_create_a_rewritten_source(self):
        source=self.root/'source.fods';text=document(row(cell('Header')),row(cell(7)),row(cell('text')))
        source.write_text('<!DOCTYPE o:document [<!ENTITY x "bad">]>'+text)
        with self.assertRaises(GuardError):
            prepare_dbf_memo_snapshot(source,self.root/'bad',cancel=self.cancel)
        source.write_text(text);self.cancel.set()
        with self.assertRaises(GuardError) as error:
            prepare_dbf_memo_snapshot(source,self.root/'cancelled',cancel=self.cancel)
        self.assertEqual(error.exception.code,'CANCELLED')
        self.assertFalse((self.root/'cancelled').exists())


if __name__=='__main__':
    unittest.main()
