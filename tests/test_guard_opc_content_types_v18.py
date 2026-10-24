"""OPC Default resolution must not reject valid spreadsheets from FULLTEST."""
from pathlib import Path
import tempfile
import unittest
import zipfile

from app_backend.converter_guard.source_contract import CALC, inspect_source_contract


TYPES = 'http://schemas.openxmlformats.org/package/2006/content-types'
RELS = 'http://schemas.openxmlformats.org/package/2006/relationships'
OFFICE_REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument'
CALC_TYPE = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml'
WORD_TYPE = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml'
CALC_XML = '<s:workbook xmlns:s="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>'


class OPCContentTypes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def package(self, declarations=None, *, part='xl/workbook.xml', xml=CALC_XML,
                relationship_type=OFFICE_REL, relationship=True):
        # The contract is content driven, including for an unrelated suffix.
        path = self.root/'renamed.bin'
        with zipfile.ZipFile(path, 'w') as archive:
            if declarations is not None:
                archive.writestr('[Content_Types].xml',
                    '<ct:Types xmlns:ct="'+TYPES+'">'+declarations+'</ct:Types>')
            if relationship:
                archive.writestr('_rels/.rels', '<Relationships xmlns="'+RELS+'">'
                    '<Relationship Id="r1" Type="'+relationship_type+'" Target="/'+part+'"/>'
                    '</Relationships>')
            archive.writestr(part, xml)
        return path

    def default(self, extension='xml', kind=CALC_TYPE):
        return '<ct:Default Extension="'+extension+'" ContentType="'+kind+'"/>'

    def override(self, kind=CALC_TYPE):
        return '<ct:Override PartName="/xl/workbook.xml" ContentType="'+kind+'"/>'

    def test_default_only_main_type_recognizes_a_real_calc_root(self):
        proof = inspect_source_contract(self.package(self.default()))
        self.assertEqual(proof['state'], 'recognized')
        self.assertEqual(proof['expectedServices'], [CALC])
        self.assertEqual(proof['mainContentType'], CALC_TYPE)

    def test_exact_override_takes_precedence_over_extension_default(self):
        proof = inspect_source_contract(self.package(self.default(kind=WORD_TYPE)+self.override()))
        self.assertEqual(proof['expectedServices'], [CALC])

    def test_default_extension_match_is_case_insensitive(self):
        for extension, part in [('XML', 'xl/workbook.xml'), ('xml', 'xl/workbook.XML')]:
            with self.subTest(extension=extension, part=part):
                proof = inspect_source_contract(self.package(self.default(extension), part=part))
                self.assertEqual(proof['expectedServices'], [CALC])

    def test_default_does_not_bypass_actual_main_namespace_check(self):
        proof = inspect_source_contract(self.package(self.default(),
            xml='<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>'))
        self.assertEqual(proof['reason'], 'ooxml_main_type_namespace_mismatch')

    def test_ambiguous_defaults_and_duplicate_overrides_remain_invalid(self):
        for declarations in [self.default()+self.default('XML'),
                             self.default()+self.override()+self.override()]:
            with self.subTest(declarations=declarations):
                proof = inspect_source_contract(self.package(declarations))
                self.assertEqual(proof['code'], 'SOURCE_CONTAINER_INCONSISTENT')
                self.assertEqual(proof['reason'], 'ooxml_main_content_type')

    def test_unmatched_and_foreign_namespace_defaults_cannot_declare_main_type(self):
        for declarations in [self.default('rels'),
            '<other:Default xmlns:other="urn:unrelated" Extension="xml" ContentType="'+CALC_TYPE+'"/>']:
            with self.subTest(declarations=declarations):
                proof = inspect_source_contract(self.package(declarations))
                self.assertEqual(proof['reason'], 'ooxml_main_content_type')

    def test_missing_content_types_is_invalid_only_with_office_relationship_and_root(self):
        proof = inspect_source_contract(self.package())
        self.assertEqual(proof['code'], 'SOURCE_CONTAINER_INCONSISTENT')
        self.assertEqual(proof['reason'], 'missing_ooxml_content_types')

    def test_generic_xml_zip_without_content_types_is_not_falsely_called_office(self):
        for options in [dict(relationship=False), dict(relationship_type='urn:my-app/document'),
                        dict(xml='<workbook xmlns="urn:my-app"/>')]:
            with self.subTest(options=options):
                self.assertEqual(inspect_source_contract(self.package(**options))['state'], 'not_applicable')

    def test_missing_content_type_probe_respects_inspection_budget(self):
        proof = inspect_source_contract(self.package(), budget=1)
        self.assertEqual(proof['state'], 'not_checked')
        self.assertEqual(proof['reason'], 'source_contract_budget')


if __name__ == '__main__':
    unittest.main()
