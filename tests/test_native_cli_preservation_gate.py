"""An unverified CLI page/sheet export cannot become a ready partial result."""
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app_backend.converter_guard.adapter import LibreOfficeAdapter
from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard.export_recipe import ExportRecipe
from app_backend.libreoffice_engine import LibreOfficeError

WRITER = 'com.sun.star.text.TextDocument'
CALC = 'com.sun.star.sheet.SpreadsheetDocument'
DRAW = 'com.sun.star.drawing.DrawingDocument'
IMPRESS = 'com.sun.star.presentation.PresentationDocument'
FLAT = 'OpenDocument Spreadsheet Flat XML'


class CliPreservationGate(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'original.docx'
        self.source.write_bytes(b'original source remains available')
        self.adapter = LibreOfficeAdapter(sys.executable)
        self.native = SimpleNamespace(valid=True, filters={
            FLAT: (CALC, {'fods'}, {'IMPORT', 'EXPORT'})},
            import_services={WRITER, CALC, DRAW, IMPRESS},
            export_filter=lambda *_: 'dBase')
        self.adapter._registry = lambda: self.native
        self.cancel = threading.Event()
        self.calls = []

    def cli(self, source, target, workspace, **kwargs):
        self.calls.append((source, target))
        folder = workspace / 'output'
        folder.mkdir(parents=True)
        output = folder / (source.stem + '.' + target)
        output.write_bytes(b'native one-file output')
        return [output]

    def convert(self, target, work='task', callback=None):
        with patch('app_backend.converter_guard.adapter.native_export_recipe',
                   return_value=ExportRecipe('fixture_export')), \
             patch('app_backend.converter_guard.adapter.convert_path', side_effect=self.cli):
            return self.adapter._convert_native(self.source, target, self.root / work,
                cancel=self.cancel, timeout=None, on_started=lambda: None,
                on_document_service=callback)

    def test_known_page_and_sheet_scopes_are_refused_without_model_receipt(self):
        for service, target in ((WRITER, 'png'), (WRITER, 'jfif'),
                                (DRAW, 'svg'), (IMPRESS, 'png'),
                                (CALC, 'png'), (CALC, 'slk')):
            with self.subTest(service=service, target=target):
                self.adapter._local.proof = {'documentServices': [service]}
                with self.assertRaises(GuardError) as caught:
                    self.convert(target, work=target + service.rsplit('.', 1)[-1])
                self.assertEqual(caught.exception.code, 'ENGINE_OUTPUT_VALIDATION_INCOMPLETE')
                self.assertEqual(caught.exception.stage, 'result')
                self.assertEqual(self.source.read_bytes(), b'original source remains available')
                checks = self.adapter.export_info()['resultChecks']
                self.assertEqual(checks[-1]['status'], 'not_checked')
                self.assertEqual(checks[-1]['reason'], 'native_cli_has_no_model_receipt')
        self.assertEqual(self.calls, [])

    def test_unscoped_native_cli_routes_are_preserved(self):
        for index, (service, target) in enumerate(((WRITER, 'pdf'), (CALC, 'xlsx'),
                                                  (DRAW, 'pdf'), (None, 'custom'))):
            self.adapter._local.proof = {'documentServices': [service] if service else []}
            paths = self.convert(target, work='plain-' + str(index))
            self.assertEqual(paths[0].read_bytes(), b'native one-file output')
        self.assertEqual(len(self.calls), 4)

    def test_checked_native_uno_page_export_still_returns_all_pages(self):
        receipt = {'check': 'native_preservation_contract', 'status': 'checked_subset',
                   'policyId': 'native_all_writer_pages_v1', 'expectedUnits': 3,
                   'observedOutputs': 3, 'qualityCompared': False}

        class Office:
            disabled = False
            def ensure_started(office, *_):
                pass
            def request(office, value, **kwargs):
                for index in range(3):
                    path = Path(value['output']) / ('original' + ('' if index == 0 else
                        '__page_%04d' % (index + 1)) + '.png')
                    path.write_bytes(b'\x89PNG\r\n\x1a\n' + b'fixture native bytes')
                return {'ok': True, 'preservationContract': receipt}
            def recycle(office):
                raise AssertionError('healthy native session should remain available')

        self.adapter._local.proof = {'documentServices': [WRITER]}
        self.adapter._local.batch = (self.native, Office())
        paths = self.convert('png')
        self.assertEqual(len(paths), 3)
        self.assertEqual(self.adapter.execution_mode(), 'native_uno')
        self.assertEqual(self.adapter.export_info()['resultChecks'], [receipt])
        self.assertEqual(self.calls, [])

    def test_uno_launch_fallback_cannot_publish_unverified_first_page(self):
        class Office:
            disabled = False
            def ensure_started(office, *_):
                raise GuardError('UNO_SESSION_UNAVAILABLE', 'Launch unavailable', stage='launch')
            def request(office, *_):
                raise AssertionError('failed launch cannot receive export')
            def recycle(office):
                pass

        self.adapter._local.proof = {'documentServices': [WRITER]}
        self.adapter._local.batch = (self.native, Office())
        with self.assertRaises(GuardError) as caught:
            self.convert('png')
        self.assertEqual(caught.exception.code, 'ENGINE_OUTPUT_VALIDATION_INCOMPLETE')
        self.assertEqual(self.calls, [])
        self.assertTrue(self.source.exists())

    def snapshot_cli(self, source, target, workspace, **kwargs):
        self.calls.append((source, target))
        folder = workspace / 'output'
        folder.mkdir(parents=True)
        output = folder / (source.stem + '.' + target)
        if target == 'fods':
            output.write_text('<o:document '
                'xmlns:o="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
                'xmlns:t="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
                'xmlns:x="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
                'o:mimetype="application/vnd.oasis.opendocument.spreadsheet">'
                '<o:body><o:spreadsheet>' + ''.join(
                '<t:table t:name="Sheet%d"><t:table-row><t:table-cell '
                'o:value-type="float" o:value="42"><x:p>42</x:p></t:table-cell>'
                '</t:table-row></t:table>' % index for index in (1, 2)) +
                '</o:spreadsheet></o:body></o:document>')
        else:
            output.write_bytes(b'one native sheet')
        return [output]

    def test_existing_all_sheet_splitter_can_export_each_private_one_sheet_snapshot(self):
        self.adapter._local.proof = {'documentServices': [CALC]}
        with patch('app_backend.converter_guard.adapter.native_export_recipe',
                   return_value=ExportRecipe('fixture_export')), \
             patch('app_backend.converter_guard.adapter.convert_path', side_effect=self.snapshot_cli):
            paths = self.adapter._convert_legacy_sheets(self.source, 'slk', self.root/'sheets',
                control=self.cancel, on_started=lambda: None, on_stage=None)
        self.assertEqual(len(paths), 2)
        self.assertEqual([target for _, target in self.calls], ['fods', 'slk', 'slk'])
        self.assertEqual(self.adapter.export_info()['resultChecks'][0]['sheetCount'], 2)
        self.assertIsNone(getattr(self.adapter._local, 'preservation_sheet_snapshot', None))

    def test_sheet_exception_is_bound_to_exact_snapshot_and_restored_after_error(self):
        self.adapter._local.proof = {'documentServices': [CALC]}
        self.adapter._local.preservation_sheet_snapshot = ('another source identity',)
        with self.assertRaises(GuardError) as caught:
            self.convert('slk', work='unrelated')
        self.assertEqual(caught.exception.code, 'ENGINE_OUTPUT_VALIDATION_INCOMPLETE')
        previous = self.adapter._local.preservation_sheet_snapshot
        original_cli = self.snapshot_cli

        def interrupted(source, target, workspace, **kwargs):
            if target == 'slk':
                raise LibreOfficeError('Interrupted fixture export', 'ENGINE_FAILED')
            return original_cli(source, target, workspace, **kwargs)

        with patch('app_backend.converter_guard.adapter.native_export_recipe',
                   return_value=ExportRecipe('fixture_export')), \
             patch('app_backend.converter_guard.adapter.convert_path', side_effect=interrupted):
            with self.assertRaises(GuardError) as caught:
                self.adapter._convert_legacy_sheets(self.source, 'slk', self.root/'interrupted',
                    control=self.cancel, on_started=lambda: None, on_stage=None)
        self.assertEqual(caught.exception.code, 'ENGINE_FAILED')
        self.assertEqual(self.adapter._local.preservation_sheet_snapshot, previous)
        self.assertTrue(self.source.exists())


if __name__ == '__main__':
    unittest.main()
