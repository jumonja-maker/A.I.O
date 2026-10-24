"""TIFF fallback compared against an independent encoder/decoder in dev QA."""
import io
from pathlib import Path
import random
import struct
import tempfile
import threading
import tracemalloc
import unittest
from unittest.mock import patch
import zlib

from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard.tiff_frames import (
    inspect_tiff_decode_support, iter_tiff_frames)

try:
    from PySide6.QtGui import QImage, QImageReader
except ImportError:
    QImage = None
try:
    from PIL import Image, TiffImagePlugin
except ImportError:
    Image = None


def patch_tag(data, tag, value):
    """Change one classic little-endian scalar tag in a test fixture."""
    offset = struct.unpack_from('<I', data, 4)[0]
    count = struct.unpack_from('<H', data, offset)[0]
    for i in range(count):
        position = offset+2+i*12
        observed, typ, amount = struct.unpack_from('<HHI', data, position)
        if observed == tag:
            if amount != 1:
                raise AssertionError('scalar fixture expected')
            struct.pack_into('<H' if typ == 3 else '<I', data, position+8, value)
            return
    raise AssertionError('missing tag')


@unittest.skipIf(QImage is None or Image is None, 'Qt and independent Pillow decoder required')
class TiffFallback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def fixture(self, compression, *, alpha=True, predictor=False):
        rng = random.Random(923)
        frames = []
        for n in range(3):
            width, height = 64+n*3, 40+n*2
            samples = 4 if alpha else 3
            # Noisy images force TIFF LZW's code-width transitions/reset.
            pixels = bytes(rng.randrange(256) for _ in range(width*height*samples))
            frames.append(Image.frombytes('RGBA' if alpha else 'RGB', (width, height), pixels))
        path = self.root/(compression+('-rgb' if not alpha else '-rgba')+'.tiff')
        tags = TiffImagePlugin.ImageFileDirectory_v2()
        if predictor:
            tags[317] = 2
        frames[0].save(path, save_all=True, append_images=frames[1:], compression=compression,
                       tiffinfo=tags, dpi=(150, 200))
        return path

    def compare(self, path):
        decoded = list(iter_tiff_frames(path))
        with Image.open(path) as expected:
            self.assertEqual(len(decoded), expected.n_frames)
            for n, image in enumerate(decoded):
                expected.seek(n)
                self.assertEqual((image.width(), image.height()), expected.size)
                rgba = image.convertToFormat(QImage.Format.Format_RGBA8888)
                self.assertEqual(bytes(rgba.constBits()), expected.convert('RGBA').tobytes())
        return decoded

    def test_raw_lzw_deflate_and_packbits_preserve_every_rgba_page(self):
        for compression in ('raw', 'tiff_lzw', 'tiff_adobe_deflate', 'packbits'):
            with self.subTest(compression=compression):
                path = self.fixture(compression)
                proof = inspect_tiff_decode_support(path)
                self.assertTrue(proof['supported'])
                self.assertEqual(proof['pages'], 3)
                frames = self.compare(path)
                self.assertAlmostEqual(frames[0].dotsPerMeterX()*0.0254, 150, delta=0.03)
                self.assertAlmostEqual(frames[0].dotsPerMeterY()*0.0254, 200, delta=0.03)

    def test_rgb_lzw_horizontal_prediction(self):
        path = self.fixture('tiff_lzw', alpha=False, predictor=True)
        with Image.open(path) as image:
            self.assertEqual(image.tag_v2[317], 2)
        self.compare(path)

    def test_bigtiff_from_streaming_writer_is_decoded_exactly(self):
        from app_backend.converter_guard.raster_writers import write_tiff
        original = self.compare(self.fixture('raw'))
        path = self.root/'bigtiff.tiff'
        write_tiff(path, ((image, 0) for image in original))
        self.assertEqual(path.read_bytes()[:4], b'II\x2b\0')
        result = self.compare(path)
        self.assertEqual(len(result), 3)

    def test_grayscale_is_explicitly_unsupported_not_misread_as_rgb(self):
        path = self.root/'gray.tiff'
        Image.new('L', (2, 2), 32).save(path)
        support = inspect_tiff_decode_support(path)
        self.assertFalse(support['supported'])
        self.assertEqual(support['code'], 'MEDIA_TIFF_LAYOUT_UNSUPPORTED')
        with self.assertRaises(GuardError) as error:
            list(iter_tiff_frames(path))
        self.assertEqual(error.exception.code, 'MEDIA_TIFF_LAYOUT_UNSUPPORTED')

    def test_truncated_strip_is_rejected(self):
        path = self.fixture('raw')
        data = path.read_bytes()
        path.write_bytes(data[:-100])
        with self.assertRaises(GuardError) as error:
            list(iter_tiff_frames(path))
        self.assertEqual(error.exception.code, 'MEDIA_OUTPUT_INVALID')

    def test_deflate_bomb_cannot_exceed_declared_pixel_buffer(self):
        path = self.root/'bomb.tiff'
        Image.new('RGB', (1, 1)).save(path, compression='raw')
        data = bytearray(path.read_bytes())
        compressed = zlib.compress(b'x'*(16*1024*1024))
        patch_tag(data, 259, 8)
        patch_tag(data, 273, len(data))
        patch_tag(data, 279, len(compressed))
        path.write_bytes(data+compressed)
        tracemalloc.start()
        try:
            with self.assertRaises(GuardError) as error:
                list(iter_tiff_frames(path))
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(error.exception.code, 'MEDIA_OUTPUT_INVALID')
        self.assertLess(peak, 1024*1024)

    def test_pre_cancel_is_honoured_before_pixel_allocation(self):
        path = self.fixture('raw')
        cancelled = threading.Event();cancelled.set()
        with self.assertRaises(GuardError) as error:
            list(iter_tiff_frames(path, cancel=cancelled))
        self.assertEqual(error.exception.code, 'CANCELLED')

    def test_existing_qt_allocation_budget_is_honoured_before_large_page_allocation(self):
        path = self.root/'large-declaration.tiff'
        Image.new('RGB', (1, 1)).save(path, compression='raw')
        data = bytearray(path.read_bytes())
        patch_tag(data, 256, 1024)
        patch_tag(data, 257, 1024)
        patch_tag(data, 278, 1024)
        path.write_bytes(data)
        with patch.object(QImageReader, 'allocationLimit', return_value=1):
            with self.assertRaises(GuardError) as error:
                list(iter_tiff_frames(path))
        self.assertEqual(error.exception.code, 'MEDIA_ALLOCATION_LIMIT')

    def test_real_fulltest_raw_rgba_and_three_lzw_rgb_pages(self):
        evidence = Path(__file__).resolve().parents[2]/'evidence'
        paths = [evidence/'05_LEVEL_2.tiff', evidence/'06_LEVEL_3.tiff']
        if not all(path.is_file() for path in paths):
            self.skipTest('external FULLTEST evidence unavailable')
        self.assertEqual(len(self.compare(paths[0])), 1)
        self.assertEqual(len(self.compare(paths[1])), 3)


if __name__ == '__main__':
    unittest.main()
