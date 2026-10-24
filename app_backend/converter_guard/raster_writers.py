"""Streaming GIF89a and BigTIFF writers using the application's Qt pixels.

Only one decoded frame is retained. GIF uses its native palette/binary-alpha
limits; BigTIFF stores uncompressed, unassociated RGBA without page loss.
"""
import math
from pathlib import Path
import struct

from .errors import GuardError


def _cancelled(cancel):
    if cancel is not None and cancel.is_set():
        raise GuardError('CANCELLED', 'Сохранение кадров отменено.', stage='result')


def _rgba(image):
    from PySide6.QtGui import QImage
    if image.isNull() or image.width() <= 0 or image.height() <= 0:
        raise GuardError('MEDIA_DECODE_FAILED', 'Не удалось прочитать кадр изображения.', stage='result')
    return image.convertToFormat(QImage.Format.Format_RGBA8888)


def _palette(image, cancel, *, reserve_transparency=False):
    """Exact small palettes, otherwise a bounded weighted median-cut palette."""
    pixels = image.constBits()
    total = image.width()*image.height()
    exact = {}
    many = False
    transparent = reserve_transparency
    for i in range(total):
        if not i % 16384:
            _cancelled(cancel)
        p = i*4
        if pixels[p+3] < 128:
            transparent = True
        elif not many:
            value = (pixels[p] << 16) | (pixels[p+1] << 8) | pixels[p+2]
            exact[value] = None
            many = len(exact) > 256
        if many and transparent:
            break
    limit = 255 if transparent else 256
    if not many and len(exact) <= limit:
        return list(exact) or [0], transparent, False

    histogram = {}
    step = max(1, math.ceil(total/65536))
    for i in range(0, total, step):
        if not i % 16384:
            _cancelled(cancel)
        p = i*4
        if pixels[p+3] < 128:
            continue
        red, green, blue = pixels[p], pixels[p+1], pixels[p+2]
        key = (red >> 3, green >> 3, blue >> 3)
        value = histogram.setdefault(key, [0, 0, 0, 0])
        value[0] += 1
        value[1] += red
        value[2] += green
        value[3] += blue
    bins = [(key, value) for key, value in histogram.items()]
    if not bins:
        return [0], transparent, True
    boxes = [bins]
    while len(boxes) < limit:
        _cancelled(cancel)
        choices = []
        for n, box in enumerate(boxes):
            if len(box) < 2:
                continue
            ranges = [max(e[0][c] for e in box)-min(e[0][c] for e in box) for c in range(3)]
            axis = max(range(3), key=ranges.__getitem__)
            choices.append((sum(e[1][0] for e in box)*ranges[axis]**2, n, axis))
        if not choices:
            break
        _, n, axis = max(choices)
        box = sorted(boxes.pop(n), key=lambda e:e[0][axis])
        midpoint = sum(e[1][0] for e in box)/2
        weight = 0
        split = 1
        for split in range(1, len(box)):
            weight += box[split-1][1][0]
            if weight >= midpoint:
                break
        boxes.extend((box[:split], box[split:]))
    palette = []
    for box in boxes:
        count = sum(e[1][0] for e in box)
        channels = [round(sum(e[1][c] for e in box)/count) for c in (1, 2, 3)]
        palette.append((channels[0] << 16) | (channels[1] << 8) | channels[2])
    return palette, transparent, True


def _indexed(image, cancel, *, reserve_transparency=False):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage
    image = _rgba(image)
    palette, transparent, quantized = _palette(image, cancel,
                                              reserve_transparency=reserve_transparency)
    flags = (Qt.ImageConversionFlag.DiffuseDither if quantized else
             Qt.ImageConversionFlag.ThresholdDither)
    # Qt's custom palette lookup maps RGB, so add transparency only *after*
    # lookup; otherwise opaque near-black pixels may select a transparent slot.
    indexed = image.convertToFormat(QImage.Format.Format_Indexed8,
                                    [0xff000000 | colour for colour in palette], flags)
    if indexed.isNull():
        raise GuardError('MEDIA_ENCODE_FAILED', 'Не удалось подготовить палитру GIF.', stage='result')
    # Logical-screen background index zero and every frame's transparency
    # index must agree. Qt's GIF compositor otherwise restores an opaque
    # preceding frame's colour behind later transparent pixels.
    transparent_index = 0 if transparent else None
    if transparent:
        original, mapped = image.constBits(), indexed.bits()
        stride = indexed.bytesPerLine()
        for y in range(image.height()):
            _cancelled(cancel)
            for x in range(image.width()):
                if original[(y*image.width()+x)*4+3] < 128:
                    mapped[y*stride+x] = 0
                else:
                    mapped[y*stride+x] += 1
        indexed.setColorTable([0]+[0xff000000 | colour for colour in palette])
        palette.insert(0, 0)
    return indexed, palette, transparent_index


class _GifBits:
    def __init__(self, output):
        self.output = output
        self.value = self.bits = 0
        self.block = bytearray()

    def emit(self, value, bits):
        self.value |= value << self.bits
        self.bits += bits
        while self.bits >= 8:
            self.block.append(self.value & 255)
            self.value >>= 8
            self.bits -= 8
            if len(self.block) == 255:
                self.output.write(b'\xff'+self.block)
                self.block.clear()

    def finish(self):
        if self.bits:
            self.block.append(self.value & 255)
        if self.block:
            self.output.write(bytes((len(self.block),))+self.block)
        self.output.write(b'\x00')


def _gif_lzw(output, image, minimum, cancel):
    clear, end = 1 << minimum, (1 << minimum)+1
    next_code, width = end+1, minimum+1
    dictionary = {}
    bits = _GifBits(output)
    bits.emit(clear, width)
    prefix = None
    data, stride = image.constBits(), image.bytesPerLine()
    for y in range(image.height()):
        _cancelled(cancel)
        for symbol in data[y*stride:y*stride+image.width()]:
            if prefix is None:
                prefix = symbol
                continue
            key = (prefix << 8) | symbol
            code = dictionary.get(key)
            if code is not None:
                prefix = code
                continue
            # GIF decoding learns one dictionary entry after the encoder.
            if next_code > (1 << width) and width < 12:
                width += 1
            bits.emit(prefix, width)
            if next_code < 4096:
                dictionary[key] = next_code
                next_code += 1
            else:
                bits.emit(clear, width)
                dictionary.clear()
                next_code, width = end+1, minimum+1
            prefix = symbol
    if prefix is not None:
        if next_code > (1 << width) and width < 12:
            width += 1
        bits.emit(prefix, width)
        # The final pixel code lets the decoder learn the last encoder entry.
        if next_code == (1 << width) and width < 12:
            width += 1
    bits.emit(end, width)
    bits.finish()


def write_gif(output_path, frames, *, width, height, loop=None, cancel=None,
              reserve_transparency=False):
    """Write full-canvas frames, native GIF delays and a per-frame palette.

    ``loop`` is GIF's repeat-count field: None omits the extension, zero loops
    forever. GIF inherently quantizes colour and alpha; callers report this.
    Set ``reserve_transparency`` after a bounded pre-pass when any frame has
    transparent pixels; all frames then share transparent background index 0.
    """
    if not 0 < width <= 65535 or not 0 < height <= 65535:
        raise GuardError('MEDIA_FORMAT_LIMIT', 'Размер холста не представим в формате GIF.', stage='result')
    if loop is not None and (type(loop) is not int or not 0 <= loop <= 65535):
        raise GuardError('MEDIA_FORMAT_LIMIT', 'Число повторов не представимо в формате GIF.', stage='result')
    path = Path(output_path)
    count = 0
    created = False
    try:
        with path.open('xb') as output:
            created = True
            output.write(b'GIF89a'+struct.pack('<HHBBB', width, height, 0x70, 0, 0))
            if loop is not None:
                output.write(b'!\xff\x0bNETSCAPE2.0\x03\x01'+struct.pack('<H', loop)+b'\x00')
            for image, duration in frames:
                _cancelled(cancel)
                if image.width() != width or image.height() != height:
                    raise GuardError('MEDIA_CANVAS_CHANGED', 'Кадр не совпадает с холстом GIF.', stage='result')
                indexed, palette, transparent = _indexed(image, cancel,
                    reserve_transparency=reserve_transparency)
                size_bits = max(1, (len(palette)-1).bit_length())
                colour_count = 1 << size_bits
                # Disposal 2 clears the preceding full-canvas frame, including
                # transparent pixels; frames must never overlay old content.
                ticks = round(duration/10)
                if not 0 <= ticks <= 65535:
                    raise GuardError('MEDIA_FORMAT_LIMIT', 'Длительность кадра не представима в GIF.', stage='result')
                flags = 8 | (transparent is not None)
                output.write(b'!\xf9\x04'+struct.pack('<BHB', flags, ticks, transparent or 0)+b'\x00')
                output.write(b','+struct.pack('<HHHHB', 0, 0, width, height, 0x80 | (size_bits-1)))
                output.write(b''.join(bytes(((v >> 16)&255, (v >> 8)&255, v&255)) for v in palette))
                output.write(b'\x00\x00\x00'*(colour_count-len(palette)))
                minimum = max(2, size_bits)
                output.write(bytes((minimum,)))
                _gif_lzw(output, indexed, minimum, cancel)
                count += 1
            if not count:
                raise GuardError('MEDIA_DECODE_FAILED', 'В исходнике не найдено кадров.', stage='result')
            output.write(b';')
        return count
    except BaseException:
        if created:
            path.unlink(missing_ok=True)
        raise


def write_tiff(output_path, frames, *, cancel=None):
    """Stream all pages into BigTIFF, with exact 8-bit unassociated RGBA."""
    path = Path(output_path)
    count = 0
    created = False
    try:
        with path.open('xb') as output:
            created = True
            output.write(b'II'+struct.pack('<HHH', 43, 8, 0)+struct.pack('<Q', 0))
            previous_next = 8
            for image, _duration in frames:
                _cancelled(cancel)
                image = _rgba(image)
                width, height = image.width(), image.height()
                if width > 0xffffffff or height > 0xffffffff:
                    raise GuardError('MEDIA_FORMAT_LIMIT', 'Размер страницы не представим в TIFF.', stage='result')
                # Every offset/count is 64-bit; page data can cross 4 GiB.
                alignment = (-output.tell()) % 8
                output.write(b'\x00'*alignment)
                current = output.tell()
                output.seek(previous_next)
                output.write(struct.pack('<Q', current))
                output.seek(current)
                x_dpm, y_dpm = image.dotsPerMeterX(), image.dotsPerMeterY()
                physical = x_dpm > 0 and y_dpm > 0
                # Keep the decoded image's physical resolution. A raster
                # encoder must not silently relabel every source as 300 DPI.
                x_resolution = (x_dpm*254, 10000) if physical else (1, 1)
                y_resolution = (y_dpm*254, 10000) if physical else (1, 1)
                tags = [(254, 4, 1, struct.pack('<I', 2)),
                        (256, 4, 1, struct.pack('<I', width)),
                        (257, 4, 1, struct.pack('<I', height)),
                        (258, 3, 4, struct.pack('<HHHH', 8, 8, 8, 8)),
                        (259, 3, 1, struct.pack('<H', 1)),
                        (262, 3, 1, struct.pack('<H', 2)),
                        (273, 16, 1, b''),  # Patched below after IFD size is known.
                        (274, 3, 1, struct.pack('<H', 1)),
                        (277, 3, 1, struct.pack('<H', 4)),
                        (278, 4, 1, struct.pack('<I', height)),
                        (279, 16, 1, struct.pack('<Q', width*height*4)),
                        (282, 5, 1, struct.pack('<II', *x_resolution)),
                        (283, 5, 1, struct.pack('<II', *y_resolution)),
                        (284, 3, 1, struct.pack('<H', 1)),
                        (296, 3, 1, struct.pack('<H', 2 if physical else 1)),
                        (338, 3, 1, struct.pack('<H', 2)),
                        (339, 3, 4, struct.pack('<HHHH', 1, 1, 1, 1))]
                pixel_offset = current+8+20*len(tags)+8
                output.write(struct.pack('<Q', len(tags)))
                for tag, typ, amount, value in tags:
                    if tag == 273:
                        value = struct.pack('<Q', pixel_offset)
                    output.write(struct.pack('<HHQ', tag, typ, amount)+value.ljust(8, b'\x00'))
                previous_next = output.tell()
                output.write(struct.pack('<Q', 0))
                pixels, stride = image.constBits(), image.bytesPerLine()
                for y in range(height):
                    _cancelled(cancel)
                    output.write(pixels[y*stride:y*stride+width*4])
                count += 1
            if not count:
                raise GuardError('MEDIA_DECODE_FAILED', 'В исходнике не найдено страниц.', stage='result')
        return count
    except BaseException:
        if created:
            path.unlink(missing_ok=True)
        raise
