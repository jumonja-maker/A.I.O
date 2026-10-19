"""Excel-merger-only OPC integrity checks, independent of file conversion.

This is NOT the new Converter Guard and does not inspect conversion routes.
Untrusted XLSX never executes macros, network links, or external entities.
"""
from __future__ import annotations
from io import BytesIO
from pathlib import PurePosixPath
import re
from xml.parsers import expat
from zipfile import ZipFile, BadZipFile

MAX_ENTRIES = 10_000
MAX_UNPACKED = 150 * 1024 * 1024
MAX_XML_PART = 16 * 1024 * 1024
MAX_DEPTH = 128
MAX_ELEMENTS = 2_000_000


def _xml_safe(blob: bytes) -> None:
    parser = expat.ParserCreate(namespace_separator='}')
    depth = 0
    elements = 0
    def forbidden(*_):
        raise ValueError('В XLSX запрещены DTD, XML-сущности и внешние ресурсы.')
    def start(name, attrs):
        nonlocal depth, elements
        depth += 1
        elements += 1
        if depth > MAX_DEPTH or elements > MAX_ELEMENTS or len(attrs) > 200:
            raise ValueError('Слишком сложный XML в XLSX.')
        for key, value in attrs.items():
            k = key.rsplit('}', 1)[-1].lower()
            if (k == 'targetmode' and value.lower() == 'external') or (
                k in ('href','target','src','url','data','action') and
                (re.match(r'^[a-z][a-z0-9+.-]*:', value.strip(), re.I) or
                 value.strip().startswith(('//', '\\')))):
                raise ValueError('Внешние связи в XLSX запрещены.')
            if k == 'contenttype' and 'macroenabled' in value.lower():
                raise ValueError('XLSX с макросами не поддерживается.')
    def end(_name):
        nonlocal depth
        depth -= 1
    parser.StartDoctypeDeclHandler = forbidden
    parser.EntityDeclHandler = forbidden
    parser.ExternalEntityRefHandler = forbidden
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    try:
        parser.Parse(blob, True)
    except expat.ExpatError as exc:
        raise ValueError('Повреждённый XML внутри XLSX.') from exc


def verify_excel_package(data: bytes) -> None:
    """Validate OPC paths/CRC/XML before the dedicated Excel merge opens XLSX."""
    if type(data) is not bytes or not data:
        raise ValueError('Пустой XLSX.')
    try:
        with ZipFile(BytesIO(data)) as archive:
            entries = archive.infolist()
            if not entries or len(entries) > MAX_ENTRIES:
                raise ValueError('Некорректное число частей XLSX.')
            if sum(item.file_size for item in entries) > MAX_UNPACKED:
                raise ValueError('Слишком большой распакованный XLSX.')
            seen = set()
            for item in entries:
                raw = item.orig_filename
                parts = raw.rstrip('/').split('/')
                if (not raw or raw in seen or raw != item.filename or
                    raw.startswith('/') or '\\' in raw or ':' in raw or
                    '\0' in raw or any(x in ('', '.', '..') for x in parts) or
                    item.flag_bits & 1):
                    raise ValueError('Небезопасное имя части XLSX.')
                seen.add(raw)
                name = raw.lower()
                if (name.startswith(('xl/externallinks/', 'xl/querytables/',
                                     'xl/activex/', 'xl/embeddings/')) or
                    'vbaproject' in name or 'connections' in name or
                    'externallink' in name):
                    raise ValueError('Внешние данные, ActiveX и макросы не поддерживаются.')
                if name.endswith(('.xml', '.rels')):
                    if item.file_size > MAX_XML_PART:
                        raise ValueError('XML-часть XLSX слишком большая.')
                    _xml_safe(archive.read(item))  # ZIP read verifies CRC.
                else:
                    with archive.open(item) as stream:
                        while stream.read(128 * 1024):
                            pass  # Read fully and verify CRC without keeping it in RAM.
            if not {'[Content_Types].xml','xl/workbook.xml'}.issubset(seen):
                raise ValueError('Отсутствует обязательная часть XLSX.')
    except (BadZipFile, OSError, RuntimeError, EOFError) as exc:
        raise ValueError('Повреждённый или неподдерживаемый XLSX.') from exc
