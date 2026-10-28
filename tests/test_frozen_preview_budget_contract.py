"""Independent, no-Qt regression for frozen worker PNG preview acceptance.

The PDF page preview budget (1400x1800) is intentionally different from
the image-preview budget (8192/axis and 12 million pixels). Test real
PNG/CRC/deflate and reject valid-CRC oversized headers before decompression.
"""
import sys
from pathlib import Path
import struct
import tempfile
import unittest
import zlib

QA_ROOT = Path(__file__).resolve().parents[1] / 'tools' / 'qa'
sys.path.insert(0, str(QA_ROOT))
from guard_v19_frozen_acceptance import png_info, synthetic_png


def reheader(source, width, height, *, preserve_crc=True):
    """Replace IHDR dimensions, update the CRC but preserve original payload."""
    raw = bytearray(source.read_bytes())
    assert raw[:16] == b'\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR'
    raw[16:24] = struct.pack('!II', width, height)
    if preserve_crc:
        raw[29:33] = struct.pack('!I', zlib.crc32(raw[12:29]) & 0xffffffff)
    source.write_bytes(raw)


class FrozenPreviewBudgetRegression(unittest.TestCase):
    def test_original_2400x1200_rgba_is_not_downscaled(self):
        with tempfile.TemporaryDirectory(prefix='aio-frozen-preview-') as folder:
            png = Path(folder) / 'native-2_8mp.png'
            synthetic_png(png)
            result = png_info(png, max_width=8192, max_height=8192, max_pixels=12_000_000)
            self.assertEqual((result['width'], result['height'], result['colourType']), (2400, 1200, 6))
            self.assertEqual(result['decodedBytes'], (2400 * 4 + 1) * 1200)

    def test_pdf_budget_stays_strictly_smaller_than_image_budget(self):
        with tempfile.TemporaryDirectory(prefix='aio-frozen-preview-') as folder:
            png = Path(folder) / 'native-2_8mp.png'
            synthetic_png(png)
            with self.assertRaisesRegex(AssertionError, 'selected production image budget'):
                png_info(png)

    def test_rejects_oversize_area_even_with_valid_crc(self):
        with tempfile.TemporaryDirectory(prefix='aio-frozen-preview-') as folder:
            png = Path(folder) / 'oversized-area.png'
            synthetic_png(png)
            reheader(png, 5000, 3000)
            with self.assertRaisesRegex(AssertionError, 'selected production image budget'):
                png_info(png, max_width=8192, max_height=8192, max_pixels=12_000_000)

    def test_rejects_oversize_axis_even_with_valid_crc(self):
        with tempfile.TemporaryDirectory(prefix='aio-frozen-preview-') as folder:
            png = Path(folder) / 'oversized-axis.png'
            synthetic_png(png)
            reheader(png, 8193, 1)
            with self.assertRaisesRegex(AssertionError, 'selected production image budget'):
                png_info(png, max_width=8192, max_height=8192, max_pixels=12_000_000)

    def test_rejects_corrupted_crc(self):
        with tempfile.TemporaryDirectory(prefix='aio-frozen-preview-') as folder:
            png = Path(folder) / 'tampered.png'
            synthetic_png(png)
            reheader(png, 2401, 1200, preserve_crc=False)
            with self.assertRaisesRegex(AssertionError, 'PNG chunk CRC mismatch'):
                png_info(png, max_width=8192, max_height=8192, max_pixels=12_000_000)


if __name__ == '__main__':
    unittest.main()
