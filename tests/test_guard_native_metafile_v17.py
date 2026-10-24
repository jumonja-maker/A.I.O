"""Native record integrity and compression regressions for EMZ/WMZ."""
import gzip
from pathlib import Path
import struct
import tempfile
import threading
import unittest

from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard.native_metafile import (
    inspect_metafile, native_metafile_target, wrap_native_metafiles)


def emf(payload=b''):
    payload += b'\0' * (-len(payload) % 4)
    records = ([struct.pack('<II', 70, len(payload) + 8) + payload] if payload else [])
    records += [struct.pack('<IIIII', 14, 20, 0, 0, 20)]
    header = bytearray(108)
    struct.pack_into('<II', header, 0, 1, 108)
    header[40:44] = b' EMF'
    struct.pack_into('<III', header, 44, 0x10000, 108 + sum(map(len, records)), len(records) + 1)
    return bytes(header) + b''.join(records)


def wmf(payload=b'', placeable=False):
    payload += b'\0' * (-len(payload) % 2)
    records = ([struct.pack('<IH', (len(payload) + 6) // 2, 0x0626) + payload] if payload else [])
    records += [struct.pack('<IH', 3, 0)]
    maximum = max(len(record) // 2 for record in records)
    body = struct.pack('<HHHIHIH', 1, 9, 0x300,
                       (18 + sum(map(len, records))) // 2, 0, maximum, 0) + b''.join(records)
    if placeable:
        header = struct.pack('<IHHHHHHIH', 0x9ac6cdd7, 0, 0, 0, 100, 100, 1440, 0, 0)
        checksum = 0
        for word in struct.unpack('<10H', header[:20]):
            checksum ^= word
        body = header[:20] + struct.pack('<H', checksum) + body
    return body


class NativeMetafile(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cancel = threading.Event()

    def file(self, content, kind='emf'):
        path = self.root / ('native.' + kind)
        path.write_bytes(content)
        return path

    def test_large_native_exports_roundtrip_exact_bytes_and_remove_only_intermediate(self):
        # Cross the compressor's former 32/64 KiB failing boundaries repeatedly.
        payload = bytes(range(256)) * 17000
        for target, kind, raw in (('emz', 'emf', emf(payload)),
                                  ('wmz', 'wmf', wmf(payload, placeable=True))):
            with self.subTest(target=target):
                original = self.file(raw, kind)
                sidecar = self.root / ('keep-' + kind + '.txt')
                sidecar.write_text('kept')
                paths, result = wrap_native_metafiles([original, sidecar], target, cancel=self.cancel)
                compressed = self.root / ('native.' + target)
                self.assertEqual(gzip.decompress(compressed.read_bytes()), raw)
                self.assertFalse(original.exists())
                self.assertEqual(paths, [compressed, sidecar])
                self.assertEqual(result['metafiles'][0]['bytes'], len(raw))
                self.assertEqual(result['status'], 'byte_preserved')
                self.assertEqual(sidecar.read_text(), 'kept')

    def test_plain_record_validator_rejects_truncation_even_with_repaired_gzip(self):
        for kind, raw in (('emf', emf(b'frame' * 10000)), ('wmf', wmf(b'frame' * 10000))):
            for broken in (raw[:-2], raw + b'\0\0', raw[:110]):
                with self.subTest(kind=kind, size=len(broken)):
                    path = self.file(broken, kind)
                    with self.assertRaises(GuardError) as caught:
                        wrap_native_metafiles([path], 'emz' if kind == 'emf' else 'wmz')
                    self.assertEqual(caught.exception.code, 'ENGINE_OUTPUT_CONTAINER_INVALID')
                    self.assertEqual(path.read_bytes(), broken)

    def test_emf_requires_record_count_boundaries_and_terminal_eof(self):
        original = emf(b'ABCD')
        for offset, value in ((52, 99), (108 + 4, 0), (120, 1), (4, 88)):
            broken = bytearray(original)
            struct.pack_into('<I', broken, offset, value)
            with self.subTest(offset=offset):
                with self.assertRaises(GuardError):
                    inspect_metafile(self.file(broken), 'emf')

    def test_wmf_requires_placeable_checksum_header_eof_and_max_record(self):
        original = wmf(b'ABCD', placeable=True)
        for offset, value in ((20, 1), (22 + 2, 8), (22 + 12, 1), (len(original) - 2, 1)):
            broken = bytearray(original)
            struct.pack_into('<H', broken, offset, value)
            with self.subTest(offset=offset):
                with self.assertRaises(GuardError):
                    inspect_metafile(self.file(broken, 'wmf'), 'wmf')

    def test_nonplaceable_wmf_is_valid(self):
        self.assertEqual(inspect_metafile(self.file(wmf(), 'wmf'), 'wmf')['records'], 1)

    def test_native_lo_header_excluded_record_count_retains_complete_bytes(self):
        original = bytearray(emf(b'ABCD'))
        struct.pack_into('<I', original, 52, 2)
        facts = inspect_metafile(self.file(original), 'emf')
        self.assertEqual(facts['records'], 3)
        self.assertTrue(facts['nativeHeaderCountExcludesHeader'])

    def test_unvalidated_or_missing_plain_route_does_not_get_selected(self):
        class Native:
            valid = True
            def observed_decision(self, target, proof):
                return 'registered' if target in proof.get('targets', []) else 'unavailable'
        native = Native()
        proof = {'targets': ['emz', 'emf']}
        self.assertEqual(native_metafile_target(native, 'emz', proof), 'emf')
        self.assertIsNone(native_metafile_target(native, 'emz', {'targets': ['emz']}))
        self.assertIsNone(native_metafile_target(native, 'emz', None))
        self.assertIsNone(native_metafile_target(native, 'pdf', proof))
        native.valid = False
        self.assertIsNone(native_metafile_target(native, 'emz', proof))

    def test_cancellation_keeps_native_plain_output_and_cleans_partial_container(self):
        class Cancel:
            checks = 0
            def is_set(self):
                self.checks += 1
                return self.checks > 5
        source = self.file(emf(b'payload' * 100000))
        with self.assertRaises(GuardError) as caught:
            wrap_native_metafiles([source], 'emz', cancel=Cancel())
        self.assertEqual(caught.exception.code, 'CANCELLED')
        self.assertTrue(source.exists())
        self.assertFalse(list(self.root.glob('*.partial')))

    def test_existing_output_is_not_overwritten(self):
        source = self.file(emf())
        output = source.with_suffix('.emz')
        output.write_bytes(b'existing')
        with self.assertRaises(GuardError):
            wrap_native_metafiles([source], 'emz')
        self.assertEqual(output.read_bytes(), b'existing')
        self.assertTrue(source.exists())

    def test_same_size_change_during_compression_is_rejected(self):
        source = self.file(emf(b'payload' * 100000))
        class MutatingControl:
            checks = 0
            def is_set(self):
                self.checks += 1
                # Three native record polls precede the compression loop.
                if self.checks == 4:
                    with source.open('r+b') as changed:
                        changed.seek(128)
                        changed.write(b'CHANGED')
                return False
        with self.assertRaises(GuardError) as caught:
            wrap_native_metafiles([source], 'emz', cancel=MutatingControl())
        self.assertEqual(caught.exception.diagnostic, 'native_metafile:source_changed')
        self.assertFalse(source.with_suffix('.emz').exists())
        self.assertFalse(list(self.root.glob('*.partial')))


if __name__ == '__main__':
    unittest.main()
