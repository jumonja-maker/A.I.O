"""Property regressions with small real PNG/APNG/GIF/TIFF containers."""
import base64
import hashlib
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch
import zlib

from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard.media_fidelity import (
    check_media_preservation, copy_media_identity, inspect_media)


# Generated once with Pillow, then embedded as actual decodable WebP bytes.
# Tests and packaged application do not depend on Pillow.
WEBP_STATIC = base64.b64decode('UklGRhwAAABXRUJQVlA4TA8AAAAvAUAAEAcQ1Y+SBSKi/wEA')
WEBP_ANIMATED = base64.b64decode(
    'UklGRrQAAABXRUJQVlA4WAoAAAASAAAAAQAAAQAAQU5JTQYAAAAAAAAAAABBTk1GKAAAAAAAAAAAAAEAAAEAAGQAAAJWUDhMDwAAAC8BQAAQBxDVj5IFIqL/AQBBTk1GKAAAAAAAAAAAAAEAAAEAAGQAAAJWUDhMDwAAAC8BQAAQB1D9iJIFIqL/AQBBTk1GKAAAAAAAAAAAAAEAAAEAAGQAAAJWUDhMDwAAAC8BQAAQBxBR/ZIFIqL/AQA=')


def chunk(kind, data):
    return struct.pack('>I', len(data))+kind+data+struct.pack('>I', zlib.crc32(kind+data)&0xffffffff)


def png(width=2, height=2, alpha=True, frames=1, delay=100):
    channels = 4 if alpha else 3
    pixel = bytes((30, 80, 120, 100) if alpha else (30, 80, 120))
    data = zlib.compress((b'\0'+pixel*width)*height)
    output = b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6 if alpha else 2, 0, 0, 0))
    if frames > 1:
        output += chunk(b'acTL', struct.pack('>II', frames, 0))
        seq = 0
        for i in range(frames):
            output += chunk(b'fcTL', struct.pack('>IIIIIHHBB', seq, width, height, 0, 0, delay, 1000, 0, 0))
            seq += 1
            output += chunk(b'IDAT', data) if i == 0 else chunk(b'fdAT', struct.pack('>I', seq)+data)
            if i:
                seq += 1
    else:
        output += chunk(b'IDAT', data)
    return output+chunk(b'IEND', b'')


def gif(frames=3, delay=10):
    # Real one-pixel images, transparent palette index zero and valid LZW.
    header = b'GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff'
    frame = (b'!\xf9\x04\x01'+struct.pack('<H', delay)+b'\x00\x00'+
             b',\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00')
    return header+frame*frames+b';'


def tiff(pages=1, alpha=True, width=2, height=2):
    """Classic little-endian uncompressed TIFF with separate RGB(A) pages."""
    entries_count = 11 if alpha else 10
    ifd_size = 2+entries_count*12+4
    bits_offset = 8+ifd_size*pages
    channels = 4 if alpha else 3
    pixel_offset = bits_offset+channels*2
    strip_size = width*height*channels
    output = b'II\x2a\x00'+struct.pack('<I', 8)
    for page in range(pages):
        tags = [(256, 4, 1, width), (257, 4, 1, height), (258, 3, channels, bits_offset),
                (259, 3, 1, 1), (262, 3, 1, 2),
                (273, 4, 1, pixel_offset+strip_size*page), (277, 3, 1, channels),
                (278, 4, 1, height), (279, 4, 1, strip_size), (284, 3, 1, 1)]
        if alpha:
            tags.append((338, 3, 1, 2))
        output += struct.pack('<H', len(tags))
        for tag, typ, count, value in tags:
            output += struct.pack('<HHII', tag, typ, count, value)
        output += struct.pack('<I', 8+(page+1)*ifd_size if page+1 < pages else 0)
    output += struct.pack('<'+'H'*channels, *([8]*channels))
    output += bytes((50, 100, 200, 128) if alpha else (50, 100, 200))*width*height*pages
    return output


class MediaProperties(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cancel = threading.Event()

    def file(self, name, data):
        path = self.root/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def checked(self, source, output, target):
        return check_media_preservation([output], target, source, cancel=self.cancel)

    def test_apng_counts_duration_alpha_and_canvas(self):
        p = self.file('source.apng', png(width=3, height=5, frames=8))
        info = inspect_media(p)
        self.assertEqual((info['width'], info['height'], info['frames']), (3, 5, 8))
        self.assertTrue(info['alpha'])
        self.assertEqual(info['durationsMs'], [100]*8)

    def test_gif_reads_actual_frame_controls(self):
        p = self.file('source.gif', gif(frames=8, delay=15))
        info = inspect_media(p)
        self.assertEqual(info['frames'], 8)
        self.assertEqual(info['durationsMs'], [150]*8)
        self.assertTrue(info['alpha'])

    def test_same_format_identity_preserves_every_byte_and_properties(self):
        for ext, data in [('png', png(width=3, height=5)), ('apng', png(frames=8)),
                          ('gif', gif(frames=8)), ('tiff', tiff(pages=3))]:
            with self.subTest(ext=ext):
                source = self.file('source.'+ext, data)
                [output] = copy_media_identity(source, self.root/('out-'+ext), ext, cancel=self.cancel)
                self.assertEqual(hashlib.sha256(output.read_bytes()).digest(), hashlib.sha256(data).digest())
                proof = self.checked(source, output, ext)
                self.assertFalse(proof['qualityCompared'])
                self.assertEqual(proof['source'], proof['result'])

    def test_tiff_alias_identity_preserves_all_pages(self):
        source = self.file('source.tif', tiff(pages=3))
        [output] = copy_media_identity(source, self.root/'out', 'tiff', cancel=self.cancel)
        self.assertEqual(inspect_media(output)['pages'], 3)
        self.assertEqual(source.read_bytes(), output.read_bytes())

    def test_webp_identity_keeps_actual_frames_alpha_and_timing(self):
        source = self.file('source.webp', WEBP_ANIMATED)
        [output] = copy_media_identity(source, self.root/'out', 'webp', cancel=self.cancel)
        info = inspect_media(output)
        self.assertEqual(info['frames'], 3)
        self.assertTrue(info['alpha'])
        self.assertEqual(info['durationsMs'], [100]*3)
        self.assertEqual(source.read_bytes(), output.read_bytes())

    def test_webp_flattening_is_detected(self):
        source = self.file('source.webp', WEBP_ANIMATED)
        output = self.file('out/source.webp', WEBP_STATIC)
        with self.assertRaises(GuardError) as error:
            self.checked(source, output, 'webp')
        self.assertEqual(error.exception.code, 'MEDIA_ANIMATION_LOSS')

    def test_webp_bad_riff_length_cannot_pass_identity(self):
        data = bytearray(WEBP_ANIMATED)
        data[4] ^= 1
        source = self.file('source.webp', data)
        with self.assertRaises(GuardError) as error:
            copy_media_identity(source, self.root/'out', 'webp', cancel=self.cancel)
        self.assertEqual(error.exception.code, 'MEDIA_OUTPUT_INVALID')

    def test_animated_to_animated_cannot_flatten(self):
        source = self.file('source.apng', png(frames=8))
        output = self.file('out/source.gif', gif(frames=1))
        with self.assertRaises(GuardError) as error:
            self.checked(source, output, 'gif')
        self.assertEqual(error.exception.code, 'MEDIA_ANIMATION_LOSS')

    def test_animation_timing_changes_are_not_ready(self):
        source = self.file('source.apng', png(frames=8, delay=100))
        output = self.file('out/source.apng', png(frames=8, delay=500))
        with self.assertRaises(GuardError) as error:
            self.checked(source, output, 'apng')
        self.assertEqual(error.exception.code, 'MEDIA_ANIMATION_TIMING_LOSS')

    def test_multipage_tiff_cannot_drop_pages(self):
        source = self.file('source.tiff', tiff(pages=3))
        output = self.file('out/source.tiff', tiff(pages=1))
        with self.assertRaises(GuardError) as error:
            self.checked(source, output, 'tiff')
        self.assertEqual(error.exception.code, 'MEDIA_PAGE_LOSS')

    def test_pdf_to_tiff_checks_all_pages(self):
        from pypdf import PdfWriter
        source = self.root/'source.pdf'
        writer = PdfWriter()
        for _ in range(3):
            writer.add_blank_page(width=72, height=72)
        with source.open('wb') as stream:
            writer.write(stream)
        output = self.file('out/source.tiff', tiff(pages=1))
        with self.assertRaises(GuardError) as error:
            self.checked(source, output, 'tiff')
        self.assertEqual(error.exception.code, 'MEDIA_PAGE_LOSS')
        output.write_bytes(tiff(pages=3))
        proof = self.checked(source, output, 'tiff')
        self.assertEqual(proof['source']['pages'], proof['result']['pages'])

    def test_alpha_loss_in_capable_format_is_rejected(self):
        source = self.file('source.png', png(alpha=True))
        output = self.file('out/source.png', png(alpha=False))
        with self.assertRaises(GuardError) as error:
            self.checked(source, output, 'png')
        self.assertEqual(error.exception.code, 'MEDIA_ALPHA_LOSS')

    def test_canvas_change_is_rejected(self):
        source = self.file('source.png', png(width=3, height=5))
        output = self.file('out/source.png', png(width=5, height=7))
        with self.assertRaises(GuardError) as error:
            self.checked(source, output, 'png')
        self.assertEqual(error.exception.code, 'MEDIA_CANVAS_CHANGED')

    def test_png_crc_failure_never_copies(self):
        data = bytearray(png())
        data[-1] ^= 1
        source = self.file('source.png', data)
        with self.assertRaises(GuardError) as error:
            copy_media_identity(source, self.root/'out', 'png', cancel=self.cancel)
        self.assertEqual(error.exception.code, 'MEDIA_OUTPUT_INVALID')
        self.assertFalse((self.root/'out').exists())

    def test_tiff_ifd_cycle_is_rejected(self):
        data = bytearray(tiff())
        count = struct.unpack('<H', data[8:10])[0]
        struct.pack_into('<I', data, 10+count*12, 8)
        source = self.file('source.tiff', data)
        with self.assertRaises(GuardError):
            inspect_media(source)

    def test_large_gif_image_blocks_do_not_consume_metadata_budget(self):
        data = gif(frames=1)
        # Multiple data sub-blocks are valid container framing. Pixel decoding
        # is intentionally outside this structural check.
        data = data.replace(b'\x02\x02\x44\x01\x00;', b'\x02'+b'\x02\x44\x01'*30+b'\x00;')
        source = self.file('source.gif', data)
        with patch('app_backend.converter_guard.media_fidelity.MAX_RECORDS', 8):
            self.assertEqual(inspect_media(source)['frames'], 1)

    def test_cancellation_removes_partial_identity_copy(self):
        source = self.file('source.png', png())
        self.cancel.set()
        with self.assertRaises(GuardError) as error:
            copy_media_identity(source, self.root/'out', 'png', cancel=self.cancel)
        self.assertEqual(error.exception.code, 'CANCELLED')
        self.assertFalse(list(self.root.rglob('*.part')))

    def test_cross_format_is_left_to_existing_engine(self):
        source = self.file('source.png', png())
        self.assertIsNone(copy_media_identity(source, self.root/'out', 'webp'))

    def test_renamed_png_is_detected_from_content_and_preserved(self):
        source = self.file('source.bin', png(width=3, height=5))
        self.assertEqual(inspect_media(source)['format'], 'png')
        [output] = copy_media_identity(source, self.root/'out', 'png', cancel=self.cancel)
        self.assertEqual(source.read_bytes(), output.read_bytes())
        self.assertEqual(self.checked(source, output, 'png')['result']['width'], 3)

    def test_wrong_source_suffix_does_not_choose_wrong_parser(self):
        source = self.file('source.png', WEBP_STATIC)
        self.assertEqual(inspect_media(source)['format'], 'webp')
        self.assertIsNone(copy_media_identity(source, self.root/'out', 'png'))
        output = self.file('out/source.png', png())
        self.assertEqual(self.checked(source, output, 'png')['source']['format'], 'webp')

    def test_wrong_output_container_never_matches_requested_suffix(self):
        source = self.file('source.bin', b'not a raster source')
        output = self.file('out/source.png', WEBP_STATIC)
        with self.assertRaises(GuardError) as error:
            self.checked(source, output, 'png')
        self.assertEqual(error.exception.code, 'MEDIA_OUTPUT_INVALID')


if __name__ == '__main__':
    unittest.main()
