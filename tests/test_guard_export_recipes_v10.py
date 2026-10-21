"""Native export/import dialect contracts; not Windows acceptance."""
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app_backend.converter_guard.adapter import LibreOfficeAdapter
from app_backend.converter_guard.routes import Registry
from test_converter_guard_v04 import component, node, C, W

CSV = 'Text - txt - csv (StarCalc)'


class ExportRecipesQA(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.exe = self.root / 'soffice'
        self.exe.touch()
        path = self.root / 'registry.xcd'
        types = node('text', Extensions='csv tsv tab txt') + node('pdf', Extensions='pdf')
        filters = (node(CSV, Type='text', DocumentService=C, Flags='IMPORT EXPORT') +
                   node('Text', Type='text', DocumentService=W, Flags='IMPORT EXPORT') +
                   node('calc_pdf_Export', Type='pdf', DocumentService=C, Flags='EXPORT') +
                   node('writer_pdf_Export', Type='pdf', DocumentService=W, Flags='EXPORT'))
        path.write_text('<oor:data xmlns:oor="http://openoffice.org/2001/registry">' +
                        component('Types', 'Types', types) +
                        component('Filter', 'Filters', filters) + '</oor:data>', encoding='utf-8')
        self.native = Registry(((str(path), 0, path.stat().st_size),), self.exe)
        self.adapter = LibreOfficeAdapter(self.exe)
        registry_patch = patch.object(self.adapter, '_registry', return_value=self.native)
        registry_patch.start()
        self.addCleanup(registry_patch.stop)
        self.proof = {'state': 'detected', 'basis': 'native_preflight_document_kind',
                      'registryFingerprint': self.native.fingerprint, 'documentServices': [C],
                      'selectedImportFilter': CSV, 'selectedImportOptions': '59,34,76,1'}
        self.source = self.root / 'renamed.bin'
        self.source.write_text('name;amount\nПривет;123\n', encoding='utf-8')
        self.sequence = 0

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, target, proof=None):
        self.sequence += 1
        self.adapter.prepare_source(self.proof if proof is None else proof)
        with patch('app_backend.converter_guard.adapter.convert_path', return_value=[]) as convert:
            self.adapter.convert(self.source, target, self.root / f'cli-{self.sequence}',
                                 cancel=threading.Event(), timeout=None, on_started=lambda: None)
        return convert.call_args.kwargs

    def test_csv_tsv_tab_have_explicit_export_dialects_separate_from_import(self):
        for target, separator in (('csv', 44), ('tsv', 9), ('tab', 9)):
            with self.subTest(target=target):
                args = self.cli(target)
                self.assertEqual(args['convert_spec'], f'{target}:{CSV}:{separator},34,76,1,')
                self.assertEqual(args['import_filter'], CSV + ':59,34,76,1')

    def test_every_source_with_a_current_calc_proof_uses_the_same_recipe(self):
        # These are conditional on an existing native proof, not admission by suffix.
        for name in ('book.ods', 'book.xlsx', 'table.csv', 'content.unknown'):
            with self.subTest(name=name):
                self.source = self.root / name
                self.source.touch()
                self.assertEqual(self.cli('tsv')['convert_spec'], f'tsv:{CSV}:9,34,76,1,')

    def test_writer_text_filter_does_not_inherit_calc_options(self):
        args = self.cli('tsv', {**self.proof, 'documentServices': [W]})
        self.assertEqual(args['convert_spec'], 'tsv:Text')
        self.assertIsNone(args['import_filter'])

    def test_next_pdf_does_not_inherit_a_text_dialect(self):
        self.cli('tsv')
        self.assertEqual(self.cli('pdf')['convert_spec'], 'pdf:calc_pdf_Export')

    def test_missing_or_stale_proof_leaves_native_cli_detection_available(self):
        for proof in ({}, {**self.proof, 'registryFingerprint': 'stale'}):
            with self.subTest(proof=proof):
                self.assertNotIn('convert_spec', self.cli('tsv', proof))

    def test_unregistered_or_ambiguous_filter_is_not_forced(self):
        self.assertNotIn('convert_spec', self.cli('custom'))
        self.native.export_candidates['tsv', C].add('another-text-export')
        self.assertNotIn('convert_spec', self.cli('tsv'))

    def test_uno_and_cli_use_the_same_filter_and_options(self):
        requests = []

        def request(value, **_):
            requests.append(value)
            (Path(value['output']) / ('result.' + value['format'])).write_text('Привет\t123\n')
            return {'ok': True}

        office = SimpleNamespace(disabled=False, ensure_started=lambda *args: None, request=request)
        self.adapter._local.batch = (self.native, office)
        for target in ('csv', 'tsv', 'tab', 'pdf'):
            self.adapter.prepare_source(self.proof)
            self.adapter.convert(self.source, target, self.root / ('uno-' + target),
                                 cancel=threading.Event(), timeout=None, on_started=lambda: None)
            observed = requests[-1]
            self.assertEqual(observed['importOptions'], '59,34,76,1')
            self.assertEqual(observed['exportFilter'], 'calc_pdf_Export' if target == 'pdf' else CSV)
            self.assertEqual(observed['exportOptions'],
                             None if target == 'pdf' else f'{44 if target == "csv" else 9},34,76,1,')

    def test_lok_confirmed_calc_keeps_requested_import_options_without_faking_selected_filter(self):
        proof = {k: v for k, v in self.proof.items() if not k.startswith('selectedImport')}
        proof['nativeKind'] = 1
        with patch('app_backend.converter_guard.preflight.inspect_source', return_value=proof):
            observed = self.adapter.preflight(self.source, self.root / 'preflight',
                                              cancel=threading.Event(), timeout=3)
        self.assertEqual(observed['requestedImportFilter'], CSV)
        self.assertEqual(observed['requestedImportOptions'], '59,34,76,1')
        self.assertNotIn('selectedImportFilter', observed)
        args = self.cli('tsv', observed)
        self.assertEqual(args['import_filter'], CSV + ':59,34,76,1')
        self.assertEqual(args['convert_spec'], f'tsv:{CSV}:9,34,76,1,')

    def test_requested_hint_cannot_replace_a_different_or_stale_native_model(self):
        for variant in ({'nativeKind': 0, 'documentServices': [W]},
                        {'nativeKind': 1, 'registryFingerprint': 'stale'}):
            with self.subTest(variant=variant):
                proof = {k: v for k, v in self.proof.items() if not k.startswith('selectedImport')}
                proof.update(variant)
                with patch('app_backend.converter_guard.preflight.inspect_source', return_value=proof):
                    observed = self.adapter.preflight(self.source, self.root / 'preflight',
                                                      cancel=threading.Event(), timeout=3)
                self.assertNotIn('requestedImportFilter', observed)
                args = self.cli('tsv', {**observed, 'requestedImportFilter': CSV,
                                        'requestedImportOptions': '59,34,76,1'})
                self.assertIsNone(args['import_filter'])


if __name__ == '__main__':
    unittest.main()
