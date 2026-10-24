"""Cross-container pixel/frame regressions using the shipped Qt codecs."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
import struct
import tempfile
import threading
import unittest
import zlib

from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard.media_fidelity import check_media_preservation, inspect_media
from app_backend.converter_guard.qt_media import convert_media_frames, media_frame_route

QT_AVAILABLE = importlib.util.find_spec('PySide6') is not None


def chunk(kind, payload):
    return struct.pack('>I', len(payload)) + kind + payload + struct.pack('>I', zlib.crc32(kind + payload) & 0xffffffff)


def apng(frames, *, size=(3, 2), controls=None, delays=None, loops=3):
    """Build real RGBA frames including sub-rect blend/disposal controls."""
    width, height = size
    result = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0))
    result += chunk(b'acTL', struct.pack('>II', len(frames), loops))
    sequence = 0
    for index, pixels in enumerate(frames):
        fw, fh, x, y, dispose, blend = controls[index] if controls else (width, height, 0, 0, 0, 0)
        delay = delays[index] if delays else 100 * (index + 1)
        result += chunk(b'fcTL', struct.pack('>IIIIIHHBB', sequence, fw, fh, x, y, delay, 1000, dispose, blend))
        sequence += 1
        raw = b''.join(b'\0' + pixels[y * fw * 4:(y + 1) * fw * 4] for y in range(fh))
        encoded = zlib.compress(raw)
        if index:
            result += chunk(b'fdAT', struct.pack('>I', sequence) + encoded)
            sequence += 1
        else:
            result += chunk(b'IDAT', encoded)
    return result + chunk(b'IEND', b'')


@unittest.skipUnless(QT_AVAILABLE, 'PySide6 required for native raster frame tests')
class NativeFrameConversion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cancel = threading.Event()

    def source(self, payload, suffix='apng'):
        source = self.root / ('source.' + suffix)
        source.write_bytes(payload)
        return source

    def convert(self, source, target):
        [output] = convert_media_frames(source, self.root / target, target, cancel=self.cancel)
        check_media_preservation([output], target, source, cancel=self.cancel)
        return output

    def qt_pixels(self, source):
        from PySide6.QtGui import QImage, QImageReader
        reader = QImageReader(str(source))
        result = []
        while reader.canRead():
            image = reader.read()
            self.assertFalse(image.isNull(), reader.errorString())
            image = image.convertToFormat(QImage.Format.Format_RGBA8888)
            pixels = [image.pixelColor(x, y).getRgb() for y in range(image.height()) for x in range(image.width())]
            # Decoders may assign different invisible RGB values to alpha=0.
            result.append([pixel if pixel[3] else (0, 0, 0, 0) for pixel in pixels])
        return result

    def test_apng_to_webp_preserves_pixels_alpha_timing_loops(self):
        red, green, blue = bytes((255, 0, 0, 128)), bytes((0, 255, 0, 64)), bytes((0, 0, 255, 255))
        source = self.source(apng([red * 6, green * 6, blue * 6], loops=4))
        output = self.convert(source, 'webp')
        after = inspect_media(output)
        self.assertEqual((after['frames'], after['width'], after['height'], after['loopCount']), (3, 3, 2, 4))
        self.assertEqual(after['durationsMs'], [100, 200, 300])
        self.assertEqual(self.qt_pixels(output), [[tuple(red)] * 6, [tuple(green)] * 6, [tuple(blue)] * 6])

    def test_apng_dispose_previous_background_and_source_blend(self):
        red, green, blue, transparent = (bytes(v) for v in [(255, 0, 0, 255), (0, 255, 0, 255),
                                                          (0, 0, 255, 255), (0, 0, 0, 0)])
        controls = [(3, 2, 0, 0, 0, 0), (1, 1, 1, 0, 2, 0),
                    (1, 1, 2, 1, 1, 0), (1, 1, 0, 0, 0, 0)]
        source = self.source(apng([red * 6, green, blue, transparent], controls=controls))
        frames = self.qt_pixels(self.convert(source, 'webp'))
        self.assertEqual(frames[0], [tuple(red)] * 6)
        expected = [tuple(red)] * 6; expected[1] = tuple(green)
        self.assertEqual(frames[1], expected)
        expected = [tuple(red)] * 6; expected[5] = tuple(blue)
        self.assertEqual(frames[2], expected)
        expected = [tuple(red)] * 6; expected[0] = expected[5] = tuple(transparent)
        self.assertEqual(frames[3], expected)

    def test_apng_source_over_composes_semitransparent_subframe(self):
        red, green = bytes((255, 0, 0, 255)), bytes((0, 255, 0, 128))
        source = self.source(apng([red * 6, green], controls=[(3, 2, 0, 0, 0, 0), (1, 1, 1, 0, 0, 1)]))
        frames = self.qt_pixels(self.convert(source, 'webp'))
        expected = [tuple(red)] * 6
        expected[1] = (127, 128, 0, 255)
        self.assertEqual(frames[1], expected)

    def test_gif_cross_roundtrip_exact_palette_and_transparency(self):
        red, green, transparent = bytes((255, 0, 0, 255)), bytes((0, 255, 0, 255)), bytes((0, 0, 0, 0))
        source = self.source(apng([red * 6, transparent * 3 + green * 3, green * 6], loops=0))
        gif = self.convert(source, 'gif')
        self.assertEqual(inspect_media(gif)['loopCount'], 0)
        self.assertEqual(self.qt_pixels(gif), [[tuple(red)] * 6, [tuple(transparent)] * 3 + [tuple(green)] * 3, [tuple(green)] * 6])
        webp = self.convert(gif, 'webp')
        self.assertEqual(self.qt_pixels(webp), self.qt_pixels(gif))
        apng_out = self.convert(gif, 'apng')
        self.assertEqual(inspect_media(apng_out)['frames'], 3)
        # Independent codec verifies the APNG container through a subsequent
        # WebP conversion; source GIF pixels supply the separate oracle.
        [roundtrip] = convert_media_frames(apng_out, self.root / 'roundtrip', 'webp')
        self.assertEqual(self.qt_pixels(roundtrip), self.qt_pixels(gif))

    def test_gif_semialpha_rejected_without_publishing_partial_file(self):
        source = self.source(apng([bytes((30, 70, 100, 80)) * 6]))
        with self.assertRaises(GuardError) as error:
            self.convert(source, 'gif')
        self.assertEqual(error.exception.code, 'MEDIA_GIF_ALPHA_UNREPRESENTABLE')
        self.assertFalse(list((self.root / 'gif').rglob('*.part')))
        self.assertFalse((self.root / 'gif' / 'source.gif').exists())

    def test_opaque_rgba_256_colours_fit_exact_gif_palette(self):
        colours = b''.join(bytes((i, 128, 37, 255)) for i in range(256))
        source = self.source(apng([colours], size=(256, 1)))
        output = self.convert(source, 'gif')
        self.assertEqual(self.qt_pixels(output), [[(i, 128, 37, 255) for i in range(256)]])

    def test_gif_palette_limit_does_not_silently_quantise(self):
        colours = b''.join(bytes((i & 255, i >> 8, 37, 255)) for i in range(257))
        source = self.source(apng([colours], size=(257, 1)))
        with self.assertRaises(GuardError) as error:
            self.convert(source, 'gif')
        self.assertEqual(error.exception.code, 'MEDIA_GIF_PALETTE_UNREPRESENTABLE')

    def test_gif_subcentisecond_timing_is_explicitly_rejected(self):
        source = self.source(apng([bytes((255, 0, 0, 255)) * 6] * 2, delays=[125, 100]))
        with self.assertRaises(GuardError) as error:
            self.convert(source, 'gif')
        self.assertEqual(error.exception.code, 'MEDIA_TIMING_UNREPRESENTABLE')

    def test_cancellation_during_rows_cleans_partial_and_frame_scratch(self):
        source = self.source(apng([bytes((255, 0, 0, 255)) * 6] * 3))
        class CancelAfter:
            def __init__(self): self.calls = 0
            def is_set(self):
                self.calls += 1
                return self.calls > 25
        with self.assertRaises(GuardError) as error:
            convert_media_frames(source, self.root / 'cancelled', 'webp', cancel=CancelAfter())
        self.assertEqual(error.exception.code, 'CANCELLED')
        self.assertFalse(list((self.root / 'cancelled').rglob('*.part')))
        self.assertFalse(list((self.root / 'cancelled').glob('.qt-frames-*')))

    def test_animation_to_tiff_keeps_each_frame_as_page(self):
        source = self.source(apng([bytes((255, 0, 0, 255)) * 6] * 3))
        output = self.convert(source, 'tiff')
        self.assertEqual(inspect_media(output)['pages'], 3)

    def test_webp_to_png_writes_animation_instead_of_flattening(self):
        source = self.source(apng([bytes((255, 0, 0, 255)) * 6, bytes((0, 0, 255, 255)) * 6]))
        webp = self.convert(source, 'webp')
        png = self.convert(webp, 'png')
        self.assertTrue(inspect_media(png)['animationContainer'])
        self.assertEqual(inspect_media(png)['durationsMs'], [100, 200])

    def test_loop_change_is_not_ready(self):
        source = self.source(apng([bytes((255, 0, 0, 255)) * 6] * 2, loops=2))
        output = self.root / 'different.apng'
        output.write_bytes(apng([bytes((255, 0, 0, 255)) * 6] * 2, loops=3))
        # Use matching output basename as required by the real postcondition.
        folder = self.root / 'changed'; folder.mkdir()
        output = output.rename(folder / 'source.apng')
        with self.assertRaises(GuardError) as error:
            check_media_preservation([output], 'apng', source, cancel=self.cancel)
        self.assertEqual(error.exception.code, 'MEDIA_ANIMATION_LOOP_LOSS')

    def test_pdf_to_tiff_renders_every_page_at_recorded_dpi(self):
        from pypdf import PdfWriter
        source = self.root / 'pages.pdf'
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=144)
        writer.add_blank_page(width=144, height=72)
        with source.open('wb') as stream:
            writer.write(stream)
        code = """
import json, sys
from PySide6.QtGui import QGuiApplication
from app_backend.converter_guard.qt_media import media_preflight, convert_document_pages
app = QGuiApplication([])
proof = media_preflight(sys.argv[1])
[output] = convert_document_pages(sys.argv[1], sys.argv[2], 'tiff')
print(json.dumps({'proof':proof, 'output':str(output)}))
"""
        env = {**os.environ, 'QT_QPA_PLATFORM': 'offscreen'}
        completed = subprocess.run([sys.executable, '-c', code, str(source), str(self.root / 'pdf-tiff')],
                                   env=env, capture_output=True, text=True, check=True, timeout=30)
        result = json.loads(completed.stdout)
        self.assertEqual(result['proof']['source']['rasterDpi'], 144)
        info = inspect_media(result['output'])
        self.assertEqual(info['pages'], 2)
        self.assertEqual(info['pageDimensions'], [[144, 288], [288, 144]])
        self.assertAlmostEqual(info['pageDpi'][0][0], 144, places=1)
        check_media_preservation([Path(result['output'])], 'tiff', source, cancel=self.cancel)

    def test_tiff_preflight_requires_actual_decoder_for_cross_container(self):
        from app_backend.converter_guard.qt_media import media_preflight
        source = self.source(apng([bytes((0, 0, 255, 255)) * 6] * 3))
        tiff = self.convert(source, 'tiff')
        proof = media_preflight(tiff)
        self.assertIn('tiff', proof['targets'])
        if proof['decoderAvailable']:
            self.assertIn('pdf', proof['targets'])
        else:
            self.assertNotIn('pdf', proof['targets'])
            self.assertNotIn('webp', proof['targets'])

    def test_jpeg_exif_orientation_is_applied_before_png_encoding(self):
        from PySide6.QtGui import QColor, QImage, QImageReader
        image = QImage(2, 3, QImage.Format.Format_RGB888)
        for y in range(3):
            for x in range(2):
                image.setPixelColor(x, y, QColor(x * 120, y * 80, 30))
        source = self.root / 'source.jpeg'
        self.assertTrue(image.save(str(source), 'JPEG', 100))
        # IFD0 orientation six rotates the displayed image 90° clockwise.
        exif = b'Exif\0\0II' + struct.pack('<HIH', 42, 8, 1) + struct.pack('<HHIH', 274, 3, 1, 6) + b'\0\0\0\0\0\0'
        raw = source.read_bytes()
        source.write_bytes(raw[:2] + b'\xff\xe1' + struct.pack('>H', len(exif) + 2) + exif + raw[2:])
        before = QImageReader(str(source)).read()
        output = self.convert(source, 'png')
        after = QImageReader(str(output)).read()
        self.assertEqual(inspect_media(source)['orientation'], 6)
        self.assertEqual((after.width(), after.height()), (3, 2))
        for y in range(3):
            for x in range(2):
                self.assertEqual(after.pixelColor(2 - y, x), before.pixelColor(x, y))

    def test_icc_profile_is_preserved_in_webp_apng_and_tiff(self):
        from PySide6.QtGui import QColorSpace, QImage, QImageReader
        image = QImage(3, 2, QImage.Format.Format_RGBA8888)
        image.fill(0xff33aa77)
        image.setColorSpace(QColorSpace(QColorSpace.NamedColorSpace.DisplayP3))
        image.setDotsPerMeterX(11811); image.setDotsPerMeterY(11811)
        source = self.root / 'source.png'
        self.assertTrue(image.save(str(source)))
        profile = bytes(image.colorSpace().iccProfile())
        webp = self.convert(source, 'webp')
        self.assertEqual(bytes(QImageReader(str(webp)).read().colorSpace().iccProfile()), profile)
        apng_out = self.convert(source, 'apng')
        raw = apng_out.read_bytes(); pos = 8; icc = None
        while pos < len(raw):
            size, kind = struct.unpack('>I4s', raw[pos:pos + 8])
            if kind == b'iCCP':
                payload = raw[pos + 8:pos + 8 + size]
                marker = payload.index(b'\0')
                icc = zlib.decompress(payload[marker + 2:])
            pos += size + 12
        self.assertEqual(icc, profile)
        tiff = self.convert(source, 'tiff')
        raw = tiff.read_bytes(); offset = struct.unpack_from('<I', raw, 4)[0]
        count = struct.unpack_from('<H', raw, offset)[0]
        icc = None
        for index in range(count):
            tag, typ, size, pointer = struct.unpack_from('<HHII', raw, offset + 2 + index * 12)
            if tag == 34675:
                icc = raw[pointer:pointer + size]
        self.assertEqual(icc, profile)
        self.assertAlmostEqual(inspect_media(tiff)['pageDpi'][0][0], 300, places=2)
        with self.assertRaises(GuardError) as error:
            self.convert(source, 'gif')
        self.assertEqual(error.exception.code, 'MEDIA_GIF_COLORSPACE_UNREPRESENTABLE')

    def test_tiff_to_pdf_postcondition_blocks_dropped_pages(self):
        from pypdf import PdfWriter
        source = self.source(apng([bytes((0, 0, 255, 255)) * 6] * 3))
        tiff = self.convert(source, 'tiff')
        folder = self.root / 'wrong-pdf'; folder.mkdir()
        output = folder / 'source.pdf'
        writer = PdfWriter(); writer.add_blank_page(width=72, height=72)
        with output.open('wb') as stream: writer.write(stream)
        with self.assertRaises(GuardError) as error:
            check_media_preservation([output], 'pdf', tiff, cancel=self.cancel)
        self.assertEqual(error.exception.code, 'MEDIA_PAGE_LOSS')

    def test_nonraster_route_is_left_to_document_adapter(self):
        source = self.root / 'source.txt'; source.write_text('document')
        self.assertIsNone(media_frame_route(source, 'webp'))
        self.assertIsNone(convert_media_frames(source, self.root / 'out', 'pdf'))


if __name__ == '__main__':
    unittest.main()
