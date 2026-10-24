"""Exercise source preparation, admission and actual existing Qt codecs together."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from app_backend.converter_guard.adapter import LibreOfficeAdapter, _RunControl
from app_backend.converter_guard.media_fidelity import inspect_media
from app_backend.converter_guard.errors import GuardError


@unittest.skipUnless(importlib.util.find_spec('PySide6'), 'Application Qt dependency required')
class AdapterRoutes(unittest.TestCase):
    def test_apng_native_import_failure_still_admits_frame_preserving_qt_routes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'animated.apng'
            corpus = Path(__file__).parent / 'data/guard_v17_fulltest_subset.zip'
            with zipfile.ZipFile(corpus) as archive:
                source.write_bytes(archive.read('05_LEVEL_2.apng'))
            native = SimpleNamespace(fingerprint='installed-test', filters={}, imports={}, valid=True,
                exports={}, external_filters=False, route_reason=lambda *args: None,
                observed_decision=lambda *args: 'unavailable')
            adapter = LibreOfficeAdapter(sys.executable)
            adapter._registry = lambda: native
            with patch.object(adapter, '_preflight_native', return_value={
                    'state': 'undetermined', 'code': 'PREFLIGHT_IMPORT_UNDETERMINED'}):
                proof = adapter.preflight(source, root / 'preflight',
                    cancel=threading.Event(), timeout=10)
            self.assertEqual(proof['basis'], 'qt_media_content')
            self.assertEqual(adapter.route_decision('webp', proof), 'registered')
            self.assertEqual(adapter.route_decision('docx', proof), 'unavailable')
            with patch('app_backend.converter_guard.adapter.catalog', return_value=[]):
                caps = adapter.capabilities([{'id': 'one', 'name': source.name, 'size': source.stat().st_size}],
                    proofs={'one': proof})
            self.assertEqual(next(row for row in caps['formats'] if row['id'] == 'webp')
                             ['selectableFileIds'], ['one'])
            adapter.prepare_source(proof)
            with patch.object(adapter, '_convert_native', side_effect=AssertionError('Unexpected Draw export')):
                outputs = adapter.convert(source, 'webp', root / 'task', cancel=threading.Event(),
                    timeout=10, on_started=lambda: None)
            before, after = inspect_media(source), inspect_media(outputs[0])
            self.assertEqual(before['frames'], after['frames'])
            self.assertEqual(before['durationsMs'], after['durationsMs'])
            self.assertEqual((before['width'], before['height']), (after['width'], after['height']))
            self.assertEqual(adapter.execution_mode(), 'qt_media_frames')
            self.assertEqual(adapter.native_timings()['source'], 'qt_media')


class LegacyStagingQuota(unittest.TestCase):
    def setup_native(self, root):
        adapter = LibreOfficeAdapter(sys.executable)
        native = SimpleNamespace(filters={
            'OpenDocument Spreadsheet Flat XML':
                ('com.sun.star.sheet.SpreadsheetDocument', {'fods'}, {'IMPORT', 'EXPORT'})},
            export_filter=lambda *args: 'dBase')
        adapter._registry = lambda: native
        source = root / 'original.ods'
        source.write_bytes(b'original-untouched')
        xml = ('<o:document xmlns:o="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
               'xmlns:t="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
               'xmlns:x="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
               'o:mimetype="application/vnd.oasis.opendocument.spreadsheet">'
               '<o:body><o:spreadsheet><t:table t:name="Values"><t:table-row>'
               '<t:table-cell o:value-type="float" o:value="42.5"><x:p>42.5</x:p>'
               '</t:table-cell></t:table-row></t:table></o:spreadsheet></o:body></o:document>')
        calls = []
        def export(path, target, workspace, *, cancel, **kwargs):
            calls.append((target, cancel))
            self.assertFalse(cancel.is_set())
            output = workspace / 'output'
            output.mkdir(parents=True)
            result = output / (path.stem + '.' + target)
            result.write_bytes(xml.encode() if target == 'fods' else b'dbf-result')
            adapter._local.metrics = {'source': 'cli_process', 'processMs': 1}
            return [result]
        return adapter, source, export, calls

    def test_native_temporary_snapshot_exceeding_explicit_quota_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            adapter, source, export, calls = self.setup_native(root)
            workspace = root / 'task'
            control = _RunControl(threading.Event(), workspace, 10, lambda: 10)
            with patch.object(adapter, '_convert_native', side_effect=export), patch(
                    'app_backend.converter_guard.legacy_sheets.prepare_calc_sheet_snapshots') as split:
                with self.assertRaises(GuardError) as error:
                    adapter._convert_legacy_sheets(source, 'dbf', workspace, control=control,
                        on_started=lambda: None, on_stage=None)
            self.assertEqual(error.exception.code, 'SPOOL_QUOTA')
            self.assertEqual(error.exception.status, 507)
            self.assertEqual([target for target, _ in calls], ['fods'])
            self.assertEqual(calls[0][1].deadline, control.deadline)
            split.assert_not_called()
            self.assertFalse((workspace / 'output').exists())
            self.assertEqual(source.read_bytes(), b'original-untouched')

    def test_no_explicit_quota_preserves_normal_snapshot_conversion(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            adapter, source, export, calls = self.setup_native(root)
            workspace = root / 'task'
            control = _RunControl(threading.Event(), workspace, 10, None)
            with patch.object(adapter, '_convert_native', side_effect=export):
                paths = adapter._convert_legacy_sheets(source, 'dbf', workspace, control=control,
                    on_started=lambda: None, on_stage=None)
            self.assertEqual([target for target, _ in calls], ['fods', 'dbf'])
            self.assertTrue(all(cancel.deadline == control.deadline for _, cancel in calls))
            self.assertEqual([path.name for path in paths], ['original.dbf'])
            self.assertEqual(paths[0].read_bytes(), b'dbf-result')
            self.assertEqual(adapter.export_info()['resultChecks'][0]['sheetCount'], 1)
            self.assertEqual(source.read_bytes(), b'original-untouched')


if __name__ == '__main__':
    unittest.main()
