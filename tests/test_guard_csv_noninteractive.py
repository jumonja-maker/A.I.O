"""Regression: noninteractive CSV UNO import must never bypass native proof."""
import io
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app_backend.converter_guard.csv_import import CALC, csv_import_hint
from app_backend.converter_guard.adapter import LibreOfficeAdapter

CSV_FILTER = 'Text - txt - csv (StarCalc)'
NATIVE = SimpleNamespace(valid=True,
    filters={CSV_FILTER: (CALC, frozenset({'csv','txt'}), frozenset({'IMPORT'})),
             'Other import': ('com.sun.star.text.TextDocument', frozenset({'txt'}),frozenset({'IMPORT'}))},
    preferred_filters={'csv':{CSV_FILTER}},import_services={CALC},
    fingerprint='csv-fixture-1')


class CsvImportHintQA(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def source(self, name, payload):
        path = self.root/name
        path.write_bytes(payload)
        return path

    def test_comma_semicolon_tab_pipe_and_utf8_bom(self):
        for separator in [b',', b';', b'\t', b'|']:
            source = self.source('mystery.bin', b'name'+separator+b'amount\nalpha'+separator+b'1\nbeta'+separator+b'2\n')
            self.assertEqual(csv_import_hint(source,NATIVE),
                             {'filter':CSV_FILTER,'options':f'{separator[0]},34,76,1'})
        source = self.source('mystery.bin', b'\xef\xbb\xbfname,amount\nalpha,1\nbeta,2\n')
        self.assertEqual(csv_import_hint(source,NATIVE)['options'],'44,34,76,1')

    def test_unknown_binary_non_utf8_and_ambiguous_are_not_forced(self):
        for bytes_ in [b'\0binary,1\nx,2\n',b'hello world\nhello again\n',
                       b'a,\xff\nc,d\n',b'a,b;c\n1,2;3\n']:
            self.assertIsNone(csv_import_hint(self.source('test.csv',bytes_),NATIVE))

    def test_lo_268_native_calc_text_csv_outweighs_orcus_when_both_installed(self):
        source = self.source('sample.unknown',b'name,amount\nalpha,1\nbeta,2\n')
        native = SimpleNamespace(valid=True, filters={
            CSV_FILTER:(CALC,frozenset({'csv','tab','tsv','txt'}),frozenset({'IMPORT'})),
            'Orcus CSV':(CALC,frozenset({'csv','tab','tsv','txt'}),frozenset({'IMPORT'}))},
            preferred_filters={})
        self.assertEqual(csv_import_hint(source,native),
                         {'filter':CSV_FILTER,'options':'44,34,76,1'})

    def test_source_extension_alone_never_grants_proof(self):
        self.assertIsNone(csv_import_hint(self.source('table.csv',b'arbitrary bytes'),NATIVE))
        self.assertIsNotNone(csv_import_hint(self.source('table.unknown',b'a,b\n1,2\n'),NATIVE))

    def test_bounded_sample_does_not_reject_a_valid_utf8_prefix_cut_inside_a_character(self):
        prefix = b'name,amount\nalpha,1\nbeta,2\n'
        for tail in ('я', '€', '🙂'):
            with self.subTest(character=tail):
                payload = prefix + b'x' * (65535 - len(prefix)) + tail.encode('utf-8') + b',3\n'
                source = self.source('large.unknown', payload)
                self.assertEqual(csv_import_hint(source, NATIVE),
                                 {'filter': CSV_FILTER, 'options': '44,34,76,1'})

    def test_incomplete_utf8_at_real_eof_is_not_treated_as_a_valid_sample(self):
        for payload in (b'a,b\n1,2\n\xd1', b'a,b\n1,2\n' + b'x' * (65535 - 8) + b'\xd1'):
            with self.subTest(length=len(payload)):
                self.assertIsNone(csv_import_hint(self.source('invalid.csv', payload), NATIVE))

    def test_ambiguous_or_unavailable_native_importer_never_guessed(self):
        path = self.source('table.csv',b'a,b\n1,2\n')
        self.assertIsNone(csv_import_hint(path,SimpleNamespace(valid=False)))
        ambiguous = SimpleNamespace(valid=True,filters={
            'a':(CALC,frozenset({'csv'}),frozenset({'IMPORT'})),
            'b':(CALC,frozenset({'csv'}),frozenset({'IMPORT'}))},
            preferred_filters={})
        self.assertIsNone(csv_import_hint(path,ambiguous))

    def test_adapter_proof_carries_options_only_if_real_model_confirms_filter(self):
        source = self.source('renamed.unknown',b'name,amount\nalpha,1\n')
        class Office:
            disabled=False
            def request(self,request,**_):
                self.seen=request
                return {'ok':True,'services':[CALC],
                        'filterName':CSV_FILTER,'typeName':'calc_Text_txt_csv_StarCalc'}
        office=Office()
        adapter=LibreOfficeAdapter(executable=self.source('soffice.com',b''))
        adapter._local.batch = (NATIVE,office)
        with patch.object(adapter,'_registry',return_value=NATIVE):
            result=adapter.preflight(source,self.root/'preflight',cancel=SimpleNamespace(),timeout=3)
        self.assertEqual(result['state'],'detected')
        self.assertEqual(result['selectedImportFilter'],CSV_FILTER)
        self.assertEqual(result['selectedImportOptions'],'44,34,76,1')
        self.assertEqual(office.seen['importOptions'],'44,34,76,1')
        adapter.prepare_source(result)
        self.assertEqual(adapter._local.import_filter,CSV_FILTER)
        self.assertEqual(adapter._local.import_options,'44,34,76,1')
        office.request=lambda request,**_: {'ok':True,'services':[CALC],
                            'filterName':'UNEXPECTED_OTHER_FILTER'}
        with patch.object(adapter,'_registry',return_value=NATIVE):
            rejected=adapter.preflight(source,self.root/'preflight2',cancel=SimpleNamespace(),timeout=3)
        self.assertEqual(rejected['state'],'undetermined')
        self.assertEqual(rejected['code'],'PREFLIGHT_IMPORT_FILTER_MISMATCH')


if __name__=='__main__':
    unittest.main()
