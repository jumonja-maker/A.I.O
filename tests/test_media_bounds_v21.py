"""A TIFF page header is not proof that its referenced pixels exist."""
from pathlib import Path
import struct
import tempfile
import threading
import unittest

from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard.media_fidelity import inspect_media, check_media_preservation
from test_guard_media_fidelity_v16 import tiff


def indexed_tiff(*, big=False, endian='<', tiled=False, sparse=False, blocks=2):
    """Independent gray TIFF with indexed strips or 16x16 tiles."""
    offset_code, size, entry_size = ('Q', 8, 20) if big else ('I', 4, 12)
    count_code, header_size = ('Q', 16) if big else ('H', 8)
    offset_tag, count_tag = (324, 325) if tiled else (273, 279)
    width, height, block_bytes = (16, 16 * blocks, 256) if tiled else (2, blocks, 2)
    tags = [(256, 4, 1, width), (257, 4, 1, height), (258, 3, 1, 8),
            (259, 3, 1, 1), (262, 3, 1, 1),
            (offset_tag, 16 if big else 4, blocks, None),
            (count_tag, 16 if big else 4, blocks, None)]
    if tiled:
        tags += [(322, 4, 1, 16), (323, 4, 1, 16)]
    else:
        tags += [(278, 4, 1, 1)]
    tags.sort()
    array_start = header_size + struct.calcsize(count_code) + len(tags) * entry_size + size
    array_start = (array_start + size - 1) // size * size
    pixel_start = array_start + 2 * blocks * size
    header = (b'II' if endian == '<' else b'MM') + struct.pack(endian+'H', 43 if big else 42)
    if big: header += struct.pack(endian+'HH', 8, 0)
    output = header + struct.pack(endian+offset_code, header_size)
    output += struct.pack(endian+count_code, len(tags))
    for tag, typ, count, value in tags:
        if tag == offset_tag: value = array_start
        if tag == count_tag: value = array_start + blocks * size
        payload = struct.pack(endian+('H' if typ == 3 else 'I' if count == 1 else offset_code), value)
        output += struct.pack(endian+'HH'+('Q' if big else 'I'), tag, typ, count) + payload.ljust(size, b'\0')
    output += struct.pack(endian+offset_code, 0)
    output = output.ljust(array_start, b'\0')
    offsets = [pixel_start + block_bytes * i for i in range(blocks)]
    lengths = [block_bytes] * blocks
    if sparse: offsets[0] = lengths[0] = 0
    output += struct.pack(endian+offset_code*blocks, *offsets)
    output += struct.pack(endian+offset_code*blocks, *lengths)
    return output + b'\x7f\x40' * (block_bytes * blocks // 2)


class TiffDataBounds(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, name, payload):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(payload)
        return p

    def test_truncated_second_page_is_not_a_valid_output(self):
        source = self.write('source.tiff', tiff(pages=2))
        result = self.write('out/source.tiff', source.read_bytes()[:-12])
        # Metadata admission remains distinct from strict output validation.
        self.assertEqual(inspect_media(result)['pages'], 2)
        with self.assertRaises(GuardError) as caught:
            check_media_preservation([result], 'tiff', source, cancel=threading.Event())
        self.assertEqual(caught.exception.code, 'MEDIA_OUTPUT_INVALID')


    def test_valid_multipage_tiff_is_still_accepted(self):
        p = self.write('good.tiff', tiff(pages=3))
        self.assertEqual(inspect_media(p, validate_container=True)['pages'], 3)

    def test_out_of_file_strip_offset_is_rejected(self):
        data = bytearray(tiff())
        for index in range(struct.unpack_from('<H', data, 8)[0]):
            at = 10 + index * 12
            if struct.unpack_from('<H', data, at)[0] == 273:
                struct.pack_into('<I', data, at + 8, len(data) + 1)
        with self.assertRaises(GuardError):
            inspect_media(self.write('outside.tiff', data), validate_container=True)


    def test_classic_and_bigtiff_strip_and_tile_arrays_all_endiannesses(self):
        for big in (False, True):
            for endian in ('<', '>'):
                for tiled in (False, True):
                    with self.subTest(big=big, endian=endian, tiled=tiled):
                        data = indexed_tiff(big=big, endian=endian, tiled=tiled)
                        path = self.write('indexed.tiff', data)
                        self.assertEqual(inspect_media(path, validate_container=True)['width'], 16 if tiled else 2)
                        path.write_bytes(data[:-1])
                        with self.assertRaises(GuardError) as caught:
                            inspect_media(path, validate_container=True)
                        self.assertEqual(caught.exception.code, 'MEDIA_OUTPUT_INVALID')

    def test_sparse_zero_extents_are_not_inferred_to_be_conversion_loss(self):
        for big in (False, True):
            with self.subTest(big=big):
                path = self.write('sparse.tiff', indexed_tiff(big=big, sparse=True))
                self.assertEqual(inspect_media(path, validate_container=True)['height'], 2)

    def test_optional_old_jpeg_zero_pointer_does_not_require_length(self):
        data = bytearray(tiff(alpha=False))
        # Replace optional PlanarConfiguration with an absent JPEG stream.
        # Baseline StripOffsets/StripByteCounts remain valid and complete.
        for index in range(struct.unpack_from('<H', data, 8)[0]):
            at = 10 + index * 12
            if struct.unpack_from('<H', data, at)[0] == 284:
                struct.pack_into('<HHII', data, at, 513, 4, 1, 0)
        path = self.write('old-jpeg-optional.tiff', data)
        self.assertEqual(inspect_media(path, validate_container=True)['pages'], 1)

    def test_large_valid_strip_index_streams_without_a_new_65536_strip_cap(self):
        path = self.write('many-strips.tiff', indexed_tiff(blocks=70000))
        self.assertEqual(inspect_media(path, validate_container=True)['height'], 70000)
        data = path.read_bytes()
        path.write_bytes(data[:-1])
        with self.assertRaises(GuardError) as caught:
            inspect_media(path, validate_container=True)
        self.assertEqual(caught.exception.code, 'MEDIA_OUTPUT_INVALID')

    def test_cancellation_closes_input_immediately(self):
        from unittest.mock import patch
        from app_backend.converter_guard import media_fidelity
        opened = []
        original = media_fidelity._Input.__init__
        def track(obj, *args):
            original(obj, *args)
            opened.append(obj.file)
        cancel = threading.Event(); cancel.set()
        with patch.object(media_fidelity._Input, '__init__', track):
            with self.assertRaises(GuardError) as caught:
                inspect_media(self.write('cancelled.tiff', tiff()), validate_container=True, cancel=cancel)
            self.assertEqual(caught.exception.code, 'CANCELLED')
            self.assertTrue(opened[0].closed)


if __name__ == '__main__':
    unittest.main()
