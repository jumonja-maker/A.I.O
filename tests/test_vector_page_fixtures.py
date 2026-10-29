"""The native acceptance inputs must declare the correct ODF component."""
import unittest
import xml.etree.ElementTree as ET
from tools.qa.fulltest_vector_pages_native import fixture, TOKENS

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


if __name__ == '__main__':
    unittest.main()
