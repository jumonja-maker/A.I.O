"""Independent ICC-model and physical-resolution controls; no LibreOffice."""
import io
import json
import os
import shutil
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
import zlib

from PIL import Image, ImageCms, ImageOps
from PySide6.QtGui import QColor, QColorSpace, QImage, QImageReader

from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard.adapter import LibreOfficeAdapter
from app_backend.converter_guard.export_recipe import warning_details
from app_backend.converter_guard.media_fidelity import check_media_preservation, inspect_media
from app_backend.converter_guard.qt_media import convert_media_frames

PROFILES = Path(__file__).parent / 'data/deep_media_profiles'


class ColorModelAndResolution(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cancel = threading.Event()

    def convert(self, source, target):
        paths = convert_media_frames(source, self.root/(source.stem+'-'+target), target,
                                     cancel=self.cancel)
        check_media_preservation(paths, target, source, cancel=self.cancel)
        return paths[0]

    def cmyk_source(self):
        source = self.root/'print.jpg'
        image = Image.new('CMYK', (4, 2))
        image.putdata([(0, 0, 0, 0), (0, 128, 255, 40), (255, 0, 0, 0),
                       (0, 255, 0, 0), (128, 128, 128, 0), (0, 0, 0, 255),
                       (32, 64, 96, 128), (5, 50, 200, 100)])
        image.save(source, quality=100, subsampling=0,
                   icc_profile=(PROFILES/'CGATS001Compat-v2-micro.icc').read_bytes())
        return source

    def test_cmyk_icc_uses_managed_rgb_conversion_instead_of_invalid_profile_copy(self):
        source = self.cmyk_source()
        before = source.read_bytes()
        with Image.open(source) as decoded:
            expected = ImageCms.profileToProfile(decoded,
                ImageCms.ImageCmsProfile(io.BytesIO(decoded.info['icc_profile'])),
                ImageCms.createProfile('sRGB'), outputMode='RGB',
                renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC)
            expected_pixels = list(expected.get_flattened_data())
        for target in ('png', 'apng', 'webp', 'tiff'):
            with self.subTest(target=target):
                output = self.convert(source, target)
                with Image.open(output) as actual:
                    profile = actual.info.get('icc_profile', b'')
                    self.assertEqual(profile[16:20], b'RGB ')
                    # LittleCMS independently rejects a CMYK profile copied onto
                    # RGB pixels; a matching model is necessary before numbers.
                    managed = ImageCms.profileToProfile(actual,
                        ImageCms.ImageCmsProfile(io.BytesIO(profile)),
                        ImageCms.createProfile('sRGB'), outputMode='RGB',
                        renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC)
                    for expected_pixel, actual_pixel in zip(expected_pixels, managed.get_flattened_data()):
                        self.assertTrue(all(abs(a-b) <= 2 for a,b in
                                            zip(expected_pixel, actual_pixel)),
                                        (target, expected_pixel, actual_pixel))
                # Some Qt installations have a TIFF writer here but no TIFF
                # decoder plugin. LittleCMS above always validates the output;
                # also check the Qt reader for the codecs available at runtime.
                reader = QImageReader(str(output))
                if target != 'tiff' or reader.canRead():
                    self.assertTrue(reader.read().colorSpace().isValid())
        self.assertEqual(source.read_bytes(), before)

    def test_gray_icc_is_converted_to_rgb_before_rgba_writers_and_jpeg(self):
        source = self.root/'gray.png'
        Image.new('L', (4, 2), 100).save(source,
            icc_profile=(PROFILES/'Gray.icc').read_bytes())
        with Image.open(source) as decoded:
            expected = ImageCms.profileToProfile(decoded,
                ImageCms.ImageCmsProfile(io.BytesIO(decoded.info['icc_profile'])),
                ImageCms.createProfile('sRGB'), outputMode='RGB',
                renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC).getpixel((0, 0))
        for target in ('apng', 'webp', 'tiff', 'jpeg'):
            with self.subTest(target=target):
                output = self.convert(source, target)
                with Image.open(output) as actual:
                    self.assertEqual(actual.info.get('icc_profile', b'')[16:20], b'RGB ')
                    managed = ImageCms.profileToProfile(actual,
                        ImageCms.ImageCmsProfile(io.BytesIO(actual.info['icc_profile'])),
                        ImageCms.createProfile('sRGB'), outputMode='RGB',
                        renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC)
                    self.assertTrue(all(abs(a-b) <= 2 for a,b in
                                        zip(expected, managed.getpixel((0, 0)))))

    def test_invalid_embedded_icc_is_not_silently_treated_as_untagged_rgb(self):
        for extension in ('png', 'jpg'):
            with self.subTest(extension=extension):
                source = self.root/('invalid.'+extension)
                Image.new('RGB', (4, 2), (20, 40, 60)).save(source,
                    icc_profile=b'this is not an ICC profile')
                with self.assertRaises(GuardError) as caught:
                    self.convert(source, 'tiff')
                self.assertIn(caught.exception.code, ('MEDIA_COLOR_PROFILE_UNSUPPORTED',
                                                     'MEDIA_COLOR_PROFILE_UNCONFIRMED'))
                self.assertFalse((self.root/(source.stem+'-tiff')/'invalid.tiff').exists())

    def test_untagged_cmyk_requires_a_confirmed_color_transform(self):
        source = self.root/'untagged.jpg'
        Image.new('CMYK', (4, 2), (0, 128, 255, 40)).save(source, quality=100)
        with self.assertRaises(GuardError) as caught:
            self.convert(source, 'png')
        self.assertEqual(caught.exception.code, 'MEDIA_COLOR_PROFILE_UNCONFIRMED')

    def test_rgb_display_p3_alpha_and_resolution_remain_in_their_original_space(self):
        source = self.root/'p3.png'
        image = QImage(3, 2, QImage.Format.Format_RGBA8888)
        image.fill(QColor(12, 34, 56, 128))
        image.setColorSpace(QColorSpace(QColorSpace.NamedColorSpace.DisplayP3))
        image.setDotsPerMeterX(11811); image.setDotsPerMeterY(5906)
        self.assertTrue(image.save(str(source)))
        profile = bytes(image.colorSpace().iccProfile())
        for target in ('apng', 'webp', 'tiff'):
            with self.subTest(target=target):
                output = self.convert(source, target)
                with Image.open(output) as actual:
                    self.assertEqual(actual.info.get('icc_profile'), profile)
                    self.assertEqual(actual.convert('RGBA').getpixel((0, 0)), (12, 34, 56, 128))

    def test_static_png_physical_resolution_is_preserved_by_apng(self):
        source = self.root/'print.png'
        Image.new('RGBA', (24, 16), (255, 0, 0, 255)).save(source, dpi=(300, 150))
        with Image.open(source) as original:
            expected_dpi = original.info['dpi']
        output = self.convert(source, 'apng')
        with Image.open(output) as actual:
            dpi = actual.info.get('dpi')
            self.assertIsNotNone(dpi)
            self.assertAlmostEqual(dpi[0], expected_dpi[0], places=4)
            self.assertAlmostEqual(dpi[1], expected_dpi[1], places=4)

    def test_apng_compositor_carries_resolution_into_every_tiff_page(self):
        source = self.root/'animated.png'
        frames = [Image.new('RGBA', (24, 16), color) for color in
                  ((255, 0, 0, 255), (0, 0, 255, 255))]
        frames[0].save(source, save_all=True, append_images=frames[1:],
                       duration=[100, 200], loop=0, dpi=(300, 150))
        with Image.open(source) as original:
            expected_dpi = original.info['dpi']
        output = self.convert(source, 'tiff')
        with Image.open(output) as actual:
            self.assertEqual(actual.n_frames, 2)
            for page in range(2):
                actual.seek(page)
                self.assertAlmostEqual(float(actual.info['dpi'][0]), expected_dpi[0], places=4)
                self.assertAlmostEqual(float(actual.info['dpi'][1]), expected_dpi[1], places=4)
        for resolution in inspect_media(output)['pageDpi']:
            self.assertAlmostEqual(resolution[0], expected_dpi[0], places=4)
            self.assertAlmostEqual(resolution[1], expected_dpi[1], places=4)

    def test_resolution_readback_rejects_a_png_with_lost_physical_scale(self):
        source = self.root/'print.png'
        Image.new('RGB', (24, 16), (20, 40, 60)).save(source, dpi=(300, 150))
        folder = self.root/'bad'; folder.mkdir()
        output = folder/'print.png'
        Image.new('RGB', (24, 16), (20, 40, 60)).save(output)
        with self.assertRaises(GuardError) as caught:
            check_media_preservation([output], 'png', source, cancel=self.cancel)
        self.assertEqual(caught.exception.code, 'MEDIA_DPI_LOSS')

    def test_late_jpeg_exif_matches_independent_decoder_orientation_and_scale(self):
        source = self.root/'rotated.jpg'
        image = Image.new('RGB', (3, 2))
        image.putdata([(200, 0, 0), (0, 200, 0), (0, 0, 200),
                       (100, 50, 0), (0, 100, 50), (50, 0, 100)])
        exif = Image.Exif(); exif[274] = 6
        buffer = io.BytesIO()
        image.save(buffer, format='JPEG', quality=100, subsampling=0,
                   exif=exif, dpi=(300, 150))
        raw, position, segments = buffer.getvalue(), 2, []
        while position < len(raw):
            marker = raw[position+1]
            length = int.from_bytes(raw[position+2:position+4], 'big')
            if marker == 0xda:
                tail = raw[position:]
                break
            segments.append((marker, raw[position:position+2+length]))
            position += length+2
        app1 = next(part for marker, part in segments if marker == 0xe1)
        reordered = []
        for marker, part in segments:
            if marker != 0xe1:
                reordered.append(part)
            if marker in (0xc0, 0xc1, 0xc2):
                reordered.append(app1)
        source.write_bytes(b'\xff\xd8'+b''.join(reordered)+tail)
        before = source.read_bytes()
        with Image.open(source) as decoded:
            expected = ImageOps.exif_transpose(decoded)
            self.assertEqual(expected.size, (2, 3))
        self.assertEqual(inspect_media(source)['orientation'], 6)
        output = self.convert(source, 'png')
        with Image.open(output) as actual:
            self.assertEqual(actual.size, expected.size)
            self.assertAlmostEqual(actual.info['dpi'][0], 150, delta=.05)
            self.assertAlmostEqual(actual.info['dpi'][1], 300, delta=.05)
        self.assertEqual(source.read_bytes(), before)

    def test_dimensions_only_source_probe_never_certifies_a_complete_jpeg(self):
        source = self.root/'partial.jpg'
        source.write_bytes(b'\xff\xd8\xff\xc0'+struct.pack('>H', 11)+
                           b'\x08\0\x02\0\x02\x01\x01\x11\0')
        original = source.read_bytes()
        profile = inspect_media(source)
        self.assertEqual((profile['width'], profile['height']), (2, 2))
        self.assertIs(profile.get('metadataComplete'), False)
        with self.assertRaises(GuardError) as caught:
            inspect_media(source, validate_container=True)
        self.assertEqual(caught.exception.code, 'MEDIA_OUTPUT_INVALID')
        with self.assertRaises(GuardError) as caught:
            self.convert(source, 'png')
        self.assertEqual(caught.exception.code, 'MEDIA_SOURCE_INVALID')
        self.assertFalse((self.root/'partial-png/partial.png').exists())
        self.assertEqual(source.read_bytes(), original)

    def test_pdf_jpeg_white_compositor_keeps_recorded_144_dpi(self):
        # A fresh QGuiApplication avoids depending on another test's Qt lifetime.
        from pypdf import PdfWriter
        source = self.root/'page.pdf'
        writer = PdfWriter(); writer.add_blank_page(width=72, height=144)
        with source.open('wb') as stream:
            writer.write(stream)
        command = r'''
import json,sys
from PySide6.QtGui import QGuiApplication
from PIL import Image
from app_backend.converter_guard.qt_media import convert_document_pages
gui=QGuiApplication([])
[output]=convert_document_pages(sys.argv[1],sys.argv[2],'jpeg')
with Image.open(output) as image:
 print(json.dumps({'size':list(image.size),'dpi':list(image.info.get('dpi',[]))}))
'''
        env = dict(os.environ, QT_QPA_PLATFORM='offscreen')
        completed = subprocess.run([sys.executable, '-c', command, str(source),
                                    str(self.root/'pdf-images')],
            cwd=Path(sys.modules[convert_media_frames.__module__].__file__).resolve().parents[2], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout.strip())
        self.assertEqual(report['size'], [144, 288])
        self.assertEqual(report['dpi'], [144, 144])

    def test_apng_refuses_pages_with_different_physical_scales(self):
        from app_backend.converter_guard.qt_media import _write_apng
        frames = [QImage(3, 2, QImage.Format.Format_RGBA8888) for _ in range(2)]
        for frame, resolution in zip(frames, (11811, 5906)):
            frame.fill(QColor(20, 40, 60, 255))
            frame.setDotsPerMeterX(resolution); frame.setDotsPerMeterY(resolution)
        with self.assertRaises(GuardError) as caught:
            _write_apng(io.BytesIO(), iter(frames),
                        {'width':3, 'height':2, 'frames':2, 'pages':1,
                         'durationsMs':[100, 200]}, self.cancel)
        self.assertEqual(caught.exception.code, 'MEDIA_PAGE_RESOLUTION_MISMATCH')

    def test_actual_expected_media_losses_reach_user_warning_details(self):
        transparent = self.root/'transparent.png'
        Image.new('RGBA', (4, 2), (255, 0, 0, 128)).save(transparent)
        animated = self.root/'animated.png'
        frames = [Image.new('RGBA', (4, 2), c) for c in
                  ((255, 0, 0, 255), (0, 0, 255, 255))]
        frames[0].save(animated, save_all=True, append_images=frames[1:],
                       duration=[100, 200], loop=0)
        solid = self.root/'solid.png'; Image.new('RGB', (4, 2), (20, 40, 60)).save(solid)
        cases = [(transparent, 'jpeg', 'MEDIA_ALPHA_REMOVED'),
                 (animated, 'jpeg', 'MEDIA_ANIMATION_FIRST_FRAME'),
                 (animated, 'tiff', 'MEDIA_ANIMATION_AS_PAGES'),
                 (solid, 'gif', 'MEDIA_GIF_PALETTE_LIMITS')]
        for index, (source, target, warning) in enumerate(cases):
            with self.subTest(target=target, warning=warning):
                original = source.read_bytes()
                adapter = LibreOfficeAdapter(sys.executable)
                native = SimpleNamespace(valid=False, fingerprint='media-qa', filters={},
                                         html_route_reason=lambda *_: None)
                adapter._registry = lambda: native
                # Admission is controlled here; the real Qt conversion, result
                # check and warning-details path are the behavior under test.
                adapter.route_decision = lambda *_: 'registered'
                adapter.prepare_source({'state':'detected', 'qtMediaTargets':[target]})
                paths = adapter.convert(source, target, self.root/('warnings-'+str(index)),
                    cancel=self.cancel, timeout=None, on_started=lambda: None)
                self.assertTrue(paths[0].is_file())
                export = adapter.export_info()
                self.assertIn(warning, export.get('warnings', ()))
                messages = warning_details(export['warnings'])
                self.assertTrue(any(row['code']==warning and row['message'] for row in messages))
                self.assertTrue(any(check.get('expectedLosses') for check in export['resultChecks']))
                self.assertEqual(source.read_bytes(), original)


@unittest.skipUnless(os.environ.get('AIO_DOM_MODULE') and shutil.which('node'),
                     'Set AIO_DOM_MODULE to the pinned offline DOM package')
class MediaWarningDom(unittest.TestCase):
    def test_loss_warning_coexists_with_quality_summary_and_error_remains_primary(self):
        completed = subprocess.run(['node', str(Path(__file__).with_name('deep_media_warnings_dom.mjs'))],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stdout+'\n'+completed.stderr)
        print(completed.stdout.strip())


if __name__ == '__main__':
    unittest.main()
