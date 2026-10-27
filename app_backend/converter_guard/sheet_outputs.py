"""All-sheet output evidence shared with LibreOffice's private Python worker.

This reads CSV and records its provenance; only LibreOffice produces data.
Sheet names are metadata and never participate in filesystem paths.
"""

SHEET_OUTPUT_SOURCE = r'''
import csv as _sheet_csv
import hashlib as _sheet_hash
import json as _sheet_json
import re as _sheet_re
from decimal import Decimal as _SheetDecimal, InvalidOperation as _SheetInvalid
from pathlib import Path as _SheetPath

SHEET_MANIFEST = 'AIO-sheets.json'
SHEET_CELL_BUDGET = 1000000
SHEET_BYTE_BUDGET = 64 * 1024 * 1024

class SheetOutputMismatch(RuntimeError):
    pass

def sheet_output_name(stem, index, extension):
    # An ordinal is unique even for duplicate names, Unicode normalization,
    # case-insensitive filesystems and names such as CON or ../../elsewhere.
    clean = _sheet_re.sub(r'[^\w .-]', '_', str(stem), flags=_sheet_re.UNICODE)
    clean = clean.strip(' .')[:60].rstrip(' .') or 'workbook'
    if clean.split('.')[0].upper() in {'CON','PRN','AUX','NUL',
            *('COM' + str(i) for i in range(1, 10)),
            *('LPT' + str(i) for i in range(1, 10))}:
        clean = '_' + clean
    if type(index) is not int or index < 0 or extension not in ('csv','tab','tsv','fods'):
        raise ValueError('invalid_sheet_output_identity')
    return clean + '__sheet_%04d.' % (index + 1) + extension

def _sheet_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise InterruptedError('sheet_output_cancelled')

def _sheet_equal(text, expected, raw_values):
    if not raw_values or isinstance(expected, str):
        return text.replace('\r\n', '\n') == str(expected).replace('\r\n', '\n')
    try:
        return _SheetDecimal(text) == _SheetDecimal(str(expected))
    except (_SheetInvalid, ValueError):
        return False

def verify_csv_sheet(path, extension, rows, columns, expected, *, raw_values=False, cancel=None):
    """Compare every inspected CSV field against this sheet's native values.

    Empty trailing rows/columns may be omitted by the native flat exporter;
    no nonempty cell may disappear, move or be introduced. Budget exhaustion
    is explicit unknown evidence, never a successful content comparison.
    """
    path = _SheetPath(path)
    if path.is_symlink() or not path.is_file():
        raise SheetOutputMismatch('csv_output_missing')
    if rows * columns > SHEET_CELL_BUDGET or path.stat().st_size > SHEET_BYTE_BUDGET:
        return {'status':'not_checked', 'reason':'csv_cell_comparison_budget'}
    previous = _sheet_csv.field_size_limit()
    _sheet_csv.field_size_limit(1024 * 1024)
    checked, observed_rows, observed_columns = 0, 0, 0
    try:
        with path.open('r', encoding='utf-8-sig', newline='') as stream:
            reader = _sheet_csv.reader(stream, delimiter=',' if extension == 'csv' else '\t', strict=True)
            for r, record in enumerate(reader):
                _sheet_cancel(cancel)
                observed_rows = r + 1
                observed_columns = max(observed_columns, len(record))
                if r >= rows:
                    if any(record):
                        raise SheetOutputMismatch('csv_extra_row:%d' % r)
                    continue
                for c in range(max(columns, len(record))):
                    value = expected(r, c) if c < columns else ''
                    actual = record[c] if c < len(record) else ''
                    if not _sheet_equal(actual, value, raw_values):
                        raise SheetOutputMismatch('csv_cell_mismatch:%d:%d' % (r, c))
                    checked += 1
            for r in range(observed_rows, rows):
                _sheet_cancel(cancel)
                for c in range(columns):
                    if not _sheet_equal('', expected(r, c), raw_values):
                        raise SheetOutputMismatch('csv_missing_row:%d:%d' % (r, c))
                    checked += 1
    except _sheet_csv.Error as error:
        if 'field larger' in str(error):
            return {'status':'not_checked', 'reason':'csv_field_comparison_budget'}
        raise SheetOutputMismatch('csv_parse_failed') from error
    except UnicodeError as error:
        raise SheetOutputMismatch('csv_encoding_changed') from error
    finally:
        _sheet_csv.field_size_limit(previous)
    return {'status':'checked_subset', 'checkedCells':checked,
            'csvRows':observed_rows, 'csvColumns':observed_columns,
            'checkedFeatures':['native_sheet_display_values' if not raw_values else 'native_sheet_raw_values']}

def sheet_file_evidence(path, index, name, check, *, cancel=None):
    path = _SheetPath(path)
    if path.is_symlink() or not path.is_file():
        raise SheetOutputMismatch('sheet_output_missing')
    digest, size = _sheet_hash.sha256(), 0
    with path.open('rb') as stream:
        before = __import__('os').fstat(stream.fileno())
        while True:
            _sheet_cancel(cancel)
            chunk = stream.read(65536)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
        after = __import__('os').fstat(stream.fileno())
    if any(getattr(before, key) != getattr(after, key)
            for key in ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')):
        raise SheetOutputMismatch('sheet_output_changed')
    return {'sourceSheetIndex':index, 'sourceSheetName':name,
            'file':path.name, 'size':size, 'sha256':digest.hexdigest(), 'contentCheck':check}

def write_sheet_manifest(output, entries):
    if not entries or [e['sourceSheetIndex'] for e in entries] != list(range(len(entries))):
        raise SheetOutputMismatch('sheet_mapping_incomplete')
    path = _SheetPath(output) / SHEET_MANIFEST
    with path.open('x', encoding='utf-8') as stream:
        _sheet_json.dump({'schemaVersion':1, 'scope':'whole_workbook', 'sheetCount':len(entries),
            'sheetIndexBase':0, 'qualityCompared':False,
            'formatLimitations':['formulas_as_values','styles_and_charts_not_represented'],
            'sheets':entries}, stream, ensure_ascii=True, separators=(',',':'))
        stream.flush()
        __import__('os').fsync(stream.fileno())
    return path
'''

exec(compile(SHEET_OUTPUT_SOURCE, __file__, 'exec'))


def verify_fods_csv(snapshot, output, extension, *, raw_values=False, cancel=None):
    """Read an already validated, native single-sheet FODS value snapshot."""
    from xml.etree import ElementTree as ET
    from .legacy_sheets import OFFICE, TABLE
    text_ns = 'urn:oasis:names:tc:opendocument:xmlns:text:1.0'
    if _SheetPath(snapshot).stat().st_size > SHEET_BYTE_BUDGET:
        return {'status':'not_checked', 'reason':'native_snapshot_comparison_budget'}
    cells = {}
    def shown(node):
        result = node.text or ''
        for child in node:
            if child.tag == '{%s}s' % text_ns:
                count = int(child.get('{%s}c' % text_ns, '1'))
                if not 0 <= count <= 1024 * 1024:
                    raise OverflowError('text_expansion_budget')
                value = ' ' * count
            elif child.tag == '{%s}tab' % text_ns:
                value = '\t'
            elif child.tag == '{%s}line-break' % text_ns:
                value = '\n'
            else:
                value = shown(child)
            result += value + (child.tail or '')
        return result
    try:
        root = ET.parse(snapshot).getroot()
        tables = root.findall('{%s}body/{%s}spreadsheet/{%s}table' % (OFFICE, OFFICE, TABLE))
        if len(tables) != 1:
            raise SheetOutputMismatch('native_snapshot_sheet_count')
        row, columns = 0, 0
        for record in tables[0].iter('{%s}table-row' % TABLE):
            _sheet_cancel(cancel)
            repeated = int(record.get('{%s}number-rows-repeated' % TABLE, '1'))
            values, column = [], 0
            for cell in record:
                if cell.tag not in ('{%s}table-cell' % TABLE, '{%s}covered-table-cell' % TABLE):
                    continue
                count = int(cell.get('{%s}number-columns-repeated' % TABLE, '1'))
                if count < 1 or repeated < 1 or column + count > SHEET_CELL_BUDGET:
                    raise OverflowError('native_snapshot_expansion_budget')
                paragraphs = cell.findall('{%s}p' % text_ns)
                value = '\n'.join(shown(p) for p in paragraphs)
                if not paragraphs:
                    value = cell.get('{%s}string-value' % OFFICE, '')
                kind = cell.get('{%s}value-type' % OFFICE)
                if raw_values and kind in ('float', 'currency', 'percentage'):
                    value = _SheetDecimal(cell.get('{%s}value' % OFFICE))
                values.extend([value] * count)
                column += count
            if (row + repeated) * max(columns, column, 1) > SHEET_CELL_BUDGET:
                raise OverflowError('native_snapshot_expansion_budget')
            for offset in range(repeated):
                for c, value in enumerate(values):
                    if value != '':
                        cells[row + offset, c] = value
            row += repeated
            columns = max(columns, column)
        return verify_csv_sheet(output, extension, row, columns,
            lambda r,c: cells.get((r,c), ''), raw_values=raw_values, cancel=cancel)
    except (OverflowError, _SheetInvalid, TypeError, ET.ParseError) as error:
        return {'status':'not_checked', 'reason':'native_snapshot_values_unavailable',
                'detail':type(error).__name__}


def read_sheet_manifest(paths, *, cancel=None):
    """Validate the complete manifest/file set in the owning output directory."""
    from .errors import GuardError
    paths = list(map(_SheetPath, paths))
    manifests = [p for p in paths if p.name == SHEET_MANIFEST]
    if not manifests:
        return None
    try:
        if len(manifests) != 1 or manifests[0].is_symlink() or manifests[0].stat().st_size > 8 * 1024 * 1024:
            raise SheetOutputMismatch('sheet_manifest_invalid')
        manifest = _sheet_json.loads(manifests[0].read_text(encoding='utf-8'))
        entries = manifest['sheets']
        if (manifest.get('schemaVersion') != 1 or manifest.get('scope') != 'whole_workbook'
                or type(manifest.get('sheetCount')) is not int or not entries
                or manifest['sheetCount'] != len(entries)
                or [e['sourceSheetIndex'] for e in entries] != list(range(len(entries)))):
            raise SheetOutputMismatch('sheet_mapping_incomplete')
        by_name = {p.name:p for p in paths if p != manifests[0]}
        if (len(by_name) != len(entries) or len({e['file'].casefold() for e in entries}) != len(entries)
                or set(by_name) != {e['file'] for e in entries}):
            raise SheetOutputMismatch('sheet_output_set_mismatch')
        for entry in entries:
            if not isinstance(entry['sourceSheetName'], str) or len(entry['sourceSheetName']) > 32767:
                raise SheetOutputMismatch('sheet_name_invalid')
            current = sheet_file_evidence(by_name[entry['file']], entry['sourceSheetIndex'],
                entry['sourceSheetName'], entry['contentCheck'], cancel=cancel)
            if (current['size'], current['sha256']) != (entry['size'], entry['sha256']):
                raise SheetOutputMismatch('sheet_output_changed')
        return manifest
    except InterruptedError as error:
        raise GuardError('CANCELLED', 'Проверка листов отменена.', stage='result') from error
    except (KeyError, TypeError, ValueError, OSError, SheetOutputMismatch) as error:
        raise GuardError('ENGINE_OUTPUT_CONTENT_LOSS',
            'Не подтверждён полный комплект листов. Результат не отмечен готовым.',
            stage='result', diagnostic=str(error)) from error
