"""Bounded exact decoder for one self-contained PICT v2 RGB DirectBitsRect.

Mixed/vector drawings, comments/profiles, masks, transforms and unhandled
opcodes stay on the guarded native route. This is not a general PICT renderer.
"""
import hashlib
from pathlib import Path
import struct

from .errors import GuardError

MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_PIXELS = 16 * 1024 * 1024


class _Unsupported(Exception):
    pass


def _poll(cancel):
    if cancel is not None and cancel.is_set():
        raise GuardError('CANCELLED', 'Чтение RGB PICT отменено.', stage='engine')


def inspect_pict_bitmap(source, *, cancel=None, decode=False):
    """Validate every row, with constant row memory unless decode is requested."""
    source = Path(source)
    _poll(cancel)
    total = source.stat().st_size
    if total > MAX_FILE_BYTES:
        return None
    try:
        with source.open('rb') as stream:
            def read(count):
                _poll(cancel)
                if count < 0 or stream.tell()+count > total:
                    raise ValueError('pict_bitmap_section_exceeds_file')
                raw = stream.read(count)
                if len(raw) != count:
                    raise ValueError('pict_bitmap_truncated')
                return raw
            head = stream.read(552)
            if (len(head) < 552 or head[522:528] != b'\0\x11\x02\xff\x0c\0'
                    or head[528:532] != b'\xff\xfe\0\0'):
                return None
            frame = struct.unpack_from('>4h', head, 514)
            top, left, bottom, right = frame
            width, height = right-left, bottom-top
            if top or left or width <= 0 or height <= 0:
                return None
            if width * height > MAX_PIXELS:
                raise GuardError('MEDIA_CANVAS_BUDGET', 'Растр RGB PICT превышает бюджет памяти.', stage='preflight')
            resolution = struct.unpack_from('>II', head, 532)
            if (struct.unpack_from('>4h', head, 540) != frame
                    or head[548:552] != bytes(4) or not all(resolution)):
                return None
            opcode = read(2)
            if opcode == b'\0\1':
                clip = read(10)
                if struct.unpack_from('>H', clip)[0] != 10 or struct.unpack_from('>4h',clip,2) != frame:
                    raise _Unsupported()
                opcode = read(2)
            if opcode != b'\0\x9a':
                raise _Unsupported()
            if read(4) not in (bytes(4), b'\0\0\0\xff'):
                raise _Unsupported()
            pixmap = read(46)
            row_word = struct.unpack_from('>H', pixmap)[0]
            if (row_word & 0xc000 != 0x8000 or row_word & 0x3fff != width*4
                    or struct.unpack_from('>4h',pixmap,2) != frame
                    or struct.unpack_from('>HHI',pixmap,10) != (0,4,0)
                    or struct.unpack_from('>II',pixmap,18) != resolution
                    or struct.unpack_from('>4H',pixmap,26) != (16,32,3,8)
                    or pixmap[34:] != bytes(12)):
                raise _Unsupported()
            rectangles = read(18)
            if (struct.unpack_from('>4h',rectangles) != frame
                    or struct.unpack_from('>4h',rectangles,8) != frame
                    or struct.unpack_from('>H',rectangles,16)[0] not in (0,64)):
                raise _Unsupported()
            pixels = bytearray() if decode else None
            digest = hashlib.sha256()
            for _row in range(height):
                _poll(cancel)
                packed = read(int.from_bytes(read(2 if width*4 > 250 else 1),'big'))
                planar = bytearray()
                position = 0
                while position < len(packed):
                    _poll(cancel)
                    control = packed[position]
                    position += 1
                    if control < 128:
                        count = control+1
                        if position+count > len(packed):
                            raise ValueError('pict_packbits_literal_truncated')
                        planar.extend(packed[position:position+count])
                        position += count
                    elif control > 128:
                        if position == len(packed):
                            raise ValueError('pict_packbits_repeat_truncated')
                        planar.extend(bytes((packed[position],))*(257-control))
                        position += 1
                    if len(planar) > width*3:
                        raise ValueError('pict_packbits_row_overflow')
                if len(planar) != width*3:
                    raise ValueError('pict_packbits_row_incomplete')
                row = bytearray(width*3)
                row[0::3], row[1::3], row[2::3] = planar[:width], planar[width:2*width], planar[2*width:]
                digest.update(row)
                if pixels is not None:
                    pixels.extend(row)
            if stream.tell() % 2:
                read(1)
            if read(2) != b'\0\xff':
                raise _Unsupported()
            while stream.tell() < total:
                block = read(min(65536,total-stream.tell()))
                _poll(cancel)
                if any(block):
                    raise _Unsupported()
        profile = {'format':'pict_bitmap','width':width,'height':height,'frames':1,'pages':1,
            'alpha':False,'durationsMs':[],'orientation':1,'bitDepth':8,
            'dpi':[value/65536 for value in resolution],
            'dpiBasis':'matching_PICT_v2_extended_header_and_PixMap_fixed_16_16',
            'rgbSha256':digest.hexdigest(),'exactBitmapSubset':True}
        return (profile, bytes(pixels)) if decode else profile
    except _Unsupported:
        return None
    except (ValueError, struct.error) as error:
        raise GuardError('MEDIA_SOURCE_INVALID', 'Структура RGB PICT повреждена.', stage='preflight') from error


def verify_pict_bitmap_pixels(output, profile, *, cancel=None):
    """Read back actual encoded RGB bytes before the output is published."""
    from PIL import Image
    _poll(cancel)
    digest = hashlib.sha256()
    with Image.open(output) as image:
        if image.size != (profile['width'],profile['height']) or getattr(image,'n_frames',1) != 1:
            raise GuardError('MEDIA_PIXEL_LOSS', 'Не подтверждён RGB-растр результата PICT.', stage='result')
        # Pillow independently decodes Qt's encoded output, including TIFF
        # when the current Qt installation has no TIFF reader plugin.
        for top in range(0,image.height,64):
            _poll(cancel)
            with image.crop((0,top,image.width,min(image.height,top+64))) as block:
                if 'A' in block.getbands() or 'transparency' in image.info:
                    with block.convert('RGBA') as rgba:
                        if rgba.getchannel('A').getextrema() != (255,255):
                            raise GuardError('MEDIA_PIXEL_LOSS', 'Непрозрачный RGB PICT получил прозрачные пиксели.', stage='result')
                digest.update(block.convert('RGB').tobytes())
    if digest.hexdigest() != profile['rgbSha256']:
        raise GuardError('MEDIA_PIXEL_LOSS', 'Экспорт изменил RGB-пиксели PICT. Исходник сохранён.', stage='result')
