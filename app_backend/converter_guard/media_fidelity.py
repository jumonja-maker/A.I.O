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
ANIMATED_TARGETS = frozenset(('gif', 'apng', 'webp'))
ALPHA_TARGETS = frozenset(('png', 'apng', 'webp', 'tiff', 'tif'))
MAX_RECORDS = 65536


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
            'pages': pages, 'alpha': bool(alpha), 'durationsMs': durations or []}


def _png(r):
    if r.read(8) != b'\x89PNG\r\n\x1a\n':
        raise _Invalid('png_signature')
    width = height = 0
    alpha = False
    declared = None
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
            if len(head) < 32:
                head += block[:32-len(head)]
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
        elif kind == b'tRNS':
            alpha = True
        elif kind == b'acTL':
            if declared is not None or saw_data or length != 8:
                raise _Invalid('apng_control')
            declared = struct.unpack('>I', head[:4])[0]
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
    while True:
        r.record()
        block = r.read(1)
        if block == b';':
            if not durations:
                raise _Invalid('gif_frames')
            return _base('gif', width, height, frames=len(durations), alpha=alpha, durations=durations)
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
    durations = []
    image = False
    while r.file.tell() < r.size:
        r.record()
        kind, length = struct.unpack('<4sI', r.read(8))
        start = r.file.tell()
        head = r.read(min(length, 16))
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
    return _base('webp', width, height, frames=len(durations) or 1, alpha=alpha, durations=durations)


def _tiff(r):
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
    alpha = False
    sizes = {1: 1, 3: 2, 4: 4, 16: 8}
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
        for _ in range(count):
            r.record()
            entry = r.read(entry_size)
            tag, typ = struct.unpack(endian+'HH', entry[:4])
            if tag not in (254, 256, 257, 277, 338):
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
            values[tag] = struct.unpack(endian+codes[typ]*n, payload[:amount])
        offset = struct.unpack(endian+offset_code, r.read(inline_size))[0]
        if values.get(254, (0,))[0] & 1:  # Reduced-resolution thumbnails are not pages.
            continue
        width, height = values.get(256, (0,))[0], values.get(257, (0,))[0]
        if not width or not height:
            raise _Invalid('tiff_dimensions')
        dimensions.append([width, height])
        alpha |= any(v in (1, 2) for v in values.get(338, ()))
    if not dimensions:
        raise _Invalid('tiff_pages')
    result = _base('tiff', *dimensions[0], pages=len(dimensions), alpha=alpha)
    result['pageDimensions'] = dimensions
    return result


def _jpeg(r):
    if r.read(2) != b'\xff\xd8':
        raise _Invalid('jpeg_signature')
    while True:
        r.record()
        if r.read(1) != b'\xff':
            raise _Invalid('jpeg_marker')
        marker = r.read(1)[0]
        while marker == 0xff:
            marker = r.read(1)[0]
        if marker in (0xd9, 0xda):
            raise _Invalid('jpeg_dimensions')
        if marker in range(0xd0, 0xd8) or marker == 0x01:
            continue
        length = struct.unpack('>H', r.read(2))[0]
        if length < 2:
            raise _Invalid('jpeg_segment')
        if marker in (0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf):
            if length < 8:
                raise _Invalid('jpeg_sof')
            depth, height, width = struct.unpack('>BHH', r.read(5))
            return _base('jpeg', width, height)
        r.skip(length-2)


def _bmp(r):
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
    return _base('bmp', abs(width), abs(height))


def inspect_media(path, *, cancel=None):
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
        result = {'png': _png, 'gif': _gif, 'webp': _webp, 'tiff': _tiff,
                  'jpeg': _jpeg, 'bmp': _bmp}[kind](r)
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
    # JPEG/BMP metadata alone does not validate all compressed image bytes.
    # Identity is restricted to containers whose structure is fully traversed.
    if ALIASES.get(target, target) not in ('png', 'gif', 'webp', 'tiff'):
        return None
    info = inspect_media(source, cancel=cancel)
    if info is None or info['format'] != ALIASES.get(target, target):
        return None
    if target == 'apng' and not info.get('animationContainer'):
        return None
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


def check_media_preservation(paths, target, source, *, cancel):
    """Mandatory property check, independent of optional deep fidelity QA."""
    if target not in RASTER:
        return None
    source = Path(source)
    candidates = [Path(p) for p in paths if Path(p).suffix.lower() == '.'+target]
    primary = next((p for p in candidates if p.stem == source.stem), None)
    if primary is None:
        raise GuardError('MEDIA_INSPECTION_INCOMPLETE',
                         'Не удалось однозначно определить основной результат изображения.', stage='result')
    after = inspect_media(primary, cancel=cancel)
    if after is None or after['format'] != ALIASES.get(target, target):
        raise GuardError('MEDIA_OUTPUT_INVALID',
                         'Контейнер изображения не соответствует выбранному формату результата.', stage='result')
    if target == 'apng' and not after.get('animationContainer'):
        raise GuardError('MEDIA_OUTPUT_INVALID',
                         'Вместо APNG получен PNG без структуры анимации.', stage='result')
    before = inspect_media(source, cancel=cancel)
    expected_losses = []
    if before:
        if before['frames'] > 1:
            if target in ANIMATED_TARGETS:
                if after['frames'] != before['frames']:
                    raise GuardError('MEDIA_ANIMATION_LOSS',
                                     'Экспорт изменил число кадров анимации. Результат не отмечен готовым.', stage='result')
                old, new = before['durationsMs'], after['durationsMs']
                if len(old) == len(new) and any(abs(a-b) > max(20, a*0.05) for a, b in zip(old, new)):
                    raise GuardError('MEDIA_ANIMATION_TIMING_LOSS',
                                     'Экспорт изменил длительности кадров анимации.', stage='result')
            else:
                expected_losses.append('animation')
        if target in ('tif', 'tiff') and before['pages'] != after['pages']:
            raise GuardError('MEDIA_PAGE_LOSS', 'Экспорт изменил число страниц TIFF.', stage='result')
        if before['alpha'] and target in ALPHA_TARGETS and not after['alpha']:
            raise GuardError('MEDIA_ALPHA_LOSS',
                             'Экспорт удалил прозрачность в формате, поддерживающем альфа-канал.', stage='result')
        if before['alpha'] and target not in ALPHA_TARGETS:
            expected_losses.append('alpha')
        if (before['width'], before['height']) != (after['width'], after['height']):
            raise GuardError('MEDIA_CANVAS_CHANGED',
                             'Экспорт изменил размер холста изображения.', stage='result')
        if before.get('pageDimensions') and target in ('tif', 'tiff'):
            if before['pageDimensions'] != after.get('pageDimensions'):
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
