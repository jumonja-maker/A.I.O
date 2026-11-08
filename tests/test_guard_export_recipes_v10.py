"""Native export/import dialect contracts; not Windows acceptance."""
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app_backend.converter_guard.adapter import LibreOfficeAdapter
from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard.routes import Registry
from test_converter_guard_v04 import component, node, C, W

CSV = 'Text - txt - csv (StarCalc)'
FLAT = 'OpenDocument Spreadsheet Flat XML'


class ExportRecipesQA(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.exe = self.root / 'soffice'
        self.exe.touch()
        path = self.root / 'registry.xcd'
        types = node('text', Extensions='csv tsv tab txt') + node('pdf', Extensions='pdf') + node('flat', Extensions='fods')
        filters = (node(FLAT, Type='flat', DocumentService=C, Flags='IMPORT EXPORT') + node(CSV, Type='text', DocumentService=C, Flags='IMPORT EXPORT') +
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

    def cli(self, target, proof=None, *, expect_refusal=False):
        self.sequence += 1
        self.adapter.prepare_source(self.proof if proof is None else proof)
        original = self.source.read_bytes()
        def native_output(source, fmt, work, **kwargs):
            # B30 adds native FODS staging before the same dialect boundary.
            # The fixture models that stage; it is not a native acceptance.
            from test_guard_legacy_sheets_v17 import native_flat, sheet
            output = Path(work) / 'output'
            output.mkdir(parents=True, exist_ok=True)
            p = output / (Path(source).stem + '.' + fmt)
            p.write_bytes(native_flat(sheet('Data', '42.5')) if fmt == 'fods' else b'42.5\n')
            return [p] if fmt in ('fods', 'csv', 'tsv', 'tab') else []
        with patch('app_backend.converter_guard.adapter.convert_path', side_effect=native_output) as convert:
            def execute():
                return self.adapter.convert(self.source, target, self.root / f'cli-{self.sequence}',
                    cancel=threading.Event(), timeout=None, on_started=lambda: None)
            if expect_refusal:
                with self.assertRaises(GuardError) as caught:
                    execute()
                self.assertEqual(caught.exception.code, 'ENGINE_OUTPUT_VALIDATION_INCOMPLETE')
                convert.assert_not_called()  # No dialect is forced via a partial CLI export.
                self.assertNotIn('filter', self.adapter.export_info())
                self.assertEqual(self.source.read_bytes(), original)
                return
            execute()
        return {**convert.call_args.kwargs,
                'source_import_filter':convert.call_args_list[0].kwargs['import_filter']}

    def test_csv_tsv_tab_have_explicit_export_dialects_separate_from_import(self):
        for target, separator in (('csv', 44), ('tsv', 9), ('tab', 9)):
            with self.subTest(target=target):
                args = self.cli(target)
                self.assertEqual(args['convert_spec'], f'{target}:{CSV}:{separator},34,76,1,')
                self.assertEqual(args['source_import_filter'], CSV + ':59,34,76,1')

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
        self.assertIsNone(args['source_import_filter'])

    def test_next_pdf_does_not_inherit_a_text_dialect(self):
        self.cli('tsv')
        import json
        extension, filter_name, properties = self.cli('pdf')['convert_spec'].split(':', 2)
        self.assertEqual((extension, filter_name), ('pdf', 'calc_pdf_Export'))
        self.assertEqual(json.loads(properties), {
            'UseLosslessCompression': {'type': 'boolean', 'value': True},
            'ReduceImageResolution': {'type': 'boolean', 'value': False}})

    def test_missing_or_stale_proof_leaves_native_cli_detection_available(self):
        self.assertNotIn('convert_spec', self.cli('tsv', {}))
        stale = {**self.proof, 'registryFingerprint': 'stale'}
        # A known Calc sheet route cannot safely fall back to a first-sheet
        # export without a current recipe/all-sheet receipt. Unknown and
        # unscoped Writer CLI detection remain available without forcing Calc.
        self.cli('tsv', stale, expect_refusal=True)
        self.assertNotIn('convert_spec', self.cli('tsv', {**stale, 'documentServices': [W]}))

    def test_unregistered_or_ambiguous_filter_is_not_forced(self):
        self.assertNotIn('convert_spec', self.cli('custom'))
        self.native.export_candidates['tsv', C].add('another-text-export')
        self.cli('tsv', expect_refusal=True)

    def test_uno_and_cli_use_the_same_filter_and_options(self):
        requests = []

        def request(value, **_):
            requests.append(value)
            output = Path(value['output']) / ('result.' + value['format'])
            if value['format'] == 'pdf':
                output.write_bytes(b'%PDF-1.7\n% synthetic export test\n')
            else:
                output.write_text('Привет\t123\n')
                from app_backend.converter_guard.sheet_outputs import sheet_file_evidence, write_sheet_manifest
                write_sheet_manifest(output.parent, [sheet_file_evidence(output,0,'Sheet1',
                    {'status':'not_checked','reason':'controlled_test_exporter'})])
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
        self.assertEqual(args['source_import_filter'], CSV + ':59,34,76,1')
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
                injected = {**observed, 'requestedImportFilter': CSV,
                            'requestedImportOptions': '59,34,76,1'}
                if observed.get('documentServices') == [C]:
                    self.cli('tsv', injected, expect_refusal=True)
                    self.assertIsNone(self.adapter._local.import_filter)
                    self.assertIsNone(self.adapter._local.import_options)
                else:
                    args = self.cli('tsv', injected)
                    self.assertIsNone(args['source_import_filter'])


if __name__ == '__main__':
    unittest.main()
