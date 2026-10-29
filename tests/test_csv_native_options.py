"""Regression for native option handoff: a computed hint must reach LibreOffice."""
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest

from app_backend.converter_guard import LibreOfficeAdapter
from app_backend.converter_guard.csv_id_contract import CALC_TEXT, _import_dialect
from app_backend.converter_guard.csv_import import csv_import_hint
from app_backend.converter_guard.legacy_import import validate_options

CALC = 'com.sun.star.sheet.SpreadsheetDocument'


def native_fixture():
    return SimpleNamespace(valid=True, fingerprint='fixed-test-registry', preferred_filters={},
        filters={CALC_TEXT: (CALC, {'csv', 'tab', 'tsv'}, {'IMPORT', 'EXPORT'}),
                 'Other CSV': (CALC, {'csv'}, {'IMPORT'}),
                 'DIF': (CALC, {'dif'}, {'IMPORT'}),
                 'dBase': (CALC, {'dbf'}, {'IMPORT'})})


def handoff(native, options):
    # Exercise actual prepare_source; no executable or native process is mocked
    # as successful. This unit checks only validated parameter handoff.
    adapter = LibreOfficeAdapter.__new__(LibreOfficeAdapter)
    adapter._local = threading.local()
    adapter._registry = lambda: native
    adapter.prepare_source({'state': 'detected', 'basis': 'native_preflight_uno_document',
        'selectedImportFilter': CALC_TEXT, 'selectedImportOptions': options,
        'documentServices': [CALC], 'registryFingerprint': native.fingerprint})
    return adapter


class CsvNativeOptionTests(unittest.TestCase):
    def test_matching_import_and_readback_grammars(self):
        native = native_fixture()
        for options in ('44,34,76,1', '44,34,76,1,1/2', '44,34,76,1,1/2/4/10/5/10',
                        '9,34,76,1,1/2/2/10', '59,34,76,1,2/10'):
            with self.subTest(options=options):
                self.assertTrue(validate_options(CALC_TEXT, options, native))
                self.assertIsNotNone(_import_dialect(CALC_TEXT, options))
        self.assertTrue(validate_options('Other CSV', '44,34,76,1', native))
        self.assertFalse(validate_options('Other CSV', '44,34,76,1,1/10', native))
        self.assertTrue(validate_options('DIF', '76', native))
        self.assertTrue(validate_options('dBase', '34', native))

    def test_adapter_retains_validated_decimal_hints(self):
        options = '44,34,76,1,1/2/4/10/5/10'
        adapter = handoff(native_fixture(), options)
        self.assertEqual(adapter._local.import_filter, CALC_TEXT)
        self.assertEqual(adapter._local.import_options, options)

    def test_arbitrary_locale_and_unknown_codes_remain_rejected(self):
        native = native_fixture()
        for options in ('44,34,76,1,1/2/4/10,1031', '44,34,76,1,1/2/4/10,1049',
                        '44,34,76,1,1/9', '44,34,76,1,1/2/1/10',
                        '44,34,76,1,257/10', '44,34,76,1,1/10,0,true,true',
                        '44,34,76,1,1/2/4/__import__'):
            with self.subTest(options=options):
                self.assertFalse(validate_options(CALC_TEXT, options, native))
                self.assertIsNone(handoff(native, options)._local.import_options)

    def test_actual_hint_reaches_native_handoff_unchanged(self):
        native = native_fixture()
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'sample.csv'
            path.write_text('ID,Price\n00001,14.50\n00002,7.25\n', encoding='utf-8')
            hint = csv_import_hint(path, native)
            self.assertEqual(hint, {'filter': CALC_TEXT, 'options': '44,34,76,1,1/2/2/10'})
            self.assertEqual(handoff(native, hint['options'])._local.import_options, hint['options'])


if __name__ == '__main__':
    unittest.main()
