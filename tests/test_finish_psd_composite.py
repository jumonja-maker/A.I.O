"""PSD merged pixels are distinct from PSD layers and editing semantics."""
from pathlib import Path
import struct
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest

from PIL import Image
from PySide6.QtGui import QColorSpace

from app_backend.converter_guard.adapter import LibreOfficeAdapter
from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard.export_recipe import warning_details
from app_backend.converter_guard.media_fidelity import check_media_preservation
from app_backend.converter_guard.qt_media import convert_media_frames, pillow_legacy_profile


def psd_bytes(*, width=3, height=2, channels=3, depth=8, mode=3, resources=b''):
    header = b'8BPS' + struct.pack('>H', 1) + bytes(6)
    header += struct.pack('>HIIHH', channels, height, width, depth, mode)
    header += struct.pack('>I', 0) + struct.pack('>I', len(resources)) + resources + struct.pack('>I', 0)
    pixels = [(index * 31 % 256, index * 47 % 256, 73) for index in range(width * height)]
    return header + struct.pack('>H', 0) + bytes(p[c] for c in range(3) for p in pixels)


def resource(ident, payload):
    return b'8BIM' + struct.pack('>H', ident) + b'\0\0' + struct.pack('>I', len(payload)) + payload + bytes(len(payload) % 2)


class PsdComposite(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cancel = threading.Event()

    def write(self, raw, name='composite.psd'):
        source = self.root/name
        source.write_bytes(raw)
        return source

    def test_merged_rgb_pixels_canvas_and_explicit_editability_warning(self):
        source = self.write(psd_bytes())
        original = source.read_bytes()
        profile = pillow_legacy_profile(source)
        self.assertTrue(profile['mergedCompositeOnly'])
        self.assertEqual(profile['sourceLayerCount'], 0)
        adapter = LibreOfficeAdapter(sys.executable)
        adapter._registry = lambda: SimpleNamespace(valid=False, filters={}, html_route_reason=lambda *_: None)
        adapter.route_decision = lambda *_: 'registered'
        for target in ('png', 'apng', 'tiff'):
            adapter.prepare_source({'state':'detected', 'qtMediaTargets':[target]})
            output = adapter.convert(source, target, self.root/target, cancel=self.cancel,
                                     timeout=None, on_started=lambda: None)[0]
            with Image.open(source) as expected, Image.open(output) as actual:
                self.assertEqual(actual.size, (3, 2))
                self.assertEqual(actual.convert('RGB').tobytes(), expected.tobytes())
            export = adapter.export_info()
            self.assertIn('MEDIA_PSD_MERGED_COMPOSITE', export['warnings'])
            text = next(row['message'] for row in warning_details(export['warnings'])
                        if row['code'] == 'MEDIA_PSD_MERGED_COMPOSITE')
            self.assertIn('Слои', text)
            self.assertIn('не переносятся', text)
        self.assertEqual(source.read_bytes(), original)

    def test_supported_rgb_profile_is_carried_without_changing_rgb_values(self):
        profile = bytes(QColorSpace(QColorSpace.NamedColorSpace.DisplayP3).iccProfile())
        source = self.write(psd_bytes(resources=resource(1039, profile)))
        self.assertTrue(pillow_legacy_profile(source)['iccProfilePresent'])
        output = convert_media_frames(source, self.root/'out', 'png')[0]
        with Image.open(source) as expected, Image.open(output) as actual:
            self.assertEqual(actual.convert('RGB').tobytes(), expected.tobytes())
            self.assertEqual(actual.info['icc_profile'], profile)

    def test_invalid_or_non_rgb_icc_never_publishes_unmanaged_pixels(self):
        gray = (Path(__file__).parent/'data/deep_media_profiles/Gray.icc').read_bytes()
        for index, profile in enumerate((b'not an ICC profile', gray)):
            with self.subTest(index=index):
                source = self.write(psd_bytes(resources=resource(1039, profile)), '%d.psd' % index)
                with self.assertRaises(GuardError) as caught:
                    convert_media_frames(source, self.root/str(index), 'png')
                self.assertEqual(caught.exception.code, 'MEDIA_COLOR_PROFILE_UNSUPPORTED')
                self.assertFalse((self.root/str(index)/(source.stem+'.png')).exists())

    def test_alpha_cmyk_and_high_depth_models_are_not_promoted_to_rgb_composite(self):
        for index, values in enumerate(({'channels':4}, {'channels':4, 'mode':4}, {'depth':16})):
            with self.subTest(values=values):
                source = self.write(psd_bytes(**values), '%d.psd' % index)
                self.assertIsNone(pillow_legacy_profile(source))
                self.assertIsNone(convert_media_frames(source, self.root/str(index), 'png'))

    def test_truncated_pixels_bad_resource_and_metadata_overflow_fail_before_decode(self):
        data = psd_bytes()
        malformed = [data[:-1], psd_bytes(resources=resource(1039, b'abc')[:-1]),
                     data[:30]+struct.pack('>I', 17 * 1024 * 1024)+data[34:]]
        for index, raw in enumerate(malformed):
            with self.subTest(index=index):
                source = self.write(raw, '%d.psd' % index)
                with self.assertRaises(GuardError) as caught:
                    pillow_legacy_profile(source)
                self.assertIn(caught.exception.code, ('MEDIA_SOURCE_INVALID', 'MEDIA_METADATA_BUDGET'))

    def test_oversized_canvas_is_rejected_without_allocating_its_pixels(self):
        raw = bytearray(psd_bytes())
        struct.pack_into('>II', raw, 14, 65535, 65535)
        with self.assertRaises(GuardError) as caught:
            pillow_legacy_profile(self.write(raw))
        self.assertEqual(caught.exception.code, 'MEDIA_CANVAS_BUDGET')

    def test_source_checker_catches_reintroduced_native_draw_canvas(self):
        source = self.write(psd_bytes())
        output = self.root/'out'/source.with_suffix('.png').name
        output.parent.mkdir()
        Image.new('RGB', (794, 1123), 'white').save(output)
        with self.assertRaises(GuardError) as caught:
            check_media_preservation([output], 'png', source, cancel=self.cancel)
        self.assertEqual(caught.exception.code, 'MEDIA_CANVAS_CHANGED')


if __name__ == '__main__':
    unittest.main()
