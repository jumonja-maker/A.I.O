"""M14: real sample declarations, not fabricated evidence of LO support."""
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

from tools.qa.m14_native_matrix import FAMILIES, make_samples, write_odf, TARGETS
from app_backend.converter_guard.source_contract import inspect_source_contract


class M14SampleContracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def test_odc_odm_otm_have_true_distinct_contents(self):
        samples, negative = make_samples(self.root)
        self.assertEqual(set(samples), {'odc', 'odm', 'otm', 'psd'})
        for extension, (kind, _, service) in FAMILIES.items():
            with self.subTest(extension=extension):
                path = samples[extension]
                self.assertEqual(path.suffix, '.' + extension)
                with ZipFile(path) as archive:
                    self.assertEqual(archive.testzip(), None)
                    self.assertEqual(archive.read('mimetype').decode('ascii'),
                                     'application/vnd.oasis.opendocument.' + kind)
                contract = inspect_source_contract(path)
                self.assertEqual(contract['state'], 'recognized', contract)
                self.assertEqual(contract['expectedServices'], [service], contract)
        self.assertEqual(negative.suffix, '.orp')
        self.assertEqual(inspect_source_contract(negative)['state'], 'not_applicable')

    def test_mime_body_mismatch_is_not_a_successful_format(self):
        path = write_odf(self.root / 'mismatch.odm', 'text-master', 'chart')
        result = inspect_source_contract(path)
        self.assertEqual(result['state'], 'invalid')
        self.assertEqual(result['code'], 'SOURCE_CONTAINER_INCONSISTENT')

    def test_minimal_psd_is_decodable_not_merely_a_suffix(self):
        from PIL import Image
        samples, _ = make_samples(self.root)
        with Image.open(samples['psd']) as im:
            self.assertEqual(im.format, 'PSD')
            self.assertEqual(im.size, (2, 2))
            self.assertEqual(im.convert('RGB').getpixel((0, 0)), (11, 51, 91))
        self.assertEqual(inspect_source_contract(samples['psd'])['state'], 'not_applicable')

    def test_orp_cannot_be_promoted_from_an_unsupported_filename(self):
        self.assertIn('orp', TARGETS)
        self.assertNotIn('orp', FAMILIES)
        self.assertNotIn('orp', make_samples(self.root)[0])


if __name__ == '__main__':
    unittest.main()
