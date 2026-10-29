"""Prove QA rejects repeated/wrong page markers, not just page counts."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from PIL import Image
from tools.qa.fulltest_vector_pages_native import inspect_page, TOKENS


class VectorPageAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def test_distinct_bitmap_markers_are_accepted(self):
        for index, colour in enumerate(((255,0,0),(0,0,255),(0,170,0))):
            with self.subTest(page=index):
                path=self.root/('page-%d.bmp'%index)
                Image.new('RGB',(80,80),colour).save(path)
                self.assertEqual(inspect_page(path,index)['page'],index+1)

    def test_repeated_last_bitmap_is_not_first_page(self):
        path=self.root/'page.bmp'
        Image.new('RGB',(80,80),(0,170,0)).save(path)
        with self.assertRaises(AssertionError):inspect_page(path,0)

    def test_blank_bitmap_cannot_pass(self):
        path=self.root/'page.bmp'
        Image.new('RGB',(80,80),'white').save(path)
        with self.assertRaises(AssertionError):inspect_page(path,0)

    def test_correct_marker_plus_other_page_marker_fails(self):
        path=self.root/'page.bmp'
        image=Image.new('RGB',(80,80),(255,0,0))
        image.paste((0,0,255),(0,0,40,80));image.save(path)
        with self.assertRaises(AssertionError):inspect_page(path,0)

    def test_renamed_non_bmp_is_rejected(self):
        path=self.root/'page.bmp'
        Image.new('RGB',(80,80),(255,0,0)).save(path,format='PNG')
        with self.assertRaises(AssertionError):inspect_page(path,0)

    def test_vector_tokens_in_both_native_text_encodings(self):
        for extension in ('emf','wmf'):
            for encoding in ('ascii','utf-16-le'):
                for page in range(3):
                    with self.subTest(extension=extension,encoding=encoding,page=page):
                        path=self.root/('page.'+extension)
                        path.write_bytes(b'prefix'+TOKENS[page].encode(encoding)+b'suffix')
                        self.assertEqual(inspect_page(path,page)['textTokens'],
                                         [page==i for i in range(3)])

    def test_missing_wrong_or_combined_vector_tokens_fail(self):
        path=self.root/'page.emf'
        for payload in (b'',TOKENS[2].encode(),(' '.join(TOKENS)).encode()):
            with self.subTest(payload=payload):
                path.write_bytes(payload)
                with self.assertRaises(AssertionError):inspect_page(path,0)


if __name__=='__main__':unittest.main()
