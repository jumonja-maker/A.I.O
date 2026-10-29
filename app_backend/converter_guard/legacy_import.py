"""Small content-confirmed native hints; no extension-based admission."""
from pathlib import Path
import struct

CALC = 'com.sun.star.sheet.SpreadsheetDocument'


def _dbase3_options(source, *, cancel=None):
    """Inspect declared dBase III in bounded memory, including its entire tail.

    A file-size threshold cannot decide encoding or damage. The caller's
    cancellation/deadline applies throughout scanning; native import still
    owns all document/model proof and conversion.
    """
    import codecs
    import os
    from .errors import GuardError
    def check():
        if cancel is not None and cancel.is_set():
            raise GuardError('CANCELLED','Проверка DBF отменена.',stage='import')
    def stop(code):
        raise GuardError(code,'Структура или кодировка DBF не подтверждена. Исходный файл сохранён; автоматический импорт не запущен.',stage='import')
    check()
    with Path(source).open('rb') as stream:
        head=stream.read(32)
        if len(head)<32 or head[0]!=3:return None
        records,header,length=struct.unpack_from('<IHH',head,4)
        date_plausible=1<=head[2]<=12 and 1<=head[3]<=31
        if not(date_plausible or header>=33 and (header-33)%32==0):return None
        if header<33 or (header-33)%32 or length<1:stop('DBF_SOURCE_LAYOUT_INVALID')
        size=os.fstat(stream.fileno()).st_size;end=header+records*length
        if header>size or end>size:stop('DBF_SOURCE_TRUNCATED')
        declaration=stream.read(header-32)
        if len(declaration)!=header-32 or declaration[-1]!=13:stop('DBF_SOURCE_LAYOUT_INVALID')
        fields=[declaration[p:p+32] for p in range(0,len(declaration)-1,32)]
        if (not fields or 1+sum(f[16] for f in fields)!=length or
                any(not f[16] or f[11] not in b'CNFLD' or any(c>=128 for c in f[:11]) for f in fields)):
            stop('DBF_SOURCE_LAYOUT_INVALID')
        ascii_only=True;utf8_valid=True;utf8=codecs.getincrementaldecoder('utf-8')('strict')
        for index in range(records):
            check();record=stream.read(length)
            if len(record)!=length:stop('DBF_SOURCE_TRUNCATED')
            if record[0] not in (32,42):stop('DBF_SOURCE_LAYOUT_INVALID')
            if utf8_valid:
                try:utf8.decode(record,final=False)
                except UnicodeDecodeError:utf8_valid=False
            if record.isascii():continue
            ascii_only=False
            if head[29]!=0xC9:stop('DBF_ENCODING_NOT_CONFIRMED')
            offset=1
            for field in fields:
                value=record[offset:offset+field[16]];offset+=field[16]
                if field[11]==ord('C'):
                    try:value.decode('cp1251','strict')
                    except UnicodeDecodeError:stop('DBF_ENCODING_NOT_CONFIRMED')
                elif not value.isascii():stop('DBF_ENCODING_NOT_CONFIRMED')
        check()
        if ascii_only:return '76'
        if utf8_valid:
            try:utf8.decode(b'',final=True)
            except UnicodeDecodeError:utf8_valid=False
        if utf8_valid:stop('DBF_ENCODING_AMBIGUOUS')
        return '34'


def validate_legacy_dbf_source(source, native, *, cancel=None):
    """Reject known ambiguity; other dialects keep native interaction policy."""
    from .errors import GuardError
    if not Path(source).is_file():return  # Existing source admission owns I/O errors.
    options=_dbase3_options(source,cancel=cancel)
    item=native.filters.get('dBase') if options=='34' else None
    if options=='34' and not(item and item[0]==CALC and 'IMPORT' in item[2]):
        raise GuardError('DBF_ENCODING_NOT_CONFIRMED','В установке не подтверждён штатный импорт DBF CP1251.',stage='import')


def legacy_import_hint(source, native, *, cancel=None):
    with Path(source).open('rb') as stream:
        head = stream.read(64)
        is_dif = head.startswith(b'TABLE\r') or head.startswith(b'TABLE\n')
        is_dbf = len(head) >= 32 and head[0] in (3, 4, 0x30, 0x31, 0x83, 0x8b, 0xf5)
        if not is_dif and not is_dbf:
            return None
        if is_dbf and head[0]==3:
            from .errors import GuardError
            try:options=_dbase3_options(source,cancel=cancel)
            except GuardError as error:
                if error.code.startswith('DBF_'):return None
                raise
            item=native.filters.get('dBase')
            return ({'filter':'dBase','options':options,'basis':'content_confirmed_legacy'}
                    if options and item and item[0]==CALC and 'IMPORT' in item[2] else None)
        stream.seek(0)
        data = stream.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        return None
    name, options = None, '76'
    if is_dif:
        try:
            text = data.decode('utf-8')
        except UnicodeError:
            return None
        if all(token in text.splitlines() for token in ('TABLE', 'VECTORS', 'TUPLES', 'DATA')):
            name = 'DIF'
    elif is_dbf:
        records, header, length = struct.unpack_from('<IHH', data, 4)
        end = header + records * length
        if (header < 33 or (header - 33) % 32 or length < 1
                or header > len(data) or data[header-1] != 13 or end > len(data)):
            return None
        fields = [data[p:p+32] for p in range(32, header-1, 32)]
        if (any(any(c >= 128 for c in f[:11]) for f in fields)
                or any(data[header+i*length] not in (32,42) for i in range(records))):
            return None
        body = data[header:end]
        if all(c < 128 for c in body):
            name = 'dBase'  # ASCII data has the same bytes in supported encodings.
        elif (data[29] == 0xC9 and data[0] == 3 and fields
                and 1 + sum(f[16] for f in fields) == length
                and all(f[16] and f[11] in b'CNFLD' for f in fields)):
            # dBase language-driver C9 declares Windows Cyrillic, not an
            # extension/locale guess. Unknown drivers retain abort-on-dialog.
            # A valid UTF-8 payload conflicting with this declaration remains
            # ambiguous; never silently reinterpret it as Windows-1251.
            try:
                body.decode('utf-8')
            except UnicodeDecodeError:
                try:
                    for i in range(records):
                        record=data[header+i*length:header+(i+1)*length]
                        offset=1
                        for field in fields:
                            value=record[offset:offset+field[16]]
                            if field[11] == ord('C'):
                                value.decode('cp1251', errors='strict')
                            elif any(c >= 128 for c in value):
                                return None
                            offset += field[16]
                except UnicodeDecodeError:
                    return None
                name, options = 'dBase', '34'  # RTL_TEXTENCODING_MS_1251, native LO.
    item = native.filters.get(name)
    return ({'filter': name, 'options': options, 'basis': 'content_confirmed_legacy'}
            if item and item[0] == CALC and 'IMPORT' in item[2] else None)


def validate_options(name, options, native):
    item = native.filters.get(name)
    if not item or item[0] != CALC or 'IMPORT' not in item[2]:
        return False
    if name == 'DIF':
        return options == '76'
    if name == 'dBase':
        return options in ('76', '34')
    if 'csv' not in item[1] or not isinstance(options, str) or len(options) > 1024:
        return False
    defaults = {f'{n},34,76,1' for n in (44, 59, 9, 124)}
    if options in defaults:
        return True
    # Use one strict grammar for import and mandatory ID verification:
    # Text (2) and US-English decimal (10) columns only, bounded and ordered.
    # Other filter types, locale tokens and arbitrary options remain rejected.
    # This cannot switch the importer or admit a new source/target route.
    from .csv_id_contract import _import_dialect
    return _import_dialect(name, options) is not None
