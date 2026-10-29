"""The native acceptance inputs must declare the correct ODF component."""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile
import posixpath
import xml.etree.ElementTree as ET
from tools.qa.fulltest_vector_pages_native import fixture, TOKENS
from tools.qa.vector_page_fixture import write_pptx_fixture, COLOURS, A, P, R

OFFICE = 'urn:oasis:names:tc:opendocument:xmlns:office:1.0'
DRAW = 'urn:oasis:names:tc:opendocument:xmlns:drawing:1.0'


class VectorPageFixtures(unittest.TestCase):
    def check_fixture(self, kind, mime, component):
        root = ET.fromstring(fixture(kind))
        self.assertEqual(root.attrib['{'+OFFICE+'}mimetype'],
                         'application/vnd.oasis.opendocument.'+mime)
        body = root.find('{'+OFFICE+'}body/{'+OFFICE+'}'+component)
        self.assertIsNotNone(body)
        pages = body.findall('{'+DRAW+'}page')
        self.assertEqual(len(pages),3)
        for index,page in enumerate(pages):
            content = ''.join(page.itertext())
            self.assertEqual([token in content for token in TOKENS],
                             [index == other for other in range(3)])
    def test_fodp_declares_presentation(self):
        self.check_fixture('fodp','presentation','presentation')
    def test_fodg_declares_graphics_not_drawing_mimetype(self):
        self.check_fixture('fodg','graphics','drawing')
    def test_unknown_fixture_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            fixture('not-an-odf-type')


class IndependentPptxFixtures(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'source.pptx'
        write_pptx_fixture(self.path)

    def test_each_slide_has_its_own_explicit_rgb_and_text(self):
        ns={'p':P,'a':A}
        with ZipFile(self.path) as archive:
            for index,colour in enumerate(COLOURS,1):
                page=ET.fromstring(archive.read('ppt/slides/slide%d.xml'%index))
                text=''.join(page.itertext())
                self.assertEqual([token in text for token in TOKENS],[index==i+1 for i in range(3)])
                shapes=page.findall('.//p:sp',ns)
                self.assertEqual(len(shapes),2)
                marker=shapes[1]
                self.assertEqual(marker.find('p:spPr/a:solidFill/a:srgbClr',ns).attrib['val'],colour)
                self.assertIsNone(marker.find('.//a:schemeClr',ns))

    def test_all_relationship_targets_exist_without_external_links(self):
        with ZipFile(self.path) as archive:
            names=set(archive.namelist())
            for name in names:
                if not name.endswith('.rels'):continue
                directory='' if name=='_rels/.rels' else name.rsplit('/_rels/',1)[0]
                for relationship in ET.fromstring(archive.read(name)):
                    self.assertNotEqual(relationship.attrib.get('TargetMode'),'External')
                    target=posixpath.normpath(posixpath.join(directory,relationship.attrib['Target']))
                    self.assertIn(target,names,(name,target))
            slides=ET.fromstring(archive.read('ppt/presentation.xml')).find('{'+P+'}sldIdLst')
            self.assertEqual(len(slides),3)

    def test_fixture_is_deterministic(self):
        second=self.path.with_name('another.pptx')
        write_pptx_fixture(second)
        self.assertEqual(self.path.read_bytes(),second.read_bytes())

    def test_existing_file_is_not_overwritten(self):
        original=self.path.read_bytes()
        with self.assertRaises(FileExistsError):write_pptx_fixture(self.path)
        self.assertEqual(self.path.read_bytes(),original)


if __name__ == '__main__':
    unittest.main()
