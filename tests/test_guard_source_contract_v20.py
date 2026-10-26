"""OPC declarations and MathML packages: positive native-model contracts and negatives.

Fixtures are generated without altering user source bytes. The contract recognizes
metadata only; source_model_matches is still required after native model detection.
"""
from hashlib import sha256
from pathlib import Path
import tempfile
import threading
import unittest
import warnings
import zipfile

from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard.source_contract import (
    CALC, FORMULA, IMPRESS, MATHML, OFFICE, WRITER, inspect_source_contract, source_model_matches,
    source_import_hint)

CT = 'http://schemas.openxmlformats.org/package/2006/content-types'
REL = 'http://schemas.openxmlformats.org/package/2006/relationships'
OFFICE_REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument'
CALC_TYPE = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml'
WRITER_TYPE = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml'
WORKBOOK = '<s:workbook xmlns:s="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>'
DOCUMENT = '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>'


class SourceContractV20(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def package(self, declarations=None, *, target='xl/workbook.xml', item='xl/workbook.xml',
                document=WORKBOOK, extra=(), mode='', rel_type=OFFICE_REL, relationships=''):
        path = self.root / 'renamed.bin'
        if declarations is None:
            declarations = f'<Default Extension="xml" ContentType="{CALC_TYPE}"/>'
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('[Content_Types].xml', f'<Types xmlns="{CT}">{declarations}</Types>')
                archive.writestr('_rels/.rels', f'<Relationships xmlns="{REL}">'
                    f'<Relationship Id="main" Type="{rel_type}" Target="{target}" {mode}/>'
                    f'{relationships}</Relationships>')
                archive.writestr(item, document)
                for name, content in extra:
                    archive.writestr(name, content)
        return path

    def contract(self, *args, **kwargs):
        path = self.package(*args, **kwargs)
        before = sha256(path.read_bytes()).hexdigest()
        contract = inspect_source_contract(path)
        self.assertEqual(sha256(path.read_bytes()).hexdigest(), before)
        return contract

    def formula(self, *, mime='formula', xml=None):
        path = self.root / 'math.unknown'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('mimetype', 'application/vnd.oasis.opendocument.' + mime)
            archive.writestr('content.xml', xml or f'<math xmlns="{MATHML}">'
                '<mrow><mn>2</mn><mo>+</mo><mn>2</mn><mo>=</mo><mn>4</mn></mrow></math>')
        return path

    def test_default_content_type_is_a_real_calc_contract_not_a_filename_hint(self):
        contract = self.contract()
        self.assertEqual(contract['state'], 'recognized')
        self.assertEqual(contract['contentTypeBasis'], 'default')
        self.assertEqual(contract['expectedServices'], [CALC])
        self.assertTrue(source_model_matches(contract, [CALC]))
        self.assertFalse(source_model_matches(contract, [WRITER]))
        self.assertFalse(source_model_matches(contract, []))

    def test_override_precedes_incompatible_default(self):
        declarations = (f'<Default Extension="xml" ContentType="{WRITER_TYPE}"/>'
            f'<Override PartName="/xl/workbook.xml" ContentType="{CALC_TYPE}"/>')
        self.assertEqual(self.contract(declarations)['contentTypeBasis'], 'override')
        declarations = (f'<Default Extension="xml" ContentType="{CALC_TYPE}"/>'
            f'<Override PartName="/xl/workbook.xml" ContentType="{WRITER_TYPE}"/>')
        self.assertEqual(self.contract(declarations)['reason'], 'ooxml_main_type_namespace_mismatch')

    def test_case_insensitive_extension_and_part_equivalence(self):
        declaration = f'<Default Extension="XML" ContentType="{CALC_TYPE}"/>'
        self.assertEqual(self.contract(declaration, target='/XL/WORKBOOK.XML')['expectedServices'], [CALC])
        declaration = f'<Override PartName="/XL/WORKBOOK.XML" ContentType="{CALC_TYPE}"/>'
        self.assertEqual(self.contract(declaration)['expectedServices'], [CALC])

    def test_internal_root_relationship_dot_segments_resolve_without_leaving_package(self):
        for target in ['/xl/workbook.xml', './xl/workbook.xml', 'unused/../xl/workbook.xml']:
            with self.subTest(target=target):
                self.assertEqual(self.contract(target=target)['expectedServices'], [CALC])

    def test_percent_encoded_zip_part_maps_without_decoding_to_filesystem(self):
        declaration = f'<Override PartName="/xl/sheet%20data.xml" ContentType="{CALC_TYPE}"/>'
        self.assertEqual(self.contract(declaration, target='/xl/sheet%20data.xml',
            item='xl/sheet%20data.xml')['expectedServices'], [CALC])
        declaration = f'<Override PartName="/xl/%C3%A9.xml" ContentType="{CALC_TYPE}"/>'
        self.assertEqual(self.contract(declaration, target='/xl/%c3%a9.xml',
            item='xl/%C3%A9.xml')['expectedServices'], [CALC])

    def test_external_fragment_query_controls_and_traversal_are_not_native_proof(self):
        for target in ['../xl/workbook.xml', '/../../xl/workbook.xml', '//host/xl/workbook.xml',
            'file:///xl/workbook.xml', 'https://host/xl/workbook.xml', '/xl/workbook.xml#frag',
            '/xl/workbook.xml?', '/xl/workbook.xml#', '/xl/workbook.xml?x=1',
            'xl\\workbook.xml', '/xl/%2fworkbook.xml', '/xl/%5cworkbook.xml',
            '/xl/%2e%2e/workbook.xml', '/xl/%00.xml', '/xl/%09.xml', '/xl/%bad%xx.xml']:
            with self.subTest(target=target):
                self.assertEqual(self.contract(target=target)['state'], 'invalid')
        self.assertEqual(self.contract(mode='TargetMode="External"')['reason'], 'ooxml_main_relationship')

    def test_duplicate_members_and_case_aliases_are_ambiguous(self):
        for name in ['xl/workbook.xml', 'XL/WORKBOOK.XML']:
            with self.subTest(name=name):
                self.assertEqual(self.contract(extra=[(name, WORKBOOK)])['reason'],
                    'ooxml_main_part_missing_or_ambiguous')

    def test_duplicate_defaults_and_overrides_cannot_hide_a_conflict(self):
        for declarations in [
            f'<Default Extension="xml" ContentType="{CALC_TYPE}"/>' * 2,
            f'<Default Extension="xml" ContentType="{CALC_TYPE}"/>'
            f'<Default Extension="XML" ContentType="{WRITER_TYPE}"/>',
            f'<Override PartName="/xl/workbook.xml" ContentType="{CALC_TYPE}"/>' * 2,
            f'<Override PartName="/xl/workbook.xml" ContentType="{CALC_TYPE}"/>'
            f'<Override PartName="/XL/WORKBOOK.XML" ContentType="{WRITER_TYPE}"/>',
            f'<Default Extension="xml" ContentType="{CALC_TYPE}"/>'
            f'<Override PartName="/xl/../xl/workbook.xml" ContentType="{CALC_TYPE}"/>']:
            with self.subTest(declarations=declarations):
                self.assertEqual(self.contract(declarations)['reason'], 'ooxml_content_type_declarations')

    def test_unknown_namespace_cannot_impersonate_type_or_relationship(self):
        declaration = f'<Other xmlns="urn:other" PartName="/xl/workbook.xml" ContentType="{CALC_TYPE}"/>'
        self.assertEqual(self.contract(declaration)['reason'], 'ooxml_main_content_type')
        self.assertEqual(self.contract(rel_type='urn:other/officeDocument')['reason'], 'ooxml_main_relationship')

    def test_duplicate_main_relationship_and_relationship_id_are_rejected(self):
        second = f'<Relationship Id="other" Type="{OFFICE_REL}" Target="xl/workbook.xml"/>'
        self.assertEqual(self.contract(relationships=second)['reason'], 'ooxml_main_relationship')
        second = '<Relationship Id="main" Type="urn:other" Target="another.xml"/>'
        self.assertEqual(self.contract(relationships=second)['reason'], 'ooxml_relationship_ids')

    def test_strict_main_namespace_is_supported_without_relaxing_family(self):
        strict = '<s:workbook xmlns:s="http://purl.oclc.org/ooxml/spreadsheetml/main"/>'
        self.assertEqual(self.contract(document=strict)['expectedServices'], [CALC])
        self.assertEqual(self.contract(document=DOCUMENT)['reason'], 'ooxml_main_type_namespace_mismatch')

    def test_mathml_formula_package_demands_formula_native_model(self):
        contract = inspect_source_contract(self.formula())
        self.assertEqual(contract['state'], 'recognized')
        self.assertEqual(contract['contentClass'], 'odf_formula_package')
        self.assertEqual(contract['expectedServices'], [FORMULA])
        self.assertTrue(source_model_matches(contract, [FORMULA]))
        self.assertFalse(source_model_matches(contract, [CALC]))
        self.assertFalse(source_model_matches(contract, []))

    def test_mathml_does_not_authorize_a_writer_or_wrong_namespace_package(self):
        self.assertEqual(inspect_source_contract(self.formula(mime='text'))['state'], 'invalid')
        self.assertEqual(inspect_source_contract(self.formula(xml='<math xmlns="urn:wrong"/>'))['state'], 'invalid')
        xml = f'<o:document-content xmlns:o="{OFFICE}"><o:body><o:text/></o:body></o:document-content>'
        self.assertEqual(inspect_source_contract(self.formula(xml=xml))['state'], 'invalid')

    def test_native_flat_presentation_alias_requires_body_installed_import_and_model(self):
        from types import SimpleNamespace
        path = self.root / 'presentation.unknown'
        path.write_text(f'<o:document xmlns:o="{OFFICE}" '
            'o:mimetype="application/vnd.oasis.opendocument.presentation-flat-xml">'
            '<o:body><o:presentation/></o:body></o:document>')
        contract = inspect_source_contract(path)
        self.assertEqual(contract['state'], 'recognized')
        self.assertEqual(contract['expectedServices'], [IMPRESS])
        self.assertEqual(contract['warnings'], ['SOURCE_MIMETYPE_ALIAS'])
        self.assertTrue(contract['nativeImportRequired'])
        name = 'OpenDocument Presentation Flat XML'
        native = SimpleNamespace(valid=True, filters={name: (IMPRESS, (), {'IMPORT'})})
        self.assertEqual(source_import_hint(contract, native)['filter'], name)
        self.assertFalse(source_model_matches(contract, [CALC]))
        self.assertFalse(source_model_matches(contract, [WRITER]))
        self.assertTrue(source_model_matches(contract, [IMPRESS]))
        native.filters = {}
        self.assertIsNone(source_import_hint(contract, native))

    def test_flat_alias_never_accepts_wrong_missing_or_foreign_body(self):
        for body in ['<o:body><o:text/></o:body>', '<o:body/>',
                '<x:body xmlns:x="urn:foreign"><o:presentation/></x:body>']:
            with self.subTest(body=body):
                path = self.root / 'presentation.unknown'
                path.write_text(f'<o:document xmlns:o="{OFFICE}" '
                    'o:mimetype="application/vnd.oasis.opendocument.presentation-flat-xml">'
                    + body + '</o:document>')
                self.assertEqual(inspect_source_contract(path)['state'], 'invalid')

    def test_native_flat_text_alias_uses_installed_writer_import_and_exact_model(self):
        from types import SimpleNamespace
        path = self.root / 'text.unknown'
        path.write_text(f'<o:document xmlns:o="{OFFICE}" '
            'o:mimetype="application/vnd.oasis.opendocument.text-flat-xml">'
            '<o:body><o:text/></o:body></o:document>')
        contract = inspect_source_contract(path)
        self.assertEqual(contract['state'], 'recognized')
        self.assertEqual(contract['expectedServices'], [WRITER])
        self.assertEqual(contract['warnings'], ['SOURCE_MIMETYPE_ALIAS'])
        self.assertEqual(contract['importProfile'], 'flat_text_mimetype_alias')
        name = 'OpenDocument Text Flat XML'
        native = SimpleNamespace(valid=True, filters={name: (WRITER, (), {'IMPORT'})})
        self.assertEqual(source_import_hint(contract, native)['filter'], name)
        self.assertFalse(source_model_matches(contract, [CALC]))
        self.assertFalse(source_model_matches(contract, [IMPRESS]))
        native.filters[name] = (CALC, (), {'IMPORT'})
        self.assertIsNone(source_import_hint(contract, native))
        path.write_text(path.read_text().replace('<o:text/>', '<o:presentation/>'))
        self.assertEqual(inspect_source_contract(path)['state'], 'invalid')

    def test_flat_text_alias_keeps_existing_explicit_column_preparation(self):
        from app_backend.converter_guard.odf_table_import import prepare_odf_table_source
        table = 'urn:oasis:names:tc:opendocument:xmlns:table:1.0'
        text = 'urn:oasis:names:tc:opendocument:xmlns:text:1.0'
        path = self.root / 'text.fodt'
        path.write_text(f'<o:document xmlns:o="{OFFICE}" xmlns:t="{table}" xmlns:x="{text}" '
            'o:mimetype="application/vnd.oasis.opendocument.text-flat-xml"><o:body><o:text>'
            '<t:table><t:table-row><t:table-cell><x:p>one</x:p></t:table-cell>'
            '<t:table-cell><x:p>two</x:p></t:table-cell></t:table-row></t:table>'
            '</o:text></o:body></o:document>')
        before = path.read_bytes()
        prepared, proof = prepare_odf_table_source(path, self.root / 'prep', cancel=threading.Event())
        self.assertNotEqual(prepared, path)
        self.assertEqual(proof['columns'], [2])
        self.assertEqual(path.read_bytes(), before)
        self.assertIn(b'one</x:p>', prepared.read_bytes())
        self.assertIn(b'two</x:p>', prepared.read_bytes())
        self.assertEqual(inspect_source_contract(prepared)['expectedServices'], [WRITER])

    def test_alias_cannot_fall_back_to_automatic_import_when_its_importer_is_missing(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from app_backend.converter_guard.adapter import LibreOfficeAdapter, prepare_profile
        from app_backend.converter_guard.preflight import inspect_source
        import sys
        source = self.root / 'alias.fodp'
        source.write_text(f'<o:document xmlns:o="{OFFICE}" '
            'o:mimetype="application/vnd.oasis.opendocument.presentation-flat-xml">'
            '<o:body><o:presentation/></o:body></o:document>')
        native = SimpleNamespace(valid=True, filters={})
        adapter = LibreOfficeAdapter(sys.executable)
        with patch.object(adapter, '_registry', return_value=native):
            result = adapter._preflight_native(source, self.root / 'adapter',
                cancel=threading.Event(), timeout=5)
        self.assertEqual(result['code'], 'SOURCE_NATIVE_IMPORT_FILTER_REQUIRED')
        result = inspect_source(Path(sys.executable), source, self.root / 'standalone',
            cancel=threading.Event(), timeout=5, prepare_profile=prepare_profile, native=native)
        self.assertEqual(result['code'], 'SOURCE_NATIVE_IMPORT_FILTER_REQUIRED')
        self.assertFalse((self.root / 'adapter').exists())
        self.assertFalse((self.root / 'standalone').exists())

    def test_budget_and_cancel_do_not_become_content_or_route_proof(self):
        path = self.package()
        self.assertEqual(inspect_source_contract(path, budget=10)['state'], 'not_checked')
        cancel = threading.Event(); cancel.set()
        with self.assertRaises(GuardError) as failure:
            inspect_source_contract(path, cancel=cancel)
        self.assertEqual(failure.exception.code, 'CANCELLED')
        path = self.formula()
        self.assertEqual(inspect_source_contract(path, budget=10)['state'], 'not_checked')


if __name__ == '__main__':
    unittest.main()
