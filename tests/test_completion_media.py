"""FULLTEST #4: physical scale, standalone canvases and one GIF policy."""
import json
from pathlib import Path
import struct
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image

from app_backend.converter_guard.adapter import LibreOfficeAdapter
from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard.export_recipe import warning_details
from app_backend.converter_guard.media_fidelity import check_media_preservation, inspect_media
from app_backend.converter_guard.native_fidelity import NATIVE_FIDELITY_SOURCE, aio_pict_frame
from app_backend.converter_guard.qt_media import (
    convert_media_frames, encode_native_gif_pages, media_preflight, pillow_legacy_profile)


def pcd_bytes(orientation=0):
    """PhotoCD Base: two full luma rows followed by two half chroma rows."""
    raw = bytearray(96 * 2048)
    raw[2048:2055] = b'PCD_IPI'
    raw[2048 + 1538] = orientation
    for y in range(0, 512, 2):
        raw.extend(bytes([32 + y // 4]) * 768)
        raw.extend(bytes([33 + y // 4]) * 768)
        raw.extend(bytes([156]) * 384)
        raw.extend(bytes([137]) * 384)
    return bytes(raw)


def pict_bytes(width=37, height=19):
    frame = struct.pack('>4h', 0, 0, height, width)
    return b'\0' * 512 + b'\0\0' + frame + b'\x11\x01\x01\0\x0a' + frame + b'\x31' + frame + b'\xff'


class CompletionMedia(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cancel = threading.Event()

    def source(self, name, raw):
        path = self.root / name
        path.write_bytes(raw)
        return path

    def convert(self, source, target):
        paths = convert_media_frames(source, self.root/(source.name + '-' + target),
                                     target, cancel=self.cancel)
        self.assertIsNotNone(paths)
        receipt = check_media_preservation(paths, target, source, cancel=self.cancel)
        self.assertIsNotNone(receipt['source'])
        return paths[0]

    def test_pcx_header_resolution_survives_all_encoders_that_store_it(self):
        source = self.root/'print.pcx'
        image = Image.new('RGB', (37, 19))
        image.putdata([(x * 5 % 256, y * 11 % 256, 37) for y in range(19) for x in range(37)])
        image.save(source, dpi=(100, 200))
        # Pillow's PCX writer currently writes its first DPI value twice.
        # Author the independent Y-resolution field directly in this fixture.
        raw = bytearray(source.read_bytes())
        struct.pack_into('<H', raw, 14, 200)
        source.write_bytes(raw)
        before = source.read_bytes()
        self.assertEqual(pillow_legacy_profile(source)['dpi'], [100, 200])
        for target in ('png', 'apng', 'tiff', 'bmp', 'jpg', 'jfif', 'jif', 'jpe'):
            with self.subTest(target=target):
                output = self.convert(source, target)
                with Image.open(output) as actual:
                    self.assertEqual(actual.size, (37, 19))
                    self.assertAlmostEqual(actual.info['dpi'][0], 100, delta=.03)
                    self.assertAlmostEqual(actual.info['dpi'][1], 200, delta=.03)
                    if target in ('png', 'apng', 'tiff', 'bmp'):
                        self.assertEqual(actual.convert('RGB').tobytes(), image.tobytes())
                info = inspect_media(output)
                resolution = info.get('dpi', info.get('pageDpi', [None])[0])
                self.assertAlmostEqual(resolution[0], 100, delta=.03)
        self.assertEqual(source.read_bytes(), before)

    def test_pcx_lost_dpi_is_a_mandatory_failure(self):
        source = self.root/'print.pcx'
        Image.new('RGB', (37, 19), 'red').save(source, dpi=(100, 100))
        output = self.root/'out'/'print.png'
        output.parent.mkdir()
        Image.new('RGB', (37, 19), 'red').save(output, dpi=(96, 96))
        with self.assertRaises(GuardError) as caught:
            check_media_preservation([output], 'png', source, cancel=self.cancel)
        self.assertEqual(caught.exception.code, 'MEDIA_DPI_LOSS')

    def test_pcx_zero_resolution_is_not_invented(self):
        source = self.root/'untagged.pcx'
        Image.new('RGB', (2, 3), 'red').save(source)
        raw = bytearray(source.read_bytes())
        raw[12:16] = b'\0' * 4
        source.write_bytes(raw)
        self.assertNotIn('dpi', pillow_legacy_profile(source))

    def test_static_jpeg_bmp_check_only_the_represented_tiff_page_resolution(self):
        source = self.root/'book.tiff'
        first = Image.new('RGB', (37, 19), 'red')
        first.save(source, save_all=True,
                   append_images=[Image.new('RGB', (37, 19), 'blue')], dpi=(100, 200))
        for target in ('jpg', 'bmp'):
            with self.subTest(target=target):
                output = self.root/target/('book.' + target)
                output.parent.mkdir()
                first.save(output, dpi=(100, 200))
                receipt = check_media_preservation([output], target, source, cancel=self.cancel)
                self.assertIn('pages', receipt['expectedLosses'])
                first.save(output, dpi=(72, 72))
                with self.assertRaises(GuardError) as caught:
                    check_media_preservation([output], target, source, cancel=self.cancel)
                self.assertEqual(caught.exception.code, 'MEDIA_DPI_LOSS')

    def test_photo_cd_base_and_oriented_canvases_preserve_pixels(self):
        for orientation, dimensions in ((0, (768, 512)), (1, (512, 768)), (3, (512, 768))):
            with self.subTest(orientation=orientation):
                source = self.source('base%d.pcd' % orientation, pcd_bytes(orientation))
                with Image.open(source) as decoded:
                    expected = decoded.convert('RGB').tobytes()
                profile = media_preflight(source)
                self.assertEqual(profile['source']['decodedResolution'], 'PhotoCD_Base')
                for target in ('png', 'apng', 'tiff'):
                    output = self.convert(source, target)
                    with Image.open(output) as actual:
                        self.assertEqual(actual.size, dimensions)
                        self.assertEqual(actual.convert('RGB').tobytes(), expected)

    def test_photo_cd_unknown_orientation_and_truncation_stay_native(self):
        self.assertIsNone(pillow_legacy_profile(self.source('rotate.pcd', pcd_bytes(2))))
        self.assertIsNone(pillow_legacy_profile(self.source('truncated.pcd', pcd_bytes()[:-1])))
        self.assertIsNone(pillow_legacy_profile(self.source('wrong.pcd', b'ordinary text')))

    def test_photo_cd_base_scope_reaches_user_warning_without_proof_metadata_or_claimed_loss(self):
        source = self.source('base.pcd', pcd_bytes())
        adapter = LibreOfficeAdapter(sys.executable)
        adapter._registry = lambda: SimpleNamespace(valid=False, filters={}, html_route_reason=lambda *_: None)
        adapter.route_decision = lambda *_: 'registered'
        # Prove that warnings come from the confirmed decoding scope even
        # when no optional mediaProfile/quality metadata was supplied by UI.
        adapter.prepare_source({'state':'detected', 'qtMediaTargets':['png']})
        paths = adapter.convert(source, 'png', self.root/'pcd-warning', cancel=self.cancel,
                                timeout=None, on_started=lambda: None)
        self.assertTrue(paths[0].is_file())
        export = adapter.export_info()
        self.assertIn('MEDIA_PHOTOCD_BASE_ONLY', export['warnings'])
        messages = warning_details(export['warnings'])
        message = next(row['message'] for row in messages if row['code'] == 'MEDIA_PHOTOCD_BASE_ONLY')
        for detail in ('Base', '768 × 512', '4Base', '16Base', 'не проверены'):
            self.assertIn(detail, message)
        check = next(row for row in export['resultChecks'] if row['check'] == 'media_properties')
        self.assertEqual(check['source']['decodedResolution'], 'PhotoCD_Base')
        self.assertEqual(check['expectedLosses'], [])

    def test_tga_uses_the_confirmed_raster_canvas_and_alpha(self):
        for mode in ('RGB', 'RGBA'):
            with self.subTest(mode=mode):
                source = self.root/(mode+'.tga')
                image = Image.new(mode, (37, 19))
                image.putdata([(x*5, y*11, 37, x*7 % 256)[:len(mode)]
                               for y in range(19) for x in range(37)])
                image.save(source)
                original = source.read_bytes()
                profile = media_preflight(source)
                self.assertEqual(profile['source']['format'], 'tga')
                self.assertEqual(profile['decoder'], 'qt_image_reader')
                for target in ('png', 'apng', 'tiff'):
                    output = self.convert(source, target)
                    with Image.open(output) as actual:
                        self.assertEqual(actual.size, image.size)
                        self.assertEqual(actual.convert(mode).tobytes(), image.tobytes())
                self.assertEqual(source.read_bytes(), original)

    def test_native_gif_pages_use_exact_palette_and_updated_manifest(self):
        pages = []
        for index, color in enumerate(('red', 'blue')):
            page = self.root/('book%s.png' % ('' if not index else '__page_0002'))
            Image.new('RGB', (37, 19), color).save(page)
            pages.append(page)
        manifest = self.root/'AIO-pages.json'
        manifest.write_text(json.dumps({'schema': 1, 'pageCount': 2,
            'pages': [{'page': index+1, 'file': page.name} for index, page in enumerate(pages)]}))
        result, receipt = encode_native_gif_pages([*pages, manifest], cancel=self.cancel)
        self.assertEqual([p.suffix for p in result], ['.gif', '.gif', '.json'])
        self.assertFalse(receipt['paletteQuantized'])
        self.assertFalse(receipt['qualityCompared'])
        self.assertEqual([p['file'] for p in json.loads(manifest.read_text())['pages']],
                         [p.name for p in result[:2]])
        self.assertFalse(any(p.exists() for p in pages))
        for page, color in zip(result[:2], ((255, 0, 0), (0, 0, 255))):
            with Image.open(page) as decoded:
                self.assertEqual(decoded.size, (37, 19))
                self.assertEqual(decoded.convert('RGB').getpixel((0, 0)), color)

    def test_native_and_qt_gif_reject_the_same_unrepresentable_page_without_partial_result(self):
        first = self.root/'book.png'
        Image.new('RGB', (37, 19), 'red').save(first)
        rich = self.root/'book__page_0002.png'
        image = Image.new('RGB', (37, 19))
        image.putdata([(x % 256, x // 256, 37) for x in range(37 * 19)])
        image.save(rich)
        for native in (False, True):
            with self.subTest(native=native):
                with self.assertRaises(GuardError) as caught:
                    if native:
                        encode_native_gif_pages([first, rich], cancel=self.cancel)
                    else:
                        convert_media_frames(rich, self.root/'direct', 'gif', cancel=self.cancel)
                self.assertEqual(caught.exception.code, 'MEDIA_GIF_PALETTE_UNREPRESENTABLE')
                self.assertFalse(list(self.root.glob('*.gif')))
                self.assertTrue(first.is_file() and rich.is_file())

    def test_native_gif_refuses_a_contradictory_page_manifest_without_publishing(self):
        first = self.root/'book.png'
        second = self.root/'book__page_0002.png'
        for page in (first, second):
            Image.new('RGB', (37, 19), 'red').save(page)
        manifest = self.root/'AIO-pages.json'
        valid = {'schema':1, 'pageCount':2,
                 'pages':[{'page':1, 'file':first.name}, {'page':2, 'file':second.name}]}
        for invalid in ({**valid, 'schema':999}, {**valid, 'pageCount':999},
                        {**valid, 'pages':[{'page':0, 'file':first.name}, {'page':0, 'file':second.name}]}):
            with self.subTest(invalid=invalid):
                manifest.write_text(json.dumps(invalid))
                original = manifest.read_bytes()
                with self.assertRaises(GuardError) as caught:
                    encode_native_gif_pages([first, second, manifest], cancel=self.cancel)
                self.assertEqual(caught.exception.code, 'MEDIA_INSPECTION_INCOMPLETE')
                self.assertEqual(manifest.read_bytes(), original)
                self.assertFalse(list(self.root.glob('*.gif')))
                self.assertTrue(first.is_file() and second.is_file())

    def test_adapter_native_gif_requests_png_before_the_shared_exact_encoder(self):
        adapter = LibreOfficeAdapter(sys.executable)
        adapter._registry = lambda: SimpleNamespace(valid=False, filters={}, html_route_reason=lambda *_: None)
        source = self.source('drawing.fodg', b'<independent-document/>')
        def native(path, target, work, **kwargs):
            self.assertEqual(target, 'png')
            output = work/'output'/source.with_suffix('.png').name
            output.parent.mkdir(parents=True)
            Image.new('RGB', (37, 19), (12, 34, 56)).save(output)
            adapter._local.execution_mode = 'native_uno'
            adapter._local.export_info = {'filter': 'draw_png_Export'}
            return [output]
        with patch.object(adapter, '_convert_native', native):
            output = adapter.convert(source, 'gif', self.root/'task', cancel=self.cancel,
                                     timeout=None, on_started=lambda: None)
        self.assertEqual(output[0].suffix, '.gif')
        self.assertTrue(any(c['check'] == 'gif_exact_palette'
                            for c in adapter.export_info()['resultChecks']))

    def test_pict_declaration_catches_a_reintroduced_draw_page_canvas(self):
        source = self.source('picture.pct', pict_bytes())
        self.assertEqual(aio_pict_frame(source), (37, 19))
        output = self.root/'out'/'picture.png'
        output.parent.mkdir()
        Image.new('RGB', (794, 1123), 'white').save(output)
        with self.assertRaises(GuardError) as caught:
            check_media_preservation([output], 'png', source, cancel=self.cancel)
        self.assertEqual(caught.exception.code, 'MEDIA_CANVAS_CHANGED')
        self.assertEqual(caught.exception.diagnostic, 'source_canvas:37x19:result_canvas:794x1123')

    def test_pict_native_extraction_requires_actual_import_filter_and_one_unmodified_graphic(self):
        namespace = {}
        exec(compile(NATIVE_FIDELITY_SOURCE, '<native_media>', 'exec'), namespace)
        source = self.source('picture.pct', pict_bytes())
        target = self.root/'picture.png'
        class Items:
            def __init__(self, *items): self.items = items
            def getCount(self): return len(self.items)
            def getByIndex(self, index): return self.items[index]
        shape = SimpleNamespace(getShapeType=lambda: 'com.sun.star.drawing.GraphicObjectShape',
            Graphic=object(), RotateAngle=0, ShearAngle=0,
            GraphicCrop=SimpleNamespace(Top=0, Bottom=0, Left=0, Right=0))
        model = SimpleNamespace(getDrawPages=lambda: Items(Items(shape)))
        stored = []
        source_documents = []
        def render(properties):
            stored.append(dict(properties))
            Image.new('RGB', (37, 19), 'black').save(target)
            return True
        exporter = SimpleNamespace(filter=render, supportsMimeType=lambda mime: mime == 'image/png',
                                   setSourceDocument=source_documents.append)
        services = []
        def service(name, context):
            services.append(name)
            return exporter
        remote = SimpleNamespace(ServiceManager=SimpleNamespace(createInstanceWithContext=service))
        export = namespace['aio_export_graphic_pages']
        self.assertIsNone(export(model, remote, lambda *p:p, target, source=source,
                                 import_filter='draw8'))
        with patch.dict(sys.modules, {'uno': SimpleNamespace(Any=lambda kind, data: data)}):
            self.assertEqual(export(model, remote, lambda *p:p, target, source=source,
                                    import_filter='PCT - Mac Pict'), [target])
        self.assertEqual(dict(stored[0]['FilterData']), {'PixelWidth':37, 'PixelHeight':19})
        self.assertEqual(stored[0]['MediaType'], 'image/png')
        self.assertEqual(source_documents, [shape])
        self.assertEqual(services, ['com.sun.star.drawing.GraphicExportFilter'])
        target.unlink()
        shape.RotateAngle = 9000
        with self.assertRaisesRegex(namespace['AioPreservationIncomplete'], 'pict_graphic_model_unconfirmed'):
            export(model, remote, lambda *p:p, target, source=source, import_filter='PCT - Mac Pict')
        self.assertFalse(target.exists())


if __name__ == '__main__':
    unittest.main()
