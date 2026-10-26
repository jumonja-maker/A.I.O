"""Content/model mismatches from FULLTEST; no synthetic Windows-PASS claim."""
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from app_backend.converter_guard.adapter import LibreOfficeAdapter
from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard.export_recipe import native_export_recipe
from app_backend.converter_guard.native_identity import copy_chart_identity
from app_backend.converter_guard.routes import Registry
from app_backend.converter_guard.source_contract import (
    CALC, CHART, DRAW, IMPRESS, OFFICE, WRITER, inspect_source_contract, source_import_hint)
from test_converter_guard_v04 import component, node
import test_guard_uno_session as worker_contracts


class SourceContractsV16(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cancel = threading.Event()

    def flat(self, mime='text', body='text'):
        p = self.root / 'renamed.unknown'
        p.write_text(f'<office:document xmlns:office="{OFFICE}" '
                     f'office:mimetype="application/vnd.oasis.opendocument.{mime}">'
                     f'<office:body><office:{body}/></office:body></office:document>')
        return p

    def odf(self, mime='text', xml=None):
        p = self.root / 'renamed.unknown'
        with zipfile.ZipFile(p, 'w') as z:
            z.writestr('mimetype', 'application/vnd.oasis.opendocument.' + mime)
            z.writestr('content.xml', xml or f'<office:document-content xmlns:office="{OFFICE}">'
                       '<office:body><office:text/></office:body></office:document-content>')
        return p

    def registry(self):
        p = self.root / 'native.xcd'
        types = (node('flattext', Extensions='fodt odt xml') + node('flatcalc', Extensions='fods ods xml') +
                 node('legacy', Extensions='xml xls') + node('txt', Extensions='txt csv tab tsv') +
                 node('svm', Extensions='svm') + node('chart', Extensions='odc') + node('epub', Extensions='epub'))
        filters = (node('OpenDocument Text Flat XML', Type='flattext', DocumentService=WRITER, Flags='IMPORT EXPORT') +
                   node('OpenDocument Spreadsheet Flat XML', Type='flatcalc', DocumentService=CALC, Flags='IMPORT EXPORT') +
                   node('MS Excel 2003 XML', Type='legacy', DocumentService=CALC, Flags='EXPORT') +
                   node('Text (encoded)', Type='txt', DocumentService=WRITER, Flags='IMPORT EXPORT') +
                   node('SVM - StarView Metafile', Type='svm', DocumentService=DRAW, Flags='IMPORT') +
                   node('chart8', Type='chart', DocumentService=CHART, Flags='IMPORT EXPORT DEFAULT') +
                   node('EPUB', Type='epub', DocumentService=WRITER, Flags='EXPORT'))
        p.write_text('<oor:data xmlns:oor="http://openoffice.org/2001/registry">' +
                     component('Types', 'Types', types) + component('Filter', 'Filters', filters) + '</oor:data>')
        return Registry(((str(p), 0, p.stat().st_size),), p)

    def test_wrong_flat_mime_is_negative_while_valid_renamed_flat_has_content_contract(self):
        for mime, body, service in [('text', 'text', WRITER), ('presentation', 'presentation', IMPRESS)]:
            with self.subTest(mime=mime):
                contract = inspect_source_contract(self.flat(mime, body))
                self.assertEqual(contract['expectedServices'], [service])
                # A native-proven flat-presentation MIME alias is supported;
                # negative coverage must use a real body/type mismatch rather
                # than treating its suffix alone as corruption.
                invalid = inspect_source_contract(self.flat(mime + '-flat-xml',
                    'text' if body == 'presentation' else 'presentation'))
                self.assertEqual(invalid['code'], 'SOURCE_CONTAINER_INCONSISTENT')
        self.assertEqual(inspect_source_contract(self.flat('text', 'presentation'))['state'], 'invalid')

    def test_legacy_mime_and_modern_xml_do_not_pass_as_a_valid_sxw(self):
        p = self.odf()
        with zipfile.ZipFile(p, 'a') as z:
            # Use a separate package to avoid conflating duplicate-mime checks.
            xml = z.read('content.xml')
        with zipfile.ZipFile(p, 'w') as z:
            z.writestr('mimetype', 'application/vnd.sun.xml.writer')
            z.writestr('content.xml', xml)
        self.assertEqual(inspect_source_contract(p)['reason'], 'legacy_mimetype_namespace_mismatch')

    def test_implicit_odf_columns_are_not_falsely_declared_invalid(self):
        xml = (f'<o:document-content xmlns:o="{OFFICE}" '
               'xmlns:t="urn:oasis:names:tc:opendocument:xmlns:table:1.0">'
               '<o:body><o:text><t:table><t:table-row><t:table-cell/>'
               '<t:table-cell/></t:table-row></t:table></o:text></o:body></o:document-content>')
        self.assertEqual(inspect_source_contract(self.odf(xml=xml))['state'], 'recognized')
        master = inspect_source_contract(self.odf('text-master-template', xml=xml))
        self.assertEqual(master['expectedServices'], ['com.sun.star.text.GlobalDocument'])
        self.assertEqual(inspect_source_contract(self.odf('future-native-type'))['state'], 'not_checked')

    def test_ooxml_main_part_must_agree_with_declared_family(self):
        p = self.root / 'renamed.bin'
        def write(root):
            with zipfile.ZipFile(p, 'w') as z:
                z.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                           '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
                z.writestr('_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                           '<Relationship Id="r1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
                z.writestr('word/document.xml', root)
        write('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>')
        self.assertEqual(inspect_source_contract(p)['expectedServices'], [WRITER])
        write('<s:workbook xmlns:s="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>')
        self.assertEqual(inspect_source_contract(p)['reason'], 'ooxml_main_type_namespace_mismatch')

    def test_inspection_budget_does_not_veto_large_valid_documents(self):
        p = self.flat()
        self.assertEqual(inspect_source_contract(p, budget=1)['state'], 'not_checked')
        self.cancel.set()
        with self.assertRaises(GuardError) as error:
            inspect_source_contract(p, cancel=self.cancel)
        self.assertEqual(error.exception.code, 'CANCELLED')

    def test_dtd_entities_and_utf16_are_deferred_without_parser_expansion(self):
        p = self.root / 'entity.bin'
        dtd = '<!DOCTYPE office:document [<!ENTITY payload "A">]>'
        document = (f'<office:document xmlns:office="{OFFICE}" '
                    'office:mimetype="application/vnd.oasis.opendocument.text">'
                    '<office:body><office:text>&payload;</office:text></office:body></office:document>')
        p.write_text(dtd + document)
        self.assertEqual(inspect_source_contract(p)['state'], 'not_checked')
        # Declaration spans the parser's 64 KiB boundary after a legal comment.
        prefix = '<!--' + ' ' * (65536 - 4 - 3 - 4) + '-->'
        p.write_text(prefix + dtd + document)
        with zipfile.ZipFile(self.root / 'entity.odt', 'w') as z:
            z.writestr('mimetype', 'application/vnd.oasis.opendocument.text')
            z.writestr('content.xml', (prefix + dtd + document).encode())
        self.assertEqual(inspect_source_contract(self.root / 'entity.odt')['state'], 'not_checked')
        self.odf()
        with zipfile.ZipFile(self.root / 'utf16.odt', 'w') as z:
            z.writestr('mimetype', 'application/vnd.oasis.opendocument.text')
            z.writestr('content.xml', document.replace('&payload;', '').encode('utf-16'))
        self.assertEqual(inspect_source_contract(self.root / 'utf16.odt')['state'], 'not_checked')

    def test_malformed_flat_is_rejected_before_native_import_and_never_rewritten(self):
        source = self.flat('text-flat-xml', 'presentation')
        before = source.read_bytes()
        exe = self.root / 'soffice'; exe.touch()
        adapter = LibreOfficeAdapter(exe)
        native = self.registry()
        office = SimpleNamespace(disabled=False, request=lambda *a, **k: self.fail('negative source reached UNO'))
        adapter._local.batch = native, office
        with patch.object(adapter, '_registry', return_value=native):
            result = adapter.preflight(source, self.root / 'work', cancel=self.cancel, timeout=5)
        self.assertEqual(result['code'], 'SOURCE_CONTAINER_INCONSISTENT')
        self.assertEqual(source.read_bytes(), before)

    def test_correct_flat_import_is_explicit_but_a_different_actual_model_is_rejected(self):
        source = self.flat()
        native = self.registry()
        hint = source_import_hint(inspect_source_contract(source), native)
        self.assertEqual(hint['filter'], 'OpenDocument Text Flat XML')
        exe = self.root / 'soffice';exe.touch()
        adapter = LibreOfficeAdapter(exe)
        received = []
        def request(value, **kwargs):
            received.append(value)
            return {'ok': True, 'services': [CALC], 'filterName': 'OpenDocument Spreadsheet Flat XML'}
        adapter._local.batch = native, SimpleNamespace(disabled=False, request=request)
        with patch.object(adapter, '_registry', return_value=native):
            result = adapter.preflight(source, self.root / 'work', cancel=self.cancel, timeout=5)
        self.assertEqual(received[0]['expectedSourceServices'], [WRITER])
        self.assertEqual(received[0]['importFilter'], 'OpenDocument Text Flat XML')
        self.assertEqual(result['code'], 'SOURCE_MODEL_MISMATCH')

    def test_workers_reject_wrong_source_model_even_during_inspection(self):
        harness = worker_contracts.UnoWorkerContracts()
        for operation in ('inspect', 'export'):
            _, _, exports, replies = harness.execute([{'operation': operation, 'expectedSourceServices': [CALC]}])
            self.assertEqual(exports, [])
            self.assertEqual(replies[-1]['code'], 'SOURCE_MODEL_MISMATCH')
        _, _, exports, replies = harness.execute([{'expectedImportFilter': 'Ignored requested importer'}])
        self.assertEqual(exports, [])
        self.assertEqual(replies[-1]['code'], 'NATIVE_IMPORT_FILTER_CHANGED')

    def test_blocked_native_interaction_records_type_and_number_without_approving_it(self):
        def ask(options):
            options['InteractionHandler'].handle(SimpleNamespace(
                getRequest=lambda: SimpleNamespace(ErrCode=1234, Message='private source path'),
                getContinuations=lambda: []))
        harness = worker_contracts.UnoWorkerContracts()
        _, _, _, replies = harness.execute([{}], on_export=ask)
        self.assertFalse(replies[-1]['ok'])
        self.assertEqual(replies[-1]['code'], 'EXPORT_INTERACTION_REQUIRED')
        self.assertEqual(replies[-1]['interaction'], {'requestType': 'SimpleNamespace', 'errorCode': 1234})
        self.assertNotIn('private source path', str(replies))

    def test_txt_is_registered_while_import_only_svm_and_missing_report_are_explained(self):
        native = self.registry()
        proof = {'state': 'detected', 'basis': 'native_preflight_uno_document', 'modelLoaded': True,
                 'registryFingerprint': native.fingerprint, 'documentServices': [WRITER]}
        self.assertEqual(native.observed_decision('txt', proof), 'registered')
        self.assertEqual(native_export_recipe(native, 'txt', proof).filter_name, 'Text (encoded)')
        self.assertEqual(native.route_reason('svm', proof), 'NATIVE_FORMAT_IMPORT_ONLY')
        self.assertEqual(native.route_reason('orp', proof), 'NATIVE_REPORT_EXPORT_UNAVAILABLE')

    def test_generic_xml_uses_explicit_native_dialect_and_never_forces_a_missing_filter(self):
        native = self.registry()
        proof = {'state': 'detected', 'basis': 'native_preflight_uno_document', 'modelLoaded': True,
                 'registryFingerprint': native.fingerprint, 'documentServices': [CALC]}
        recipe = native_export_recipe(native, 'xml', proof)
        self.assertEqual(recipe.filter_name, 'OpenDocument Spreadsheet Flat XML')
        self.assertIn('NATIVE_XML_ODF_DIALECT', recipe.warnings)
        del native.filters['OpenDocument Spreadsheet Flat XML']
        fallback = native_export_recipe(native, 'xml', proof)
        self.assertTrue(fallback is None or fallback.filter_name != 'OpenDocument Spreadsheet Flat XML')

    def test_odc_identity_requires_native_chart_proof_and_full_zip_crc(self):
        source = self.odf('chart', f'<o:document-content xmlns:o="{OFFICE}"><o:body><o:chart/></o:body></o:document-content>')
        contract = inspect_source_contract(source)
        proof = {'state': 'detected', 'sourceContract': contract, 'documentServices': [CHART]}
        result = copy_chart_identity(source, self.root / 'out', 'odc', proof, cancel=self.cancel)
        self.assertEqual(result[0].read_bytes(), source.read_bytes())
        self.assertIsNone(copy_chart_identity(source, self.root / 'out2', 'pdf', proof, cancel=self.cancel))
        self.assertIsNone(copy_chart_identity(source, self.root / 'out3', 'odc', {}, cancel=self.cancel))
        # Exclusive-create failure must preserve a result already on disk.
        with self.assertRaises(FileExistsError):
            copy_chart_identity(source, self.root / 'out', 'odc', proof, cancel=self.cancel)
        self.assertEqual(result[0].read_bytes(), source.read_bytes())
        data = bytearray(source.read_bytes())
        where = data.index(b'<o:document-content')
        data[where] ^= 1
        source.write_bytes(data)
        with self.assertRaises(GuardError) as error:
            copy_chart_identity(source, self.root / 'out4', 'odc', proof, cancel=self.cancel)
        self.assertEqual(error.exception.code, 'SOURCE_CONTAINER_INVALID')

    def test_admitted_odc_identity_skips_native_export_and_obeys_timeout(self):
        source = self.odf('chart', f'<o:document-content xmlns:o="{OFFICE}"><o:body><o:chart/></o:body></o:document-content>')
        native = self.registry()
        proof = {'state': 'detected', 'basis': 'native_preflight_uno_document', 'modelLoaded': True,
                 'registryFingerprint': native.fingerprint, 'documentServices': [CHART],
                 'sourceContract': inspect_source_contract(source)}
        exe = self.root / 'soffice';exe.touch()
        adapter = LibreOfficeAdapter(exe)
        with patch.object(adapter, '_registry', return_value=native), \
                patch.object(adapter, '_convert_native', side_effect=AssertionError('identity called LO')):
            adapter.prepare_source(proof)
            paths = adapter.convert(source, 'odc', self.root / 'identity', cancel=self.cancel,
                                    timeout=None, on_started=lambda: self.fail('LO was not started'))
            self.assertEqual(paths[0].read_bytes(), source.read_bytes())
            self.assertEqual(adapter.execution_mode(), 'native_identity')
            self.assertEqual(adapter.export_info()['resultChecks'][0]['status'], 'byte_preserved')
            # Windows clocks may report the same tick repeatedly; expire the
            # configured deadline deterministically instead of using 1 ns.
            with patch('app_backend.converter_guard.adapter.time.monotonic', side_effect=[10, 11]), \
                    self.assertRaises(GuardError) as error:
                adapter.convert(source, 'odc', self.root / 'timeout', cancel=self.cancel,
                                timeout=.5, on_started=lambda: None)
            self.assertEqual(error.exception.code, 'ENGINE_TIMEOUT')


if __name__ == '__main__':
    unittest.main()
