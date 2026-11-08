"""Bounded readback of original high-precision OOXML numeric literals.

This checks a declared subset, not formulas, formatting or every workbook
value. No source is rewritten. Integral literals compare as exact decimals;
nonintegral values compare in the IEEE-double domain used by spreadsheets.
"""
from decimal import Decimal, InvalidOperation
import json
import math
from pathlib import Path
import posixpath
import re
import time
from xml.parsers import expat
import xml.etree.ElementTree as ET
import zipfile
import zlib

from .errors import GuardError
from .source_contract import CALC, _OFFICE_RELATIONSHIPS, _opc_part_key, inspect_source_contract

TARGETS = frozenset(('xlsx', 'xlsm', 'xltx', 'xltm'))
SHEET_NS = frozenset(('http://schemas.openxmlformats.org/spreadsheetml/2006/main',
                     'http://purl.oclc.org/ooxml/spreadsheetml/main'))
DOC_REL = frozenset(value.rsplit('/', 1)[0] for value in _OFFICE_RELATIONSHIPS)
PKG_REL = 'http://schemas.openxmlformats.org/package/2006/relationships'
MAX_XML_BYTES = 64 * 1024 * 1024
MAX_METADATA_BYTES = 4 * 1024 * 1024
MAX_NODES = 1000000
MAX_CANDIDATES = 20000
MAX_SHEETS = 1024
MAX_NUMERIC_TOKEN = 512
MAX_FORMULA_RANGES = 1024
_NUMBER = re.compile(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z')
_COORDINATE = re.compile(r'([A-Za-z]{1,3})([1-9][0-9]{0,6})\Z')


class _Limited(Exception):
    pass


class _ControlFailure(Exception):
    def __init__(self, error):
        self.error = error


class _Budget:
    def __init__(self, cancel, seconds):
        self.cancel, self.until = cancel, time.monotonic() + seconds
        self.used = 0

    def check(self, size=0):
        if self.cancel is not None:
            try:
                stopped = self.cancel.is_set()
            except Exception as error:
                # Legacy storage control raises ValueError-derived errors.
                # They are control failures, never malformed numeric/XML data.
                raise _ControlFailure(error) from error
            if stopped:
                raise GuardError('CANCELLED', 'Проверка чисел отменена.', stage='result')
        self.used += size
        if self.used > MAX_XML_BYTES or time.monotonic() >= self.until:
            raise _Limited('numeric_inspection_budget')

    def is_set(self):
        self.check()
        return False


def _stamp(path):
    path = Path(path)
    info = path.lstat()
    if not path.is_file() or path.is_symlink():
        raise _Limited('numeric_file_unavailable')
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _metadata(archive, name, budget):
    if archive.getinfo(name).file_size > MAX_METADATA_BYTES:
        raise _Limited('numeric_metadata_budget')
    data = archive.read(name)
    budget.check(len(data))
    probe = data.replace(b'\0', b'').upper()
    if b'<!DOCTYPE' in probe or b'<!ENTITY' in probe:
        raise _Limited('numeric_xml_declarations')
    return ET.fromstring(data)


def _relations(archive, name, budget):
    root = _metadata(archive, name, budget)
    if root.tag != '{' + PKG_REL + '}Relationships':
        raise _Limited('numeric_relationship_namespace')
    records = {}
    for row in root:
        if row.tag != '{' + PKG_REL + '}Relationship':
            continue
        identity = row.get('Id')
        if not identity or identity in records:
            raise _Limited('numeric_relationship_identity')
        records[identity] = dict(row.attrib)
    return records


def _sheets(archive, budget):
    infos = archive.infolist()
    if len(infos) > 10000:
        raise _Limited('numeric_package_member_budget')
    parts = {}
    for info in infos:
        if info.is_dir():
            continue
        key = _opc_part_key('/' + info.filename)
        if key in parts:
            raise _Limited('numeric_package_part_ambiguous')
        parts[key] = info.filename
    roots = _relations(archive, '_rels/.rels', budget)
    mains = [r for r in roots.values() if r.get('Type') in _OFFICE_RELATIONSHIPS]
    if len(mains) != 1 or mains[0].get('TargetMode', 'Internal') != 'Internal':
        raise _Limited('numeric_workbook_relationship')
    part = parts[_opc_part_key(mains[0].get('Target'), relationship=True)]
    rel_part = posixpath.join(posixpath.dirname(part), '_rels', posixpath.basename(part) + '.rels')
    relations = _relations(archive, rel_part, budget)
    workbook = _metadata(archive, part, budget)
    if workbook.tag not in {'{' + ns + '}workbook' for ns in SHEET_NS}:
        raise _Limited('numeric_workbook_namespace')
    namespace = workbook.tag[1:].split('}', 1)[0]
    containers = workbook.findall('{' + namespace + '}sheets')
    if len(containers) != 1:
        raise _Limited('numeric_workbook_sheets_missing')
    container = containers[0]
    result, names, identities, destinations = [], set(), set(), set()
    for item in container:
        budget.check()
        if item.tag != '{' + namespace + '}sheet':
            continue
        name = item.get('name')
        identity = next((item.get('{' + ns + '}id') for ns in DOC_REL
                         if item.get('{' + ns + '}id')), None)
        if not name or name.casefold() in names or not identity or identity in identities:
            raise _Limited('numeric_sheet_identity')
        names.add(name.casefold()); identities.add(identity)
        relation = relations.get(identity, {})
        if relation.get('TargetMode', 'Internal') != 'Internal':
            raise _Limited('numeric_external_sheet')
        target = relation.get('Target', '')
        uri = target if target.startswith('/') else '/' + posixpath.dirname(part) + '/' + target
        key = _opc_part_key(uri, relationship=True)
        if key not in parts or key in destinations:
            raise _Limited('numeric_sheet_part_missing_or_ambiguous')
        destinations.add(key)
        if any(relation.get('Type') == ns + '/worksheet' for ns in DOC_REL):
            result.append((name, parts[key]))
        elif not any(relation.get('Type') == ns + '/' + kind
                     for ns in DOC_REL for kind in ('chartsheet', 'dialogsheet', 'macrosheet', 'intlMacrosheet')):
            raise _Limited('numeric_sheet_relationship_type')
        if len(names) > MAX_SHEETS:
            raise _Limited('numeric_sheet_budget')
    return result


def _coordinate(column, row):
    letters = ''
    while column:
        column, tail = divmod(column - 1, 26)
        letters = chr(65 + tail) + letters
    return letters + str(row)


def _point(value):
    match = _COORDINATE.fullmatch(value)
    if not match:
        raise _Limited('numeric_formula_range')
    column = 0
    for letter in match[1].upper():
        column = column * 26 + ord(letter) - 64
    row = int(match[2])
    if column > 16384 or row > 1048576:
        raise _Limited('numeric_formula_range')
    return column, row


def _range(value):
    points = value.split(':')
    if len(points) not in (1, 2):
        raise _Limited('numeric_formula_range')
    left, right = _point(points[0]), _point(points[-1])
    if left[0] > right[0] or left[1] > right[1]:
        raise _Limited('numeric_formula_range')
    return left[0], left[1], right[0], right[1]


def _covered(coordinate, ranges, budget):
    if not ranges:
        return False
    column, row = _point(coordinate)
    for index, (left, top, right, bottom) in enumerate(ranges):
        if index % 64 == 0:
            budget.check()
        if left <= column <= right and top <= row <= bottom:
            return True
    return False


def _cells(archive, part, budget, visit):
    parser = expat.ParserCreate(namespace_separator='}')
    stack, current = [], None
    row_number = previous_row = column_number = nodes = data_parts = 0
    namespace = None
    formula_ranges = []

    def decline(*_):
        raise _Limited('numeric_xml_declarations')

    def start(tag, attrs):
        nonlocal current, namespace, row_number, previous_row, column_number, nodes, data_parts
        stack.append(tag); nodes += 1
        if nodes % 256 == 0:
            budget.check()
        if nodes > MAX_NODES or len(stack) > 128:
            raise _Limited('numeric_xml_structure_budget')
        if len(stack) == 1:
            namespace, _, local = tag.rpartition('}')
            if namespace not in SHEET_NS or local != 'worksheet':
                raise _Limited('numeric_worksheet_namespace')
        prefix = namespace + '}'
        if len(stack) == 2 and tag == prefix + 'sheetData':
            data_parts += 1
            if data_parts != 1:
                raise _Limited('numeric_worksheet_data_ambiguous')
        if len(stack) == 3 and stack[-2:] == [prefix + 'sheetData', prefix + 'row']:
            raw = attrs.get('r', str(previous_row + 1))
            if not re.fullmatch('[1-9][0-9]{0,6}', raw) or not previous_row < int(raw) <= 1048576:
                raise _Limited('numeric_row_coordinate')
            row_number = previous_row = int(raw); column_number = 0
        elif len(stack) == 4 and stack[-3:] == [prefix + 'sheetData', prefix + 'row', prefix + 'c']:
            raw = attrs.get('r')
            if raw is None:
                column = column_number + 1
            else:
                match = _COORDINATE.fullmatch(raw)
                if not match or int(match[2]) != row_number:
                    raise _Limited('numeric_cell_coordinate')
                column = 0
                for letter in match[1].upper():
                    column = column * 26 + ord(letter) - 64
            if not column_number < column <= 16384:
                raise _Limited('numeric_cell_coordinate')
            column_number = column
            current = {'coordinate': _coordinate(column, row_number), 'type': attrs.get('t', 'n'),
                       'formula': False, 'value': '', 'values': 0,
                       'metadata': 'cm' in attrs or 'vm' in attrs}
        elif current is not None and len(stack) == 5:
            if tag == prefix + 'f':
                current['formula'] = True
                if attrs.get('ref'):
                    if len(formula_ranges) >= MAX_FORMULA_RANGES:
                        raise _Limited('numeric_formula_range_budget')
                    formula_ranges.append(_range(attrs['ref']))
            elif tag == prefix + 'v':
                current['values'] += 1

    def text(value):
        if current is not None and len(stack) == 5 and stack[-1] == namespace + '}v':
            current['value'] += value
            if len(current['value']) > MAX_NUMERIC_TOKEN:
                raise _Limited('numeric_cell_token_budget')

    def end(tag):
        nonlocal current
        if current is not None and len(stack) == 4 and tag == namespace + '}c':
            if current['values'] > 1:
                raise _Limited('numeric_cell_value_ambiguous')
            visit(current)
            current = None
        stack.pop()

    parser.StartElementHandler, parser.EndElementHandler = start, end
    parser.CharacterDataHandler = text
    parser.StartDoctypeDeclHandler = decline
    parser.EntityDeclHandler = decline
    parser.ExternalEntityRefHandler = decline
    with archive.open(part) as stream:
        while block := stream.read(65536):
            budget.check(len(block)); parser.Parse(block, False)
        parser.Parse(b'', True)
    return formula_ranges


def _number(token):
    token = token.strip()
    if not token or not _NUMBER.fullmatch(token):
        raise _Limited('numeric_literal_unrecognized')
    value = Decimal(token)
    if not value.is_finite() or abs(value.adjusted()) > 4096:
        raise _Limited('numeric_comparison_range')
    return value


def _source(source, budget):
    contract = inspect_source_contract(source, cancel=budget)
    if contract.get('contentClass') != 'ooxml_package' or contract.get('expectedServices') != [CALC]:
        return None
    if contract.get('state') != 'recognized':
        raise _Limited('numeric_source_not_inspected')
    risks, formulas = {}, 0
    with zipfile.ZipFile(source) as archive:
        sheets = _sheets(archive, budget)
        for sheet, part in sheets:
            sheet_risks = {}
            def visit(cell):
                nonlocal formulas
                if cell['formula'] or cell['metadata']:
                    formulas += 1
                    return
                token = cell['value'].strip()
                if cell['type'] != 'n' or not token:
                    return
                mantissa = re.split('[eE]', token, maxsplit=1)[0].lstrip('+-')
                if len(mantissa.replace('.', '').lstrip('0')) <= 15:
                    return
                value = _number(token)
                if len(risks) + len(sheet_risks) >= MAX_CANDIDATES:
                    raise _Limited('numeric_candidate_budget')
                sheet_risks[cell['coordinate']] = (token, value == value.to_integral_value())
            ranges = _cells(archive, part, budget, visit)
            for coordinate, value in sheet_risks.items():
                if _covered(coordinate, ranges, budget):
                    formulas += 1
                else:
                    risks[(sheet, coordinate)] = value
    return {'risks': risks, 'sheets': len(sheets), 'formulaOrMetadataCellsSkipped': formulas}


def check_spreadsheet_numeric_literals(paths, target, source, *, cancel=None,
                                       source_evidence=None, seconds=8.0):
    """Return subset evidence, None when inapplicable, or raise on known loss."""
    if str(target).lower().lstrip('.') not in TARGETS:
        return None
    budget = _Budget(cancel, seconds)
    base = {'check': 'ooxml_numeric_literals', 'scope': 'high_precision_source_literals_by_sheet_and_coordinate',
            'qualityCompared': False, 'fullWorkbookFidelity': False, 'formulaCachesCompared': False}
    try:
        budget.check(); initial = _stamp(source)
        def load():
            try:
                return _source(source, budget)
            except _Limited as error:
                return {'unknown': str(error)}
        summary = (source_evidence.get('ooxml_numeric_literals', source, load)
                   if source_evidence is not None else load())
        if summary is None:
            return None
        if 'unknown' in summary:
            raise _Limited(summary['unknown'])
        risks = summary['risks']
        if not risks:
            return None
        base.update(candidateCells=len(risks), sourceSheets=summary['sheets'],
                    formulaOrMetadataCellsSkipped=summary['formulaOrMetadataCellsSkipped'])
        candidates = [Path(path) for path in paths if Path(path).suffix.lower() == '.' + str(target).lower().lstrip('.')]
        if len(candidates) != 1:
            raise _Limited('numeric_primary_output_mapping')
        output = candidates[0]; before_output = _stamp(output)
        seen, differences = set(), set()
        with zipfile.ZipFile(output) as archive:
            for sheet, part in _sheets(archive, budget):
                def visit(cell):
                    key = (sheet, cell['coordinate'])
                    if key not in risks:
                        return
                    seen.add(key)
                    if cell['type'] != 'n' or cell['formula'] or cell['metadata'] or not cell['value']:
                        differences.add(key); return
                    token, integral = risks[key]
                    expected, observed = _number(token), _number(cell['value'])
                    equal = expected == observed if integral else (
                        math.isfinite(float(expected)) and math.isfinite(float(observed)) and
                        float(expected) == float(observed))
                    if not equal:
                        differences.add(key)
                ranges = _cells(archive, part, budget, visit)
                for key in seen:
                    if key[0] == sheet and _covered(key[1], ranges, budget):
                        differences.add(key)
        differences.update(set(risks) - seen)
        budget.check()
        if initial != _stamp(source) or before_output != _stamp(output):
            raise GuardError('SOURCE_CHANGED_DURING_VALIDATION',
                'Исходник или результат изменился во время проверки чисел.', stage='result')
        if differences:
            raise GuardError('ENGINE_OUTPUT_CONTENT_LOSS',
                'Исходные числовые значения Excel изменились после конвертации. '
                'Результат не отмечен готовым; исходник сохранён.', stage='result',
                diagnostic=json.dumps({'check': 'ooxml_numeric_literals', 'differentCells': len(differences)},
                                      separators=(',', ':')))
        return {**base, 'status': 'checked_subset', 'cellsCompared': len(risks), 'differentCells': 0}
    except _ControlFailure as error:
        raise error.error from error
    except (_Limited, OSError, ValueError, KeyError, zipfile.BadZipFile,
            ET.ParseError, expat.ExpatError, InvalidOperation, OverflowError,
            RuntimeError, NotImplementedError, zlib.error) as error:
        return {**base, 'status': 'not_checked',
                'reason': str(error) if isinstance(error, _Limited) else 'numeric_inspection_' + type(error).__name__,
                'warning': 'NATIVE_OUTPUT_NOT_CHECKED'}
