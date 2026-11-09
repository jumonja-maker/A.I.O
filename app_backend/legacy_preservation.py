"""Bounded sheet-count evidence for the direct legacy conversion boundary.

This does not introduce a new importer or certify values/formatting. A known
multi-sheet source may not silently become one flat exported sheet. Unknown
and binary source inventories remain explicitly not checked.
"""
from pathlib import Path
import posixpath
import xml.etree.ElementTree as ET
import zipfile
import zlib

from .libreoffice_engine import LibreOfficeError
from .converter_guard.source_contract import (inspect_source_contract, _opc_part_key,
                                               _OFFICE_RELATIONSHIPS, CALC, OFFICE)
from .converter_guard.errors import GuardError

SHEET_TARGETS = frozenset(('csv', 'tab', 'tsv', 'dbf', 'dif', 'slk', 'sylk'))
MAX_XML_BYTES = 64 * 1024 * 1024
MAX_XML_NODES = 1000000
MAX_SHEETS = 20000
PKG_REL = 'http://schemas.openxmlformats.org/package/2006/relationships'
SHEET_NS = frozenset(('http://schemas.openxmlformats.org/spreadsheetml/2006/main',
                     'http://purl.oclc.org/ooxml/spreadsheetml/main'))
DOC_REL = frozenset(('http://schemas.openxmlformats.org/officeDocument/2006/relationships',
                    'http://purl.oclc.org/ooxml/officeDocument/relationships'))
TABLE = 'urn:oasis:names:tc:opendocument:xmlns:table:1.0'


class _Incomplete(ValueError):
    pass


class _ControlFailure(Exception):
    def __init__(self, error):
        self.error = error


class _SourceControl:
    """Source-contract parsing must not swallow a storage-control ValueError."""
    def __init__(self, cancel):
        self.cancel = cancel

    def is_set(self):
        try:
            return self.cancel is not None and self.cancel.is_set()
        except LibreOfficeError as error:
            raise _ControlFailure(error) from error


def _check(cancel):
    if cancel is not None and cancel.is_set():
        raise LibreOfficeError('Проверка сохранности листов отменена.', 'CANCELLED')


class _Reader:
    def __init__(self, source, cancel):
        self.source, self.cancel, self.bytes, self.tail = source, cancel, 0, b''

    def read(self, size=-1):
        _check(self.cancel)
        block = self.source.read(min(size, 65536) if size >= 0 else 65536)
        self.bytes += len(block)
        if self.bytes > MAX_XML_BYTES:
            raise _Incomplete('source_xml_budget')
        probe = (self.tail + block).replace(b'\0', b'').lower()
        if b'<!doctype' in probe or b'<!entity' in probe:
            raise _Incomplete('source_xml_declarations')
        self.tail = probe[-16:]
        return block


def _events(stream, cancel):
    nodes, parents = 0, []
    for event, item in ET.iterparse(_Reader(stream, cancel), events=('start', 'end')):
        _check(cancel)
        if event == 'start':
            nodes += 1
            parents.append(item)
            if nodes > MAX_XML_NODES or len(parents) > 128:
                raise _Incomplete('source_xml_structure_budget')
        yield event, item
        if event == 'end':
            parents.pop()
            if parents:
                parents[-1].remove(item)


def _parts(archive, cancel):
    result = {}
    for name in archive.namelist():
        _check(cancel)
        if name.endswith('/'):
            continue
        try:
            key = _opc_part_key('/' + name)
        except ValueError:
            continue
        if key in result:
            raise _Incomplete('ambiguous_package_parts')
        result[key] = name
    return result


def _relationships(archive, name, cancel):
    records = {}
    with archive.open(name) as stream:
        root = None
        for event, item in _events(stream, cancel):
            if root is None:
                root = item.tag
                if root != '{' + PKG_REL + '}Relationships':
                    raise _Incomplete('relationship_namespace')
            if event == 'end':
                if item.tag == '{' + PKG_REL + '}Relationship':
                    rid = item.get('Id')
                    if not rid or rid in records:
                        raise _Incomplete('relationship_ids')
                    records[rid] = dict(item.attrib)
                item.clear()
    return records


def _ooxml_sheets(source, cancel):
    with zipfile.ZipFile(source) as archive:
        parts = _parts(archive, cancel)
        roots = _relationships(archive, '_rels/.rels', cancel)
        main = [r for r in roots.values() if r.get('Type') in _OFFICE_RELATIONSHIPS]
        if len(main) != 1 or main[0].get('TargetMode', 'Internal') != 'Internal':
            raise _Incomplete('workbook_main_relationship')
        key = _opc_part_key(main[0].get('Target'), relationship=True)
        part = parts[key]
        relationship_part = posixpath.join(posixpath.dirname(part), '_rels',
                                          posixpath.basename(part) + '.rels')
        relations = _relationships(archive, relationship_part, cancel)
        names, seen_names, ids, sheet_ids, destinations, stack = [], set(), set(), set(), set(), []
        with archive.open(part) as stream:
            for event, item in _events(stream, cancel):
                if event == 'start':
                    stack.append(item.tag)
                    if len(stack) == 1 and item.tag not in {'{' + ns + '}workbook' for ns in SHEET_NS}:
                        raise _Incomplete('workbook_root')
                    if (len(stack) == 3 and any(stack == ['{' + ns + '}workbook',
                            '{' + ns + '}sheets', '{' + ns + '}sheet'] for ns in SHEET_NS)):
                        name = item.get('name')
                        sheet_id = item.get('sheetId', '')
                        rid = next((item.get('{' + ns + '}id') for ns in DOC_REL
                                    if item.get('{' + ns + '}id')), None)
                        if (not name or name in seen_names or not rid or rid in ids or
                                not sheet_id.isdecimal() or int(sheet_id) < 1 or int(sheet_id) in sheet_ids):
                            raise _Incomplete('workbook_sheet_identity')
                        relation = relations.get(rid, {})
                        if relation.get('TargetMode', 'Internal') != 'Internal':
                            raise _Incomplete('external_sheet')
                        kind = relation.get('Type', '')
                        accepted = {'worksheet', 'chartsheet', 'dialogsheet', 'macrosheet', 'intlMacrosheet'}
                        if not any(kind == ns + '/' + local for ns in DOC_REL for local in accepted):
                            raise _Incomplete('workbook_sheet_relationship')
                        target = relation.get('Target', '')
                        destination = (target if target.startswith('/') else
                            '/' + posixpath.dirname(part) + '/' + target)
                        destination_key = _opc_part_key(destination, relationship=True)
                        if destination_key not in parts or destination_key in destinations:
                            raise _Incomplete('workbook_sheet_part_missing')
                        names.append(name)
                        seen_names.add(name)
                        ids.add(rid)
                        sheet_ids.add(int(sheet_id))
                        destinations.add(destination_key)
                        if len(names) > MAX_SHEETS:
                            raise _Incomplete('sheet_inventory_budget')
                else:
                    item.clear()
                    stack.pop()
        if not names:
            raise _Incomplete('workbook_sheets_missing')
        return names


def _odf_sheets(stream, cancel):
    names, seen_names, stack = [], set(), []
    spreadsheet = '{' + OFFICE + '}spreadsheet'
    table = '{' + TABLE + '}table'
    for event, item in _events(stream, cancel):
        if event == 'start':
            if (item.tag == table and len(stack) == 3 and stack[-2:] ==
                    ['{' + OFFICE + '}body', spreadsheet] and stack[0] in
                    ('{' + OFFICE + '}document', '{' + OFFICE + '}document-content')):
                name = item.get('{' + TABLE + '}name')
                if not name or name in seen_names:
                    raise _Incomplete('odf_sheet_identity')
                names.append(name)
                seen_names.add(name)
                if len(names) > MAX_SHEETS:
                    raise _Incomplete('sheet_inventory_budget')
            stack.append(item.tag)
        else:
            item.clear()
            stack.pop()
    if not names:
        raise _Incomplete('odf_sheets_missing')
    return names


def check_legacy_sheet_preservation(source, target, paths, output_root, *, cancel=None):
    """Count declared source sheets and exact-root primary output files.

    Hidden, empty and chart tabs remain source inventory entries; no active
    sheet was selected by this legacy batch API. Matching counts do not prove
    output values, cell layout, formulas or source importer equivalence.
    """
    if target not in SHEET_TARGETS:
        return None
    _check(cancel)
    base = {'check': 'legacy_sheet_inventory', 'scope': 'declared_source_sheet_count',
            'qualityCompared': False, 'valuesCompared': False, 'layoutCompared': False}
    try:
        contract = inspect_source_contract(source, cancel=_SourceControl(cancel))
    except _ControlFailure as error:
        raise error.error from error
    except GuardError as error:
        if error.code == 'CANCELLED':
            raise LibreOfficeError('Проверка сохранности листов отменена.', 'CANCELLED') from error
        raise LibreOfficeError('Не удалось подтвердить состав листов исходника.',
            'LEGACY_OUTPUT_VALIDATION_INCOMPLETE', diagnostic='source_inventory:' + error.code) from error
    if contract.get('state') != 'recognized' or contract.get('expectedServices') != [CALC]:
        return {**base, 'status': 'not_checked',
                'reason': contract.get('reason') or 'source_sheet_inventory_not_available'}
    source = Path(source)
    try:
        initial = source.stat()
        if contract.get('contentClass') == 'ooxml_package':
            sheets = _ooxml_sheets(source, cancel)
        elif contract.get('contentClass') == 'flat_odf':
            with source.open('rb') as stream:
                sheets = _odf_sheets(stream, cancel)
        elif contract.get('contentClass') == 'odf_package':
            with zipfile.ZipFile(source) as archive, archive.open('content.xml') as stream:
                sheets = _odf_sheets(stream, cancel)
        else:
            return {**base, 'status': 'not_checked', 'reason': 'source_sheet_inventory_not_available'}
        final = source.stat()
        if any(getattr(initial, field) != getattr(final, field) for field in
               ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')):
            raise _Incomplete('source_changed_during_inventory')
        root = Path(output_root).absolute()
        if root.is_symlink():
            raise _Incomplete('output_root_link')
        extensions = {'slk', 'sylk'} if target in ('slk', 'sylk') else {target}
        primaries = [Path(path) for path in paths if Path(path).absolute().parent == root
                     and Path(path).suffix.lower().lstrip('.') in extensions]
        names = [path.name.casefold() for path in primaries]
        if len(names) != len(set(names)) or any(p.is_symlink() or not p.is_file() or
                                                p.stat().st_size == 0 for p in primaries):
            raise _Incomplete('primary_output_inventory_ambiguous_or_empty')
        _check(cancel)
        if len(primaries) != len(sheets):
            raise LibreOfficeError('В результате не сохранён полный комплект листов. '
                'Неполный экспорт не опубликован; исходник сохранён.', 'LEGACY_OUTPUT_CONTENT_LOSS',
                diagnostic='legacy_sheet_count:%d:%d' % (len(sheets), len(primaries)))
        return {**base, 'status': 'checked_subset', 'sourceSheets': len(sheets),
                'observedOutputs': len(primaries), 'hiddenAndEmptySheetsIncluded': True}
    except LibreOfficeError:
        raise
    except (OSError, KeyError, ValueError, EOFError, zipfile.BadZipFile, ET.ParseError,
            RuntimeError, NotImplementedError, zlib.error) as error:
        raise LibreOfficeError('Не удалось подтвердить сохранность всех листов. '
            'Экспорт не опубликован; исходник сохранён.', 'LEGACY_OUTPUT_VALIDATION_INCOMPLETE',
            diagnostic='legacy_sheet_inventory:' + str(error) if isinstance(error, _Incomplete)
            else 'legacy_sheet_inventory:' + type(error).__name__) from error
