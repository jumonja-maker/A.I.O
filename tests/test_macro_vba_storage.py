import io
import json
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch
from zipfile import ZipFile
import xml.etree.ElementTree as ET

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

    def test_non_opc_zip_names_are_not_subject_to_opc_uri_restrictions(self):
        source=self.root/'ordinary.odt'
        with ZipFile(source,'w') as z:
            z.writestr('mimetype','application/vnd.oasis.opendocument.text')
            z.writestr('Pictures/100% RGB.png',b'not relevant to VBA')
        self.assertEqual(inspect_vba(source)['state'],'absent')

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

    def opc(self, *, target='custom-program.bin', declared=True, mode=None, parts=None, extra=None):
        with ZipFile(self.source) as z:raw=z.read('xl/vbaProject.bin')
        path=self.root/'controlled.xlsm'
        ct='{http://schemas.openxmlformats.org/package/2006/content-types}'
        rel='{http://schemas.openxmlformats.org/package/2006/relationships}'
        types=ET.Element(ct+'Types')
        if declared:
            ET.SubElement(types,ct+'Override',PartName='/xl/'+target,
                          ContentType='application/vnd.ms-office.vbaProject')
        links=ET.Element(rel+'Relationships')
        node=ET.SubElement(links,rel+'Relationship',Id='vba',Target=target,
                           Type='http://schemas.microsoft.com/office/2006/relationships/vbaProject')
        if mode is not None:node.set('TargetMode',mode)
        with ZipFile(path,'w') as z:
            z.writestr('[Content_Types].xml',ET.tostring(types))
            z.writestr('xl/_rels/workbook.xml.rels',ET.tostring(links))
            for name in parts if parts is not None else ['xl/'+target]:z.writestr(name,raw)
            for name,data in (extra or {}).items():z.writestr(name,data)
        return path

    def test_vba_relationship_finds_nonstandard_program_part(self):
        source=self.opc(declared=False)
        self.assertEqual(inspect_vba(source)['state'],'present')

    def test_vba_content_type_finds_nonstandard_program_part(self):
        source=self.opc()
        self.assertEqual(inspect_vba(source)['state'],'present')
        check=check_vba_preservation([], 'pdf', source)
        self.assertEqual(check['status'],'expected_loss')

    def test_renamed_project_is_not_silent_absence_in_real_ooxml_package(self):
        source=self.root/'renamed.xlsm'
        with ZipFile(self.source) as original,ZipFile(source,'w') as output:
            for entry in original.infolist():
                raw=original.read(entry)
                name=entry.filename
                if name=='xl/vbaProject.bin':name='xl/custom-program.bin'
                if name in ('[Content_Types].xml','xl/_rels/workbook.xml.rels'):
                    raw=raw.replace(b'vbaProject.bin',b'custom-program.bin')
                output.writestr(name,raw)
        from app_backend.converter_guard.source_contract import inspect_source_contract
        self.assertEqual(inspect_source_contract(source)['state'],'recognized')
        self.assertEqual(inspect_vba(source)['projects'],inspect_vba(self.source)['projects'])

    def test_vba_target_safe_parent_relative_and_percent_encoded_uri(self):
        for target in ('../program.bin','%70rogram.bin'):
            with self.subTest(target=target):
                parts=['program.bin'] if target.startswith('..') else ['xl/program.bin']
                self.assertEqual(inspect_vba(self.opc(target=target,declared=False,parts=parts))['state'],'present')

    def test_external_vba_relationship_is_unverified_without_network_fetch(self):
        with patch('socket.socket',side_effect=AssertionError('No external fetch allowed')):
            source=self.opc(target='https://example.org/code.bin',declared=False,mode='External',parts=[])
            self.assertEqual(inspect_vba(source)['state'],'not_checked')

    def test_relationship_must_not_escape_package(self):
        source=self.opc(target='../../outside.bin',declared=False,parts=[])
        self.assertEqual(inspect_vba(source)['state'],'not_checked')

    def test_missing_declared_vba_is_unverified_not_absent(self):
        self.assertEqual(inspect_vba(self.opc(parts=[]))['state'],'not_checked')

    def test_percent_encoded_alias_collision_is_unverified(self):
        source=self.opc(declared=False,parts=['xl/custom-program.bin','xl/%63ustom-program.bin'])
        self.assertEqual(inspect_vba(source)['state'],'not_checked')

    def test_xml_doctype_is_never_expanded(self):
        source=self.opc(extra={'unused.rels':b'<!DOCTYPE r [<!ENTITY data "value">]><r>&data;</r>'})
        self.assertEqual(inspect_vba(source)['state'],'not_checked')

    def test_utf16_xml_doctype_is_never_expanded(self):
        source=self.opc(extra={'unused.rels':'<!DOCTYPE r [<!ENTITY data "value">]><r>&data;</r>'.encode('utf-16')})
        self.assertEqual(inspect_vba(source)['state'],'not_checked')

    def test_opc_metadata_budget_is_explicit(self):
        source=self.opc()
        with patch('app_backend.converter_guard.macro_vba.MAX_OPC_METADATA',16):
            check=inspect_vba(source)
        self.assertEqual(check['state'],'not_checked')
        self.assertEqual(check['reason'],'vba_inspection_budget')

    def test_independent_qa_reader_rejects_copy_before_chunk(self):
        with self.assertRaises(ValueError):decompress_vba(b'\x01\x03\xb0\x01\xff\xff')


class RegisteredMacroExporter(unittest.TestCase):
    CALC='com.sun.star.sheet.SpreadsheetDocument'
    WRITER='com.sun.star.text.TextDocument'
    STANDARD='Calc MS Excel 2007 VBA XML'

    def native(self,names):
        from app_backend.converter_guard.routes import Registry
        native=Registry.__new__(Registry)
        native.valid=True;native.uncertain_extensions=set();native.fingerprint='current-native'
        native.filters={name:(self.CALC,frozenset(['xlsm']),frozenset(['IMPORT','EXPORT'])) for name in names}
        native.primary_extensions={name:'xlsm' for name in names}
        native.export_candidates={('xlsm',self.CALC):set(names)}
        native.preferred_filters={};native.exports={'xlsm':{self.CALC}};native.import_services={self.CALC}
        return native

    def test_two_installed_native_vba_variants_choose_standard_uno_recipe(self):
        from app_backend.converter_guard.export_recipe import native_export_recipe
        native=self.native([self.STANDARD,'Calc Office Open XML VBA'])
        proof={'state':'detected','basis':'native_preflight_uno_document','modelLoaded':True,
               'documentServices':[self.CALC],'registryFingerprint':native.fingerprint}
        recipe=native_export_recipe(native,'xlsm',proof)
        self.assertEqual(recipe.filter_name,self.STANDARD)
        self.assertEqual(recipe.cli_spec('xlsm'),'xlsm:'+self.STANDARD)
        self.assertIsNone(native.export_filter('xlsm',[self.WRITER]))
        self.assertIsNone(native_export_recipe(native,'xlsm',{**proof,'registryFingerprint':'stale'}))
        native.uncertain_extensions.add('xlsm')
        self.assertIsNone(native.export_filter('xlsm',[self.CALC]))

    def test_uninstalled_standard_never_fabricates_a_macro_filter(self):
        native=self.native(['custom1','custom2'])
        self.assertIsNone(native.export_filter('xlsm',[self.CALC]))


if __name__=='__main__':unittest.main()
