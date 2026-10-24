"""Raster HTML must retain original media or every independently decoded TIFF page."""
import hashlib
from html.parser import HTMLParser
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET

from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard.media_fidelity import inspect_media
from app_backend.converter_guard.media_html import convert_media_html
from test_guard_media_fidelity_v16 import gif, png, WEBP_ANIMATED

try:
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QApplication
except ImportError:
    QImage = None
try:
    from PIL import Image
except ImportError:
    Image = None


class ParsedHTML(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.images = []
        self.sections = []
        self.tags = []
        self.title = ''
        self.in_title = False
        self.feed(source)
        self.close()

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        self.tags.append(tag)
        if tag == 'img':
            self.images.append(attributes)
        elif tag == 'section':
            self.sections.append(attributes)
        elif tag == 'title':
            self.in_title = True

    def handle_startendtag(self, tag, attributes):
        self.handle_starttag(tag, attributes)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag == 'title':
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title += data


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@unittest.skipIf(QImage is None, 'Packaged Qt required')
class MediaHTML(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        # Preserve a shared widget application for subsequent Qt UI tests.
        cls.application = QCoreApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cancel = threading.Event()

    def source(self, filename, data):
        path = self.root/filename
        path.write_bytes(data)
        return path

    def converted(self, source, *, target='html', directory=None, cancel=None):
        output = directory or self.root/(source.suffix[1:]+'-'+target)
        paths = convert_media_html(source, output, target, cancel=cancel or self.cancel)
        self.assertIsNotNone(paths)
        paths = list(map(Path, paths))
        primary = next(path for path in paths if path.suffix == '.'+target)
        return primary, paths, ParsedHTML(primary.read_text(encoding='utf-8'))

    def sidecar(self, primary, attributes):
        value = urlsplit(attributes['src'])
        self.assertEqual((value.scheme, value.netloc, value.query, value.fragment), ('', '', '', ''))
        self.assertFalse(value.path.startswith('/'))
        path = primary.parent/unquote(value.path)
        self.assertTrue(path.resolve().is_relative_to(primary.parent.resolve()))
        self.assertTrue(path.is_file())
        return path

    def test_browser_animations_are_byte_identical_sidecars_with_real_frames(self):
        if Image is None:
            self.skipTest('Independent Pillow decoder unavailable')
        for ext, data, frames in [('gif', gif(frames=8, delay=15), 8),
                                  ('apng', png(width=3, height=5, frames=8, delay=200), 8),
                                  ('webp', WEBP_ANIMATED, 3)]:
            with self.subTest(format=ext):
                source = self.source('source.'+ext, data)
                primary, paths, html = self.converted(source)
                self.assertEqual(len(html.images), 1)
                sidecar = self.sidecar(primary, html.images[0])
                self.assertIn(sidecar, paths)
                self.assertEqual(digest(sidecar), digest(source))
                self.assertEqual(sidecar.suffix, '.png' if ext == 'apng' else '.'+ext)
                with Image.open(source) as expected, Image.open(sidecar) as actual:
                    self.assertEqual(actual.n_frames, frames)
                    self.assertEqual(actual.n_frames, expected.n_frames)
                    for n in range(frames):
                        expected.seek(n);actual.seek(n)
                        self.assertEqual(actual.convert('RGBA').tobytes(), expected.convert('RGBA').tobytes())
                        self.assertEqual(actual.info.get('duration'), expected.info.get('duration'))
                    self.assertEqual((int(html.images[0]['width']), int(html.images[0]['height'])), expected.size)

    def test_png_jpeg_and_bmp_sidecars_keep_source_bytes(self):
        image = QImage(3, 5, QImage.Format.Format_RGBA8888)
        image.fill(0xff7020c0)
        for suffix, fmt in [('png', 'PNG'), ('jpeg', 'JPEG'), ('bmp', 'BMP')]:
            with self.subTest(format=suffix):
                source = self.root/('source.'+suffix)
                self.assertTrue(image.save(str(source), fmt))
                primary, _paths, html = self.converted(source)
                self.assertEqual(len(html.images), 1)
                sidecar = self.sidecar(primary, html.images[0])
                self.assertEqual(digest(sidecar), digest(source))
                self.assertEqual((html.images[0]['width'], html.images[0]['height']), ('3', '5'))

    def tiff_fixture(self):
        if Image is None:
            self.skipTest('Independent Pillow encoder/decoder unavailable')
        frames = []
        for n, (width, height) in enumerate(((3, 2), (5, 3), (2, 4))):
            frame = Image.new('RGBA', (width, height), (n*70, 60+n*40, 200-n*50, 255))
            frame.putpixel((0, 0), (20+n*30, 80, 150, n*70))
            frames.append(frame)
        path = self.root/'pages.tiff'
        frames[0].save(path, save_all=True, append_images=frames[1:], compression='tiff_lzw')
        return path

    def test_tiff_keeps_all_independently_decoded_pages_and_geometry(self):
        source = self.tiff_fixture()
        primary, paths, html = self.converted(source)
        self.assertEqual(len(html.images), 3)
        sections = [row for row in html.sections if 'aio-media-page' in row.get('class', '').split()]
        self.assertEqual([row['data-aio-page'] for row in sections], ['1', '2', '3'])
        with Image.open(source) as expected:
            for n, attributes in enumerate(html.images):
                expected.seek(n)
                path = self.sidecar(primary, attributes)
                self.assertEqual(path.name, 'page-'+str(n+1).zfill(6)+'.png')
                self.assertIn(path, paths)
                self.assertEqual((int(attributes['width']), int(attributes['height'])), expected.size)
                with Image.open(path) as actual:
                    self.assertEqual(actual.format, 'PNG')
                    self.assertEqual(actual.size, expected.size)
                    self.assertEqual(actual.convert('RGBA').tobytes(), expected.convert('RGBA').tobytes())

    def test_adapter_selects_browser_media_route_without_freezing_native_export(self):
        from app_backend.converter_guard.adapter import LibreOfficeAdapter
        from app_backend.converter_guard.qt_media import media_preflight

        source = self.source('animation.gif', gif(frames=8, delay=15))
        media = media_preflight(source, cancel=self.cancel)
        self.assertIn('html', media['targets'])
        adapter = LibreOfficeAdapter(self.root/'unused-soffice')
        adapter._local.proof = {'state': 'detected', 'basis': 'qt_media_content',
            'registryFingerprint': 'native-registry', 'qtMediaTargets': media['targets'],
            'mediaProfile': media['source']}
        started = []
        with (patch.object(adapter, '_registry', return_value=SimpleNamespace(fingerprint='native-registry')),
              patch.object(adapter, '_convert_native', side_effect=AssertionError('Unexpected native image export'))):
            paths = adapter.convert(source, 'html', self.root/'adapter', cancel=self.cancel,
                timeout=5, on_started=lambda: started.append(True))
        primary = next(Path(path) for path in paths if Path(path).suffix == '.html')
        html = ParsedHTML(primary.read_text(encoding='utf-8'))
        sidecar = self.sidecar(primary, html.images[0])
        self.assertEqual(digest(sidecar), digest(source))
        self.assertEqual(inspect_media(sidecar)['frames'], 8)
        self.assertEqual(adapter.execution_mode(), 'qt_media_html')
        self.assertEqual(started, [])
        checks = adapter.export_info()['resultChecks']
        self.assertTrue(any(row['check'] == 'html_body' and row['status'] == 'content_present'
                            for row in checks))

    def test_adapter_tiff_html_retains_every_page_in_saved_companion_list(self):
        from app_backend.converter_guard.adapter import LibreOfficeAdapter
        from app_backend.converter_guard.qt_media import media_preflight

        source = self.tiff_fixture()
        media = media_preflight(source, cancel=self.cancel)
        self.assertIn('xhtml', media['targets'])
        adapter = LibreOfficeAdapter(self.root/'unused-soffice')
        adapter._local.proof = {'state': 'detected', 'basis': 'qt_media_content',
            'registryFingerprint': 'native-registry', 'qtMediaTargets': media['targets'],
            'mediaProfile': media['source']}
        with (patch.object(adapter, '_registry', return_value=SimpleNamespace(fingerprint='native-registry')),
              patch.object(adapter, '_convert_native', side_effect=AssertionError('Unexpected native image export'))):
            paths = adapter.convert(source, 'xhtml', self.root/'adapter', cancel=self.cancel,
                timeout=5, on_started=lambda: self.fail('LibreOffice was not needed'))
        primary = next(Path(path) for path in paths if Path(path).suffix == '.xhtml')
        html = ParsedHTML(primary.read_text(encoding='utf-8'))
        self.assertEqual(len(html.images), 3)
        self.assertEqual(len(paths), 4)
        with Image.open(source) as expected:
            for index, attributes in enumerate(html.images):
                expected.seek(index)
                sidecar = self.sidecar(primary, attributes)
                self.assertIn(sidecar, paths)
                with Image.open(sidecar) as actual:
                    self.assertEqual(actual.size, expected.size)
                    self.assertEqual(actual.convert('RGBA').tobytes(), expected.convert('RGBA').tobytes())

    def test_xhtml_is_well_formed_xml_with_safe_relative_images(self):
        source = self.source('source.apng', png(frames=3))
        primary, _paths, html = self.converted(source, target='xhtml')
        document = ET.parse(primary)
        images = [element for element in document.iter() if element.tag.rsplit('}', 1)[-1] == 'img']
        self.assertEqual(len(images), 1)
        self.assertEqual(len(html.images), 1)
        self.assertEqual(digest(self.sidecar(primary, images[0].attrib)), digest(source))

    def test_htm_alias_is_an_actual_html_document(self):
        source = self.source('source.png', png())
        primary, _paths, html = self.converted(source, target='htm')
        self.assertEqual(primary.suffix, '.htm')
        self.assertEqual(len(html.images), 1)

    def test_source_filename_is_escaped_without_creating_markup(self):
        names = ["quote & apostrophe 'example'.png",
                 '&lt;img src=x onerror=alert(1)&gt;.png']
        if os.name != 'nt':
            names.append('<img src=x onerror=alert(1)>.png')
        for n, name in enumerate(names):
            with self.subTest(name=name):
                source = self.source(name, png())
                primary, _paths, html = self.converted(source, target='xhtml', directory=self.root/('escaped-'+str(n)))
                ET.parse(primary)
                self.assertIn(source.stem, html.title)
                self.assertEqual(len(html.images), 1)
                self.assertNotIn('script', html.tags)
                self.assertFalse(any(key.startswith('on') for image in html.images for key in image))
                if name.startswith('&lt;'):
                    self.assertIn('&amp;lt;', primary.read_text(encoding='utf-8'))

    def test_pre_cancel_preserves_existing_unrelated_files(self):
        source = self.source('source.png', png())
        output = self.root/'existing';output.mkdir()
        keep = output/'keep.txt';keep.write_bytes(b'keep')
        self.cancel.set()
        with self.assertRaises(GuardError) as error:
            convert_media_html(source, output, 'html', cancel=self.cancel)
        self.assertEqual(error.exception.code, 'CANCELLED')
        self.assertEqual(keep.read_bytes(), b'keep')
        self.assertEqual([path for path in output.rglob('*') if path.is_file()], [keep])

    def test_existing_media_directory_is_preserved_on_collision(self):
        source = self.source('source.png', png())
        output = self.root/'existing-media'
        media = output/'media';media.mkdir(parents=True)
        keep = media/'source.png';keep.write_bytes(b'previous result')
        with self.assertRaises(GuardError) as error:
            convert_media_html(source, output, 'html', cancel=self.cancel)
        self.assertEqual(error.exception.code, 'MEDIA_OUTPUT_COLLISION')
        self.assertEqual(keep.read_bytes(), b'previous result')
        self.assertEqual([path for path in output.rglob('*') if path.is_file()], [keep])

    def test_mid_page_cancel_cleans_owned_outputs_and_preserves_unrelated_file(self):
        source = self.tiff_fixture()
        output = self.root/'cancelled';output.mkdir()
        keep = output/'keep.txt';keep.write_bytes(b'keep')
        first = QImage(3, 2, QImage.Format.Format_RGBA8888);first.fill(0xff0080ff)
        def frames(*args, **kwargs):
            yield first, 0
            self.cancel.set()
            raise GuardError('CANCELLED', 'Test cancellation between pages.', stage='result')
        with patch('app_backend.converter_guard.media_html.iter_media_frames', frames):
            with self.assertRaises(GuardError) as error:
                convert_media_html(source, output, 'html', cancel=self.cancel)
        self.assertEqual(error.exception.code, 'CANCELLED')
        self.assertEqual(keep.read_bytes(), b'keep')
        self.assertEqual([path for path in output.rglob('*') if path.is_file()], [keep])

    def test_other_target_or_non_raster_retains_existing_conversion_route(self):
        source = self.source('source.png', png())
        self.assertIsNone(convert_media_html(source, self.root/'other', 'pdf', cancel=self.cancel))
        other = self.source('document.bin', b'ordinary document')
        self.assertIsNone(convert_media_html(other, self.root/'other', 'html', cancel=self.cancel))


if __name__ == '__main__':
    unittest.main()
