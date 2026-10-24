"""Small content-confirmed native hints; no extension-based admission."""
from pathlib import Path
import codecs
import os
import re
import struct

from .errors import GuardError

CALC = 'com.sun.star.sheet.SpreadsheetDocument'


def _dif_utf8(stream, cancel):
    """Full UTF-8 confirmation with a tiny token buffer, not a size veto."""
    decoder = codecs.getincrementaldecoder('utf-8-sig')('strict')
    wanted, seen = {b'TABLE', b'VECTORS', b'TUPLES', b'DATA'}, set()
    tail, long_line, first = b'', False, True
    try:
        stream.seek(0)
        while block := stream.read(65536):
            if cancel is not None and cancel.is_set():
                raise GuardError('CANCELLED', 'Проверка кодировки DIF отменена.', stage='preflight')
            decoder.decode(block, final=False)
            if first and block.startswith(b'\xef\xbb\xbf'):
                block = block[3:]
            first = False
            parts = re.split(b'[\r\n]', block)
            for index, part in enumerate(parts):
                if not long_line:
                    if len(tail) + len(part) <= 16:
                        tail += part
                    else:
                        long_line = True
                if index != len(parts)-1:
                    if not long_line and tail in wanted:
                        seen.add(tail)
                    tail, long_line = b'', False
        decoder.decode(b'', final=True)
        if not long_line and tail in wanted:
            seen.add(tail)
    except UnicodeError:
        return False
    return wanted <= seen


def _dbf_utf8(stream, cancel):
    """Inspect every classic DBF character field when its codepage is unset.

    UTF-8 validity is an inference for non-ASCII content, not a declaration of
    the author's intent. Explicit codepage marks and unsupported binary/Memo
    field layouts retain the native importer's selection. Memo pointers alone
    cannot establish the encoding of a separate, possibly absent DBT file.
    """
    def check_cancel():
        if cancel is not None and cancel.is_set():
            raise GuardError('CANCELLED', 'Проверка кодировки DBF отменена.', stage='preflight')
    check_cancel()
    before = os.fstat(stream.fileno())
    stream.seek(0)
    head = stream.read(32)
    if len(head) != 32 or head[0] not in (3, 4) or head[29] != 0:
        return None
    records, header, length = struct.unpack_from('<IHH', head, 4)
    if (header < 65 or (header - 33) % 32 or length < 2
            or header + records * length > before.st_size):
        return None
    definitions = stream.read(header - 32)
    if len(definitions) != header - 32 or definitions[-1:] != b'\r':
        return None
    fields, offset, non_ascii = [], 1, False
    for position in range(0, len(definitions) - 1, 32):
        check_cancel()
        descriptor = definitions[position:position + 32]
        name = descriptor[:11].split(b'\0', 1)[0]
        kind, width = descriptor[11], descriptor[16]
        if not name or not width or kind not in b'CNFDL':
            return None
        try:
            name.decode('utf-8', 'strict')
        except UnicodeError:
            return None
        non_ascii |= not name.isascii()
        fields.append((offset, offset + width, kind))
        offset += width
    if offset != length:
        return None
    for _ in range(records):
        check_cancel()
        record = stream.read(length)
        if len(record) != length or record[:1] not in (b' ', b'*'):
            return None
        for start, end, kind in fields:
            value = record[start:end]
            if kind == ord('C'):
                try:
                    value.decode('utf-8', 'strict')
                except UnicodeError:
                    return None
                non_ascii |= not value.isascii()
            elif not value.isascii():
                # Numeric/date/logical fields are native ASCII, not arbitrary
                # UTF-8 text or a binary field that can be decoded as text.
                return None
    check_cancel()
    if stream.read(2) not in (b'', b'\x1a'):
        return None
    after = os.fstat(stream.fileno())
    if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
        return None
    return {'encoding': 'UTF-8', 'codepageMark': 0,
            'encodingBasis': 'undefined_codepage_complete_utf8_character_fields',
            'encodingInferred': non_ascii,
            'warnings': ('DBF_UNDECLARED_UTF8_IMPORT',) if non_ascii else ()}


def legacy_import_hint(source, native, *, cancel=None):
    with Path(source).open('rb') as stream:
        head = stream.read(64)
        logical = head[3:] if head.startswith(b'\xef\xbb\xbf') else head
        is_dif = logical.startswith(b'TABLE\r') or logical.startswith(b'TABLE\n')
        is_dbf = len(head) >= 32 and head[0] in (3, 4, 0x30, 0x31, 0x83, 0x8b, 0xf5)
        if not is_dif and not is_dbf:
            return None
        if is_dif:
            item = native.filters.get('DIF')
            if item and item[0] == CALC and 'IMPORT' in item[2] and _dif_utf8(stream, cancel):
                return {'filter': 'DIF', 'options': '76', 'basis': 'content_confirmed_legacy'}
            return None
        item = native.filters.get('dBase')
        if item and item[0] == CALC and 'IMPORT' in item[2]:
            inspection = _dbf_utf8(stream, cancel)
            if inspection is not None:
                return {'filter': 'dBase', 'options': '76',
                        'basis': 'content_confirmed_legacy', **inspection}
    return None


def validate_options(name, options, native):
    item = native.filters.get(name)
    if not item or item[0] != CALC or 'IMPORT' not in item[2]:
        return False
    if name in ('DIF', 'dBase'):
        return options == '76'
    return ('csv' in item[1] and options in {f'{n},34,76,1' for n in (44, 59, 9, 124)})

