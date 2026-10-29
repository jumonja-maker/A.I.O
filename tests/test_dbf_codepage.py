"""Fast M25 regression: codepage declaration without DBF rewrite or rescanning."""
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest

from app_backend.converter_guard.dbf_codepage import attach_utf8_codepages
from app_backend.converter_guard.errors import GuardError


def sample_dbf(path, *, ldid=0, valid=True):
    header = bytearray(32)
    header[0] = 3 if valid else 0
    header[8:10] = (33).to_bytes(2, 'little')
    header[10:12] = (1).to_bytes(2, 'little')
    header[29] = ldid
    path.write_bytes(header + b'\r')
    return path


class DbFCodepageTests(unittest.TestCase):
    def test_one_header_read_per_output_and_preserve_bytes(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            originals = [sample_dbf(root / ('book__sheet_%03d.dbf' % i))
                         for i in (1, 2, 3)]
            before = [p.read_bytes() for p in originals]
            result = attach_utf8_codepages(originals, filter_name='dBase')
            self.assertEqual(len(result), 3)
            for source, saved in zip(originals, before):
                self.assertEqual(source.read_bytes(), saved)
                self.assertEqual(source.with_suffix('.cpg').read_bytes(), b'UTF-8\n')
                self.assertIn(source.with_suffix('.cpg'), result)

    def test_unchanged_format_is_noop(self):
        with TemporaryDirectory() as temp:
            dbf = sample_dbf(Path(temp) / 'sample.dbf')
            self.assertEqual(attach_utf8_codepages([dbf], filter_name='Calc XML'), [])
            self.assertFalse(dbf.with_suffix('.cpg').exists())

    def test_declared_native_codepage_is_not_overwritten(self):
        with TemporaryDirectory() as temp:
            dbf = sample_dbf(Path(temp) / 'sample.dbf', ldid=0x57)
            self.assertEqual(attach_utf8_codepages([dbf], filter_name='dBase'), [])
            self.assertFalse(dbf.with_suffix('.cpg').exists())

    def test_existing_matching_label_is_reused(self):
        with TemporaryDirectory() as temp:
            dbf = sample_dbf(Path(temp) / 'sample.dbf')
            cpg = dbf.with_suffix('.cpg')
            cpg.write_text('UTF-8\n', encoding='ascii')
            self.assertEqual(attach_utf8_codepages([dbf], filter_name='dBase'), [cpg])

    def test_conflicting_label_fails_closed(self):
        with TemporaryDirectory() as temp:
            dbf = sample_dbf(Path(temp) / 'sample.dbf')
            dbf.with_suffix('.cpg').write_text('1251\n', encoding='ascii')
            with self.assertRaises(GuardError):
                attach_utf8_codepages([dbf], filter_name='dBase')

    def test_invalid_dbf_is_not_declared_utf8(self):
        with TemporaryDirectory() as temp:
            dbf = sample_dbf(Path(temp) / 'sample.dbf', valid=False)
            with self.assertRaises(GuardError):
                attach_utf8_codepages([dbf], filter_name='dBase')
            self.assertFalse(dbf.with_suffix('.cpg').exists())

    def test_cancellation_creates_no_sidecars(self):
        with TemporaryDirectory() as temp:
            dbf = sample_dbf(Path(temp) / 'sample.dbf')
            flag = threading.Event()
            flag.set()
            with self.assertRaises(GuardError):
                attach_utf8_codepages([dbf], filter_name='dBase', cancel=flag)
            self.assertFalse(dbf.with_suffix('.cpg').exists())


if __name__ == '__main__':
    unittest.main()
