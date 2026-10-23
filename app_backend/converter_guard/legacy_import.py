"""Small content-confirmed native hints; no extension-based admission."""
from pathlib import Path
import struct

CALC = 'com.sun.star.sheet.SpreadsheetDocument'


def legacy_import_hint(source, native):
    with Path(source).open('rb') as stream:
        head = stream.read(64)
        is_dif = head.startswith(b'TABLE\r') or head.startswith(b'TABLE\n')
        is_dbf = len(head) >= 32 and head[0] in (3, 4, 0x30, 0x31, 0x83, 0x8b, 0xf5)
        if not is_dif and not is_dbf:
            return None
        stream.seek(0)
        data = stream.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        return None
    name = None
    if is_dif:
        try:
            text = data.decode('utf-8')
        except UnicodeError:
            return None
        if all(token in text.splitlines() for token in ('TABLE', 'VECTORS', 'TUPLES', 'DATA')):
            name = 'DIF'
    elif is_dbf:
        records, header, length = struct.unpack_from('<IHH', data, 4)
        # UTF-8 is safe for ASCII names/values only. Never guess an unknown
        # legacy codepage from a handful of records.
        if (header >= 33 and (header - 33) % 32 == 0 and length > 0
                and header <= len(data) and data[header-1] == 13
                and header + records * length <= len(data)
                and all(all(c < 128 for c in data[p:p+11]) for p in range(32, header-1, 32))
                and all(c < 128 for c in data[header:header + records * length])):
            name = 'dBase'
    item = native.filters.get(name)
    return ({'filter': name, 'options': '76', 'basis': 'content_confirmed_legacy'}
            if item and item[0] == CALC and 'IMPORT' in item[2] else None)


def validate_options(name, options, native):
    item = native.filters.get(name)
    if not item or item[0] != CALC or 'IMPORT' not in item[2]:
        return False
    if name in ('DIF', 'dBase'):
        return options == '76'
    return ('csv' in item[1] and options in {f'{n},34,76,1' for n in (44, 59, 9, 124)})
