"""Bounded TIFF strip decoding when Qt's optional TIFF plugin is absent.

This deliberately supports 8-bit contiguous RGB/RGBA pages, including TIFF
LZW/Deflate/PackBits and horizontal prediction. Other layouts are identified
explicitly rather than decoded as a different image.
"""
from pathlib import Path
import struct
import zlib

from .errors import GuardError

MAX_METADATA_ITEMS = 1024*1024
MAX_PAGES = 65536
TAGS = {254, 256, 257, 258, 259, 262, 273, 274, 277, 278, 279, 282, 283,
        284, 296, 317, 338, 339, 34675}
TYPE_SIZE = {1:1, 2:1, 3:2, 4:4, 5:8, 7:1, 16:8}


def _cancelled(cancel):
    if cancel is not None and cancel.is_set():
        raise GuardError('CANCELLED', 'Чтение страниц TIFF отменено.', stage='result')


def _invalid(reason):
    raise GuardError('MEDIA_OUTPUT_INVALID', 'Структура TIFF повреждена.', stage='result', diagnostic=reason)


def _unsupported(reason):
    raise GuardError('MEDIA_TIFF_LAYOUT_UNSUPPORTED',
                     'Этот TIFF требует доступного нативного кодека: неподдерживаемая структура страницы.',
                     stage='result', diagnostic=reason)


class _Reader:
    def __init__(self, stream, cancel):
        self.stream, self.cancel = stream, cancel
        self.size = stream.seek(0, 2)
        stream.seek(0)
        self.items = 0

    def read(self, amount):
        _cancelled(self.cancel)
        if amount < 0 or self.stream.tell()+amount > self.size:
            _invalid('tiff_read_bounds')
        data = self.stream.read(amount)
        if len(data) != amount:
            _invalid('tiff_truncated')
        return data

    def at(self, position, amount):
        if position < 0 or position+amount > self.size:
            _invalid('tiff_offset_bounds')
        old = self.stream.tell()
        self.stream.seek(position)
        data = self.read(amount)
        self.stream.seek(old)
        return data

    def account(self, amount):
        self.items += amount
        if self.items > MAX_METADATA_ITEMS:
            raise GuardError('MEDIA_INSPECTION_INCOMPLETE',
                             'Не завершена проверка сложной структуры TIFF.', stage='result')


def _pages(reader):
    order = reader.read(2)
    if order not in (b'II', b'MM'):
        _invalid('tiff_signature')
    endian = '<' if order == b'II' else '>'
    version = struct.unpack(endian+'H', reader.read(2))[0]
    if version == 42:
        count_code, offset_code, inline, entry_size = 'H', 'I', 4, 12
    elif version == 43:
        if reader.read(4) != struct.pack(endian+'HH', 8, 0):
            _invalid('tiff_bigtiff_header')
        count_code, offset_code, inline, entry_size = 'Q', 'Q', 8, 20
    else:
        _invalid('tiff_version')
    offset = struct.unpack(endian+offset_code, reader.read(inline))[0]
    seen = set()
    while offset:
        _cancelled(reader.cancel)
        if offset in seen or len(seen) >= MAX_PAGES:
            _invalid('tiff_ifd_cycle_or_count')
        seen.add(offset)
        if offset >= reader.size:
            _invalid('tiff_ifd_offset')
        reader.stream.seek(offset)
        count = struct.unpack(endian+count_code, reader.read(struct.calcsize(count_code)))[0]
        reader.account(count)
        if reader.stream.tell()+count*entry_size+inline > reader.size:
            _invalid('tiff_ifd_size')
        values = {}
        for _ in range(count):
            entry = reader.read(entry_size)
            tag, typ = struct.unpack(endian+'HH', entry[:4])
            if tag not in TAGS:
                continue
            if tag in values or typ not in TYPE_SIZE:
                _invalid('tiff_duplicate_or_type')
            amount = struct.unpack(endian+('I' if inline == 4 else 'Q'), entry[4:-inline])[0]
            if amount <= 0:
                _invalid('tiff_empty_tag')
            reader.account(amount)
            size = TYPE_SIZE[typ]*amount
            payload = entry[-inline:]
            if size > inline:
                payload = reader.at(struct.unpack(endian+offset_code, payload)[0], size)
            else:
                payload = payload[:size]
            if tag == 34675:
                if typ not in (1, 7):
                    _invalid('tiff_icc_type')
                values[tag] = payload
            elif typ in (1, 3, 4, 16):
                code = {1:'B', 3:'H', 4:'I', 16:'Q'}[typ]
                values[tag] = struct.unpack(endian+str(amount)+code, payload)
            elif typ == 5:
                words = struct.unpack(endian+str(amount*2)+'I', payload)
                if any(not words[n] for n in range(1, len(words), 2)):
                    _invalid('tiff_rational_zero')
                values[tag] = tuple(words[n]/words[n+1] for n in range(0, len(words), 2))
            else:
                _unsupported('tiff_tag_encoding')
        offset = struct.unpack(endian+offset_code, reader.read(inline))[0]
        if values.get(254, (0,))[0] & 1:
            continue  # Reduced-resolution thumbnails are not document pages.
        _check_layout(values, reader.size)
        yield values


def _check_layout(page, file_size):
    width, height = page.get(256, (0,))[0], page.get(257, (0,))[0]
    samples = page.get(277, (1,))[0]
    rows = page.get(278, (height,))[0]
    if not width or not height or not rows:
        _invalid('tiff_dimensions')
    if (page.get(262, (0,))[0] != 2 or samples not in (3, 4)
            or page.get(284, (1,))[0] != 1
            or page.get(258, ()) != (8,)*samples
            or page.get(339, (1,)*samples) not in ((1,), (1,)*samples)
            or page.get(274, (1,))[0] not in range(1, 9)):
        _unsupported('tiff_requires_rgb8_contiguous')
    if samples == 4 and page.get(338, ()) not in ((1,), (2,)):
        _unsupported('tiff_unspecified_fourth_channel')
    if page.get(259, (1,))[0] not in (1, 5, 8, 32946, 32773):
        _unsupported('tiff_compression')
    if page.get(317, (1,))[0] not in (1, 2):
        _unsupported('tiff_predictor')
    offsets, counts = page.get(273), page.get(279)
    expected_strips = (height+rows-1)//rows
    if offsets is None or counts is None:
        _unsupported('tiff_requires_strips')
    if len(offsets) != expected_strips or len(counts) != expected_strips:
        _invalid('tiff_strip_count')
    for offset, count in zip(offsets, counts):
        if not offset or not count or offset+count > file_size:
            _invalid('tiff_strip_bounds')


class _Strip:
    def __init__(self, reader, offset, amount):
        self.reader, self.remaining = reader, amount
        reader.stream.seek(offset)
        self.buffer = b''
        self.cursor = 0

    def read(self, count):
        _cancelled(self.reader.cancel)
        if count > self.remaining:
            _invalid('tiff_strip_truncated')
        self.remaining -= count
        return self.reader.read(count)

    def byte(self):
        _cancelled(self.reader.cancel)
        if self.cursor == len(self.buffer):
            if not self.remaining:
                _invalid('tiff_compressed_strip_truncated')
            self.buffer = self.read(min(8192, self.remaining))
            self.cursor = 0
        value = self.buffer[self.cursor]
        self.cursor += 1
        return value


def _lzw(strip, expected):
    table = [bytes((n,)) for n in range(256)]+[None, None]
    next_code, width = 258, 9
    value = bits = 0
    previous = None
    result = bytearray()
    while True:
        while bits < width:
            value = (value << 8) | strip.byte()
            bits += 8
        bits -= width
        code = (value >> bits) & ((1 << width)-1)
        value &= (1 << bits)-1
        if code == 256:
            table = [bytes((n,)) for n in range(256)]+[None, None]
            next_code, width, previous = 258, 9, None
            continue
        if code == 257:
            if len(result) != expected:
                _invalid('tiff_lzw_decoded_size')
            return result
        if code < len(table) and table[code] is not None:
            entry = table[code]
        elif code == next_code and previous is not None:
            entry = previous+previous[:1]
        else:
            _invalid('tiff_lzw_code')
        if len(result)+len(entry) > expected:
            _invalid('tiff_lzw_expansion')
        result.extend(entry)
        if previous is not None and next_code < 4096:
            table.append(previous+entry[:1])
            next_code += 1
            # TIFF 6.0 uses the one-code-early 9->12-bit transition.
            if next_code == (1 << width)-1 and width < 12:
                width += 1
        previous = entry


def _inflate(strip, expected):
    decoder = zlib.decompressobj()
    result = bytearray()
    try:
        while strip.remaining:
            data = strip.read(min(65536, strip.remaining))
            result.extend(decoder.decompress(data, expected-len(result)+1))
            if len(result) > expected or decoder.unconsumed_tail:
                _invalid('tiff_deflate_expansion')
        result.extend(decoder.flush(expected-len(result)+1))
    except zlib.error as error:
        _invalid('tiff_deflate:'+type(error).__name__)
    if len(result) != expected or not decoder.eof or decoder.unused_data:
        _invalid('tiff_deflate_size_or_end')
    return result


def _packbits(strip, expected):
    result = bytearray()
    while strip.remaining or strip.cursor < len(strip.buffer):
        code = strip.byte()
        if code == 128:
            continue
        if code < 128:
            count = code+1
            if len(result)+count > expected:
                _invalid('tiff_packbits_expansion')
            result.extend(strip.byte() for _ in range(count))
        else:
            count = 257-code
            if len(result)+count > expected:
                _invalid('tiff_packbits_expansion')
            result.extend(bytes((strip.byte(),))*count)
    if len(result) != expected:
        _invalid('tiff_packbits_size')
    return result


def _decode_strip(reader, offset, size, compression, expected):
    strip = _Strip(reader, offset, size)
    if compression == 1:
        if size != expected:
            _invalid('tiff_raw_strip_size')
        result = bytearray(expected)
        for begin in range(0, expected, 65536):
            _cancelled(reader.cancel)
            end = min(expected, begin+65536)
            result[begin:end] = strip.read(end-begin)
        return result
    if compression == 5:
        return _lzw(strip, expected)
    if compression in (8, 32946):
        return _inflate(strip, expected)
    return _packbits(strip, expected)


def inspect_tiff_decode_support(path, *, cancel=None):
    """Describe the fallback's exact supported layout; corruption stays an error."""
    try:
        with Path(path).open('rb') as stream:
            pages = list(_pages(_Reader(stream, cancel)))
        if not pages:
            _invalid('tiff_no_pages')
        return {'supported': True, 'pages': len(pages),
                'compressions': sorted({page.get(259, (1,))[0] for page in pages})}
    except GuardError as error:
        if error.code == 'MEDIA_TIFF_LAYOUT_UNSUPPORTED':
            return {'supported': False, 'code': error.code, 'reason': error.diagnostic}
        raise


def tiff_decoder_available(path, *, cancel=None):
    return inspect_tiff_decode_support(path, cancel=cancel)['supported']


def iter_tiff_frames(path, *, cancel=None):
    """Yield raw-orientation pages; the caller applies inspected orientation.

    Physical resolution is retained on each QImage. Keeping orientation here
    raw avoids applying TIFF's orientation twice in the common media iterator.
    """
    from PySide6.QtGui import QColorSpace, QImage, QImageReader
    with Path(path).open('rb') as stream:
        reader = _Reader(stream, cancel)
        count = 0
        for page in _pages(reader):
            _cancelled(cancel)
            width, height = page[256][0], page[257][0]
            samples = page[277][0]
            allocation = QImageReader.allocationLimit()
            if allocation > 0 and width*height*4 > allocation*1024*1024:
                raise GuardError('MEDIA_ALLOCATION_LIMIT',
                                 'Для страницы TIFF превышен настроенный в Qt бюджет памяти изображения.',
                                 stage='result')
            image_format = (QImage.Format.Format_RGB888 if samples == 3 else
                            QImage.Format.Format_RGBA8888_Premultiplied if page[338] == (1,) else
                            QImage.Format.Format_RGBA8888)
            image = QImage(width, height, image_format)
            if image.isNull():
                raise GuardError('MEDIA_DECODE_FAILED', 'Недостаточно памяти для страницы TIFF.', stage='result')
            destination, stride = image.bits(), image.bytesPerLine()
            rows = page.get(278, (height,))[0]
            compression, predictor = page.get(259, (1,))[0], page.get(317, (1,))[0]
            row_bytes = width*samples
            for n, (offset, size) in enumerate(zip(page[273], page[279])):
                first_row = n*rows
                strip_rows = min(rows, height-first_row)
                decoded = _decode_strip(reader, offset, size, compression, strip_rows*row_bytes)
                if predictor == 2:
                    for y in range(strip_rows):
                        _cancelled(cancel)
                        begin = y*row_bytes
                        for i in range(begin+samples, begin+row_bytes):
                            decoded[i] = (decoded[i]+decoded[i-samples]) & 255
                for y in range(strip_rows):
                    _cancelled(cancel)
                    begin, end = (first_row+y)*stride, y*row_bytes
                    destination[begin:begin+row_bytes] = decoded[end:end+row_bytes]
            if 34675 in page:
                colour = QColorSpace.fromIccProfile(page[34675])
                if colour.isValid():
                    image.setColorSpace(colour)
            units = page.get(296, (2,))[0]
            multiplier = 100/2.54 if units == 2 else 100 if units == 3 else 0
            if multiplier and page.get(282) and page.get(283):
                image.setDotsPerMeterX(round(page[282][0]*multiplier))
                image.setDotsPerMeterY(round(page[283][0]*multiplier))
            count += 1
            yield image
        if not count:
            _invalid('tiff_no_pages')
