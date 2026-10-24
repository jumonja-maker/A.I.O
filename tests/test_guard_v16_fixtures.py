"""Fixture validity checks are separate from native converter acceptance."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
import zipfile
from xml.etree import ElementTree as ET

FILE = Path(__file__).resolve().parents[1] / 'tools/qa/guard_v16_bugbag_acceptance.py'
SPEC = importlib.util.spec_from_file_location('guard_v16_fixtures', FILE)
qa = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(qa)


class NativeFixtureDefinitions(unittest.TestCase):
    def test_writer_control_has_all_48_cells_and_explicit_four_columns(self):
        root = ET.fromstring(qa.writer_seed())
        columns = root.findall('.//{%s}table-column' % qa.TABLE)
        self.assertEqual(len(columns), 1)
        self.assertEqual(columns[0].get('{%s}number-columns-repeated' % qa.TABLE), '4')
        cells = root.findall('.//{%s}table-cell' % qa.TABLE)
        self.assertEqual(len(cells), 48)
        self.assertEqual(tuple(''.join(c.itertext()) for c in cells), qa.MARKERS)
        self.assertEqual(root.get('{%s}mimetype' % qa.OFFICE), 'application/vnd.oasis.opendocument.text')

    def test_flat_presentation_body_matches_mimetype(self):
        root = ET.fromstring(qa.presentation_seed())
        self.assertEqual(root.get('{%s}mimetype' % qa.OFFICE), 'application/vnd.oasis.opendocument.presentation')
        self.assertIsNotNone(root.find('{%s}body/{%s}presentation' % (qa.OFFICE, qa.OFFICE)))
        self.assertIn(qa.SLIDE_MARKER, ''.join(root.itertext()))

    def test_original_inventory_does_not_repair_or_claim_vba(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            bad = root / 'bad.fodt'
            bad.write_text(qa.writer_seed().replace('opendocument.text"', 'opendocument.text-flat-xml"'))
            macro = root / 'no_macro.dotm'
            with zipfile.ZipFile(macro, 'w') as z:
                z.writestr('[Content_Types].xml', '<Types/>')
            before = {p.name: p.read_bytes() for p in root.iterdir()}
            inventory = {r['source']['name']: r for r in qa.classify_originals(root)}
            self.assertEqual(inventory['bad.fodt']['role'], 'negative_inconsistent_fixture')
            self.assertEqual(inventory['no_macro.dotm']['warning'], 'cannot_test_VBA_retention_without_actual_VBA')
            self.assertFalse(inventory['no_macro.dotm']['source']['hasVbaProject'])
            self.assertEqual(before, {p.name: p.read_bytes() for p in root.iterdir()})

    def test_independent_content_check_fails_for_dropped_cells(self):
        with tempfile.TemporaryDirectory() as folder:
            docx = Path(folder) / 'lost.docx'
            with zipfile.ZipFile(docx, 'w') as z:
                z.writestr('word/document.xml', '<w:document xmlns:w="%s"><w:body><w:p>'
                           '<w:r><w:t>%s</w:t></w:r></w:p></w:body></w:document>' % (qa.WORD, qa.MARKERS[0]))
            result = qa.check_markers(docx, qa.MARKERS)
            self.assertEqual(result['status'], 'fail')
            self.assertEqual(len(result['missingMarkers']), 47)

    def test_native_positive_suffix_requires_matching_main_content_type(self):
        facts = {'mainContentTypes': {'/word/document.xml':
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml'}}
        with self.assertRaises(ValueError):
            qa.positive_container_check(Path('fake.dotx'), facts)
        qa.positive_container_check(Path('real.docx'), facts)

    def test_undeclared_odf_columns_are_diagnostic_not_claimed_invalid(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'ambiguous.odt'
            xml = qa.writer_seed().replace('<table:table-column table:number-columns-repeated="4"/>', '')
            xml = xml.replace('<office:document ', '<office:document-content ').replace(
                '</office:document>', '</office:document-content>')
            with zipfile.ZipFile(source, 'w') as z:
                z.writestr('mimetype', 'application/vnd.oasis.opendocument.text')
                z.writestr('content.xml', xml)
            row, = qa.classify_originals(root)
            self.assertEqual(row['role'], 'diagnostic_original')
            self.assertEqual(row['source']['tableCells'], 48)
            self.assertIn('requires_content_check', row['warning'])


if __name__ == '__main__':
    unittest.main()
