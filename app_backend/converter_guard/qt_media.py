"""Stream raster frames through the application's existing Qt image codecs.

Qt decodes/composes GIF/WebP and encodes lossless WebP. APNG/GIF/TIFF framing
below keeps every decoded frame/page rather than exporting a Draw page. No new
image library is used for ordinary sources. PCX and Sun RAS use a separate,
content-confirmed Pillow decoder to avoid LibreOffice's white Draw canvas.
Cancellation and bounded pixel allocation are checked between stages.
"""
from fractions import Fraction
import os
import math
import re
from pathlib import Path
import struct
import tempfile
from itertools import chain
import zlib

from .errors import GuardError
from .media_fidelity import ALIASES, RASTER, _cancel, display_dimensions, inspect_media

# Qt's own decoder allocation limit remains active as a second boundary. This
# bounds our additional RGBA canvas/disposal copies before decoding allocation.
MAX_CANVAS_PIXELS = 64 * 1024 * 1024
MAX_PNG_AUX_BYTES = 16 * 1024 * 1024


def _qt():
    try:
        from PySide6.QtCore import QByteArray, QBuffer, QIODevice, Qt
        from PySide6.QtGui import QImage, QImageReader, QImageWriter, QPainter
    except ImportError as error:
        raise GuardError('MEDIA_QT_UNAVAILABLE',
                         'Компонент Qt для сохранения кадров недоступен.', stage='engine') from error
    return QByteArray, QBuffer, QIODevice, Qt, QImage, QImageReader, QImageWriter, QPainter


def _bounds(width, height):
    if width <= 0 or height <= 0 or width * height > MAX_CANVAS_PIXELS:
        raise GuardError('MEDIA_CANVAS_BUDGET',
                         'Холст изображения превышает безопасный бюджет обработки Qt.', stage='engine')



_PNM_MAGICS = {
    'pbm': frozenset((b'P1', b'P4')),
    'pgm': frozenset((b'P2', b'P5')),
    'ppm': frozenset((b'P3', b'P6')),
}


def qt_pnm_profile(source, *, cancel=None):
    """Small, content-confirmed Qt route for plain portable bitmaps.

    This is only attempted for the three image suffixes and does not decode
    the raster in preflight. Qt independently confirms format and dimensions.
    Unknown sources continue through the existing native LibreOffice path.
    """
    source = Path(source)
    kind = source.suffix.lower().lstrip('.')
    signatures = _PNM_MAGICS.get(kind)
    if signatures is None:
        return None
    _cancel(cancel)
    with source.open('rb') as stream:
        prefix = stream.read(3)
    if len(prefix) < 3 or prefix[:2] not in signatures or prefix[2] not in b' \r\n\t':
        return None
    QImageReader = _qt()[5]
    reader = QImageReader(str(source))
    observed = bytes(reader.format()).decode('ascii', errors='ignore').lower()
    if observed != kind or not reader.canRead():
        return None
    size = reader.size()
    _bounds(size.width(), size.height())
    # PBM is a real 1-bit format. Treating it as 8-bit caused our mandatory
    # media check to reject a lossless 1-bit PNG as a depth reduction.
    bit_depth = 1
    if kind != 'pbm':
        # Only the bounded header is needed to distinguish 8- from 16-bit PNM.
        # Never interpret 16-bit input as an 8-bit source.
        with source.open('rb') as stream:
            header = stream.read(4096)
        values = []
        for row in header.splitlines():
            values.extend(row.split(b'#', 1)[0].split())
            if len(values) >= 4:
                break
        try:
            header_width, header_height, maxval = map(int, values[1:4])
        except (ValueError, TypeError):
            return None
        if (len(values) < 4 or header_width != size.width()
                or header_height != size.height() or not 1 <= maxval <= 65535):
            return None
        bit_depth = 16 if maxval > 255 else 8
    _cancel(cancel)
    return {'format': kind, 'width': size.width(), 'height': size.height(),
            'frames': 1, 'pages': 1, 'alpha': False, 'durationsMs': [],
            'orientation': 1, 'bitDepth': bit_depth}


def qt_xbm_xpm_profile(source, *, cancel=None):
    """Use the existing Qt decoder for bounded, content-confirmed X bitmaps.

    Only XBM/XPM suffixes pay for the declaration check; normal documents
    incur no extra work. No pixel decode occurs during preflight. Unknown or
    unparseable declarations remain on the original native LibreOffice route.
    """
    source = Path(source)
    kind = source.suffix.lower().lstrip('.')
    if kind not in ('xbm', 'xpm'):
        return None
    _cancel(cancel)
    with source.open('rb') as stream:
        prefix = stream.read(65536)
    if kind == 'xbm':
        width = re.search(rb'(?m)^#define[ \t]+[A-Za-z0-9_]+_width[ \t]+([0-9]+)[ \t]*$', prefix)
        height = re.search(rb'(?m)^#define[ \t]+[A-Za-z0-9_]+_height[ \t]+([0-9]+)[ \t]*$', prefix)
        if not width or not height:
            return None
        declared = (int(width.group(1)), int(height.group(1)))
        alpha, depth = False, 1
    else:
        if not prefix.lstrip().startswith(b'/* XPM */'):
            return None
        rows = re.findall(rb'"([^"\r\n]{1,2048})"', prefix)
        if not rows:
            return None
        parts = rows[0].split()
        if len(parts) != 4 or any(not part.isdigit() for part in parts):
            return None
        width, height, colors, cpp = (int(part) for part in parts)
        if not 1 <= colors <= 4096 or not 1 <= cpp <= 8 or len(rows) < colors + 1:
            return None
        palette = rows[1:colors + 1]
        if any(len(row) < cpp or not re.search(rb'(?i)(?:^|\s)c\s+\S+', row[cpp:])
               for row in palette):
            return None
        alpha = any(re.search(rb'(?i)(?:^|\s)c\s+none(?:\s|$)', row[cpp:])
                    for row in palette)
        declared, depth = (width, height), 8
    _bounds(*declared)
    QImageReader = _qt()[5]
    reader = QImageReader(str(source))
    observed = bytes(reader.format()).decode('ascii', errors='ignore').lower()
    if observed != kind or not reader.canRead():
        return None
    size = reader.size()
    if (size.width(), size.height()) != declared or reader.imageCount() > 1:
        return None
    _cancel(cancel)
    return {'format': kind, 'width': size.width(), 'height': size.height(),
            'frames': 1, 'pages': 1, 'alpha': bool(alpha),
            'durationsMs': [], 'orientation': 1, 'bitDepth': depth}



_LEGACY_PIL_FORMATS = {'pcx': 'PCX', 'ras': 'SUN'}
# Pillow RGBA plus the owned QImage copy: avoid unbounded dual pixel buffers.
_LEGACY_PIL_PIXELS = 16 * 1024 * 1024


def pillow_legacy_profile(source, *, cancel=None):
    """Admit PCX/RAS only with a plausible binary header and Pillow metadata.

    Ordinary office files and other images do not import Pillow or make any
    additional passes. Preflight opens the header only, not the pixel raster.
    """
    source = Path(source)
    kind = source.suffix.lower().lstrip('.')
    if kind not in _LEGACY_PIL_FORMATS:
        return None
    _cancel(cancel)
    with source.open('rb') as stream:
        header = stream.read(128)
        total = stream.seek(0, 2)
    if kind == 'pcx':
        if len(header) != 128 or header[0] != 10 or header[2] != 1:
            return None
        xmin, ymin, xmax, ymax = struct.unpack_from('<HHHH', header, 4)
        depth, planes = header[3], header[65]
        stride = struct.unpack_from('<H', header, 66)[0]
        if (xmax < xmin or ymax < ymin or not stride
                or depth not in (1, 2, 4, 8) or planes not in (1, 3, 4)
                or total <= 128):
            return None
        width, height = xmax - xmin + 1, ymax - ymin + 1
        if stride * 8 < width * depth:
            return None
    else:
        if len(header) < 32 or header[:4] != b'\x59\xa6\x6a\x95':
            return None
        _, width, height, depth, data_length, encoding, map_type, map_length = (
            struct.unpack_from('>8I', header))
        if (encoding not in (0, 1, 2, 3) or depth not in (1, 4, 8, 24, 32)
                or map_type not in (0, 1) or map_length > 1024
                or (map_length and map_type != 1) or total <= 32 + map_length):
            return None
    _bounds(width, height)
    if width * height > _LEGACY_PIL_PIXELS:
        raise GuardError('MEDIA_CANVAS_BUDGET',
                         'Растр PCX/RAS превышает бюджет памяти декодера.',
                         stage='preflight')
    try:
        from PIL import Image
    except ImportError as error:
        raise GuardError('MEDIA_CODEC_UNAVAILABLE',
                         'Графический декодер PCX/RAS недоступен.',
                         stage='preflight') from error
    try:
        with Image.open(source) as image:
            if (image.format != _LEGACY_PIL_FORMATS[kind]
                    or image.size != (width, height)
                    or getattr(image, 'n_frames', 1) != 1):
                raise ValueError('legacy_image_model_mismatch')
            alpha = 'A' in image.getbands()
    except (OSError, ValueError, SyntaxError) as error:
        raise GuardError('MEDIA_SOURCE_INVALID',
                         'Изображение PCX/RAS не подтвердилось декодером.',
                         stage='preflight') from error
    _cancel(cancel)
    return {'format': kind, 'width': width, 'height': height,
            'frames': 1, 'pages': 1, 'alpha': alpha, 'durationsMs': [],
            'orientation': 1, 'bitDepth': min(depth, 8)}


def media_frame_route(source, target, *, cancel=None):
    """Return a content-based route description, or None for a non-raster route."""
    if target not in RASTER:
        return None
    info = (inspect_media(source, cancel=cancel) or qt_pnm_profile(source, cancel=cancel)
            or qt_xbm_xpm_profile(source, cancel=cancel)
            or pillow_legacy_profile(source, cancel=cancel))
    if info is None:
        return None
    _bounds(info['width'], info['height'])
    return {'source': info, 'target': ALIASES.get(target, target),
            'executionMode': 'qt_media_frames'}



def supported_media_targets(info):
    """Targets available from currently loaded Qt codecs, without decoding."""
    _, _, _, _, _, QImageReader, QImageWriter, _ = _qt()
    if info.get('format') == 'pdf':
        try:
            from PySide6.QtPdf import QPdfDocument  # noqa: F401
        except ImportError:
            return ()
        return ('tif', 'tiff')
    formats = {bytes(value).decode('ascii') for value in QImageReader.supportedImageFormats()}
    writers = {bytes(value).decode('ascii') for value in QImageWriter.supportedImageFormats()}
    source_kind = info['format']
    targets = set()
    # Byte-preserving containers do not depend on a decoder plugin.
    if source_kind in ('png', 'gif', 'webp', 'tiff'):
        targets.add(source_kind)
        if source_kind == 'tiff': targets.add('tif')
        if info.get('animationContainer'): targets.add('apng')
    if source_kind in _LEGACY_PIL_FORMATS:
        formats.add(source_kind)  # Pillow provides source decode; Qt writes output.
    if source_kind not in formats:
        return tuple(sorted(targets))
    targets.update(('png', 'apng', 'gif', 'tif', 'tiff'))
    targets.update(writers.intersection(('jpeg', 'jpg', 'bmp', 'webp')))
    # JPEG aliases use the same live Qt JPEG encoder. Leaving these out
    # silently sent JFIF/JIF/JPE through Draw's page export, changing the
    # pixel canvas even though an installed image encoder could preserve it.
    if 'jpeg' in writers or 'jpg' in writers:
        targets.update(('jpeg', 'jpg', 'jpe', 'jfif', 'jif'))
    if source_kind == 'tiff':
        targets.add('pdf')
    return tuple(sorted(targets))


def media_preflight(source, *, cancel=None):
    """Content summary and loaded-codec targets, without exporting/decoding."""
    source = Path(source)
    _cancel(cancel)
    info = (inspect_media(source, cancel=cancel) or qt_pnm_profile(source, cancel=cancel)
            or qt_xbm_xpm_profile(source, cancel=cancel)
            or pillow_legacy_profile(source, cancel=cancel))
    if info is None:
        with source.open('rb') as stream:
            magic = stream.read(5)
        if magic != b'%PDF-':
            return None
        try:
            from PySide6.QtPdf import QPdfDocument
        except ImportError:
            return None
        document = QPdfDocument(None)
        try:
            error = document.load(str(source))
            _cancel(cancel)
            if error != QPdfDocument.Error.None_ or document.pageCount() <= 0:
                return None
            size = document.pagePointSize(0)
            info = {'format': 'pdf', 'pages': document.pageCount(), 'frames': 1,
                    'width': math.ceil(size.width() * 2), 'height': math.ceil(size.height() * 2),
                    'alpha': False, 'durationsMs': [], 'rasterDpi': 144}
        finally:
            document.close()
    _bounds(info['width'], info['height'])
    targets = supported_media_targets(info)
    decoder = ('pillow_legacy' if info['format'] in _LEGACY_PIL_FORMATS else
               'manual_apng_qt_png' if info.get('animationContainer') else 'qt_image_reader')
    if info['format'] in _LEGACY_PIL_FORMATS:
        can_read = True
    elif info['format'] != 'pdf':
        reader = _qt()[5](str(source))
        can_read = reader.canRead()
        if not can_read:
            # Still expose exact-copy targets; never claim a missing plugin
            # supports a cross-container conversion.
            canonical = info['format']
            targets = tuple(t for t in targets if ALIASES.get(t, t) == canonical
                            and (t != 'apng' or info.get('animationContainer')))
    else:
        can_read = True
        decoder = 'qt_pdf_pages'
    _cancel(cancel)
    return {'source': info, 'targets': list(targets), 'decoder': decoder,
            'decoderAvailable': can_read, 'exported': False}

def _png_chunk(stream, kind, payload):
    stream.write(struct.pack('>I', len(payload)))
    stream.write(kind)
    stream.write(payload)
    stream.write(struct.pack('>I', zlib.crc32(payload, zlib.crc32(kind)) & 0xffffffff))


def _riff_chunk(kind, payload):
    return kind + struct.pack('<I', len(payload)) + payload + (b'\0' if len(payload) & 1 else b'')


def _u24(value):
    if not 0 <= value <= 0xffffff:
        raise GuardError('MEDIA_CONTAINER_LIMIT',
                         'Размер или длительность кадра не представимы в WebP.', stage='engine')
    return int(value).to_bytes(3, 'little')


def _rgba_rows(image, cancel):
    QImage = _qt()[4]
    rgba = image.convertToFormat(QImage.Format.Format_RGBA8888)
    bits, stride, row_bytes = rgba.constBits(), rgba.bytesPerLine(), rgba.width() * 4
    for y in range(rgba.height()):
        _cancel(cancel)
        yield bytes(bits[y * stride:y * stride + row_bytes])


def _apng_frames(source, scratch, info, cancel):
    """Rebuild one PNG subframe on disk, then apply APNG blend/disposal."""
    _, _, _, Qt, QImage, QImageReader, _, QPainter = _qt()
    width, height = info['width'], info['height']
    canvas = QImage(width, height, QImage.Format.Format_RGBA8888)
    canvas.fill(Qt.GlobalColor.transparent)
    frame_path = scratch / 'apng-frame.png'
    aux = []
    aux_size = 0
    ihdr = None
    current = None
    frame_file = None

    def composite(control):
        nonlocal canvas
        _, fw, fh, x, y, num, den, dispose, blend = control
        reader = QImageReader(str(frame_path))
        image = reader.read()
        if image.isNull() or (image.width(), image.height()) != (fw, fh):
            raise GuardError('MEDIA_DECODE_FAILED',
                             'Qt не смог прочитать кадр APNG.', stage='engine',
                             diagnostic=reader.errorString())
        if image.colorSpace().isValid() and not canvas.colorSpace().isValid():
            canvas.setColorSpace(image.colorSpace())
        if canvas.colorSpace().isValid() and image.colorSpace().isValid() and image.colorSpace() != canvas.colorSpace():
            image = image.convertedToColorSpace(canvas.colorSpace())
        previous = canvas.copy() if dispose == 2 else None
        painter = QPainter(canvas)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver
                                   if blend else QPainter.CompositionMode.CompositionMode_Source)
        painter.drawImage(x, y, image)
        painter.end()
        yield canvas
        if dispose == 1:
            painter = QPainter(canvas)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
            painter.fillRect(x, y, fw, fh, Qt.GlobalColor.transparent)
            painter.end()
        elif dispose == 2:
            canvas = previous

    try:
        with Path(source).open('rb') as stream:
            stream.read(8)
            while True:
                _cancel(cancel)
                header = stream.read(8)
                if len(header) != 8:
                    raise GuardError('MEDIA_DECODE_FAILED', 'Оборванная структура APNG.', stage='engine')
                size, kind = struct.unpack('>I4s', header)
                if kind == b'fcTL':
                    payload = stream.read(size)
                    stream.read(4)
                    if current is not None:
                        _png_chunk(frame_file, b'IEND', b'')
                        frame_file.close()
                        frame_file = None
                        yield from composite(current)
                    current = struct.unpack('>IIIIIHHBB', payload)
                    frame_file = frame_path.open('wb')
                    frame_file.write(b'\x89PNG\r\n\x1a\n')
                    _png_chunk(frame_file, b'IHDR', struct.pack('>II', current[1], current[2]) + ihdr[8:])
                    for akind, apayload in aux:
                        _png_chunk(frame_file, akind, apayload)
                    continue
                if kind in (b'IDAT', b'fdAT'):
                    if current is None:
                        stream.seek(size + 4, 1)  # APNG fallback image is not an animation frame.
                        continue
                    if kind == b'fdAT':
                        stream.read(4)
                        size -= 4
                    frame_file.write(struct.pack('>I4s', size, b'IDAT'))
                    crc = zlib.crc32(b'IDAT')
                    remaining = size
                    while remaining:
                        _cancel(cancel)
                        block = stream.read(min(65536, remaining))
                        if not block:
                            raise GuardError('MEDIA_DECODE_FAILED', 'Оборванный кадр APNG.', stage='engine')
                        frame_file.write(block)
                        crc = zlib.crc32(block, crc)
                        remaining -= len(block)
                    frame_file.write(struct.pack('>I', crc & 0xffffffff))
                    stream.read(4)
                    continue
                if kind == b'IHDR':
                    ihdr = stream.read(size)
                    stream.read(4)
                elif kind in (b'PLTE', b'tRNS', b'gAMA', b'cHRM', b'sRGB', b'iCCP') and current is None:
                    aux_size += size
                    if aux_size > MAX_PNG_AUX_BYTES:
                        raise GuardError('MEDIA_METADATA_BUDGET',
                                         'Метаданные PNG превышают бюджет обработки.', stage='engine')
                    aux.append((kind, stream.read(size)))
                    stream.read(4)
                elif kind == b'IEND':
                    if current is not None:
                        _png_chunk(frame_file, b'IEND', b'')
                        frame_file.close()
                        frame_file = None
                        yield from composite(current)
                    return
                else:
                    stream.seek(size + 4, 1)
    finally:
        if frame_file is not None:
            frame_file.close()
        frame_path.unlink(missing_ok=True)



def _orient_image(image, orientation):
    if orientation == 1:
        return image
    from PySide6.QtGui import QTransform
    if orientation in (2, 5, 7):
        image = image.mirrored(True, False)
    elif orientation == 4:
        return image.mirrored(False, True)
    rotation = {3: 180, 5: 270, 6: 90, 7: 90, 8: 270}.get(orientation)
    return image.transformed(QTransform().rotate(rotation)) if rotation else image

def _frames(source, scratch, info, cancel):
    if info.get('format') in _LEGACY_PIL_FORMATS:
        from PIL import Image
        QImage = _qt()[4]
        with Image.open(source) as decoded:
            _cancel(cancel)
            if (decoded.format != _LEGACY_PIL_FORMATS[info['format']]
                    or decoded.size != (info['width'], info['height'])):
                raise GuardError('MEDIA_SOURCE_CHANGED',
                                 'PCX/RAS изменился после проверки.',
                                 stage='engine')
            decoded.load()
            _cancel(cancel)
            pixels = decoded.convert('RGBA').tobytes()
            width, height = decoded.size
            if len(pixels) != width * height * 4:
                raise GuardError('MEDIA_DECODE_FAILED',
                                 'Неполный кадр PCX/RAS.', stage='engine')
            frame = QImage(pixels, width, height, width * 4,
                           QImage.Format.Format_RGBA8888).copy()
            if frame.isNull():
                raise GuardError('MEDIA_DECODE_FAILED',
                                 'Не удалось передать пиксели PCX/RAS в Qt.',
                                 stage='engine')
            del pixels
            _cancel(cancel)
            yield frame
        return
    if info.get('animationContainer'):
        for image in _apng_frames(source, scratch, info, cancel):
            yield _orient_image(image, info.get('orientation', 1))
        return
    QImageReader = _qt()[5]
    reader = QImageReader(str(source))
    expected = max(info['frames'], info['pages'])
    for index in range(expected):
        _cancel(cancel)
        if info.get('format') == 'tiff':
            # Qt's TIFF plugin advances its internal IFD while read() returns
            # the current page. A subsequent absolute jump can then fail even
            # though that IFD is valid. Start from the file header for each
            # validated page index; keep streaming animations on their reader.
            reader = QImageReader(str(source))
        # TIFF thumbnails occupy native IFD indexes but are not document pages.
        native_index = info.get('pageImageIndexes', range(expected))[index]
        if native_index and not reader.supportsAnimation() and not reader.jumpToImage(native_index):
            raise GuardError('MEDIA_DECODE_FAILED',
                             'Qt не смог перейти к следующей странице изображения.', stage='engine')
        size = reader.size()
        _bounds(size.width(), size.height())
        image = reader.read()
        if image.isNull():
            raise GuardError('MEDIA_DECODE_FAILED',
                             'Qt не смог прочитать все кадры или страницы изображения.', stage='engine',
                             diagnostic=reader.errorString())
        _bounds(image.width(), image.height())
        if info.get('format') == 'xbm':
            # Qt's monochrome XBM reader maps a set source bit to white;
            # XBM specifies a set bit as black. Flip in-place once after
            # decoding, without a second raster buffer or another file pass.
            image.invertPixels()
        orientation = info.get('pageOrientations', [info.get('orientation', 1)] * expected)[index]
        yield _orient_image(image, orientation)


def _write_apng(stream, frames, info, cancel):
    width, height = info['width'], info['height']
    count = max(info['frames'], info['pages'])
    stream.write(b'\x89PNG\r\n\x1a\n')
    _png_chunk(stream, b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0))
    _png_chunk(stream, b'acTL', struct.pack('>II', count, info.get('loopCount', 1)))
    sequence = 0
    first_profile = None
    for index, image in enumerate(frames):
        _cancel(cancel)
        if (image.width(), image.height()) != (width, height):
            raise GuardError('MEDIA_PAGE_GEOMETRY',
                             'APNG требует одинаковый размер всех страниц; используйте TIFF.', stage='engine')
        profile = bytes(image.colorSpace().iccProfile()) if image.colorSpace().isValid() else b''
        if first_profile is None:
            first_profile = profile
        elif profile != first_profile:
            raise GuardError('MEDIA_COLOR_PROFILE_MISMATCH',
                             'APNG хранит один цветовой профиль для всех кадров; страницы имеют разные профили. Используйте TIFF.', stage='engine')
        if index == 0 and profile:
            _png_chunk(stream, b'iCCP', b'Qt ICC Profile\0\0' + zlib.compress(profile))
        duration = info['durationsMs'][index] if info['frames'] > 1 else 0
        delay = Fraction(duration / 1000).limit_denominator(65535)
        if delay.numerator > 65535:
            raise GuardError('MEDIA_CONTAINER_LIMIT', 'Длительность кадра не представима в APNG.', stage='engine')
        _png_chunk(stream, b'fcTL', struct.pack('>IIIIIHHBB', sequence, width, height, 0, 0,
                                             delay.numerator, delay.denominator, 0, 0))
        sequence += 1
        compressor = zlib.compressobj()
        for row in _rgba_rows(image, cancel):
            block = compressor.compress(b'\0' + row)
            if block:
                if index == 0:
                    _png_chunk(stream, b'IDAT', block)
                else:
                    _png_chunk(stream, b'fdAT', struct.pack('>I', sequence) + block)
                    sequence += 1
        block = compressor.flush()
        if index == 0:
            _png_chunk(stream, b'IDAT', block)
        else:
            _png_chunk(stream, b'fdAT', struct.pack('>I', sequence) + block)
            sequence += 1
    _png_chunk(stream, b'IEND', b'')


def _lossless_webp(image):
    QByteArray, QBuffer, QIODevice, _, _, _, QImageWriter, _ = _qt()
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    writer = QImageWriter(buffer, b'webp')
    writer.setQuality(100)
    if not writer.write(image):
        raise GuardError('MEDIA_ENCODE_FAILED', 'Qt не смог записать WebP.', stage='engine',
                         diagnostic=writer.errorString())
    raw = bytes(data)
    # Do not assume that a plugin silently falling back to lossy output is safe.
    chunks = []
    pos = 12
    while pos + 8 <= len(raw):
        kind, size = struct.unpack('<4sI', raw[pos:pos + 8])
        payload = raw[pos + 8:pos + 8 + size]
        if kind == b'VP8 ':
            raise GuardError('MEDIA_LOSSLESS_UNAVAILABLE',
                             'Установленный Qt не поддерживает WebP без потерь.', stage='engine')
        if kind in (b'VP8L', b'ALPH'):
            chunks.append(_riff_chunk(kind, payload))
        pos += 8 + size + (size & 1)
    if not any(chunk[:4] == b'VP8L' for chunk in chunks):
        raise GuardError('MEDIA_ENCODE_FAILED', 'Qt вернул неполный WebP.', stage='engine')
    return b''.join(chunks)


def _write_webp(stream, frames, info, cancel):
    width, height = info['width'], info['height']
    animated = max(info['frames'], info['pages']) > 1
    first = next(frames)
    profile = bytes(first.colorSpace().iccProfile()) if first.colorSpace().isValid() else b''
    stream.write(b'RIFF\0\0\0\0WEBP')
    flags = (0x10 if info['alpha'] else 0) | (2 if animated else 0) | (0x20 if profile else 0)
    stream.write(_riff_chunk(b'VP8X', bytes((flags, 0, 0, 0)) + _u24(width - 1) + _u24(height - 1)))
    if profile:
        stream.write(_riff_chunk(b'ICCP', profile))
    if animated:
        loops = info.get('loopCount', 1)
        if loops > 65535:
            raise GuardError('MEDIA_CONTAINER_LIMIT', 'Число повторов не представимо в WebP.', stage='engine')
        stream.write(_riff_chunk(b'ANIM', struct.pack('<IH', 0, loops)))
    for index, image in enumerate(chain((first,), frames)):
        _cancel(cancel)
        if (image.width(), image.height()) != (width, height):
            raise GuardError('MEDIA_PAGE_GEOMETRY',
                             'WebP требует одинаковый размер всех кадров; используйте TIFF.', stage='engine')
        frame_profile = bytes(image.colorSpace().iccProfile()) if image.colorSpace().isValid() else b''
        if frame_profile != profile:
            raise GuardError('MEDIA_COLOR_PROFILE_MISMATCH',
                             'WebP хранит один цветовой профиль для всех кадров; страницы имеют разные профили. Используйте TIFF.', stage='engine')
        encoded = _lossless_webp(image)
        if animated:
            duration = info['durationsMs'][index] if info['frames'] > 1 else 0
            if abs(round(duration) - duration) > 0.001:
                raise GuardError('MEDIA_TIMING_UNREPRESENTABLE',
                                 'Длительность кадра не представима в целых миллисекундах WebP.', stage='engine')
            frame = b'\0' * 6 + _u24(width - 1) + _u24(height - 1) + _u24(round(duration)) + b'\x02'
            stream.write(_riff_chunk(b'ANMF', frame + encoded))
        else:
            stream.write(encoded)
    end = stream.tell()
    if end - 8 > 0xffffffff:
        raise GuardError('MEDIA_CONTAINER_LIMIT', 'Размер WebP превышает предел RIFF.', stage='engine')
    stream.seek(4)
    stream.write(struct.pack('<I', end - 8))
    stream.seek(end)


def _gif_frame(stream, image, delay, cancel, reserve_transparency):
    from PySide6.QtGui import QColorSpace
    if image.colorSpace().isValid() and image.colorSpace() != QColorSpace(QColorSpace.NamedColorSpace.SRgb):
        raise GuardError('MEDIA_GIF_COLORSPACE_UNREPRESENTABLE',
                         'GIF не сохраняет цветовой профиль этого изображения; выберите PNG, APNG, WebP или TIFF.', stage='engine')
    # A transparent animation needs a transparent background even on its
    # fully opaque frames, so disposal before the next frame clears correctly.
    palette = [b'\0\0\0'] if reserve_transparency else []
    lookup = {b'\0\0\0\0': 0} if reserve_transparency else {}
    transparent = 0 if reserve_transparency else None
    # Store one index per pixel, rather than another full RGBA image.
    indices = bytearray()
    for row in _rgba_rows(image, cancel):
        for pos in range(0, len(row), 4):
            pixel = row[pos:pos + 4]
            if pixel[3] not in (0, 255):
                raise GuardError('MEDIA_GIF_ALPHA_UNREPRESENTABLE',
                                 'GIF не сохраняет полупрозрачность; выберите APNG или WebP.', stage='engine')
            key = b'\0\0\0\0' if pixel[3] == 0 else pixel
            index = lookup.get(key)
            if index is None:
                index = len(palette)
                if index == 256:
                    raise GuardError('MEDIA_GIF_PALETTE_UNREPRESENTABLE',
                                     'В кадре больше 256 цветов; GIF без потери цветов невозможен. Выберите APNG или WebP.',
                                     stage='engine')
                lookup[key] = index
                palette.append(key[:3])
                if pixel[3] == 0:
                    transparent = index
            indices.append(index)
    if transparent is not None and transparent != 0:
        # Match the logical-screen background index so disposal clears prior
        # opaque pixels. A fully opaque 256-colour image keeps all 256 entries.
        palette[0], palette[transparent] = palette[transparent], palette[0]
        mapping = bytearray(range(256))
        mapping[0], mapping[transparent] = transparent, 0
        indices = indices.translate(bytes(mapping))
        transparent = 0
    if abs(round(delay / 10) * 10 - delay) > 0.001 or delay > 655350:
        raise GuardError('MEDIA_TIMING_UNREPRESENTABLE',
                         'GIF хранит задержки с шагом 10 мс; точное сохранение этого кадра невозможно.', stage='engine')
    # Each frame replaces the canvas. Disposal restores transparent background
    # before the following full-canvas frame, preventing ghost pixels.
    flags = 8 | (1 if transparent is not None else 0)
    stream.write(b'!\xf9\x04' + struct.pack('<BHB', flags, round(delay / 10), transparent or 0) + b'\0')
    bits = max(1, (len(palette) - 1).bit_length())
    stream.write(b',' + struct.pack('<HHHHB', 0, 0, image.width(), image.height(), 0x80 | (bits - 1)))
    stream.write(b''.join(palette) + b'\0\0\0' * ((1 << bits) - len(palette)))
    minimum = max(2, bits)
    stream.write(bytes((minimum,)))
    clear, width = 1 << minimum, minimum + 1
    # Literal LZW with frequent clear codes avoids a large per-frame dictionary.
    # This is valid lossless GIF framing; no quantisation or dithering occurs.
    block, accumulator, available = bytearray(), 0, 0

    def code(value):
        nonlocal accumulator, available, block
        accumulator |= value << available
        available += width
        while available >= 8:
            block.append(accumulator & 255)
            accumulator >>= 8
            available -= 8
            if len(block) == 255:
                stream.write(b'\xff' + block)
                block = bytearray()

    limit = max(1, clear - 3)
    for start in range(0, len(indices), limit):
        _cancel(cancel)
        code(clear)
        for value in indices[start:start + limit]:
            code(value)
    code(clear + 1)
    if available:
        block.append(accumulator & 255)
    if block:
        stream.write(bytes((len(block),)) + block)
    stream.write(b'\0')


def _write_gif(stream, frames, info, cancel):
    width, height = info['width'], info['height']
    if width > 65535 or height > 65535:
        raise GuardError('MEDIA_CONTAINER_LIMIT', 'Размер холста не представим в GIF.', stage='engine')
    stream.write(b'GIF89a' + struct.pack('<HHBBB', width, height, 0x80, 0, 0) + b'\0\0\0\0\0\0')
    loops = info.get('loopCount', 1)
    if loops != 1:
        repeats = 0 if loops == 0 else loops - 1
        if repeats > 65535:
            raise GuardError('MEDIA_CONTAINER_LIMIT', 'Число повторов не представимо в GIF.', stage='engine')
        stream.write(b'!\xff\x0bNETSCAPE2.0\x03\x01' + struct.pack('<H', repeats) + b'\0')
    for index, image in enumerate(frames):
        if (image.width(), image.height()) != (width, height):
            raise GuardError('MEDIA_PAGE_GEOMETRY', 'GIF требует одинаковый размер всех кадров.', stage='engine')
        delay = info['durationsMs'][index] if info['frames'] > 1 else 0
        _gif_frame(stream, image, delay, cancel, info['alpha'])
    stream.write(b';')


def _write_tiff(stream, frames, info, cancel):
    """Write a linked classic TIFF IFD per page; pixel rows remain uncompressed."""
    stream.write(b'II\x2a\0\0\0\0\0')
    previous_pointer = 4
    for image in frames:
        _cancel(cancel)
        width, height = image.width(), image.height()
        pixel_offset = stream.tell()
        for row in _rgba_rows(image, cancel):
            stream.write(row)
        bits_offset = stream.tell()
        stream.write(struct.pack('<4H', 8, 8, 8, 8))
        resolution_offsets = []
        for dpm in (image.dotsPerMeterX(), image.dotsPerMeterY()):
            resolution_offsets.append(stream.tell())
            dpi = Fraction(max(1, dpm) * 254, 10000).limit_denominator(10000)
            stream.write(struct.pack('<II', dpi.numerator, dpi.denominator))
        profile = bytes(image.colorSpace().iccProfile()) if image.colorSpace().isValid() else b''
        profile_offset = stream.tell()
        stream.write(profile)
        if stream.tell() & 1:
            stream.write(b'\0')
        ifd_offset = stream.tell()
        if ifd_offset + 150 > 0xffffffff:
            raise GuardError('MEDIA_CONTAINER_LIMIT', 'TIFF превышает предел контейнера 4 ГиБ.', stage='engine')
        stream.seek(previous_pointer)
        stream.write(struct.pack('<I', ifd_offset))
        stream.seek(ifd_offset)
        tags = [(256, 4, 1, width), (257, 4, 1, height), (258, 3, 4, bits_offset),
                (259, 3, 1, 1), (262, 3, 1, 2), (273, 4, 1, pixel_offset),
                (277, 3, 1, 4), (278, 4, 1, height), (279, 4, 1, width * height * 4),
                (282, 5, 1, resolution_offsets[0]), (283, 5, 1, resolution_offsets[1]),
                (284, 3, 1, 1), (296, 3, 1, 2), (338, 3, 1, 2)]
        if profile:
            tags.append((34675, 7, len(profile), profile_offset))
        stream.write(struct.pack('<H', len(tags)))
        for tag in tags:
            stream.write(struct.pack('<HHII', *tag))
        previous_pointer = stream.tell()
        stream.write(b'\0\0\0\0')



def has_transparent_pixels(source, *, cancel=None):
    """Check actual alpha values without retaining the animation in memory."""
    info = inspect_media(source, cancel=cancel)
    if info is None or not info['alpha']:
        return False
    with tempfile.TemporaryDirectory(prefix='aio-alpha-') as folder:
        frames = _frames(source, Path(folder), info, cancel)
        try:
            for image in frames:
                for row in _rgba_rows(image, cancel):
                    if any(a != 255 for a in row[3::4]):
                        return True
        finally:
            frames.close()
    return False

def convert_media_frames(source, output_dir, target, *, cancel=None, on_started=None):
    """Return output paths, or None when this helper does not own the route.

    The caller performs ordinary Guard source admission first. Its run-control
    object can enforce the existing timeout/output quota through ``is_set``.
    """
    source, output_dir = Path(source), Path(output_dir)
    route = media_frame_route(source, target, cancel=cancel)
    if route is None:
        return None
    info = route['source']
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / (source.stem + '.' + target)
    if output.resolve() == source.resolve():
        raise GuardError('MEDIA_IDENTITY_PATH', 'Исходник и результат должны храниться отдельно.', stage='result')
    partial = output.with_name(output.name + '.part')
    if on_started:
        on_started()
    try:
        with tempfile.TemporaryDirectory(prefix='.qt-frames-', dir=output_dir) as folder:
            normal = ALIASES.get(target, target)
            if normal == 'gif' and info['alpha']:
                # Alpha-channel presence does not prove actual transparent
                # pixels. A bounded first pass avoids rejecting an opaque
                # 256-colour RGBA animation that GIF can represent exactly.
                probe = _frames(source, Path(folder), info, cancel)
                actual_alpha = False
                try:
                    for image in probe:
                        if any(any(a != 255 for a in row[3::4]) for row in _rgba_rows(image, cancel)):
                            actual_alpha = True
                            break
                finally:
                    probe.close()
                info = {**info, 'alpha': actual_alpha}
            frames = _frames(source, Path(folder), info, cancel)
            writer_info = {**info, 'width': display_dimensions(info)[0], 'height': display_dimensions(info)[1]}
            try:
                with partial.open('xb') as stream:
                    if normal == 'png' and (target == 'apng' or max(info['frames'], info['pages']) > 1):
                        _write_apng(stream, frames, writer_info, cancel)
                    elif normal == 'webp':
                        _write_webp(stream, frames, writer_info, cancel)
                    elif normal == 'gif':
                        _write_gif(stream, frames, writer_info, cancel)
                    elif normal == 'tiff':
                        _write_tiff(stream, frames, info, cancel)
                    else:
                        # Static-only containers explicitly take the first frame;
                        # Guard records animation/page loss in resultChecks.
                        image = next(frames)
                        if normal == 'bmp' and image.colorSpace().isValid():
                            from PySide6.QtGui import QColorSpace
                            if image.colorSpace() != QColorSpace(QColorSpace.NamedColorSpace.SRgb):
                                raise GuardError('MEDIA_COLOR_PROFILE_UNREPRESENTABLE',
                                                 'BMP не сохраняет цветовой профиль этого изображения; выберите PNG, WebP или TIFF.', stage='engine')
                        QByteArray, QBuffer, QIODevice, _, _, _, QImageWriter, _ = _qt()
                        data = QByteArray()
                        buffer = QBuffer(data)
                        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
                        writer = QImageWriter(buffer, normal.encode('ascii'))
                        if not writer.write(image):
                            raise GuardError('MEDIA_ENCODE_FAILED', 'Qt не смог записать изображение.',
                                             stage='engine', diagnostic=writer.errorString())
                        stream.write(bytes(data))
                    _cancel(cancel)
                    stream.flush()
                # Always inspect before publishing even the private output name.
                actual = inspect_media(partial, cancel=cancel)
                if actual is None or actual['format'] != normal:
                    raise GuardError('MEDIA_ENCODE_FAILED', 'Контейнер результата не прошёл проверку.', stage='result')
                if (info['format'] in _LEGACY_PIL_FORMATS
                        and (actual['width'], actual['height']) != (info['width'], info['height'])):
                    raise GuardError('MEDIA_CANVAS_CHANGED',
                                     'Результат PCX/RAS изменил размер исходного холста.', stage='result')
                os.replace(partial, output)
            finally:
                frames.close()
    finally:
        partial.unlink(missing_ok=True)
    return [output]



def _pdf_frames(source, cancel, *, dpi=144):
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtPdf import QPdfDocument
    if QGuiApplication.instance() is None:
        raise GuardError('MEDIA_QT_APPLICATION_REQUIRED',
                         'Для растеризации PDF требуется работающий графический Qt.', stage='engine')
    document = QPdfDocument(None)
    try:
        error = document.load(str(source))
        if error != QPdfDocument.Error.None_ or document.pageCount() <= 0:
            raise GuardError('MEDIA_DECODE_FAILED', 'Qt не смог открыть страницы PDF.', stage='engine')
        for index in range(document.pageCount()):
            _cancel(cancel)
            size = document.pagePointSize(index)
            width, height = math.ceil(size.width() * dpi / 72), math.ceil(size.height() * dpi / 72)
            _bounds(width, height)
            image = document.render(index, QSize(width, height))
            _cancel(cancel)
            if image.isNull():
                raise GuardError('MEDIA_DECODE_FAILED', 'Qt не смог отрисовать страницу PDF.', stage='engine')
            image.setDotsPerMeterX(round(dpi / 0.0254))
            image.setDotsPerMeterY(round(dpi / 0.0254))
            yield image
    finally:
        document.close()


def _write_pdf(path, frames, cancel, info=None):
    from PySide6.QtCore import QMarginsF, QSizeF, QRectF
    from PySide6.QtGui import QColorSpace, QPageSize, QPainter, QPdfWriter
    writer = QPdfWriter(str(path))
    writer.setResolution(72)
    painter = None
    try:
        for index, image in enumerate(frames):
            _cancel(cancel)
            if image.colorSpace().isValid() and image.colorSpace() != QColorSpace(QColorSpace.NamedColorSpace.SRgb):
                raise GuardError('MEDIA_PDF_COLORSPACE_UNREPRESENTABLE',
                                 'Qt PDF не сохраняет исходный цветовой профиль этой страницы; используйте TIFF.', stage='engine')
            dpi = (info or {}).get('pageDpi', [])
            resolution = dpi[index] if index < len(dpi) and dpi[index] else [72, 72]
            orientation = (info or {}).get('pageOrientations', [1] * (index + 1))[index]
            if orientation >= 5:
                resolution = resolution[::-1]
            size = QPageSize(QSizeF(image.width() * 72 / resolution[0], image.height() * 72 / resolution[1]), QPageSize.Unit.Point)
            if not writer.setPageSize(size) or not writer.setPageMargins(QMarginsF(0, 0, 0, 0)):
                raise GuardError('MEDIA_ENCODE_FAILED', 'Qt не принял размер страницы PDF.', stage='engine')
            if painter is None:
                painter = QPainter(writer)
                painter.setRenderHint(QPainter.RenderHint.LosslessImageRendering, True)
                if not painter.isActive():
                    raise GuardError('MEDIA_ENCODE_FAILED', 'Qt не смог начать запись PDF.', stage='engine')
            elif not writer.newPage():
                raise GuardError('MEDIA_ENCODE_FAILED', 'Qt не смог добавить страницу PDF.', stage='engine')
            painter.drawImage(QRectF(0, 0, writer.width(), writer.height()), image)
            _cancel(cancel)
        if painter is None:
            raise GuardError('MEDIA_DECODE_FAILED', 'В изображении нет страниц.', stage='engine')
    finally:
        if painter is not None:
            painter.end()
        # QPdfWriter finalises its cross-reference table upon destruction.
        del writer


def convert_document_pages(source, output_dir, target, *, cancel=None, on_started=None):
    """Only PDF→TIFF rasterisation and TIFF→PDF page assembly; no PDF→PDF."""
    source, output_dir = Path(source), Path(output_dir)
    target = target.lower()
    if target not in ('pdf', 'tif', 'tiff'):
        return None
    proof = media_preflight(source, cancel=cancel)
    if proof is None:
        return None
    info = proof['source']
    if not (info['format'] == 'pdf' and target in ('tif', 'tiff') or
            info['format'] == 'tiff' and target == 'pdf'):
        return None
    if target not in proof['targets']:
        raise GuardError('MEDIA_QT_CODEC_UNAVAILABLE',
                         'Установленный Qt не содержит декодер страниц TIFF.', stage='engine')
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / (source.stem + '.' + target)
    partial = output.with_name(output.name + '.part')
    if on_started:
        on_started()
    try:
        with tempfile.TemporaryDirectory(prefix='.qt-pages-', dir=output_dir) as folder:
            frames = (_pdf_frames(source, cancel) if info['format'] == 'pdf' else
                      _frames(source, Path(folder), info, cancel))
            try:
                if target == 'pdf':
                    _write_pdf(partial, frames, cancel, info)
                    from pypdf import PdfReader
                    with partial.open('rb') as stream:
                        count = len(PdfReader(stream).pages)
                    if count != info['pages']:
                        raise GuardError('MEDIA_PAGE_LOSS', 'Qt изменил число страниц PDF.', stage='result')
                else:
                    with partial.open('xb') as stream:
                        _write_tiff(stream, frames, info, cancel)
                    actual = inspect_media(partial, cancel=cancel)
                    if actual['pages'] != info['pages']:
                        raise GuardError('MEDIA_PAGE_LOSS', 'Qt изменил число страниц TIFF.', stage='result')
                _cancel(cancel)
                os.replace(partial, output)
            finally:
                frames.close()
    finally:
        partial.unlink(missing_ok=True)
    return [output]
