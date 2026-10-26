"""Regressions for native BMP size bug and JPEG alias route parity."""
import hashlib
import importlib.util
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch

from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard.media_fidelity import inspect_media, media_route_reason
from app_backend.converter_guard.native_media_repair import repair_native_bmp_size


def bmp(*, width=4, height=2, declared=None, depth=24, compression=0):
    stride = ((width * depth + 31) // 32) * 4
    pixels = bytes((n * 17) % 256 for n in range(stride * abs(height)))
    size = 54 + len(pixels)
    header = b'BM' + struct.pack('<IHHI', size if declared is None else declared, 0, 0, 54)
    header += struct.pack('<IiiHHIIiiII', 40, width, height, 1, depth, compression, len(pixels), 3780, 3780, 0, 0)
    return header + pixels


class BmpNativeRepair(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'document.bmp'
        self.cancel = threading.Event()

    def test_known_lo_32bit_bfsize_is_fixed_without_touching_any_pixel(self):
        original = bmp(declared=54 + 4 * 4 * 2)
        self.path.write_bytes(original)
        with self.assertRaises(GuardError) as caught:
            inspect_media(self.path, validate_container=True)
        self.assertIn('bmp_file_size', caught.exception.diagnostic)
        [record] = repair_native_bmp_size([self.path], cancel=self.cancel)
        repaired = self.path.read_bytes()
        self.assertEqual(repaired[:2], original[:2])
        self.assertEqual(repaired[6:], original[6:])
        self.assertEqual(int.from_bytes(repaired[2:6], 'little'), len(original))
        self.assertEqual(record['sourceOutputSha256'], hashlib.sha256(original).hexdigest())
        self.assertEqual(record['pixelBytesChanged'], 0)
        self.assertEqual(record['headerBytesTouched'], 4)
        self.assertEqual(inspect_media(self.path, validate_container=True)['width'], 4)
        self.assertEqual(repair_native_bmp_size([self.path], cancel=self.cancel), [])

    def test_windows_replace_runs_only_after_source_handle_is_closed(self):
        import os
        original = bmp(declared=54 + 4 * 4 * 2)
        self.path.write_bytes(original)
        real_open, real_replace = Path.open, os.replace
        source_handles = []
        def observe_open(path, mode='r', *args, **kwargs):
            handle = real_open(path, mode, *args, **kwargs)
            if path == self.path and mode == 'rb':
                source_handles.append(handle)
            return handle
        def windows_replace(candidate, destination):
            self.assertTrue(source_handles)
            self.assertTrue(all(handle.closed for handle in source_handles))
            real_replace(candidate, destination)
        with patch.object(Path, 'open', observe_open), patch(
                'app_backend.converter_guard.native_media_repair.os.replace',
                side_effect=windows_replace):
            self.assertEqual(len(repair_native_bmp_size([self.path])), 1)
        self.assertEqual(self.path.read_bytes()[6:], original[6:])

    def test_replace_failure_preserves_original_and_removes_private_candidate(self):
        original = bmp(declared=54 + 4 * 4 * 2)
        self.path.write_bytes(original)
        with patch('app_backend.converter_guard.native_media_repair.os.replace',
                   side_effect=PermissionError('simulated Windows lock')):
            with self.assertRaises(PermissionError):
                repair_native_bmp_size([self.path])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.root.glob('.*.part')), [])

    def test_top_down_complete_bmp_is_not_flipped_by_header_repair(self):
        original = bmp(height=-2, declared=54 + 4 * 4 * 2)
        self.path.write_bytes(original)
        [record] = repair_native_bmp_size([self.path])
        self.assertEqual(record['height'], -2)
        self.assertEqual(self.path.read_bytes()[6:], original[6:])

    def test_unknown_header_or_truncated_data_stays_rejected_and_unchanged(self):
        cases = [bmp(declared=500), bmp(declared=54 + 4 * 4 * 2)[:-1],
                 bmp(depth=32, declared=500), bmp(compression=3, declared=54 + 4 * 4 * 2)]
        for raw in cases:
            with self.subTest(header=raw[:54].hex(), size=len(raw)):
                self.path.write_bytes(raw)
                self.assertEqual(repair_native_bmp_size([self.path]), [])
                self.assertEqual(self.path.read_bytes(), raw)
                with self.assertRaises(GuardError):
                    inspect_media(self.path, validate_container=True)

    def test_bad_image_size_or_extra_trailing_bytes_are_not_repaired(self):
        original = bmp(declared=54 + 4 * 4 * 2)
        for raw in (original[:34] + (0).to_bytes(4, 'little') + original[38:], original + b'extra'):
            self.path.write_bytes(raw)
            self.assertEqual(repair_native_bmp_size([self.path]), [])
            self.assertEqual(self.path.read_bytes(), raw)

    def test_cancelled_repair_leaves_original_and_removes_private_partial(self):
        original = bmp(declared=54 + 4 * 4 * 2)
        self.path.write_bytes(original)
        class CancelAfterFirstBlock:
            calls = 0
            def is_set(self):
                self.calls += 1
                return self.calls >= 3
        with self.assertRaises(GuardError) as caught:
            repair_native_bmp_size([self.path], cancel=CancelAfterFirstBlock())
        self.assertEqual(caught.exception.code, 'CANCELLED')
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.root.glob('*.part')), [])
        self.assertEqual(list(self.root.glob('.*.part')), [])

    def test_source_symlink_is_never_rewritten(self):
        original = bmp(declared=54 + 4 * 4 * 2)
        self.path.write_bytes(original)
        linked = self.root / 'linked.bmp'
        try:
            linked.symlink_to(self.path)
        except (OSError, NotImplementedError):
            self.skipTest('Symlinks unavailable on this filesystem')
        self.assertEqual(repair_native_bmp_size([linked]), [])
        self.assertEqual(self.path.read_bytes(), original)


class MediaAdmission(unittest.TestCase):
    def test_document_png_filter_does_not_promise_an_apng_encoder(self):
        for services in (['com.sun.star.sheet.SpreadsheetDocument'],
                         ['com.sun.star.text.TextDocument'],
                         ['com.sun.star.presentation.PresentationDocument'],
                         ['com.sun.star.drawing.DrawingDocument'], []):
            with self.subTest(services=services):
                self.assertEqual(media_route_reason('apng', {'documentServices': services}),
                                 'APNG_ENCODER_UNAVAILABLE')
        self.assertIsNone(media_route_reason('apng', {'qtMediaTargets': ['apng']}))
        self.assertIsNone(media_route_reason('png', {}))
        for malformed in (None, 'apng', {'apng': True}):
            self.assertEqual(media_route_reason('apng', {'qtMediaTargets': malformed}),
                             'APNG_ENCODER_UNAVAILABLE')

    def test_jpeg_aliases_require_a_live_jpeg_encoder(self):
        from app_backend.converter_guard.qt_media import supported_media_targets
        class Reader:
            @staticmethod
            def supportedImageFormats(): return [b'png']
        class Writer:
            @staticmethod
            def supportedImageFormats(): return [b'png', b'jpeg']
        fake_qt = (None, None, None, None, None, Reader, Writer, None)
        with patch('app_backend.converter_guard.qt_media._qt', return_value=fake_qt):
            targets = supported_media_targets({'format': 'png'})
            self.assertTrue({'jpg', 'jpeg', 'jpe', 'jfif', 'jif'} <= set(targets))
        class NoJpeg:
            @staticmethod
            def supportedImageFormats(): return [b'png']
        with patch('app_backend.converter_guard.qt_media._qt',
                   return_value=(*fake_qt[:6], NoJpeg, None)):
            self.assertFalse({'jpg', 'jpeg', 'jpe', 'jfif', 'jif'} &
                             set(supported_media_targets({'format': 'png'})))

    @unittest.skipUnless(importlib.util.find_spec('PySide6'), 'Qt codec runtime required')
    def test_all_jpeg_aliases_use_real_jpeg_bytes_and_same_pixel_canvas(self):
        from PySide6.QtGui import QColor, QImage, QImageReader
        from app_backend.converter_guard.qt_media import convert_media_frames, media_preflight
        from app_backend.converter_guard.media_fidelity import check_media_preservation
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'original.png'
            image = QImage(37, 19, QImage.Format.Format_RGB32)
            image.fill(QColor(52, 102, 153))
            self.assertTrue(image.save(str(source), 'PNG'))
            original = source.read_bytes()
            targets = media_preflight(source)['targets']
            self.assertTrue({'jpe', 'jfif', 'jif'} <= set(targets))
            for alias in ('jpg', 'jpeg', 'jpe', 'jfif', 'jif'):
                with self.subTest(alias=alias):
                    [result] = convert_media_frames(source, root / alias, alias)
                    self.assertTrue(result.read_bytes().startswith(b'\xff\xd8'))
                    check = check_media_preservation([result], alias, source,
                                                     cancel=threading.Event())
                    self.assertEqual((check['result']['width'], check['result']['height']), (37, 19))
                    decoded = QImageReader(str(result)).read()
                    self.assertFalse(decoded.isNull())
                    self.assertEqual((decoded.width(), decoded.height()), (37, 19))
                    self.assertEqual(source.read_bytes(), original)


class OoxmlInstalledExporters(unittest.TestCase):
    WRITER = 'com.sun.star.text.TextDocument'
    CALC = 'com.sun.star.sheet.SpreadsheetDocument'

    def registry(self, target, names):
        from app_backend.converter_guard.routes import Registry
        native = Registry.__new__(Registry)
        native.valid = True
        native.uncertain_extensions = set()
        native.fingerprint = 'controlled-installed-registry'
        native.filters = {name: (self.WRITER, frozenset([target]), frozenset(['IMPORT', 'EXPORT']))
                          for name in names}
        native.primary_extensions = {name: target for name in names}
        native.export_candidates = {(target, self.WRITER): set(names)}
        native.preferred_filters = {}
        native.exports = {target: {self.WRITER}}
        native.import_services = {self.WRITER}
        return native

    def test_docm_and_dotx_choose_installed_standard_not_ambiguous_cli_guess(self):
        from app_backend.converter_guard.export_recipe import native_export_recipe
        for target, standard, alternative in (
                ('docm', 'MS Word 2007 XML VBA', 'Office Open XML Text VBA'),
                ('dotx', 'MS Word 2007 XML Template', 'Office Open XML Text Template')):
            with self.subTest(target=target):
                native = self.registry(target, [standard, alternative])
                proof = {'state': 'detected', 'basis': 'native_preflight_uno_document',
                         'modelLoaded': True, 'registryFingerprint': native.fingerprint,
                         'documentServices': [self.WRITER]}
                self.assertEqual(native.export_filter(target, [self.WRITER]), standard)
                recipe = native_export_recipe(native, target, proof)
                self.assertEqual(recipe.filter_name, standard)
                self.assertEqual(recipe.cli_spec(target), target + ':' + standard)
                self.assertIsNone(native.export_filter(target, [self.CALC]))
                self.assertIsNone(native_export_recipe(native, target,
                    {**proof, 'registryFingerprint': 'old-installation'}))
                native.uncertain_extensions.add(target)
                self.assertIsNone(native.export_filter(target, [self.WRITER]))

    def test_standard_filter_name_is_never_invented_when_absent(self):
        for target in ('docm', 'dotx'):
            with self.subTest(target=target):
                native = self.registry(target, ['custom first', 'custom second'])
                self.assertIsNone(native.export_filter(target, [self.WRITER]))
