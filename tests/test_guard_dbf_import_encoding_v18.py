"""Complete DBF character inspection respects explicit native codepage marks."""
from pathlib import Path
import struct
import tempfile
import threading
from types import SimpleNamespace
import unittest

from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard.legacy_import import CALC, legacy_import_hint


def dbf(rows, *, fields=(('NAME', 'C', 12), ('COUNT', 'N', 6)), codepage=0, version=3):
    header = 33 + len(fields) * 32
    width = 1 + sum(field[2] for field in fields)
    data = bytearray(header)
    data[0] = version; data[29] = codepage
    struct.pack_into('<IHH', data, 4, len(rows), header, width)
    for index, (name, kind, size) in enumerate(fields):
        offset = 32 + index * 32
        encoded = name.encode('utf-8')[:11]
        data[offset:offset+len(encoded)] = encoded
        data[offset+11] = ord(kind); data[offset+16] = size
    data[-1] = 13
    for row in rows:
        data += b' '
        for value, (_, kind, size) in zip(row, fields):
            encoded = value if isinstance(value, bytes) else str(value).encode('utf-8')
            if len(encoded) > size: raise ValueError('fixture value too long')
            data += encoded.ljust(size, b' ')
    return bytes(data) + b'\x1a'


class DbfImportEncoding(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.source = Path(self.tmp.name) / 'source.dbf'
        self.native = SimpleNamespace(filters={'dBase': (CALC, {'dbf'}, {'IMPORT', 'EXPORT'})})
        self.cancel = threading.Event()

    def hint(self, data, cancel=None):
        self.source.write_bytes(data)
        result = legacy_import_hint(self.source, self.native, cancel=cancel or self.cancel)
        self.assertEqual(self.source.read_bytes(), data)
        return result

    def test_unset_codepage_native_utf8_fields_get_explicit_charset_and_inference_warning(self):
        result = self.hint(dbf([['ALPHA', '1'], ['БЕТА', '2'], ['Я🙂', '3']]))
        self.assertEqual(result['filter'], 'dBase'); self.assertEqual(result['options'], '76')
        self.assertEqual(result['codepageMark'], 0)
        self.assertTrue(result['encodingInferred'])
        self.assertEqual(result['warnings'], ('DBF_UNDECLARED_UTF8_IMPORT',))

    def test_explicit_codepage_is_never_overridden_even_for_valid_utf8_bytes(self):
        for marker in (1, 3, 0x57, 0xC9):
            with self.subTest(marker=marker):
                self.assertIsNone(self.hint(dbf([['БЕТА', '2']], codepage=marker)))

    def test_ascii_without_codepage_has_identical_mapping_without_inference_warning(self):
        result = self.hint(dbf([['ALPHA', '1'], ['BETA', '2']]))
        self.assertFalse(result['encodingInferred'])
        self.assertEqual(result['warnings'], ())

    def test_all_records_beyond_one_mib_are_checked_with_bounded_record_memory(self):
        data = dbf([['ALPHA', '1']] * 60000 + [['БЕТА', '2']])
        self.assertGreater(len(data), 1024 * 1024)
        self.assertEqual(self.hint(data)['options'], '76')
        invalid = bytearray(data); invalid[-15] = 0xFF
        self.assertIsNone(self.hint(bytes(invalid)))

    def test_invalid_or_split_utf8_does_not_establish_charset(self):
        self.assertIsNone(self.hint(dbf([[b'\xff', '1']])))
        self.assertIsNone(self.hint(dbf([[b'\xc2', b'\xa0']], fields=(('A', 'C', 1), ('B', 'C', 1)))))

    def test_memo_or_binary_layouts_do_not_claim_to_check_unseen_data(self):
        for kind, version in [('M', 0x83), ('M', 3), ('I', 3), ('B', 0x30)]:
            with self.subTest(kind=kind, version=version):
                self.assertIsNone(self.hint(dbf([['1']], fields=(('VALUE', kind, 4),), version=version)))

    def test_bad_header_record_bounds_flags_and_trailing_data_get_no_hint(self):
        valid = dbf([['БЕТА', '2']])
        bad_width = bytearray(valid); struct.pack_into('<H', bad_width, 10, 1)
        bad_header = bytearray(valid); bad_header[96] = 0
        bad_count = bytearray(valid); struct.pack_into('<I', bad_count, 4, 99999999)
        bad_flag = bytearray(valid); bad_flag[97] = 0
        for data in (valid[:-3], valid + b'extra', bytes(bad_width), bytes(bad_header), bytes(bad_count), bytes(bad_flag)):
            with self.subTest(size=len(data)):
                self.assertIsNone(self.hint(data))

    def test_utf8_field_names_are_checked_as_well_as_values(self):
        self.assertTrue(self.hint(dbf([['ТЕКСТ']], fields=(('ИМЯ', 'C', 12),)))['encodingInferred'])
        data = bytearray(dbf([['ALPHA', '1']])); data[32] = 0xFF
        self.assertIsNone(self.hint(bytes(data)))

    def test_cancellation_during_full_record_scan_is_preserved(self):
        class DuringRead:
            calls = 0
            def is_set(self):
                self.calls += 1
                return self.calls >= 10
        with self.assertRaises(GuardError) as error:
            self.hint(dbf([['ALPHA', '1']] * 1000), cancel=DuringRead())
        self.assertEqual(error.exception.code, 'CANCELLED')


if __name__ == '__main__':
    unittest.main()
