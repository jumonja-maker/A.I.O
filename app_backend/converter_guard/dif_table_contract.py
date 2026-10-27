"""Bounded independent DIF cell contract for native snapshots and exports.

This is not an importer or renderer. Native LibreOffice still produces every
output. Only proven missing/changed cells veto a result; unsupported grammars
and resource limits are explicitly not_checked, never a compatibility ban.
"""
from dataclasses import dataclass, field
from html.parser import HTMLParser
import hashlib
import json
import math
from pathlib import Path
import posixpath
import re
import time
from xml.parsers import expat
from xml.etree import ElementTree as ET
import zipfile

from .errors import GuardError

MAX_BYTES = 16 * 1024 * 1024
MAX_CELLS = 200000
MAX_NODES = 500000
TARGETS = frozenset(('fods', 'ods', 'ots', 'xlsx', 'xlsm', 'xltx', 'xltm',
                     'html', 'htm', 'xhtml'))
OFFICE = 'urn:oasis:names:tc:opendocument:xmlns:office:1.0'
TABLE = 'urn:oasis:names:tc:opendocument:xmlns:table:1.0'
TEXT = 'urn:oasis:names:tc:opendocument:xmlns:text:1.0'
SHEETS = frozenset(('http://schemas.openxmlformats.org/spreadsheetml/2006/main',
                    'http://purl.oclc.org/ooxml/spreadsheetml/main'))
REL = 'http://schemas.openxmlformats.org/package/2006/relationships'
DOC_RELS = frozenset(('http://schemas.openxmlformats.org/officeDocument/2006/relationships',
                      'http://purl.oclc.org/ooxml/officeDocument/relationships'))
NUMBER = re.compile(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[Ee][+-]?[0-9]+)?\Z')
ADDRESS = re.compile(r'([A-Z]{1,3})([1-9][0-9]{0,6})\Z')


class _Unknown(ValueError):
    pass


@dataclass
class _Budget:
    cancel: object = None
    seconds: float = 5.0
    used: int = 0
    started: float = field(default_factory=time.monotonic)

    def check(self, size=0):
        if self.cancel is not None and self.cancel.is_set():
            raise GuardError('CANCELLED', 'Проверка содержимого DIF отменена.', stage='result')
        self.used += size
        if self.used > MAX_BYTES or self.seconds <= 0 or time.monotonic() - self.started >= self.seconds:
            raise _Unknown('dif_table_inspection_budget')

    def read(self, stream):
        chunks = []
        while True:
            self.check()
            block = stream.read(min(65536, MAX_BYTES + 1 - self.used))
            if not block:
                return b''.join(chunks)
            self.check(len(block))
            chunks.append(block)


def _stamp(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise _Unknown('dif_table_file_unavailable')
    s = path.stat()
    return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)


def _number(value):
    if not NUMBER.fullmatch(str(value)):
        raise _Unknown('dif_table_numeric_grammar')
    number = float(value)
    if not math.isfinite(number):
        raise _Unknown('dif_table_nonfinite_number')
    return number


def _put(cells, row, col, value):
    if not 0 <= row < 1048576 or not 0 <= col < 16384:
        raise _Unknown('dif_table_cell_address')
    if value is not None and value != ('s', ''):
        if (row, col) in cells:
            raise _Unknown('dif_table_duplicate_cell')
        if len(cells) >= MAX_CELLS:
            raise _Unknown('dif_table_cell_budget')
        cells[row, col] = value


def _source(path, budget):
    with Path(path).open('rb') as stream:
        head = stream.read(16)
        if not head.removeprefix(b'\xef\xbb\xbf').startswith((b'TABLE\r', b'TABLE\n')):
            return None
        stream.seek(0)
        raw = budget.read(stream)
    # No guessing of a legacy codepage. UTF-8 and its ASCII subset only.
    text = raw.decode('utf-8-sig')
    lines = iter(text.splitlines())
    def line():
        budget.check()
        value = next(lines, None)
        if value is None:
            raise _Unknown('dif_table_truncated_source')
        return value
    tag, rows, columns = line(), None, None
    seen = set()
    while tag != 'DATA':
        pair, value = line(), line()
        if not re.fullmatch(r'-?[0-9]+,-?[0-9]+', pair):
            raise _Unknown('dif_table_source_topic')
        if tag in ('VECTORS', 'TUPLES'):
            if tag in seen or not pair.startswith('0,'):
                raise _Unknown('dif_table_source_dimensions')
            seen.add(tag)
            count = int(pair.split(',')[1])
            if tag == 'VECTORS': columns = count
            else: rows = count
        tag = line()
    if line() != '0,0' or line() != '""':
        raise _Unknown('dif_table_source_data')
    if rows is None or columns is None or rows < 1 or columns < 1 or rows * columns > MAX_CELLS:
        raise _Unknown('dif_table_source_grid_budget')
    cells, row, col = {}, -1, 0
    while True:
        token, payload = line(), line()
        if token == '-1,0':
            if row >= 0 and col != columns:
                raise _Unknown('dif_table_source_column_count')
            if payload == 'BOT':
                row, col = row + 1, 0
                if row >= rows: raise _Unknown('dif_table_source_row_count')
                continue
            if payload == 'EOD':
                if row + 1 != rows: raise _Unknown('dif_table_source_row_count')
                break
            raise _Unknown('dif_table_source_control')
        if row < 0 or col >= columns:
            raise _Unknown('dif_table_source_coordinate')
        if token == '1,0':
            if not (len(payload) >= 2 and payload.startswith('"') and payload.endswith('"')):
                raise _Unknown('dif_table_multiline_string')
            value = payload[1:-1]
            if '"' in value.replace('""', ''):
                raise _Unknown('dif_table_string_escaping')
            value = ('s', value.replace('""', '"'))
        elif token.startswith('0,') and payload == 'V':
            value = ('n', _number(token[2:]))
        else:
            raise _Unknown('dif_table_source_record_not_supported')
        _put(cells, row, col, value)
        col += 1
    for tail in lines:
        budget.check()
        if tail.strip(): raise _Unknown('dif_table_source_trailing_data')
    return cells, rows, columns, hashlib.sha256(raw).hexdigest()


def _xml(raw, budget):
    # First pass prevents DTD/entity expansion and pathological depth before
    # constructing a bounded tree. No network resolver or external file I/O.
    parser = expat.ParserCreate()
    depth, nodes = 0, 0
    def start(*_):
        nonlocal depth, nodes
        depth += 1; nodes += 1
        budget.check()
        if depth > 128 or nodes > MAX_NODES:
            raise _Unknown('dif_table_xml_budget')
    def end(*_):
        nonlocal depth
        depth -= 1
    def reject(*_):
        raise _Unknown('dif_table_xml_declarations')
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.StartDoctypeDeclHandler = reject
    parser.EntityDeclHandler = reject
    parser.ExternalEntityRefHandler = reject
    parser.Parse(raw, True)
    return ET.fromstring(raw)


def _member(archive, name, budget):
    info = archive.getinfo(name)
    if info.flag_bits & 1 or info.file_size > MAX_BYTES - budget.used:
        raise _Unknown('dif_table_package_budget_or_encryption')
    with archive.open(info) as stream:
        return _xml(budget.read(stream), budget)


def _odf_text(node):
    def visit(item):
        if item.tag == '{' + TEXT + '}s':
            count = int(item.get('{' + TEXT + '}c', '1'))
            if not 1 <= count <= 65536: raise _Unknown('dif_table_space_budget')
            return ' ' * count
        if item.tag == '{' + TEXT + '}tab': return '\t'
        if item.tag == '{' + TEXT + '}line-break': return '\n'
        return (item.text or '') + ''.join(visit(child) + (child.tail or '') for child in item)
    return '\n'.join(visit(p) for p in node.findall('{' + TEXT + '}p'))


def _odf(root, budget):
    tables = root.findall('./{' + OFFICE + '}body/{' + OFFICE + '}spreadsheet/{' + TABLE + '}table')
    if len(tables) != 1 or len(list(tables[0].iter('{' + TABLE + '}table'))) != 1:
        raise _Unknown('dif_table_output_sheet_mapping')
    cells, r = {}, 0
    for row in tables[0].iter('{' + TABLE + '}table-row'):
        budget.check()
        repeated = int(row.get('{' + TABLE + '}number-rows-repeated', '1'))
        if repeated < 1 or r + repeated > 1048576: raise _Unknown('dif_table_output_row_range')
        c, values = 0, []
        for cell in row:
            if cell.tag not in ('{' + TABLE + '}table-cell', '{' + TABLE + '}covered-table-cell'):
                continue
            columns = int(cell.get('{' + TABLE + '}number-columns-repeated', '1'))
            if columns < 1 or c + columns > 16384: raise _Unknown('dif_table_output_column_range')
            if (cell.tag.endswith('}covered-table-cell') or
                    any(key in cell.attrib for key in ('{' + TABLE + '}formula',
                        '{' + TABLE + '}number-columns-spanned', '{' + TABLE + '}number-rows-spanned'))):
                raise _Unknown('dif_table_output_formula_or_merge')
            kind = cell.get('{' + OFFICE + '}value-type')
            if kind in ('float', 'currency', 'percentage'):
                value = ('n', _number(cell.attrib['{' + OFFICE + '}value']))
            elif kind in ('string', None):
                value = ('s', cell.get('{' + OFFICE + '}string-value', _odf_text(cell)))
            else:
                value = (kind, cell.get('{' + OFFICE + '}value', _odf_text(cell)))
            if value != ('s', ''):
                if repeated * columns + len(cells) > MAX_CELLS:
                    raise _Unknown('dif_table_cell_budget')
                values.append((c, columns, value))
            c += columns
        for rr in range(repeated if values else 0):
            budget.check()
            for first, count, value in values:
                for cc in range(first, first + count): _put(cells, r + rr, cc, value)
        r += repeated
    return cells


def _address(value):
    match = ADDRESS.fullmatch(value or '')
    if match is None: raise _Unknown('dif_table_output_cell_address')
    col = 0
    for char in match[1]: col = col * 26 + ord(char) - 64
    return int(match[2]) - 1, col - 1


def _sheet_text(node, ns):
    # Phonetic runs/metadata must not be counted as cell text.
    return ''.join((item.text or '') for item in node.findall('{'+ns+'}t')) + ''.join(
        item.text or '' for item in node.findall('{'+ns+'}r/{'+ns+'}t'))


def _ooxml(archive, budget):
    root = _member(archive, 'xl/workbook.xml', budget)
    namespace, _, name = root.tag[1:].partition('}')
    if namespace not in SHEETS or name != 'workbook': raise _Unknown('dif_table_workbook_namespace')
    sheets = root.findall('./{' + namespace + '}sheets/{' + namespace + '}sheet')
    if len(sheets) != 1: raise _Unknown('dif_table_output_sheet_mapping')
    ids = [sheets[0].get('{' + ns + '}id') for ns in DOC_RELS
           if '{' + ns + '}id' in sheets[0].attrib]
    if len(ids) != 1: raise _Unknown('dif_table_output_sheet_relationship')
    rels = _member(archive, 'xl/_rels/workbook.xml.rels', budget)
    if rels.tag != '{' + REL + '}Relationships':
        raise _Unknown('dif_table_relationship_namespace')
    entries = [rel for rel in rels if rel.tag == '{' + REL + '}Relationship'
               and rel.get('Id') == ids[0]]
    if len(entries) != 1 or entries[0].get('TargetMode', 'Internal') != 'Internal':
        raise _Unknown('dif_table_output_sheet_relationship')
    if entries[0].get('Type') not in {ns + '/worksheet' for ns in DOC_RELS}:
        raise _Unknown('dif_table_relationship_type')
    target = entries[0].get('Target', '')
    if not target or any(c in target for c in '\\%?#:'):
        raise _Unknown('dif_table_output_sheet_relationship')
    target = posixpath.normpath(target.lstrip('/') if target.startswith('/') else 'xl/' + target)
    if not target.startswith('xl/') or '..' in target.split('/'):
        raise _Unknown('dif_table_output_sheet_relationship')
    strings = []
    if 'xl/sharedStrings.xml' in archive.namelist():
        shared = _member(archive, 'xl/sharedStrings.xml', budget)
        if shared.tag != '{' + namespace + '}sst': raise _Unknown('dif_table_shared_strings_namespace')
        strings = [_sheet_text(item, namespace) for item in shared.findall('{' + namespace + '}si')]
        if len(strings) > MAX_CELLS: raise _Unknown('dif_table_shared_strings_budget')
    sheet = _member(archive, target, budget)
    if sheet.tag != '{' + namespace + '}worksheet': raise _Unknown('dif_table_worksheet_namespace')
    cells, seen = {}, set()
    for cell in sheet.findall('./{' + namespace + '}sheetData/{' + namespace + '}row/{' + namespace + '}c'):
        budget.check()
        position = _address(cell.get('r'))
        if position in seen: raise _Unknown('dif_table_duplicate_cell')
        seen.add(position)
        if len(seen) > MAX_CELLS: raise _Unknown('dif_table_cell_budget')
        if cell.find('{' + namespace + '}f') is not None:
            raise _Unknown('dif_table_output_formula')
        value = cell.find('{' + namespace + '}v')
        text, kind = '' if value is None else value.text or '', cell.get('t', 'n')
        if kind == 's':
            index = int(text)
            if not 0 <= index < len(strings): raise _Unknown('dif_table_shared_string_index')
            decoded = ('s', strings[index])
        elif kind == 'inlineStr':
            inline = cell.find('{' + namespace + '}is')
            decoded = ('s', _sheet_text(inline, namespace) if inline is not None else '')
        elif kind == 'str': decoded = ('s', text)
        elif kind == 'n': decoded = ('n', _number(text)) if text else None
        else: decoded = (kind, text)
        _put(cells, *position, decoded)
    return cells


class _HTMLTable(HTMLParser):
    def __init__(self, budget):
        super().__init__(convert_charrefs=True)
        self.budget, self.cells = budget, {}
        self.tables, self.in_table, self.in_row, self.cell = 0, False, False, None
        self.row, self.col, self.closed, self.ignore = -1, 0, False, 0

    def handle_starttag(self, tag, attrs):
        self.budget.check()
        if tag in ('script', 'style'):
            if self.cell is not None: raise _Unknown('dif_table_html_active_cell')
            self.ignore += 1
        if self.ignore: return
        if len(attrs) != len({key for key, _ in attrs}):
            raise _Unknown('dif_table_html_duplicate_attributes')
        attrs = dict(attrs)
        if tag == 'table':
            self.tables += 1
            if self.in_table or self.tables != 1: raise _Unknown('dif_table_html_table_mapping')
            self.in_table = True
        elif tag == 'tr' and self.in_table:
            if self.in_row or self.cell is not None: raise _Unknown('dif_table_html_row_mapping')
            self.in_row, self.row, self.col = True, self.row + 1, 0
        elif tag in ('td', 'th') and self.in_table:
            if not self.in_row or self.cell is not None: raise _Unknown('dif_table_html_cell_mapping')
            if any(attrs.get(a, '1') != '1' for a in ('rowspan', 'colspan')):
                raise _Unknown('dif_table_html_merged_cells')
            self.cell = (attrs, [])
        elif tag == 'br' and self.cell is not None:
            self.cell[1].append('\n')

    def handle_data(self, text):
        if self.cell is not None and not self.ignore: self.cell[1].append(text)

    def handle_endtag(self, tag):
        self.budget.check()
        if tag in ('script', 'style') and self.ignore:
            self.ignore -= 1
            return
        if self.ignore: return
        if tag in ('td', 'th') and self.in_table:
            if self.cell is None: raise _Unknown('dif_table_html_cell_mapping')
            attrs, parts = self.cell
            value = ''.join(parts).strip()
            if 'sdval' in attrs: decoded = ('n', _number(attrs['sdval']))
            elif attrs.get('data-sheets-value'):
                meta = json.loads(attrs['data-sheets-value'])
                if not isinstance(meta, dict):
                    raise _Unknown('dif_table_html_metadata')
                if meta.get('1') == 3: decoded = ('n', _number(meta['3']))
                elif meta.get('1') == 2 and meta.get('2', '') == value: decoded = ('s', value)
                else: decoded = ('html_display', value)
            else: decoded = ('html_display', value)
            _put(self.cells, self.row, self.col, decoded if value or decoded[0] == 'n' else None)
            self.col += 1; self.cell = None
        elif tag == 'tr' and self.in_table:
            if self.cell is not None: raise _Unknown('dif_table_html_cell_mapping')
            self.in_row = False
        elif tag == 'table' and self.in_table:
            if self.cell is not None or self.in_row: raise _Unknown('dif_table_html_row_mapping')
            self.in_table, self.closed = False, True


def _output(path, target, budget):
    if target in ('html', 'htm', 'xhtml'):
        with Path(path).open('rb') as stream: raw = budget.read(stream)
        parser = _HTMLTable(budget)
        parser.feed(raw.decode('utf-8-sig')); parser.close()
        if parser.tables != 1 or not parser.closed: raise _Unknown('dif_table_html_incomplete')
        return parser.cells
    if target == 'fods':
        with Path(path).open('rb') as stream: root = _xml(budget.read(stream), budget)
        return _odf(root, budget)
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) > 10000 or len(names) != len(set(names)):
            raise _Unknown('dif_table_package_entries')
        if target in ('ods', 'ots'): return _odf(_member(archive, 'content.xml', budget), budget)
        return _ooxml(archive, budget)


def check_dif_table(paths, target, source, *, cancel=None, seconds=5.0):
    """Reject proven DIF cell loss; report bounded/unsupported coverage honestly."""
    target, source = str(target).lower(), Path(source)
    if target not in TARGETS: return None
    result = {'check': 'dif_table_content', 'qualityCompared': False,
              'scope': 'cell_coordinates_values_and_available_types',
              'fullDocumentFidelity': False}
    budget = _Budget(cancel, seconds)
    try:
        source_stamp = _stamp(source)
        expected = _source(source, budget)
        if expected is None: return None
        old, rows, columns, digest = expected
        result.update(sourceRows=rows, sourceColumns=columns, sourceNonEmptyCells=len(old), sourceSHA256=digest)
        candidates = [Path(p) for p in paths if Path(p).suffix.lower() == '.' + target]
        if len(candidates) != 1: raise _Unknown('dif_table_primary_ambiguous')
        output = candidates[0]
        output_stamp = _stamp(output)
        actual = _output(output, target, budget)
        incomplete, differences = False, []
        matched = 0
        for position in sorted(set(old) | set(actual)):
            budget.check()
            left, right = old.get(position), actual.get(position)
            if left == right:
                matched += 1
                continue
            if left and right and right[0] == 'html_display':
                # A visible HTML numeric string does not prove its typed value
                # or locale. Text-only equality proves text, not numeric types.
                if left[0] == 's' and left[1] == right[1] and left[1] == left[1].strip():
                    matched += 1
                    continue
                if left[0] == 'n' or (left[0] == 's' and left[1] != left[1].strip()):
                    incomplete = True
                    continue
            differences.append(position)
        budget.check()
        if source_stamp != _stamp(source) or output_stamp != _stamp(output):
            raise GuardError('SOURCE_CHANGED_DURING_PREPARATION',
                'Файл изменился во время проверки DIF; результат не подтверждён.', stage='result')
        result.update(resultNonEmptyCells=len(actual), matchedCells=matched)
        if differences:
            # Coordinates/counts only: never put document text into diagnostics.
            diagnostic = json.dumps({'check': 'dif_table_content', 'sourceCells': len(old),
                'resultCells': len(actual), 'differentCells': len(differences),
                'firstCoordinates': [[r + 1, c + 1] for r, c in differences[:12]]})
            raise GuardError('ENGINE_OUTPUT_CONTENT_LOSS',
                'В результате DIF отсутствуют или изменены ячейки исходника. Результат не отмечен готовым.',
                stage='result', diagnostic=diagnostic)
        if incomplete: raise _Unknown('dif_table_html_numeric_types_unverified')
        return {**result, 'status': 'checked_subset', 'valuesCompared': True,
                'typesCompared': target not in ('html', 'htm', 'xhtml')}
    except (_Unknown, UnicodeError) as error:
        if cancel is not None and cancel.is_set():
            budget.check()
        reason = str(error)[:128] if isinstance(error, _Unknown) else 'dif_table_encoding_not_supported'
        return {**result, 'status': 'not_checked', 'reason': reason,
                'warning': 'DIF_TABLE_CONTENT_NOT_CHECKED'}
    except (OSError, ValueError, KeyError, IndexError, OverflowError, ET.ParseError,
            expat.ExpatError, zipfile.BadZipFile, RuntimeError, NotImplementedError) as error:
        if cancel is not None and cancel.is_set():
            budget.check()
        return {**result, 'status': 'not_checked', 'reason': 'dif_table_read:' + type(error).__name__,
                'warning': 'DIF_TABLE_CONTENT_NOT_CHECKED'}
