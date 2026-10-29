"""Structural raster preservation checks; no image decoder or new engine.

The native same-format route copies admitted original bytes. Cross-format
exports still belong to LibreOffice; metadata checks stop demonstrable loss
of animation, pages, transparency or canvas from becoming ready. They do not
claim pixel, colour or visual equivalence.
"""
from pathlib import Path
import os
import struct
import zlib

from .errors import GuardError

ALIASES = {'apng': 'png', 'tif': 'tiff', 'jpg': 'jpeg', 'jpe': 'jpeg',
           'jfif': 'jpeg', 'jif': 'jpeg'}
RASTER = frozenset(('png', 'apng', 'gif', 'webp', 'tiff', 'tif', 'jpeg',
                    'jpg', 'jpe', 'jfif', 'jif', 'bmp'))
ANIMATED_TARGETS = frozenset(('png', 'gif', 'apng', 'webp'))
ALPHA_TARGETS = frozenset(('png', 'apng', 'gif', 'webp', 'tiff', 'tif'))
MAX_RECORDS = 65536


def media_route_reason(target, proof):
    """A registered PNG exporter is not an APNG encoder.

    Raster routes in qtMediaTargets own a real APNG writer (including exact
    APNG identity). Native document PNG filters have no animation framing;
    advertising their output as APNG guarantees a rejected postcondition.
    The caller still checks the native proof's registry identity and model.
    """
    targets = (proof or {}).get('qtMediaTargets', ())
    if not isinstance(targets, (list, tuple, set, frozenset)):
        targets = ()
    if target == 'apng' and target not in targets:
        return 'APNG_ENCODER_UNAVAILABLE'
    return None


class _Invalid(ValueError):
    pass


def _cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise GuardError('CANCELLED', 'Проверка изображения отменена.', stage='result')


class _Input:
    def __init__(self, path, cancel):
        self.file = path.open('rb')
        self.size = os.fstat(self.file.fileno()).st_size
        self.cancel = cancel
        self.records = 0

    def read(self, count):
        _cancel(self.cancel)
        if count < 0 or self.file.tell() + count > self.size:
            raise _Invalid('truncated')
        value = self.file.read(count)
        if len(value) != count:
            raise _Invalid('truncated')
        return value

    def seek(self, offset):
        _cancel(self.cancel)
        if not 0 <= offset <= self.size:
            raise _Invalid('offset')
        self.file.seek(offset)

    def skip(self, count):
        self.seek(self.file.tell() + count)

    def record(self):
        _cancel(self.cancel)
        self.records += 1
        if self.records > MAX_RECORDS:
            raise GuardError('MEDIA_INSPECTION_INCOMPLETE',
                             'Структура изображения превышает бюджет проверки; результат не подтверждён.',
                             stage='result')


def _base(kind, width, height, *, frames=1, pages=1, alpha=False, durations=None):
    if not (width > 0 and height > 0 and frames > 0 and pages > 0):
        raise _Invalid('dimensions_or_count')
    return {'format': kind, 'width': width, 'height': height, 'frames': frames,
            'pages': pages, 'alpha': bool(alpha), 'durationsMs': durations or [], 'orientation': 1}



def _exif_orientation(data):
    """Read only the bounded IFD0 orientation from Exif TIFF bytes."""
    if data.startswith(b'Exif\0\0'):
        data = data[6:]
    if len(data) < 8 or data[:2] not in (b'II', b'MM'):
        return 1
    endian = '<' if data[:2] == b'II' else '>'
    try:
        if struct.unpack_from(endian + 'H', data, 2)[0] != 42:
            return 1
        offset = struct.unpack_from(endian + 'I', data, 4)[0]
        count = struct.unpack_from(endian + 'H', data, offset)[0]
        if count > MAX_RECORDS or offset + 2 + count * 12 > len(data):
            return 1
        for index in range(count):
            position = offset + 2 + index * 12
            tag, typ, amount = struct.unpack_from(endian + 'HHI', data, position)
            if tag == 274 and typ == 3 and amount == 1:
                orientation = struct.unpack_from(endian + 'H', data, position + 8)[0]
                return orientation if 1 <= orientation <= 8 else 1
    except (struct.error, OverflowError):
        return 1
    return 1


def display_dimensions(info):
    width, height = info['width'], info['height']
    return (height, width) if info.get('orientation', 1) >= 5 else (width, height)


def display_page_dimensions(info):
    dimensions = info.get('pageDimensions')
    if not dimensions:
        return None
    orientations = info.get('pageOrientations', [1] * len(dimensions))
    return [[height, width] if orientation >= 5 else [width, height]
            for (width, height), orientation in zip(dimensions, orientations)]

def _png(r):
    if r.read(8) != b'\x89PNG\r\n\x1a\n':
        raise _Invalid('png_signature')
    width = height = 0
    orientation = 1
    alpha = False
    declared = None
    loops = 1
    durations = []
    saw_data = False
    sequence = 0
    first = True
    while True:
        r.record()
        length, kind = struct.unpack('>I4s', r.read(8))
        if first and kind != b'IHDR':
            raise _Invalid('png_ihdr')
        first = False
        crc = zlib.crc32(kind)
        head = b''
        remaining = length
        while remaining:
            block = r.read(min(65536, remaining))
            head_limit = 65536 if kind == b'eXIf' else 32
            if len(head) < head_limit:
                head += block[:head_limit-len(head)]
            crc = zlib.crc32(block, crc)
            remaining -= len(block)
        if (crc & 0xffffffff) != struct.unpack('>I', r.read(4))[0]:
            raise _Invalid('png_crc')
        if kind == b'IHDR':
            if width or length != 13:
                raise _Invalid('png_ihdr')
            width, height, depth, colour, comp, filt, interlace = struct.unpack('>IIBBBBB', head)
            if colour not in (0, 2, 3, 4, 6) or comp or filt or interlace > 1:
                raise _Invalid('png_encoding')
            alpha = colour in (4, 6)
        elif kind == b'eXIf':
            orientation = _exif_orientation(head)
        elif kind == b'tRNS':
            alpha = True
        elif kind == b'acTL':
            if declared is not None or saw_data or length != 8:
                raise _Invalid('apng_control')
            declared, loops = struct.unpack('>II', head[:8])
            if not 0 < declared <= MAX_RECORDS:
                raise _Invalid('apng_count')
        elif kind == b'fcTL':
            if declared is None or length != 26:
                raise _Invalid('apng_frame')
            seq, fw, fh, x, y, num, den, dispose, blend = struct.unpack('>IIIIIHHBB', head)
            if seq != sequence or not fw or not fh or x+fw > width or y+fh > height or dispose > 2 or blend > 1:
                raise _Invalid('apng_frame')
            sequence += 1
            durations.append(1000 * num / (den or 100))
        elif kind == b'fdAT':
            if declared is None or length < 4 or struct.unpack('>I', head[:4])[0] != sequence:
                raise _Invalid('apng_sequence')
            sequence += 1
            saw_data = True
        elif kind == b'IDAT':
            saw_data = True
        elif kind == b'IEND':
            if length or not saw_data or r.file.tell() != r.size:
                raise _Invalid('png_end')
            if declared is not None and len(durations) != declared:
                raise _Invalid('apng_count')
            result = _base('png', width, height, frames=declared or 1,
                           alpha=alpha, durations=durations)
            result['animationContainer'] = declared is not None
            result['loopCount'] = loops
            result['bitDepth'] = depth
            result['orientation'] = orientation
            return result


def _subblocks(r):
    while True:
        # Image data is split into 255-byte blocks; its size must not consume
        # the metadata/frame budget or impose a hidden image-size limit.
        _cancel(r.cancel)
        size = r.read(1)[0]
        if not size:
            return
        r.skip(size)


def _gif(r):
    if r.read(6) not in (b'GIF87a', b'GIF89a'):
        raise _Invalid('gif_signature')
    width, height, packed, background, aspect = struct.unpack('<HHBBB', r.read(7))
    if packed & 128:
        r.skip(3 * (2 << (packed & 7)))
    durations = []
    alpha = False
    delay = 0
    loops = 1
    while True:
        r.record()
        block = r.read(1)
        if block == b';':
            if not durations:
                raise _Invalid('gif_frames')
            result = _base('gif', width, height, frames=len(durations), alpha=alpha, durations=durations)
            result['loopCount'] = loops
            return result
        if block == b'!':
            label = r.read(1)
            if label == b'\xf9':
                if r.read(1) != b'\x04':
                    raise _Invalid('gif_control')
                flags, ticks, index = struct.unpack('<BHB', r.read(4))
                if r.read(1) != b'\x00':
                    raise _Invalid('gif_control')
                delay = ticks * 10
                alpha |= bool(flags & 1)
            elif label == b'\xff':
                name_size = r.read(1)[0]
                name = r.read(name_size)
                if name in (b'NETSCAPE2.0', b'ANIMEXTS1.0'):
                    size = r.read(1)[0]
                    payload = r.read(size)
                    if size != 3 or payload[0] != 1:
                        raise _Invalid('gif_loop')
                    repeats = struct.unpack('<H', payload[1:])[0]
                    loops = repeats + 1 if repeats else 0
                    _subblocks(r)
                else:
                    _subblocks(r)
            else:
                _subblocks(r)
        elif block == b',':
            x, y, fw, fh, flags = struct.unpack('<HHHHB', r.read(9))
            if not fw or not fh or x+fw > width or y+fh > height:
                raise _Invalid('gif_canvas')
            if flags & 128:
                r.skip(3 * (2 << (flags & 7)))
            if not 2 <= r.read(1)[0] <= 8:
                raise _Invalid('gif_lzw')
            _subblocks(r)
            durations.append(delay)
            delay = 0
        else:
            raise _Invalid('gif_block')


def _webp(r):
    if r.read(4) != b'RIFF':
        raise _Invalid('webp_signature')
    declared = struct.unpack('<I', r.read(4))[0] + 8
    if r.read(4) != b'WEBP' or declared != r.size:
        raise _Invalid('webp_riff_size')
    width = height = 0
    alpha = animation = False
    orientation = 1
    loops = 1
    durations = []
    image = False
    while r.file.tell() < r.size:
        r.record()
        kind, length = struct.unpack('<4sI', r.read(8))
        start = r.file.tell()
        head = r.read(min(length, 65536 if kind == b'EXIF' else 16))
        r.seek(start + length + (length & 1))
        if kind == b'VP8X':
            if length != 10:
                raise _Invalid('webp_vp8x')
            alpha |= bool(head[0] & 0x10)
            animation = bool(head[0] & 2)
            width, height = int.from_bytes(head[4:7], 'little')+1, int.from_bytes(head[7:10], 'little')+1
        elif kind == b'VP8L':
            if length < 5 or head[0] != 0x2f:
                raise _Invalid('webp_vp8l')
            bits = int.from_bytes(head[1:5], 'little')
            width = width or (bits & 0x3fff)+1
            height = height or ((bits >> 14) & 0x3fff)+1
            alpha |= bool(bits & (1 << 28))
            image = True
        elif kind == b'VP8 ':
            if length < 10 or head[3:6] != b'\x9d\x01\x2a':
                raise _Invalid('webp_vp8')
            width = width or (int.from_bytes(head[6:8], 'little') & 0x3fff)
            height = height or (int.from_bytes(head[8:10], 'little') & 0x3fff)
            image = True
        elif kind == b'EXIF':
            orientation = _exif_orientation(head)
        elif kind == b'ANIM':
            if length != 6 or not animation:
                raise _Invalid('webp_anim')
            loops = struct.unpack('<H', head[4:6])[0]
        elif kind == b'ANMF':
            if not animation or length < 24:
                raise _Invalid('webp_frame')
            x, y = int.from_bytes(head[0:3], 'little')*2, int.from_bytes(head[3:6], 'little')*2
            fw, fh = int.from_bytes(head[6:9], 'little')+1, int.from_bytes(head[9:12], 'little')+1
            if x+fw > width or y+fh > height:
                raise _Invalid('webp_canvas')
            durations.append(int.from_bytes(head[12:15], 'little'))
    if animation and not durations or not animation and not image:
        raise _Invalid('webp_frames')
    result = _base('webp', width, height, frames=len(durations) or 1, alpha=alpha, durations=durations)
    result['loopCount'] = loops
    result['orientation'] = orientation
    return result


def _tiff_array(r, descriptor, endian, inline_size, offset_code):
    """Read strip/tile index values in bounded chunks, including BigTIFF."""
    typ, count, payload = descriptor
    sizes, codes = {3: 2, 4: 4, 16: 8}, {3: 'H', 4: 'I', 16: 'Q'}
    if typ not in sizes or count <= 0:
        raise _Invalid('tiff_data_table_type')
    size = sizes[typ]
    if count * size <= inline_size:
        yield from struct.unpack(endian + codes[typ] * count, payload[:count * size])
        return
    position = struct.unpack(endian + offset_code, payload)[0]
    if position + count * size > r.size:
        raise _Invalid('tiff_data_table_bounds')
    for start in range(0, count, 4096):
        amount = min(4096, count - start)
        r.seek(position + start * size)
        chunk = r.read(amount * size)
        yield from struct.unpack(endian + codes[typ] * amount, chunk)


def _tiff_data_bounds(r, tables, endian, inline_size, offset_code):
    # TIFF6 section 2 (strips), section 15 (tiles), and BigTIFF LONG8.
    # Only bounds are proved here: valid offsets do not certify decompression,
    # pixels, colours, or completeness of all private TIFF extensions.
    for offsets, counts in ((273, 279), (324, 325), (513, 514)):
        if offsets not in tables and counts not in tables:
            continue
        if offsets == 513:
            # Old-style JPEG may explicitly declare no interchange stream
            # with offset 0 (TIFF6 p105); its length is then irrelevant.
            if offsets not in tables:
                continue
            if tables[offsets][1] == 1 and next(
                    _tiff_array(r, tables[offsets], endian, inline_size, offset_code)) == 0:
                continue
        if offsets not in tables or counts not in tables:
            raise _Invalid('tiff_data_table_incomplete')
        if tables[offsets][1] != tables[counts][1]:
            raise _Invalid('tiff_data_table_count')
        starts = _tiff_array(r, tables[offsets], endian, inline_size, offset_code)
        lengths = _tiff_array(r, tables[counts], endian, inline_size, offset_code)
        for position, length in zip(starts, lengths):
            # Large scans can legitimately contain millions of short strips.
            # Their index is streamed from an already bounded byte extent;
            # apply cancellation/deadline, not the IFD/frame metadata count cap.
            _cancel(r.cancel)
            # Keep sparse 0/0 entries compatible; never infer absent data from
            # them. A positive extent beyond EOF is unambiguously truncated.
            if position > r.size or length > r.size - position:
                raise _Invalid('tiff_pixel_data_truncated')


def _tiff(r, *, complete=False):
    order = r.read(2)
    if order not in (b'II', b'MM'):
        raise _Invalid('tiff_signature')
    endian = '<' if order == b'II' else '>'
    version = struct.unpack(endian+'H', r.read(2))[0]
    if version == 42:
        count_code, offset_code, inline_size, entry_size = 'H', 'I', 4, 12
    elif version == 43:
        if r.read(4) != struct.pack(endian+'HH', 8, 0):
            raise _Invalid('bigtiff_header')
        count_code, offset_code, inline_size, entry_size = 'Q', 'Q', 8, 20
    else:
        raise _Invalid('tiff_version')
    offset = struct.unpack(endian+offset_code, r.read(inline_size))[0]
    seen = set()
    dimensions = []
    depths = []
    page_indexes = []
    page_orientations = []
    page_dpi = []
    alpha = False
    sizes = {1: 1, 3: 2, 4: 4, 5: 8, 16: 8}
    codes = {1: 'B', 3: 'H', 4: 'I', 16: 'Q'}
    while offset:
        r.record()
        if offset in seen:
            raise _Invalid('tiff_ifd_cycle')
        seen.add(offset)
        r.seek(offset)
        count = struct.unpack(endian+count_code, r.read(struct.calcsize(count_code)))[0]
        if count > MAX_RECORDS or r.file.tell()+count*entry_size+inline_size > r.size:
            raise _Invalid('tiff_ifd_bounds')
        values = {}
        data_tables = {}
        for _ in range(count):
            r.record()
            entry = r.read(entry_size)
            tag, typ = struct.unpack(endian+'HH', entry[:4])
            if complete and tag in (273, 279, 324, 325, 513, 514):
                if tag in data_tables:
                    raise _Invalid('tiff_duplicate_data_table')
                number = struct.unpack(endian+('I' if version == 42 else 'Q'),
                                       entry[4:entry_size-inline_size])[0]
                data_tables[tag] = (typ, number, entry[-inline_size:])
            if tag not in (254, 256, 257, 258, 274, 277, 282, 283, 296, 338):
                continue
            n = struct.unpack(endian+('I' if version == 42 else 'Q'), entry[4:entry_size-inline_size])[0]
            if typ not in sizes or not 0 < n <= 16:
                raise _Invalid('tiff_tag')
            amount = sizes[typ] * n
            payload = entry[-inline_size:]
            if amount > inline_size:
                saved = r.file.tell()
                r.seek(struct.unpack(endian+offset_code, payload)[0])
                payload = r.read(amount)
                r.seek(saved)
            if typ == 5:
                numerator, denominator = struct.unpack(endian+'II', payload[:8])
                values[tag] = (numerator / denominator if denominator else 0,)
            else:
                values[tag] = struct.unpack(endian+codes[typ]*n, payload[:amount])
        offset = struct.unpack(endian+offset_code, r.read(inline_size))[0]
        if complete:
            _tiff_data_bounds(r, data_tables, endian, inline_size, offset_code)
        if values.get(254, (0,))[0] & 1:  # Reduced-resolution thumbnails are not pages.
            continue
        width, height = values.get(256, (0,))[0], values.get(257, (0,))[0]
        if not width or not height:
            raise _Invalid('tiff_dimensions')
        dimensions.append([width, height])
        depths.append(max(values.get(258, (8,))))
        page_indexes.append(len(seen) - 1)
        orientation = values.get(274, (1,))[0]
        page_orientations.append(orientation if 1 <= orientation <= 8 else 1)
        unit = values.get(296, (2,))[0]
        factor = 1 if unit == 2 else 2.54 if unit == 3 else 0
        dx, dy = values.get(282, (0,))[0] * factor, values.get(283, (0,))[0] * factor
        page_dpi.append([dx, dy] if dx > 0 and dy > 0 else None)
        alpha |= any(v in (1, 2) for v in values.get(338, ()))
    if not dimensions:
        raise _Invalid('tiff_pages')
    result = _base('tiff', *dimensions[0], pages=len(dimensions), alpha=alpha)
    result['pageDimensions'] = dimensions
    result['bitDepth'] = max(depths)
    result['pageImageIndexes'] = page_indexes
    result['pageOrientations'] = page_orientations
    result['orientation'] = page_orientations[0]
    result['pageDpi'] = page_dpi
    return result


def _jpeg_entropy_marker(r):
    """Find an unstuffed marker without retaining compressed image data.

    Byte stuffing, fill bytes and restart markers may straddle read boundaries.
    A missing EOI used to pass as soon as a valid SOF declared dimensions.
    """
    pending = False
    while True:
        _cancel(r.cancel)
        block = r.file.read(64 * 1024)
        if not block:
            raise _Invalid('jpeg_missing_eoi')
        index = 0
        while index < len(block):
            if not pending:
                position = block.find(b'\xff', index)
                if position < 0:
                    break
                index = position + 1
            while index < len(block) and block[index] == 0xff:
                index += 1
            if index == len(block):
                pending = True
                break
            marker = block[index]
            index += 1
            pending = False
            if marker in (0, 1) or 0xd0 <= marker <= 0xd7:
                continue
            r.seek(r.file.tell() - len(block) + index)
            return marker


def _jpeg(r, *, complete=False):
    if r.read(2) != b'\xff\xd8':
        raise _Invalid('jpeg_signature')
    orientation = 1
    result = None
    pending_marker = None
    saw_scan = False
    while True:
        r.record()
        if pending_marker is None:
            if r.read(1) != b'\xff':
                raise _Invalid('jpeg_marker')
            marker = r.read(1)[0]
            while marker == 0xff:
                marker = r.read(1)[0]
        else:
            marker, pending_marker = pending_marker, None
        if marker == 0xd9:
            if result is None or not saw_scan:
                raise _Invalid('jpeg_scan_missing')
            return result
        if marker in (0, 0xd8):
            raise _Invalid('jpeg_marker')
        if marker in range(0xd0, 0xd8) or marker == 0x01:
            continue
        length = struct.unpack('>H', r.read(2))[0]
        if length < 2:
            raise _Invalid('jpeg_segment')
        if marker in (0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf):
            if length < 8:
                raise _Invalid('jpeg_sof')
            depth, height, width = struct.unpack('>BHH', r.read(5))
            if result is None:
                result = _base('jpeg', width, height)
                result['orientation'] = orientation
            if not complete:
                return result
            components = r.read(length - 7)
            if not components or not components[0] or len(components) != 1 + 3 * components[0]:
                raise _Invalid('jpeg_frame_components')
        elif marker == 0xda:
            if result is None or length < 6:
                raise _Invalid('jpeg_scan_header')
            payload = r.read(length - 2)
            if not payload or not payload[0] or len(payload) != 4 + 2 * payload[0]:
                raise _Invalid('jpeg_scan_header')
            saw_scan = True
            pending_marker = _jpeg_entropy_marker(r)
        elif marker == 0xe1:
            payload = r.read(length - 2)
            if payload.startswith(b'Exif\0\0'):
                orientation = _exif_orientation(payload)
        else:
            r.skip(length-2)


def _bmp(r, *, complete=False):
    head = r.read(26)
    if head[:2] != b'BM':
        raise _Invalid('bmp_signature')
    dib = int.from_bytes(head[14:18], 'little')
    if dib == 12:
        width, height = struct.unpack('<HH', head[18:22])
    elif dib >= 40:
        width, height = struct.unpack('<ii', head[18:26])
    else:
        raise _Invalid('bmp_dib')
    result = _base('bmp', abs(width), abs(height))
    if complete:
        pixel_offset = int.from_bytes(head[10:14], 'little')
        declared = int.from_bytes(head[2:6], 'little')
        if 14 + dib > r.size or pixel_offset < 14 + dib or pixel_offset >= r.size:
            raise _Invalid('bmp_pixel_offset')
        if declared and (declared > r.size or declared < pixel_offset):
            raise _Invalid('bmp_file_size')
        if dib == 12:
            planes, depth = struct.unpack('<HH', head[22:26])
            compression, image_size = 0, 0
        else:
            remaining = r.read(14)
            planes, depth, compression, image_size = struct.unpack('<HHII', remaining[:12])
        if planes != 1 or not (0 < depth <= 64 or depth == 0 and compression in (4, 5)):
            raise _Invalid('bmp_pixel_format')
        available = (min(declared, r.size) if declared else r.size) - pixel_offset
        if compression in (0, 3, 6):
            required = ((abs(width) * depth + 31) // 32) * 4 * abs(height)
            if required > available:
                raise _Invalid('bmp_pixels_truncated')
        elif image_size and image_size > available:
            raise _Invalid('bmp_compressed_pixels_truncated')
    return result


def inspect_media(path, *, cancel=None, validate_container=False):
    """Inspect supported raster structure in bounded memory, without decoding pixels."""
    path = Path(path)
    r = _Input(path, cancel)
    try:
        # Native import is content based. A renamed file must not acquire an
        # extension-based veto here, nor be copied into the wrong container.
        magic = r.read(min(12, r.size))
        r.seek(0)
        kind = ('png' if magic.startswith(b'\x89PNG\r\n\x1a\n') else
                'gif' if magic.startswith((b'GIF87a', b'GIF89a')) else
                'webp' if magic[:4] == b'RIFF' and magic[8:12] == b'WEBP' else
                'tiff' if magic[:4] in (b'II\x2a\0', b'MM\0\x2a', b'II\x2b\0', b'MM\0\x2b') else
                'jpeg' if magic.startswith(b'\xff\xd8') else
                'bmp' if magic.startswith(b'BM') else None)
        if kind is None:
            return None
        inspector = {'png': _png, 'gif': _gif, 'webp': _webp, 'tiff': _tiff,
                     'jpeg': _jpeg, 'bmp': _bmp}[kind]
        result = (inspector(r, complete=True) if validate_container and kind in ('jpeg', 'bmp', 'tiff')
                  else inspector(r))
        return result
    except (_Invalid, struct.error, OverflowError, IndexError) as error:
        raise GuardError('MEDIA_OUTPUT_INVALID',
                         'Структура изображения повреждена или не соответствует формату.',
                         stage='result', diagnostic=type(error).__name__+':'+str(error)) from error
    finally:
        r.file.close()


def copy_media_identity(source, output_dir, target, *, cancel=None):
    """Copy only an already admitted same-format image, preserving every byte.

    Call after normal source/preflight and route admission. Returning None means
    the requested conversion still belongs to the existing LibreOffice route.
    """
    source, output_dir = Path(source), Path(output_dir)
    if target not in RASTER:
        return None
    canonical = ALIASES.get(target, target)
    if canonical not in ('png', 'gif', 'webp', 'tiff', 'jpeg'):
        return None
    # JPEG: complete marker traversal through EOI and decode before identity.
    info = inspect_media(source, cancel=cancel, validate_container=canonical == 'jpeg')
    if info is None or info['format'] != ALIASES.get(target, target):
        return None
    if target == 'apng' and not info.get('animationContainer'):
        return None
    if canonical == 'jpeg':
        from .qt_media import _qt, _bounds
        _bounds(info['width'], info['height'])
        reader = _qt()[5](str(source))
        _cancel(cancel)
        image = reader.read()
        if image.isNull():
            raise GuardError('MEDIA_OUTPUT_INVALID', 'JPEG не декодируется; копия не выдана как готовый результат.', stage='result')
        _cancel(cancel)
        del image, reader
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / (source.stem + '.' + target)
    if output.resolve() == source.resolve():
        raise GuardError('MEDIA_IDENTITY_PATH', 'Исходник и результат должны храниться отдельно.', stage='result')
    partial = output.with_name(output.name+'.part')
    try:
        with source.open('rb') as reader, partial.open('xb') as writer:
            while True:
                _cancel(cancel)
                block = reader.read(64*1024)
                if not block:
                    break
                writer.write(block)
        os.replace(partial, output)
    finally:
        partial.unlink(missing_ok=True)
    return [output]


def check_media_preservation(paths, target, source, *, cancel, source_evidence=None):
    """Mandatory property check, independent of optional deep fidelity QA."""
    if target not in RASTER and target != 'pdf':
        return None
    source = Path(source)
    def source_profile():
        def inspect_source():
            result = inspect_media(source, cancel=cancel)
            if result is None and source.suffix.lower() in ('.pbm', '.pgm', '.ppm'):
                # Do not turn every native export into a second Guard scan.
                # For these formats Qt has already confirmed image structure
                # during preflight; one cheap header probe proves dimensions.
                from .qt_media import qt_pnm_profile
                try:
                    result = qt_pnm_profile(source, cancel=cancel)
                except GuardError as error:
                    if error.code not in ('MEDIA_QT_UNAVAILABLE', 'MEDIA_CANVAS_BUDGET'):
                        raise
            return result
        return (inspect_source() if source_evidence is None else
                source_evidence.get('media_profile', source, inspect_source))
    if target == 'pdf':
        before = source_profile()
        if before is None or before['format'] != 'tiff':
            return None
        primary = next((Path(p) for p in paths if Path(p).suffix.lower() == '.pdf' and Path(p).stem == source.stem), None)
        if primary is None:
            raise GuardError('MEDIA_INSPECTION_INCOMPLETE', 'Не удалось определить основной PDF.', stage='result')
        try:
            from pypdf import PdfReader
            _cancel(cancel)
            with primary.open('rb') as stream:
                count = len(PdfReader(stream, strict=False).pages)
            _cancel(cancel)
        except GuardError:
            raise
        except Exception as error:
            raise GuardError('MEDIA_INSPECTION_INCOMPLETE', 'Не удалось проверить число страниц PDF.', stage='result') from error
        if count != before['pages']:
            raise GuardError('MEDIA_PAGE_LOSS', 'Экспорт TIFF в PDF изменил число страниц.', stage='result')
        return {'check': 'media_properties', 'status': 'checked_subset', 'qualityCompared': False,
                'source': before, 'result': {'format': 'pdf', 'pages': count}, 'expectedLosses': []}
    candidates = [Path(p) for p in paths if Path(p).suffix.lower() == '.'+target]
    primary = next((p for p in candidates if p.stem == source.stem), None)
    if primary is None:
        raise GuardError('MEDIA_INSPECTION_INCOMPLETE',
                         'Не удалось однозначно определить основной результат изображения.', stage='result')
    after = inspect_media(primary, cancel=cancel, validate_container=True)
    if after is None or after['format'] != ALIASES.get(target, target):
        raise GuardError('MEDIA_OUTPUT_INVALID',
                         'Контейнер изображения не соответствует выбранному формату результата.', stage='result')
    if target == 'apng' and not after.get('animationContainer'):
        raise GuardError('MEDIA_OUTPUT_INVALID',
                         'Вместо APNG получен PNG без структуры анимации.', stage='result')
    before = source_profile()
    expected_losses = []
    if before:
        if before.get('bitDepth', 8) > after.get('bitDepth', 8):
            if target in ('png', 'apng', 'tif', 'tiff'):
                raise GuardError('MEDIA_BIT_DEPTH_LOSS', 'Экспорт уменьшил глубину цвета изображения.', stage='result')
            expected_losses.append('bit_depth')
        if target == 'gif' and before['format'] != 'gif':
            expected_losses.append('gif_palette_and_binary_alpha')
        if before['frames'] > 1:
            if target in ANIMATED_TARGETS:
                if after['frames'] != before['frames']:
                    raise GuardError('MEDIA_ANIMATION_LOSS',
                                     'Экспорт изменил число кадров анимации. Результат не отмечен готовым.', stage='result')
                if before.get('loopCount', 1) != after.get('loopCount', 1):
                    raise GuardError('MEDIA_ANIMATION_LOOP_LOSS',
                                     'Экспорт изменил число повторов анимации.', stage='result')
                old, new = before['durationsMs'], after['durationsMs']
                if len(old) == len(new) and any(abs(a-b) > max(20, a*0.05) for a, b in zip(old, new)):
                    raise GuardError('MEDIA_ANIMATION_TIMING_LOSS',
                                     'Экспорт изменил длительности кадров анимации.', stage='result')
            else:
                expected_losses.append('animation')
        expected_pages = max(before['pages'], before['frames'])
        if target in ('tif', 'tiff') and expected_pages != after['pages']:
            raise GuardError('MEDIA_PAGE_LOSS', 'Экспорт изменил число страниц TIFF.', stage='result')
        alpha_loss = before['alpha'] and target in ALPHA_TARGETS and not after['alpha']
        if alpha_loss and target == 'gif':
            # GIF can represent 256 opaque colours or 255 plus transparency.
            # RGBA channel presence alone does not prove transparent pixels.
            from .qt_media import has_transparent_pixels
            alpha_loss = has_transparent_pixels(source, cancel=cancel)
        if alpha_loss:
            raise GuardError('MEDIA_ALPHA_LOSS',
                             'Экспорт удалил прозрачность в формате, поддерживающем альфа-канал.', stage='result')
        if before['alpha'] and target not in ALPHA_TARGETS:
            expected_losses.append('alpha')
        if display_dimensions(before) != display_dimensions(after):
            raise GuardError('MEDIA_CANVAS_CHANGED',
                             'Экспорт изменил размер холста изображения.', stage='result')
        if before['pages'] > 1 and target not in ('tif', 'tiff'):
            if target in ANIMATED_TARGETS and after['frames'] == before['pages']:
                expected_losses.append('page_semantics_as_frames')
            else:
                expected_losses.append('pages')
        if before.get('pageDimensions') and target in ('tif', 'tiff'):
            if display_page_dimensions(before) != display_page_dimensions(after):
                raise GuardError('MEDIA_CANVAS_CHANGED', 'Экспорт изменил размеры страниц TIFF.', stage='result')
    elif source.suffix.lower() == '.pdf' and target in ('tif', 'tiff'):
        try:
            from pypdf import PdfReader
            _cancel(cancel)
            with source.open('rb') as stream:
                count = len(PdfReader(stream, strict=False).pages)
            _cancel(cancel)
        except GuardError:
            raise
        except Exception as error:
            raise GuardError('MEDIA_INSPECTION_INCOMPLETE',
                             'Не удалось проверить число страниц PDF перед экспортом TIFF.', stage='result') from error
        before = {'format': 'pdf', 'pages': count}
        if count != after['pages']:
            raise GuardError('MEDIA_PAGE_LOSS', 'Экспорт PDF в TIFF изменил число страниц.', stage='result')
    return {'check': 'media_properties', 'status': 'checked_subset', 'qualityCompared': False,
            'source': before, 'result': after, 'expectedLosses': expected_losses}
