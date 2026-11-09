import io
import json
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard.export_recipe import WARNING_TEXT
from app_backend.converter_guard.macro_vba import inspect_vba, check_vba_preservation
from tools.qa.completion_macro_native import static_vba, decompress_vba


FIXTURES = Path(__file__).parent/'fixtures/macros'


class VbaStorageContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = FIXTURES/'SimpleMacro.xlsm'

    def test_actual_opc_and_legacy_projects_have_expected_static_sources(self):
        manifest = json.loads((FIXTURES/'provenance.json').read_text())
        for row in manifest['files']:
            with self.subTest(source=row['name']):
                source = FIXTURES/row['name']
                self.assertEqual(inspect_vba(source)['state'], 'present')
                self.assertEqual([list(p) for p in static_vba(source)['sourceSignature']], row['sourceSignature'])

    def test_all_project_streams_equal_is_checked_without_execution_claim(self):
        out = self.root/'copy.xlsm';shutil.copyfile(self.source,out)
        before = self.source.read_bytes()
        check = check_vba_preservation([out], 'xlsm', self.source)
        self.assertEqual(check['status'], 'streams_preserved')
        self.assertTrue(check['projectContainersByteEqual'])
        self.assertFalse(check['macrosExecuted'])
        self.assertFalse(check['behaviorCompared'])
        self.assertEqual(self.source.read_bytes(),before)

    def test_confirmed_vba_loss_blocks_macro_capable_ready_result(self):
        out = self.root/'lost.xlsm'
        with ZipFile(out,'w') as z:z.writestr('xl/workbook.xml','<workbook/>')
        with self.assertRaises(GuardError) as caught:
            check_vba_preservation([out], 'xlsm', self.source)
        self.assertEqual(caught.exception.code,'ENGINE_OUTPUT_MACROS_LOST')

    def test_nonmacro_target_has_explicit_expected_loss_warning(self):
        check = check_vba_preservation([], 'pdf', self.source)
        self.assertEqual(check['status'],'expected_loss')
        self.assertEqual(check['expectedLosses'],['vba_macros'])
        self.assertIn('VBA_MACROS_EXPECTED_LOSS',check['warnings'])

    def test_macro_suffix_alone_is_not_macro_evidence(self):
        out = self.root/'suffix.xlsm'
        with ZipFile(out,'w') as z:z.writestr('xl/workbook.xml','<workbook/>')
        self.assertEqual(inspect_vba(out)['state'],'absent')
        self.assertIsNone(check_vba_preservation([], 'xlsx', out))

    def test_ambiguous_case_colliding_parts_are_not_certified(self):
        with ZipFile(self.source) as z:raw=z.read('xl/vbaProject.bin')
        out=self.root/'ambiguous.xlsm'
        with ZipFile(out,'w') as z:
            z.writestr('xl/vbaProject.bin',raw);z.writestr('xl/VBAPROJECT.BIN',raw)
        self.assertEqual(inspect_vba(out)['state'],'not_checked')

    def test_invalid_project_is_not_reported_absent(self):
        out=self.root/'bad.xlsm'
        with ZipFile(out,'w') as z:z.writestr('xl/vbaProject.bin',b'not a CFB project')
        self.assertEqual(inspect_vba(out)['state'],'not_checked')

    def test_invalid_output_remains_unverified_not_preserved(self):
        out=self.root/'bad.xlsm';out.write_bytes(b'PK\x00\x00')
        check=check_vba_preservation([out],'xlsm',self.source)
        self.assertEqual(check['status'],'not_checked')
        self.assertIn('VBA_PRESERVATION_NOT_CHECKED',check['warnings'])

    def test_missing_unique_primary_does_not_claim_vba_preserved(self):
        check=check_vba_preservation([], 'xlsm', self.source)
        self.assertEqual(check['reason'],'vba_primary_not_unique')

    def test_cancel_is_propagated(self):
        cancel=threading.Event();cancel.set()
        with self.assertRaises(GuardError) as caught:inspect_vba(self.source,cancel=cancel)
        self.assertEqual(caught.exception.code,'CANCELLED')

    def test_read_budget_is_explicitly_unverified(self):
        with patch('app_backend.converter_guard.macro_vba.MAX_READ_BYTES',8):
            check=inspect_vba(self.source)
        self.assertEqual(check['state'],'not_checked')
        self.assertEqual(check['reason'],'vba_inspection_budget')

    def test_operation_budget_is_explicitly_unverified(self):
        with patch('app_backend.converter_guard.macro_vba.MAX_OPERATIONS',0):
            check=inspect_vba(FIXTURES/'SimpleMacro.doc')
        self.assertEqual(check['state'],'not_checked')
        self.assertEqual(check['reason'],'vba_inspection_budget')

    def test_native_stream_changes_are_visible_as_unverified(self):
        import olefile
        out=self.root/'changed.doc';shutil.copyfile(FIXTURES/'SimpleMacro.doc',out)
        with olefile.OleFileIO(str(out),write_mode=True) as z:
            raw=bytearray(z.openstream('Macros/VBA/__SRP_0').read());raw[-1]^=1
            z.write_stream('Macros/VBA/__SRP_0',bytes(raw))
        check=check_vba_preservation([out],'doc',FIXTURES/'SimpleMacro.doc')
        self.assertEqual(check['status'],'not_checked')
        self.assertEqual(check['reason'],'vba_stream_bytes_changed')

    def test_every_vba_warning_has_readable_scope(self):
        for code in ('VBA_MACROS_EXPECTED_LOSS','VBA_PRESERVATION_NOT_CHECKED','VBA_STREAMS_PRESERVED'):
            self.assertIn('Макросы не запускались',WARNING_TEXT[code])
        self.assertIn('не проверены',WARNING_TEXT['VBA_STREAMS_PRESERVED'])

    def test_independent_qa_reader_rejects_copy_before_chunk(self):
        with self.assertRaises(ValueError):decompress_vba(b'\x01\x03\xb0\x01\xff\xff')


if __name__=='__main__':unittest.main()
