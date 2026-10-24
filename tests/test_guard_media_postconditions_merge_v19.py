"""Cross-route postconditions survive the independent media/backend merge."""
from pathlib import Path
import struct
import tempfile
import threading
import unittest
import zlib

from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard.media_fidelity import check_media_preservation
from app_backend.converter_guard.qt_media import convert_media_frames
from test_guard_media_fidelity_v16 import chunk, gif, tiff


class MergedMediaPostconditions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cancel = threading.Event()

    def png(self, alpha):
        path = self.root / 'source.png'
        path.write_bytes(b'\x89PNG\r\n\x1a\n' +
            chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 6, 0, 0, 0)) +
            chunk(b'IDAT', zlib.compress(bytes((0, 30, 70, 100, alpha)))) + chunk(b'IEND', b''))
        return path

    def test_tiff_to_pdf_independently_rejects_a_missing_page(self):
        from pypdf import PdfWriter
        source = self.root / 'source.tiff'
        source.write_bytes(tiff(pages=3))
        output = self.root / 'source.pdf'
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        with output.open('wb') as stream:
            writer.write(stream)
        with self.assertRaises(GuardError) as failure:
            check_media_preservation([output], 'pdf', source, cancel=self.cancel)
        self.assertEqual(failure.exception.code, 'MEDIA_PAGE_LOSS')

    def test_gif_cannot_silently_drop_representable_transparency(self):
        source = self.png(0)
        output = self.root / 'source.gif'
        output.write_bytes(gif(frames=1).replace(b'!\xf9\x04\x01', b'!\xf9\x04\x00'))
        with self.assertRaises(GuardError) as failure:
            check_media_preservation([output], 'gif', source, cancel=self.cancel)
        self.assertEqual(failure.exception.code, 'MEDIA_ALPHA_LOSS')

    def test_gif_semialpha_quantisation_remains_an_explicit_format_limit(self):
        source = self.png(200)
        [output] = convert_media_frames(source, self.root / 'output', 'gif', cancel=self.cancel)
        check = check_media_preservation([output], 'gif', source, cancel=self.cancel)
        self.assertIn('gif_palette_and_binary_alpha', check['expectedLosses'])
        self.assertFalse(check['qualityCompared'])


if __name__ == '__main__':
    unittest.main()
