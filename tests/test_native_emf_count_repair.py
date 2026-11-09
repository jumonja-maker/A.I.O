"""M28: exact DWORD-only native count repair and fail-closed output handling."""
import gzip
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch

from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard.native_metafile import (
    inspect_metafile, repair_native_emf_counts, wrap_native_metafiles)
from test_guard_native_metafile_v17 import emf


class EmfCountRepair(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'native.emf'
        self.raw = bytearray(emf(b'graphics records remain byte identical'))
        struct.pack_into('<I', self.raw, 52, 2)
        self.path.write_bytes(self.raw)

    def test_only_count_dword_changes_and_strict_structure_then_gzip_roundtrip(self):
        checks = repair_native_emf_counts([self.path])
        result = self.path.read_bytes()
        self.assertEqual(result[:52], self.raw[:52])
        self.assertEqual(result[56:], self.raw[56:])
        self.assertEqual(struct.unpack_from('<I', result, 52)[0], 3)
        self.assertEqual(checks[0]['changedByteRange'], [52, 56])
        self.assertEqual(inspect_metafile(self.path, 'emf', allow_native_count_gap=False)['records'], 3)
        paths, proof = wrap_native_metafiles([self.path], 'emz')
        self.assertEqual(gzip.decompress(paths[0].read_bytes()), result)
        self.assertEqual(proof['metafiles'][0]['declaredRecords'], 3)

    def test_correct_file_is_not_rewritten(self):
        self.path.write_bytes(emf(b'good'))
        before = self.path.stat()
        raw = self.path.read_bytes()
        self.assertEqual(repair_native_emf_counts([self.path]), [])
        after = self.path.stat()
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))
        self.assertEqual(self.path.read_bytes(), raw)

    def test_strict_inspection_rejects_raw_gap_without_mutating_it(self):
        with self.assertRaises(GuardError):
            inspect_metafile(self.path, 'emf', allow_native_count_gap=False)
        self.assertEqual(self.path.read_bytes(), self.raw)

    def test_bad_counts_are_not_guessed(self):
        for count in (0, 1, 4, 99):
            raw = bytearray(self.raw)
            struct.pack_into('<I', raw, 52, count)
            self.path.write_bytes(raw)
            with self.subTest(count=count), self.assertRaises(GuardError):
                repair_native_emf_counts([self.path])
            self.assertEqual(self.path.read_bytes(), raw)

    def test_duplicate_header_and_corrupt_eof_are_rejected_even_with_count_gap(self):
        for offset, value in ((108, 1), (len(self.raw)-4, 16), (len(self.raw)-12, 4)):
            raw = bytearray(self.raw)
            struct.pack_into('<I', raw, offset, value)
            self.path.write_bytes(raw)
            with self.subTest(offset=offset), self.assertRaises(GuardError):
                repair_native_emf_counts([self.path])
            self.assertEqual(self.path.read_bytes(), raw)

    def test_truncation_and_trailing_bytes_are_rejected(self):
        for raw in (self.raw[:-1], self.raw+b'\0\0\0\0'):
            self.path.write_bytes(raw)
            with self.assertRaises(GuardError):
                repair_native_emf_counts([self.path])
            self.assertEqual(self.path.read_bytes(), raw)

    def test_unconfirmed_header_variant_is_not_repaired(self):
        raw = bytearray(self.raw)
        struct.pack_into('<I', raw, 60, 1)
        self.path.write_bytes(raw)
        with self.assertRaises(GuardError) as error:
            repair_native_emf_counts([self.path])
        self.assertEqual(error.exception.diagnostic, 'native_metafile:emf_count_variant_unconfirmed')
        self.assertEqual(self.path.read_bytes(), raw)

    def test_cancel_before_or_during_candidate_keeps_original(self):
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(GuardError):
            repair_native_emf_counts([self.path], cancel=cancelled)
        inspect = inspect_metafile
        def cancel_after_walk(*args, **kwargs):
            facts = inspect(*args, **kwargs)
            cancelled.set()
            return facts
        cancelled.clear()
        with patch('app_backend.converter_guard.native_metafile.inspect_metafile', cancel_after_walk):
            with self.assertRaises(GuardError):
                repair_native_emf_counts([self.path], cancel=cancelled)
        self.assertEqual(self.path.read_bytes(), self.raw)
        self.assertEqual(list(self.path.parent.glob('*.partial')), [])

    def test_write_refusal_or_replace_refusal_keeps_original_and_no_candidate(self):
        for boundary in ('tempfile.NamedTemporaryFile', 'os.replace'):
            with self.subTest(boundary=boundary):
                with patch('app_backend.converter_guard.native_metafile.'+boundary, side_effect=PermissionError('blocked')):
                    with self.assertRaises(PermissionError):
                        repair_native_emf_counts([self.path])
                self.assertEqual(self.path.read_bytes(), self.raw)
                self.assertEqual(list(self.path.parent.glob('*.partial')), [])

    def test_source_mutation_after_walk_is_rejected(self):
        inspect = inspect_metafile
        def mutate(*args, **kwargs):
            result = inspect(*args, **kwargs)
            raw = bytearray(self.raw)
            raw[120] ^= 1
            self.path.write_bytes(raw)
            return result
        with patch('app_backend.converter_guard.native_metafile.inspect_metafile', mutate):
            with self.assertRaises(GuardError):
                repair_native_emf_counts([self.path])
        self.assertEqual(list(self.path.parent.glob('*.partial')), [])


if __name__ == '__main__':
    unittest.main()
