"""Narrow repair of a reproducible LibreOffice BMP file-header size defect.

This is only for private, completed native outputs, never user source files.
A 24-bit BITMAPINFOHEADER export can declare the size of 32-bit pixels while
storing a complete, correctly sized 24-bit image. Only bfSize is corrected;
all pixel, geometry, colour and ancillary bytes remain byte-identical. Any
other discrepancy stays subject to the unchanged strict container validator.
"""
import hashlib
import os
from pathlib import Path
import secrets
import struct

from .errors import GuardError
from .media_fidelity import _cancel, inspect_media


def repair_native_bmp_size(paths, *, cancel=None):
    """Return recorded header repairs, or [] for unaffected/unknown cases.

    The owning adapter must restrict this call to native LibreOffice outputs.
    It is deliberately not invoked by the generic source/container inspector.
    """
    repaired = []
    for value in paths:
        _cancel(cancel)
        path = Path(value)
        if path.suffix.lower() != '.bmp' or path.is_symlink() or not path.is_file():
            continue
        with path.open('rb') as source:
            stamp = os.fstat(source.fileno())
            header = source.read(54)
            if len(header) != 54 or header[:2] != b'BM' or header[6:10] != b'\0' * 4:
                continue
            declared, offset, dib = (int.from_bytes(header[start:start + 4], 'little')
                                     for start in (2, 10, 14))
            width, height, planes, depth, compression, image_size = struct.unpack('<iiHHII', header[18:38])
            colours_used, important = struct.unpack('<II', header[46:54])
            if (dib != 40 or offset != 54 or planes != 1 or depth != 24 or compression != 0
                    or width <= 0 or height == 0 or colours_used != 0 or important != 0):
                continue
            rows = abs(height)
            pixels = ((width * 24 + 31) // 32) * 4 * rows
            expected = offset + pixels
            wrong_32bit_size = offset + width * 4 * rows
            if (stamp.st_size != expected or image_size != pixels or declared == expected
                    or declared != wrong_32bit_size or expected > 0xffffffff):
                continue
            # Read and write the same opened source inode in bounded blocks;
            # the original hash is obtained during the necessary copy pass.
            source.seek(0)
            original_hash = hashlib.sha256()
            partial = path.with_name('.' + path.name + '.bmp-header-' + secrets.token_hex(6) + '.part')
            try:
                with partial.open('xb') as output:
                    position = 0
                    while True:
                        _cancel(cancel)
                        block = source.read(64 * 1024)
                        if not block:
                            break
                        original_hash.update(block)
                        if position == 0:
                            block = block[:2] + expected.to_bytes(4, 'little') + block[6:]
                        output.write(block)
                        position += len(block)
                    output.flush()
                current = path.stat()
                after = os.fstat(source.fileno())
                identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
                if (identity(current) != identity(stamp) or identity(after) != identity(stamp)
                        or position != stamp.st_size):
                    raise GuardError('ENGINE_OUTPUT_CHANGED',
                                     'Нативный BMP изменился во время проверки заголовка.', stage='result')
                # Windows does not permit atomic replacement while our own
                # read handle still has the destination open. Finish both
                # inode checks first, then close before inspecting/publishing
                # the private candidate. The enclosing context may safely
                # close an already closed file on every exception path.
                source.close()
                inspect_media(partial, cancel=cancel, validate_container=True)
                _cancel(cancel)
                if identity(path.stat()) != identity(stamp):
                    raise GuardError('ENGINE_OUTPUT_CHANGED',
                                     'Нативный BMP изменился перед исправлением заголовка.', stage='result')
                os.replace(partial, path)
                repaired.append({'check': 'native_bmp_header_size', 'status': 'repaired_header',
                    'qualityCompared': False, 'file': path.name,
                    'declaredSizeBefore': declared, 'actualSize': expected,
                    'headerField': 'bfSize', 'headerBytesTouched': 4,
                    'pixelBytesChanged': 0, 'sourceOutputSha256': original_hash.hexdigest(),
                    'width': width, 'height': height, 'bitDepth': depth})
            finally:
                partial.unlink(missing_ok=True)
    return repaired
