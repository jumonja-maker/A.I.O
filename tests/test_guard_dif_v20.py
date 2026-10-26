"""DIF numeric regression: source numbers must never become false-ready text."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest

from app_backend.converter_guard.dif_evidence import (
    DIF_CORE_SOURCE, DifInspectionError, DifNumericMismatch,
    iter_dif_numeric_rows, check_dif_import_model, check_dif_output,
)
from app_backend.converter_guard.errors import GuardError


def dif(rows, *, columns=None):
    columns = columns if columns is not None else len(rows[0]) if rows else 0
    lines = ['TABLE', '0,1', '"source"', 'VECTORS', '0,' + str(columns), '""',
             'TUPLES', '0,' + str(len(rows)), '""', 'DATA', '0,0', '""']
    for row in rows:
        lines += ['-1,0', 'BOT']
        for value in row:
            if isinstance(value, (int, float)):
                lines += ['0,' + str(value), 'V']
            else:
                lines += ['1,0', '"' + value.replace('"', '""') + '"']
    return ('\r\n'.join(lines + ['-1,0', 'EOD']) + '\r\n').encode()


def model(rows):
    def range_(first, row, last, _):
        return SimpleNamespace(getDataArray=lambda: (tuple(rows[row][first:last + 1]),))
    sheet = SimpleNamespace(getCellRangeByPosition=range_)
    return SimpleNamespace(getSheets=lambda: SimpleNamespace(getByIndex=lambda _: sheet))


def fods(rows):
    prefix = ('<?xml version="1.0"?><office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
              'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"><office:body><office:spreadsheet><table:table>')
    parts = [prefix]
    for row in rows:
        parts.append('<table:table-row>')
        for value in row:
            if isinstance(value, (int, float)):
                parts.append('<table:table-cell office:value-type="float" office:value="' + str(value) + '"/>')
            else:
                parts.append('<table:table-cell office:value-type="string"/>')
        parts.append('</table:table-row>')
    return ''.join(parts) + '</table:table></office:spreadsheet></office:body></office:document>'


class DifNumericContracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'renamed.bin'
        self.rows = [['ID', 'VALUE'], [1, 1.43], [2, 2.86], [3, 'literal #ERR is valid source text']]
        self.source.write_bytes(dif(self.rows))
        self.cancel = threading.Event()

    def test_content_detection_and_all_numeric_coordinates(self):
        self.assertEqual(list(iter_dif_numeric_rows(self.source)),
                         [(0, []), (1, [(0, 1.0), (1, 1.43)]),
                          (2, [(0, 2.0), (1, 2.86)]), (3, [(0, 3.0)])])
        check = check_dif_import_model(self.source, model(self.rows))
        self.assertEqual(check['numericCells'], 5)
        self.assertTrue(check['typesCompared'])
        self.assertFalse(check['qualityCompared'])

    def test_utf8_bom_is_not_a_way_to_skip_numeric_model_checks(self):
        self.source.write_bytes(b'\xef\xbb\xbf' + self.source.read_bytes())
        broken = [list(row) for row in self.rows]
        broken[1][1] = '#ERR: 1.43 (V)'
        with self.assertRaises(DifNumericMismatch):
            check_dif_import_model(self.source, model(broken))

    def test_literal_error_instead_of_native_double_is_rejected_before_any_export(self):
        broken = [list(row) for row in self.rows]
        broken[1][1] = '#ERR: 1.43 (V)'
        with self.assertRaisesRegex(DifNumericMismatch, 'row=2:column=2'):
            check_dif_import_model(self.source, model(broken))
        self.assertNotIn(b'#ERR: 1.43', self.source.read_bytes())

    def test_numeric_looking_text_and_changed_number_are_not_accepted(self):
        for wrong in ('1.43', 143.0, None, float('nan'), True):
            with self.subTest(wrong=wrong):
                broken = [list(row) for row in self.rows]
                broken[1][1] = wrong
                with self.assertRaises(DifNumericMismatch):
                    check_dif_import_model(self.source, model(broken))

    def test_scientific_notation_and_negative_zero_use_calc_double_domain(self):
        self.source.write_bytes(dif([[0.0, -0.0, -1.43, 1e-100, 1e100]]))
        check = check_dif_import_model(self.source, model([[0.0, -0.0, -1.43, 1e-100, 1e100]]))
        self.assertEqual(check['numericCells'], 5)

    def test_unrecognized_grammar_is_explicitly_unchecked_without_modifying_input(self):
        original = self.source.read_bytes().replace(b'0,1.43\r\nV', b'0,1,43\r\nV')
        self.source.write_bytes(original)
        check = check_dif_import_model(self.source, model(self.rows))
        self.assertEqual(check['status'], 'not_checked')
        self.assertEqual(check['reason'], 'dif_numeric_grammar')
        self.assertEqual(self.source.read_bytes(), original)

    def test_whole_source_is_traversed_and_truncation_does_not_certify_prefix(self):
        self.source.write_bytes(self.source.read_bytes().replace(b'-1,0\r\nEOD\r\n', b''))
        result = check_dif_import_model(self.source, model(self.rows))
        self.assertEqual(result['status'], 'not_checked')
        self.assertEqual(result['reason'], 'dif_eod_missing')

    def test_declared_dimensions_do_not_allocate_an_expanded_grid(self):
        self.source.write_bytes(self.source.read_bytes().replace(b'TUPLES\r\n0,4', b'TUPLES\r\n0,999999999'))
        result = check_dif_import_model(self.source, model(self.rows))
        self.assertEqual(result['status'], 'not_checked')
        self.assertEqual(result['reason'], 'dif_row_count')

    def test_cancel_interrupts_source_inspection(self):
        self.cancel.set()
        with self.assertRaisesRegex(DifInspectionError, 'dif_cancelled'):
            check_dif_import_model(self.source, model(self.rows), cancel=self.cancel)

    def test_native_worker_source_is_self_contained_and_same_checker(self):
        namespace = {}
        exec(compile(DIF_CORE_SOURCE, '<dif-native-worker>', 'exec'), namespace)
        result = namespace['check_dif_import_model'](self.source, model(self.rows))
        self.assertEqual(result['numericCells'], 5)

    def test_native_snapshot_preserves_values_and_types(self):
        output = self.root / 'source.fods'
        output.write_text(fods(self.rows))
        check = check_dif_output([output], 'fods', self.source, cancel=self.cancel)
        self.assertEqual(check['numericCells'], 5)
        self.assertEqual(check['status'], 'checked_subset')

    def test_native_snapshot_string_corruption_and_missing_coordinate_are_rejected(self):
        for wrong in ('#ERR: 1.43 (V)', 143.0):
            with self.subTest(wrong=wrong):
                broken = [list(row) for row in self.rows]
                broken[1][1] = wrong
                output = self.root / 'source.fods'
                output.write_text(fods(broken))
                with self.assertRaises(GuardError) as error:
                    check_dif_output([output], 'fods', self.source, cancel=self.cancel)
                self.assertEqual(error.exception.code, 'ENGINE_OUTPUT_CONTENT_LOSS')

    def test_snapshot_malformed_tail_is_not_ignored_after_last_number(self):
        output = self.root / 'source.fods'
        output.write_text(fods(self.rows) + '<broken>')
        with self.assertRaises(GuardError) as error:
            check_dif_output([output], 'fods', self.source, cancel=self.cancel)
        self.assertEqual(error.exception.code, 'ENGINE_OUTPUT_VALIDATION_INCOMPLETE')

    def test_snapshot_repeated_rows_and_columns_are_compared_without_expansion(self):
        self.source.write_bytes(dif([[1, 1], [1, 1]]))
        output = self.root / 'source.fods'
        body = fods([[1]])
        body = body.replace('<table:table-row>', '<table:table-row table:number-rows-repeated="2">')
        body = body.replace('<table:table-cell ', '<table:table-cell table:number-columns-repeated="2" ')
        output.write_text(body)
        self.assertEqual(check_dif_output([output], 'fods', self.source, cancel=self.cancel)['numericCells'], 4)

    def test_unsupported_target_does_not_claim_numeric_proof(self):
        check = check_dif_output([], 'pdf', self.source, cancel=self.cancel)
        self.assertEqual(check['status'], 'not_checked')
        self.assertFalse(check['qualityCompared'])


class DifAdapterIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'renamed.bin'
        self.source.write_bytes(dif([['VALUE'], [1.43]]))
        self.cancel = threading.Event()

    def _adapter(self):
        from app_backend.converter_guard.adapter import LibreOfficeAdapter
        adapter = LibreOfficeAdapter(self.root / 'soffice.exe')
        native = SimpleNamespace(fingerprint='registry-one', filters={
            'DIF': ('com.sun.star.sheet.SpreadsheetDocument', {'dif'}, {'IMPORT', 'EXPORT'}),
            'OpenDocument Spreadsheet Flat XML':
                ('com.sun.star.sheet.SpreadsheetDocument', {'fods'}, {'IMPORT', 'EXPORT'})})
        adapter._registry = lambda: native
        adapter._local.import_filter, adapter._local.import_options = 'DIF', '76'
        return adapter

    def test_native_snapshot_is_checked_once_and_cache_rejects_modified_staging(self):
        from unittest.mock import patch
        from app_backend.converter_guard.adapter import _PreparedSourceCache
        adapter = self._adapter()
        cache = _PreparedSourceCache(self.root / 'cache')
        calls = []
        def convert(source, target, workspace, **kwargs):
            calls.append((source, target, kwargs))
            output = Path(workspace) / 'output' / 'renamed.fods'
            output.parent.mkdir(parents=True)
            output.write_text(fods([['VALUE'], [1.43]]))
            return [output]
        with patch('app_backend.converter_guard.adapter.convert_path', side_effect=convert):
            prepared, summary = cache.prepare(self.source, epub=False, cancel=self.cancel,
                native_dif=(('DIF', '76'), adapter._prepare_dif_snapshot))
            second, _ = cache.prepare(self.source, epub=False, cancel=self.cancel,
                native_dif=(('DIF', '76'), adapter._prepare_dif_snapshot))
            self.assertEqual(prepared, second)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][2]['import_filter'], 'DIF:76')
            self.assertEqual(calls[0][2]['convert_spec'], 'fods:OpenDocument Spreadsheet Flat XML')
            self.assertEqual(summary['numericCheck']['numericCells'], 1)
            prepared.write_text(fods([['VALUE'], ['#ERR']]))
            cache.prepare(self.source, epub=False, cancel=self.cancel,
                native_dif=(('DIF', '76'), adapter._prepare_dif_snapshot))
            self.assertEqual(len(calls), 2)
            cache.prepare(self.source, epub=False, cancel=self.cancel,
                native_dif=(('DIF', None), adapter._prepare_dif_snapshot))
            self.assertEqual(len(calls), 3)

    def test_corrupted_native_import_snapshot_cannot_authorize_opaque_export(self):
        from unittest.mock import patch
        adapter = self._adapter()
        def convert(source, target, workspace, **kwargs):
            output = Path(workspace) / 'output' / 'renamed.fods'
            output.parent.mkdir(parents=True)
            output.write_text(fods([['VALUE'], ['#ERR: 1.43 (V)']]))
            return [output]
        with patch('app_backend.converter_guard.adapter.convert_path', side_effect=convert):
            with self.assertRaises(GuardError) as error:
                adapter._prepare_dif_snapshot(self.source, self.root / 'native', cancel=self.cancel)
        self.assertEqual(error.exception.code, 'DIF_NUMERIC_IMPORT_MISMATCH')
        self.assertEqual(error.exception.stage, 'import')
        self.assertNotIn(b'#ERR', self.source.read_bytes())

    def test_optional_probe_does_not_replace_missing_source_native_diagnosis(self):
        from app_backend.converter_guard.dif_evidence import is_dif_source
        self.assertFalse(is_dif_source(self.root / 'does-not-exist.dif'))

    def test_unknown_source_grammar_is_same_explicit_uncertainty_in_cli_snapshot(self):
        from unittest.mock import patch
        adapter = self._adapter()
        self.source.write_bytes(self.source.read_bytes().replace(b'0,1.43', b'0,1,43'))
        def convert(source, target, workspace, **kwargs):
            output = Path(workspace) / 'output' / 'renamed.fods'
            output.parent.mkdir(parents=True)
            output.write_text(fods([['VALUE'], [1.43]]))
            return [output]
        with patch('app_backend.converter_guard.adapter.convert_path', side_effect=convert):
            _, summary = adapter._prepare_dif_snapshot(self.source, self.root / 'native', cancel=self.cancel)
        self.assertEqual(summary['numericCheck']['status'], 'not_checked')
        self.assertFalse(summary['numericCheck']['qualityCompared'])
        self.assertIn('DIF_NUMERIC_CONTENT_NOT_CHECKED', summary['warnings'])
        self.assertNotIn('NATIVE_DIF_NUMERIC_IMPORT_VERIFIED', summary['warnings'])

    def test_native_failure_restores_original_import_recipe_for_next_source(self):
        from unittest.mock import patch
        adapter = self._adapter()
        prepared = self.root / 'source.fods'
        prepared.write_text(fods([['VALUE'], [1.43]]))
        summary = {'kind': 'native_dif_numeric_snapshot', 'numericCheck': {}, 'warnings': ()}
        seen = []
        def fail(source, target, workspace, **kwargs):
            seen.append((source, adapter._local.import_filter, adapter._local.import_options))
            raise GuardError('CONTROLLED_NATIVE_FAILURE', 'native failed', stage='engine')
        with patch.object(adapter, '_prepare_dif_snapshot', return_value=(prepared, summary)), \
                patch.object(adapter, '_convert_native', side_effect=fail):
            with self.assertRaises(GuardError):
                adapter.convert(self.source, 'pdf', self.root / 'task', cancel=self.cancel,
                    timeout=None, on_started=lambda: None)
        self.assertEqual(seen, [(prepared, 'OpenDocument Spreadsheet Flat XML', None)])
        self.assertEqual((adapter._local.import_filter, adapter._local.import_options), ('DIF', '76'))

    def test_cli_preflight_known_numeric_loss_is_not_admitted_as_verified_route(self):
        from unittest.mock import patch
        adapter = self._adapter()
        adapter.executable.write_bytes(b'native-test-double')
        proof = {'state': 'detected', 'basis': 'native_preflight_document_kind', 'nativeKind': 1,
                 'documentServices': ['com.sun.star.sheet.SpreadsheetDocument'],
                 'registryFingerprint': 'registry-one'}
        with patch('app_backend.converter_guard.preflight.inspect_source', return_value=proof), \
                patch.object(adapter, '_prepare_dif_snapshot', side_effect=GuardError(
                    'ENGINE_OUTPUT_CONTENT_LOSS', 'numbers became text', stage='result')):
            state = adapter._preflight_native(self.source, self.root / 'preflight',
                cancel=self.cancel, timeout=5)
        self.assertEqual(state['state'], 'undetermined')
        self.assertEqual(state['code'], 'DIF_NUMERIC_IMPORT_MISMATCH')
        self.assertFalse(state['requestedExportStarted'])

    def test_worker_gate_is_before_inspect_and_export_and_retains_distinct_error(self):
        from app_backend.converter_guard.uno_session import WORKER_SOURCE
        self.assertLess(WORKER_SOURCE.index('dif_import_check = check_dif_import_model'),
                        WORKER_SOURCE.index("elif req['operation'] == 'inspect':"))
        self.assertIn("'DIF_NUMERIC_IMPORT_MISMATCH'", WORKER_SOURCE)
        namespace = {'__name__': 'dif_integrated_worker'}
        exec(compile(WORKER_SOURCE, '<integrated-worker>', 'exec'), namespace)
        self.assertIn('check_dif_import_model', namespace)


if __name__ == '__main__':
    unittest.main()
