"""Native raster aliases must preserve actual containers and pixel payloads."""
from pathlib import Path
import struct
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zlib

from app_backend.converter_guard.adapter import LibreOfficeAdapter
from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard.media_fidelity import inspect_media
from app_backend.converter_guard.native_alias import native_alias_target, wrap_native_aliases


def chunk(kind, data):
    return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind+data) & 0xffffffff)


def png():
    raw = b'\0' + bytes((255, 0, 0, 255, 0, 255, 0, 128, 0, 0, 255, 0))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 3, 1, 8, 6, 0, 0, 0)) +
            chunk(b'tEXt', b'Description\0native pixel payload') + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))


def chunks(data):
    offset = 8
    result = []
    while offset < len(data):
        length, kind = struct.unpack_from('>I4s', data, offset)
        payload = data[offset+8:offset+8+length]
        crc = struct.unpack_from('>I', data, offset+8+length)[0]
        if crc != zlib.crc32(kind+payload) & 0xffffffff:
            raise AssertionError('CRC mismatch')
        result.append((kind, payload))
        offset += length+12
    return result


class NativeAliases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cancel = threading.Event()

    def test_apng_has_real_control_chunks_and_identical_compressed_pixels(self):
        path = self.root/'native.png'; original = png();path.write_bytes(original)
        paths, check = wrap_native_aliases([path], 'apng', cancel=self.cancel)
        output = paths[0]
        before, after = chunks(original), chunks(output.read_bytes())
        self.assertEqual([data for kind, data in after if kind == b'acTL'], [struct.pack('>II', 1, 0)])
        self.assertEqual(len([data for kind, data in after if kind == b'fcTL']), 1)
        self.assertEqual([(kind, data) for kind, data in after if kind not in (b'acTL', b'fcTL')], before)
        self.assertFalse(path.exists())
        self.assertEqual(inspect_media(output)['frames'], 1)
        self.assertTrue(check['pixelPayloadPreserved'])
        from PySide6.QtGui import QImage
        image = QImage(str(output)).convertToFormat(QImage.Format.Format_RGBA8888)
        self.assertEqual((image.width(), image.height()), (3, 1))
        self.assertEqual(bytes(image.constBits()), bytes((255, 0, 0, 255, 0, 255, 0, 128, 0, 0, 255, 0)))

    def test_jpeg_aliases_do_not_reencode_or_change_bytes(self):
        from PySide6.QtGui import QImage
        image = QImage(3, 2, QImage.Format.Format_RGB32);image.fill(0xff4488cc)
        for target in ('jfif', 'jif', 'jpe'):
            path = self.root/('native_'+target+'.jpg')
            self.assertTrue(image.save(str(path), 'JPEG'))
            before = path.read_bytes()
            paths, _ = wrap_native_aliases([path], target, cancel=self.cancel)
            self.assertEqual(paths[0].read_bytes(), before)
            self.assertEqual(inspect_media(paths[0])['format'], 'jpeg')

    def test_corrupt_or_cancelled_native_pixels_are_not_renamed_to_success(self):
        path = self.root/'native.png';path.write_bytes(png()[:-8])
        with self.assertRaises(GuardError):
            wrap_native_aliases([path], 'apng', cancel=self.cancel)
        self.assertTrue(path.exists())
        self.assertFalse(path.with_suffix('.apng').exists())
        path.write_bytes(png());self.cancel.set()
        with self.assertRaises(GuardError) as error:
            wrap_native_aliases([path], 'apng', cancel=self.cancel)
        self.assertEqual(error.exception.code, 'CANCELLED')
        self.assertTrue(path.exists())

    def test_alias_requires_both_actual_native_routes(self):
        proof = {'state': 'detected'}
        native = SimpleNamespace(valid=True, observed_decision=lambda target, p:
                                 'registered' if target in ('apng', 'png') else 'unavailable')
        self.assertEqual(native_alias_target(native, 'apng', proof), 'png')
        self.assertIsNone(native_alias_target(native, 'jfif', proof))
        native.observed_decision = lambda target, p: 'registered' if target == 'apng' else 'unavailable'
        self.assertIsNone(native_alias_target(native, 'apng', proof))

    def test_adapter_requests_native_png_then_delivers_real_apng(self):
        source = self.root/'page.html';source.write_text('<html><body>source text</body></html>')
        adapter = LibreOfficeAdapter(self.root/'soffice')
        adapter._local.proof = {'state': 'detected', 'documentServices': ['com.sun.star.text.WebDocument']}
        native = SimpleNamespace(valid=True, fingerprint='installed-native', observed_decision=lambda *args: 'registered')
        requested = []
        def convert(path, target, workspace, **kwargs):
            requested.append(target)
            output = workspace/'output';output.mkdir(parents=True)
            result = output/'page.png';result.write_bytes(png())
            adapter._local.export_info = {'filter': 'writer_png_Export'}
            adapter._local.execution_mode = 'native_cli'
            return [result]
        with patch.object(adapter, '_registry', return_value=native), patch.object(adapter, '_convert_native', side_effect=convert):
            paths = adapter.convert(source, 'apng', self.root/'work', cancel=self.cancel,
                                    timeout=10, on_started=lambda: None)
        self.assertEqual(requested, ['png'])
        self.assertEqual(paths[0].suffix, '.apng')
        self.assertIn(b'acTL', paths[0].read_bytes())
        self.assertEqual(adapter.export_info()['filter'], 'writer_png_Export')
        self.assertEqual(adapter.export_info()['resultChecks'][0]['requestedFormat'], 'apng')


if __name__ == '__main__':
    unittest.main()
