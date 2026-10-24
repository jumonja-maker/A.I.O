"""Independent decoder checks for actual GIF pixels and BigTIFF pages."""
from pathlib import Path
import os
import random
import tempfile
import threading
import unittest

from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard.media_fidelity import inspect_media
from app_backend.converter_guard.raster_writers import write_gif, write_tiff

try:
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtGui import QImage, QImageReader
    from PySide6.QtWidgets import QApplication
except ImportError:
    QImage = None
try:
    from PIL import Image
except ImportError:
    Image = None


def qimage(pixels, width, height):
    data = bytes(channel for colour in pixels for channel in colour)
    return QImage(data, width, height, width*4, QImage.Format.Format_RGBA8888).copy()


@unittest.skipIf(QImage is None, 'Qt decoder required')
class GifQtCompositing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
        cls.application = QCoreApplication.instance() or QApplication([])

    def test_first_opaque_frame_cannot_leak_into_later_transparent_frame(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'opaque-then-transparent.gif'
            red = (255, 0, 0, 255)
            green = (0, 255, 0, 255)
            transparent = (0, 0, 0, 0)
            first = qimage([red]*4, 2, 2)
            second = qimage([transparent, green, transparent, green], 2, 2)
            write_gif(path, [(first, 100), (second, 200)], width=2, height=2,
                      reserve_transparency=True)
            reader = QImageReader(str(path))
            self.assertFalse(reader.read().isNull())
            actual = reader.read().convertToFormat(QImage.Format.Format_RGBA8888)
            self.assertFalse(actual.isNull())
            for y in range(2):
                for x in range(2):
                    observed, expected = actual.pixelColor(x, y), second.pixelColor(x, y)
                    self.assertEqual(observed.alpha(), expected.alpha())
                    if expected.alpha():
                        self.assertEqual(observed.getRgb(), expected.getRgb())
            # Transparency is announced on the first opaque frame as well.
            data = path.read_bytes()
            self.assertEqual(data[data.index(b'!\xf9\x04')+3] & 1, 1)


@unittest.skipIf(QImage is None or Image is None, 'Qt and independent Pillow decoder required')
class RasterWriters(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_gif_keeps_simple_palette_pixels_transparency_delays_and_loop(self):
        colours = [(255, 0, 0, 255), (0, 255, 0, 255), (0, 0, 255, 255), (0, 0, 0, 0)]
        frames = [(qimage(colours[n:]+colours[:n], 2, 2), delay)
                  for n, delay in enumerate((100, 200, 300))]
        path = self.root/'animation.gif'
        self.assertEqual(write_gif(path, iter(frames), width=2, height=2, loop=0), 3)
        with Image.open(path) as output:
            self.assertEqual(output.n_frames, 3)
            self.assertEqual(output.info['loop'], 0)
            for n, (_image, delay) in enumerate(frames):
                output.seek(n)
                self.assertEqual(output.info['duration'], delay)
                actual = list(output.convert('RGBA').getdata())
                expected = colours[n:]+colours[:n]
                for a, e in zip(actual, expected):
                    self.assertEqual(a[3], e[3])
                    if e[3]:
                        self.assertEqual(a, e)

    def test_gif_lzw_code_width_growth_and_dictionary_reset_decode_exactly(self):
        rng = random.Random(831)
        for amount in (2, 3, 4, 16, 128, 255, 256):
            with self.subTest(colours=amount):
                palette = [(n, (n*17)%256, (n*47)%256, 255) for n in range(amount)]
                pixels = [palette[rng.randrange(amount)] for _ in range(160*120)]
                path = self.root/('random-'+str(amount)+'.gif')
                write_gif(path, [(qimage(pixels, 160, 120), 120)], width=160, height=120)
                with Image.open(path) as output:
                    self.assertEqual(list(output.convert('RGBA').getdata()), pixels)

    def test_gif_single_play_omits_loop_extension(self):
        path = self.root/'once.gif'
        image = qimage([(255, 0, 0, 255)], 1, 1)
        write_gif(path, [(image, 100), (image, 200)], width=1, height=1)
        with Image.open(path) as output:
            self.assertNotIn('loop', output.info)

    def test_gif_adaptive_palette_is_bounded_and_decodable(self):
        pixels = [(x, y, (x+y)%256, 255) for y in range(64) for x in range(64)]
        path = self.root/'gradient.gif'
        write_gif(path, [(qimage(pixels, 64, 64), 100)], width=64, height=64)
        with Image.open(path) as output:
            actual = list(output.convert('RGB').getdata())
            mean_error = sum(abs(a[c]-e[c]) for a, e in zip(actual, pixels) for c in range(3))/(len(pixels)*3)
            self.assertLess(mean_error, 6)

    def test_bigtiff_preserves_all_pages_geometry_rgba_pixels(self):
        frames = []
        expected = []
        for n, (width, height) in enumerate(((3, 2), (5, 3), (2, 4))):
            pixels = [(n*70, 100, 255-n*50, (i*31)%256) for i in range(width*height)]
            frames.append((qimage(pixels, width, height), 0))
            frames[-1][0].setDotsPerMeterX(11811+n*100)
            frames[-1][0].setDotsPerMeterY(11811+n*200)
            expected.append((width, height, pixels))
        path = self.root/'pages.tiff'
        self.assertEqual(write_tiff(path, iter(frames)), 3)
        self.assertEqual(inspect_media(path)['pages'], 3)
        with Image.open(path) as output:
            self.assertEqual(output.n_frames, 3)
            for n, (width, height, pixels) in enumerate(expected):
                output.seek(n)
                self.assertEqual(output.size, (width, height))
                self.assertEqual(list(output.convert('RGBA').getdata()), pixels)
                self.assertAlmostEqual(float(output.tag_v2[282]), frames[n][0].dotsPerMeterX()*0.0254)
                self.assertAlmostEqual(float(output.tag_v2[283]), frames[n][0].dotsPerMeterY()*0.0254)

    def test_cancelled_write_removes_owned_partial_but_not_existing_file(self):
        image = qimage([(255, 0, 0, 255)], 1, 1)
        cancelled = threading.Event();cancelled.set()
        for suffix, writer in [('gif', lambda p:write_gif(p, [(image, 100)], width=1, height=1, cancel=cancelled)),
                               ('tiff', lambda p:write_tiff(p, [(image, 0)], cancel=cancelled))]:
            path = self.root/('cancelled.'+suffix)
            with self.assertRaises(GuardError) as error:
                writer(path)
            self.assertEqual(error.exception.code, 'CANCELLED')
            self.assertFalse(path.exists())
            path.write_bytes(b'keep')
            with self.assertRaises(FileExistsError):
                writer(path)
            self.assertEqual(path.read_bytes(), b'keep')


if __name__ == '__main__':
    unittest.main()
