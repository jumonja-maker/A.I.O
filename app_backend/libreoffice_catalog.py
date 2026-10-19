"""Presentation catalog from the installed LibreOffice export registry.

This is discovery, not a source/target permission table. Unknown source types
are sent unchanged to LibreOffice. The UI must never reject a route by family.
"""
from __future__ import annotations

from functools import lru_cache
import mimetypes
import os
from pathlib import Path
import re
import xml.etree.ElementTree as ET

OOR = '{http://openoffice.org/2001/registry}'
# Used only when an installation does not expose its registry. These names are
# suggestions, never an allowlist: convert_path accepts any valid extension.
COMMON = {
    'pdf': ('documents', 'Документ PDF'),
    'docx': ('documents', 'Документ Word DOCX'),
    'doc': ('documents', 'Документ Word DOC'),
    'odt': ('documents', 'Текстовый документ OpenDocument'),
    'rtf': ('documents', 'Документ Rich Text'),
    'txt': ('documents', 'Текстовый документ'),
    'html': ('documents', 'Веб-страница HTML'),
    'xlsx': ('spreadsheets', 'Книга Excel XLSX'),
    'xls': ('spreadsheets', 'Таблица Excel XLS'),
    'ods': ('spreadsheets', 'Таблица OpenDocument'),
    'csv': ('spreadsheets', 'Таблица CSV'),
    'pptx': ('documents', 'Презентация PowerPoint'),
    'ppt': ('documents', 'Презентация PowerPoint'),
    'odp': ('documents', 'Презентация OpenDocument'),
    'png': ('images', 'Изображение PNG'),
    'jpg': ('images', 'Изображение JPEG'),
    'jpeg': ('images', 'Изображение JPEG'),
    'bmp': ('images', 'Изображение BMP'),
    'gif': ('images', 'Изображение GIF'),
    'tif': ('images', 'Изображение TIFF'),
    'tiff': ('images', 'Изображение TIFF'),
    'svg': ('images', 'Векторное изображение SVG'),
    'webp': ('images', 'Изображение WebP'),
}
EXTENSION = re.compile(r'[a-z0-9][a-z0-9_-]*\Z')


def properties(node):
    return {p.get(OOR + 'name'): ' '.join(''.join(p.itertext()).split())
            for p in node.findall('prop')}


def registry_files(executable: Path | None) -> tuple[tuple[str, int, int], ...]:
    roots = []
    configured = os.environ.get('AIO_LIBREOFFICE_REGISTRY')
    if configured:
        roots.append(Path(configured))
    if executable:
        p = Path(executable).resolve()
        roots.extend((p.parent.parent / 'share/registry',
                      p.parent.parent / 'Resources/registry'))
    for root in roots:
        if root.is_dir():
            return tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size)
                         for p in sorted(root.glob('*.xcd')))
    return ()


@lru_cache(maxsize=2)
def _registered_exports(files):
    types, filters = {}, []
    for name, _modified, _size in files:
        try:
            root = ET.parse(name).getroot()
        except (ET.ParseError, OSError):
            continue
        for node in root.iter('node'):
            props = properties(node)
            identity = node.get(OOR + 'name', '')
            if 'Extensions' in props:
                types[identity] = props
            if 'EXPORT' in props.get('Flags', '').split() and props.get('Type'):
                filters.append(props)
    result = {}
    for item in filters:
        kind = types.get(item['Type'], {})
        for extension in kind.get('Extensions', '').split():
            extension = extension.lower()
            if not EXTENSION.fullmatch(extension):
                continue
            mime = kind.get('MediaType') or mime_for(extension)
            category = COMMON.get(extension, (None,))[0]
            if category is None:
                category = ('images' if mime.startswith('image/') else
                            'spreadsheets' if 'sheet' in mime else
                            'documents' if mime.startswith('text/') else 'other')
            result.setdefault(extension, (category, item.get('UIName') or
                                          extension.upper(), mime))
    return result


def mime_for(extension):
    return mimetypes.guess_type('result.' + extension)[0] or 'application/octet-stream'


def catalog(executable):
    registered = _registered_exports(registry_files(executable))
    entries = dict(registered)
    # Keep the three familiar categories visible with incomplete LO packages.
    # The actual attempt is always made by LO, including unlisted extensions.
    for ext, (category, label) in COMMON.items():
        entries.setdefault(ext, (category, label, mime_for(ext)))
    order = {extension: i for i, extension in enumerate(COMMON)}
    return [(extension, *entries[extension], extension in registered)
            for extension in sorted(entries, key=lambda e: (order.get(e, len(order)), e))]
