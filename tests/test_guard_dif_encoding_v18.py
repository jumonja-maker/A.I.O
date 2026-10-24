"""DIF metadata declares UTF-8 while retaining every native output byte."""
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from app_backend.converter_guard.dif_encoding import describe_dif_encoding
from app_backend.converter_guard.errors import GuardError


def dif(text='Позиция 1 — Я🙂', *, newline='\n'):
    return newline.join(('TABLE', '0,1', '"Data"', 'VECTORS', '0,1', '""',
        'TUPLES', '0,1', '""', 'DATA', '0,0', '""', '-1,0', 'BOT', '1,0',
        '"'+text+'"', '-1,0', 'EOD', '')).encode('utf-8')


class DifEncoding(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cancel = threading.Event()

    def test_one_companion_declares_all_sheet_files_and_exact_hashes(self):
        first, second, other = self.root/'book.dif', self.root/'book__sheet_002.DIF', self.root/'keep.txt'
        first.write_bytes(dif());second.write_bytes(dif('Часть 2', newline='\r\n'));other.write_bytes(b'keep')
        before = {p: p.read_bytes() for p in (first, second, other)}
        paths, check = describe_dif_encoding([first, second, other], cancel=self.cancel)
        self.assertEqual(paths[:3], [first, second, other])
        self.assertEqual(len(paths), 4)
        metadata = json.loads(paths[-1].read_text(encoding='utf-8'))
        self.assertEqual(metadata['encoding'], 'UTF-8')
        self.assertFalse(metadata['bom'])
        self.assertFalse(metadata['qualityCompared'])
        self.assertFalse(metadata['dataBytesChanged'])
        self.assertEqual(metadata['libreOfficeImport'], {'FilterName': 'DIF', 'FilterOptions': '76'})
        self.assertEqual(metadata['files'], [
            {'name': p.name, 'bytes': len(before[p]), 'sha256': hashlib.sha256(before[p]).hexdigest()}
            for p in (first, second)])
        self.assertIn('UTF-8', metadata['instructions']['ru'])
        self.assertNotIn(str(self.root), paths[-1].read_text(encoding='utf-8'))
        self.assertEqual(check['status'], 'encoding_declared')
        self.assertEqual(check['files'], 2)
        self.assertFalse(check['qualityCompared'])
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)
            if path.suffix.lower() == '.dif':
                self.assertFalse(content.startswith(b'\xef\xbb\xbf'))
        self.assertFalse(list(self.root.glob('*.part')))

    def test_no_dif_does_not_create_metadata(self):
        path = self.root/'file.txt';path.write_bytes(b'unrelated')
        self.assertEqual(describe_dif_encoding([path], cancel=self.cancel), ([path], None))
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_malformed_header_bom_and_invalid_utf8_never_get_sidecars(self):
        invalid = (b'not a DIF file', b'\xef\xbb\xbf'+dif(), dif()+b'\xff', dif()+b'\xf0\x9f')
        for number, content in enumerate(invalid):
            with self.subTest(number=number):
                path = self.root/('bad'+str(number)+'.dif');path.write_bytes(content)
                with self.assertRaises(GuardError) as error:
                    describe_dif_encoding([path], cancel=self.cancel)
                self.assertEqual(error.exception.code, 'DIF_ENCODING_INVALID')
                self.assertEqual(path.read_bytes(), content)
        self.assertFalse(list(self.root.glob('*.aio-encoding.json*')))

    def test_validation_reaches_invalid_utf8_after_many_blocks(self):
        path = self.root/'large.dif'
        content = dif('a'*(4*65536)) + b'\xff'
        path.write_bytes(content)
        with self.assertRaises(GuardError) as error:
            describe_dif_encoding([path], cancel=self.cancel)
        self.assertEqual(error.exception.code, 'DIF_ENCODING_INVALID')
        self.assertEqual(path.read_bytes(), content)
        self.assertFalse(list(self.root.glob('*.aio-encoding.json*')))

    def test_cancelled_stream_leaves_all_native_bytes_and_no_metadata(self):
        path = self.root/'large.dif';content = dif('Я🙂'*(4*65536));path.write_bytes(content)
        class CancelDuringRead:
            calls = 0
            def is_set(self):
                self.calls += 1
                return self.calls >= 5
        with self.assertRaises(GuardError) as error:
            describe_dif_encoding([path], cancel=CancelDuringRead())
        self.assertEqual(error.exception.code, 'CANCELLED')
        self.assertEqual(path.read_bytes(), content)
        self.assertFalse(list(self.root.glob('*.aio-encoding.json*')))

    def test_second_bad_sheet_prevents_metadata_for_the_whole_task(self):
        first, second = self.root/'first.dif', self.root/'second.dif'
        first.write_bytes(dif());second.write_bytes(dif()+b'\xff')
        with self.assertRaises(GuardError):
            describe_dif_encoding([first, second], cancel=self.cancel)
        self.assertFalse(list(self.root.glob('*.aio-encoding.json*')))
        self.assertEqual(first.read_bytes(), dif())

    def test_existing_metadata_or_partial_is_never_overwritten(self):
        path = self.root/'book.dif';path.write_bytes(dif())
        sidecar = self.root/'book.aio-encoding.json';sidecar.write_bytes(b'prior work')
        with self.assertRaises(GuardError) as error:
            describe_dif_encoding([path], cancel=self.cancel)
        self.assertEqual(error.exception.code, 'ENGINE_OUTPUT_COLLISION')
        self.assertEqual(sidecar.read_bytes(), b'prior work')
        sidecar.unlink()
        partial = self.root/'book.aio-encoding.json.part';partial.write_bytes(b'prior partial')
        with self.assertRaises(FileExistsError):
            describe_dif_encoding([path], cancel=self.cancel)
        self.assertEqual(partial.read_bytes(), b'prior partial')
        self.assertFalse(sidecar.exists())
        self.assertEqual(path.read_bytes(), dif())

    def test_atomic_publish_cannot_replace_a_concurrent_file(self):
        path = self.root/'book.dif';path.write_bytes(dif())
        sidecar = self.root/'book.aio-encoding.json'
        import os
        real_link = os.link
        def racing_link(source, destination):
            Path(destination).write_bytes(b'concurrent work')
            return real_link(source, destination)
        with patch('app_backend.converter_guard.dif_encoding.os.link', side_effect=racing_link), \
                self.assertRaises(FileExistsError):
            describe_dif_encoding([path], cancel=self.cancel)
        self.assertEqual(sidecar.read_bytes(), b'concurrent work')
        self.assertFalse(list(self.root.glob('*.part')))
        self.assertEqual(path.read_bytes(), dif())

    def test_files_outside_one_result_folder_are_rejected(self):
        first = self.root/'one.dif';first.write_bytes(dif())
        folder = self.root/'elsewhere';folder.mkdir()
        second = folder/'two.dif';second.write_bytes(dif())
        with self.assertRaises(GuardError) as error:
            describe_dif_encoding([first, second], cancel=self.cancel)
        self.assertEqual(error.exception.code, 'ENGINE_OUTPUT_PATH')
        self.assertFalse(list(self.root.rglob('*.aio-encoding.json*')))

    def test_symlink_dif_is_not_hashed_as_owned_output(self):
        folder = self.root/'target';folder.mkdir()
        target = folder/'original.dif';target.write_bytes(dif())
        link = self.root/'link.dif'
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest('Creating a test symlink is unavailable on this platform')
        with self.assertRaises(GuardError) as error:
            describe_dif_encoding([link], cancel=self.cancel)
        self.assertEqual(error.exception.code, 'ENGINE_OUTPUT_PATH')
        self.assertEqual(target.read_bytes(), dif())


if __name__ == '__main__':
    unittest.main()
