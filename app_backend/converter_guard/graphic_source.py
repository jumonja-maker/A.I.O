"""Small content declarations for binary images which LO may open as text.

No pixel scan, extension-based admission, or second office import. This probe
only pins the expected document family; the installed importer must prove it.
"""
from pathlib import Path
import struct

DRAW = 'com.sun.star.drawing.DrawingDocument'
FOOTER = b'TRUEVISION-XFILE.\x00'


def tga_contract(source, *, cancel=None):
    from .errors import GuardError
    if cancel is not None and cancel.is_set():
        raise GuardError('CANCELLED', 'Проверка изображения отменена.', stage='preflight')
    try:
        with Path(source).open('rb') as stream:
            h = stream.read(18)
            if len(h) != 18:
                return None
            size = stream.seek(0, 2)
            stream.seek(max(0, size - 26))
            footer = stream.read(26)
    except OSError:
        return None
    ident, cmap, kind = h[:3]
    first, count, cmap_depth = struct.unpack_from('<HHB', h, 3)
    width, height, depth, descriptor = struct.unpack_from('<HHBB', h, 12)
    signature = len(footer) == 26 and footer[8:] == FOOTER
    # TGA 1 has no magic: require a complete exact-sized uncompressed layout.
    # TGA 2's footer permits RLE; its native decoder still validates the data.
    if kind not in (1, 2, 3, 9, 10, 11) or cmap not in (0, 1) or not width or not height:
        return None
    if descriptor & 0xc0:
        return None
    if kind in (1, 9):
        valid = cmap == 1 and count > 0 and depth in (8, 16) and cmap_depth in (15, 16, 24, 32)
    elif kind in (2, 10):
        valid = cmap == 0 and count == 0 and depth in (15, 16, 24, 32)
    else:
        valid = cmap == 0 and count == 0 and depth in (8, 16)
    if not valid:
        return None
    start = 18 + ident + (count * ((cmap_depth + 7) // 8) if cmap else 0)
    minimum = start + width * height * ((depth + 7) // 8)
    end = size - 26 if signature else size
    if start >= end:
        return None
    if kind < 9:
        if (not signature and minimum != end) or (signature and minimum > end):
            return None
    elif not signature:
        return None
    if signature:
        extension, developer = struct.unpack_from('<II', footer)
        if any(offset and not start <= offset < size - 26 for offset in (extension, developer)):
            return None
    return {'state': 'recognized', 'contentClass': 'tga_image',
            'expectedServices': [DRAW], 'nativeImportRequired': True,
            'width': width, 'height': height, 'bitDepth': depth,
            'basis': 'tga_header_and_footer' if signature else 'tga_exact_uncompressed_layout'}


def tga_import_hint(contract, native):
    if contract.get('contentClass') != 'tga_image' or not native.valid:
        return None
    candidates = sorted(name for name, item in native.filters.items()
                        if item[0] == DRAW and 'tga' in item[1] and 'IMPORT' in item[2])
    preferred = [name for name in candidates if 'DEFAULT' in native.filters[name][2]]
    choices = preferred if len(preferred) == 1 else candidates
    if len(choices) != 1:
        return None
    return {'filter': choices[0], 'options': None, 'basis': 'content_confirmed_tga'}
