"""Local, conservative before/after evidence; no AI or conversion engine.

Public results contain bounded descriptions, not source paths or complete
document text. A checked subset never certifies visual/semantic fidelity.
The caller owns the file leases and the killable job-level deadline.
"""
from __future__ import annotations

import csv
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
from pathlib import Path
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
import zipfile

from .errors import GuardError
from .media_fidelity import ALIASES, ANIMATED_TARGETS, RASTER, display_dimensions, inspect_media

MAX_READ = 32 * 1024 * 1024
MAX_XML = 16 * 1024 * 1024
MAX_TEXT = 512000
MAX_CELLS = 50000
MAX_NODES = 200000
MAX_FINDINGS = 64
MAX_SECONDS = 12
MAX_PDF_PAGES = 256
OFFICE = 'urn:oasis:names:tc:opendocument:xmlns:office:1.0'
TABLE = 'urn:oasis:names:tc:opendocument:xmlns:table:1.0'
TEXT = 'urn:oasis:names:tc:opendocument:xmlns:text:1.0'
WORD = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
STRICT_WORD = 'http://purl.oclc.org/ooxml/wordprocessingml/main'
UNCHECKED = ('visual_layout', 'fonts', 'colour_accuracy', 'formula_evaluation', 'semantic_meaning')


class _Limited(ValueError):
    pass


class _Budget:
    def __init__(self, cancel):
        self.cancel = cancel
        self.until = time.monotonic() + MAX_SECONDS
        self.used = 0

    def check(self, count=0):
        if self.cancel is not None and self.cancel.is_set():
            raise GuardError('CANCELLED', 'Анализ до/после отменён.', stage='comparison')
        self.used += count
        if self.used > MAX_READ or time.monotonic() > self.until:
            raise _Limited('inspection_budget')

    def read(self, stream, maximum=MAX_XML):
        data = bytearray()
        while True:
            self.check()
            block = stream.read(min(65536, maximum + 1 - len(data)))
            if not block:
                return bytes(data)
            self.check(len(block))
            data.extend(block)
            if len(data) > maximum:
                raise _Limited('inspection_byte_budget')


def _normal(value):
    return ' '.join(unicodedata.normalize('NFC', str(value)).split())


def _identity(path):
    path = Path(path)
    if path.is_symlink():
        raise _Limited('source_link_not_inspected')
    stamp = path.stat()
    return (str(path.resolve(strict=True)), stamp.st_dev, stamp.st_ino,
            stamp.st_size, stamp.st_mtime_ns, stamp.st_ctime_ns)


def _xml(data, budget):
    declaration = data.replace(b'\0', b'').upper()
    if b'<!DOCTYPE' in declaration or b'<!ENTITY' in declaration:
        raise _Limited('xml_declarations_not_inspected')
    root = ET.fromstring(data)
    pending, count = [(root, 1)], 0
    while pending:
        item, depth = pending.pop()
        budget.check()
        count += 1
        if depth > 128 or count > MAX_NODES:
            raise _Limited('xml_structure_budget')
        pending.extend((child, depth + 1) for child in item)
    return root


def _element_text(item):
    parts = [item.text or '']
    for child in item:
        local = child.tag.rsplit('}', 1)[-1]
        if child.tag == '{%s}s' % TEXT:
            count = int(child.get('{%s}c' % TEXT, '1'))
            if not 1 <= count <= 4096:
                raise _Limited('expanded_spaces_budget')
            parts.append(' ' * count)
        elif child.tag in ('{%s}tab' % TEXT, '{%s}tab' % WORD, '{%s}tab' % STRICT_WORD):
            parts.append('\t')
        elif child.tag in ('{%s}line-break' % TEXT, '{%s}br' % WORD, '{%s}br' % STRICT_WORD):
            parts.append('\n')
        elif child.tag in ('{%s}annotation' % OFFICE, '{%s}note' % TEXT, '{%s}tracked-changes' % TEXT):
            pass
        else:
            parts.append(_element_text(child))
        if local in ('p', 'h'):
            parts.append(' ')
        parts.append(child.tail or '')
    return ''.join(parts)


def _cell_text(cell):
    tags = {'{%s}%s' % (namespace, local) for namespace, locals_ in (
        (TEXT, ('p', 'h')), (WORD, ('p',)), (STRICT_WORD, ('p',))) for local in locals_}
    return '\n'.join(_element_text(item) for item in cell.iter() if item.tag in tags)


def _paragraph_text(root, budget):
    paragraphs = []
    size = 0
    tags = {'{%s}%s' % (namespace, local) for namespace, locals_ in (
        (TEXT, ('p', 'h')), (WORD, ('p',)), (STRICT_WORD, ('p',))) for local in locals_}
    for item in root.iter():
        budget.check()
        if item.tag in tags:
            value = _normal(_element_text(item))
            size += len(value)
            if size > MAX_TEXT:
                raise _Limited('text_budget')
            if value:
                paragraphs.append(value)
    return '\n'.join(paragraphs)


def _value(value, kind='text', *, formula=None):
    # Cell text is data: leading/trailing/repeated spaces must survive. Only
    # document-layout text is whitespace-normalized by its own extractor.
    return {'value': unicodedata.normalize('NFC', str(value)), 'type': kind,
            **({'formula': formula} if formula else {})}


def _trim(rows):
    result = []
    for row in rows:
        row = list(row)
        while row and not row[-1]['value'] and not row[-1].get('formula'):
            row.pop()
        result.append(row)
    while result and not result[-1]:
        result.pop()
    return result


def _odf_grids(root, budget):
    sheets, cells, total_rows = [], 0, 0
    for table in root.findall('.//{%s}table' % TABLE):
        if table.findall('.//{%s}table' % TABLE):
            raise _Limited('nested_table_mapping_not_inspected')
        rows = []
        for row in table.iter('{%s}table-row' % TABLE):
            repeat = int(row.get('{%s}number-rows-repeated' % TABLE, '1'))
            if not 1 <= repeat <= MAX_CELLS:
                raise _Limited('repeated_rows_budget')
            total_rows += repeat
            if total_rows > MAX_CELLS:
                raise _Limited('row_budget')
            items = []
            for cell in row:
                if cell.tag not in ('{%s}table-cell' % TABLE, '{%s}covered-table-cell' % TABLE):
                    continue
                if cell.tag.endswith('}covered-table-cell') or any(
                    cell.get('{%s}%s' % (TABLE, key), '1') != '1'
                    for key in ('number-columns-spanned', 'number-rows-spanned')):
                    raise _Limited('merged_cells_mapping_not_inspected')
                columns = int(cell.get('{%s}number-columns-repeated' % TABLE, '1'))
                if not 1 <= columns <= MAX_CELLS:
                    raise _Limited('repeated_columns_budget')
                kind = cell.get('{%s}value-type' % OFFICE, 'string')
                text = _cell_text(cell)
                if kind in ('float', 'percentage', 'currency'):
                    value = _value(cell.get('{%s}value' % OFFICE, text), 'number')
                elif kind in ('date', 'time', 'boolean'):
                    value = _value(cell.get('{%s}%s-value' % (OFFICE, kind), text), kind)
                else:
                    value = _value(text)
                value['display'] = text
                formula = cell.get('{%s}formula' % TABLE)
                if formula:
                    value['formula'] = formula
                cells += columns * repeat
                if cells > MAX_CELLS:
                    raise _Limited('cell_budget')
                items.extend([value] * columns)
                budget.check()
            rows.extend([items] * repeat)
        sheets.append({'name': table.get('{%s}name' % TABLE, ''), 'rows': _trim(rows)})
    return sheets


def _word_grids(root, budget):
    result, cells = [], 0
    for namespace in (WORD, STRICT_WORD):
        for table in root.findall('.//{%s}tbl' % namespace):
            if table.findall('.//{%s}tbl' % namespace):
                raise _Limited('nested_table_mapping_not_inspected')
            rows = []
            for row in table.findall('{%s}tr' % namespace):
                values = []
                for cell in row.findall('{%s}tc' % namespace):
                    if cell.find('.//{%s}gridSpan' % namespace) is not None or cell.find('.//{%s}vMerge' % namespace) is not None:
                        raise _Limited('merged_cells_mapping_not_inspected')
                    cells += 1
                    if cells > MAX_CELLS:
                        raise _Limited('cell_budget')
                    values.append(_value(_cell_text(cell)))
                    budget.check()
                rows.append(values)
            result.append({'name': '', 'rows': _trim(rows)})
    return result


def _csv_summary(path, budget):
    with path.open('rb') as stream:
        data = budget.read(stream, MAX_READ)
    text = data.decode('utf-8-sig', 'strict')
    if '\0' in text:
        raise _Limited('csv_encoding_not_inspected')
    choices = []
    for delimiter in (',', ';', '\t', '|'):
        reader = csv.reader(io.StringIO(text[:65536], newline=''), delimiter=delimiter, strict=True)
        sample = []
        try:
            for row in reader:
                if row:
                    sample.append(row)
                if len(sample) == 8:
                    break
        except csv.Error:
            continue
        if sample and len(sample[0]) > 1 and all(len(row) == len(sample[0]) for row in sample):
            choices.append(delimiter)
    if len(choices) != 1:
        raise _Limited('csv_dialect_not_determined')
    rows, count = [], 0
    for row in csv.reader(io.StringIO(text, newline=''), delimiter=choices[0], strict=True):
        budget.check()
        if len(rows) >= MAX_CELLS:
            raise _Limited('row_budget')
        count += len(row)
        if count > MAX_CELLS:
            raise _Limited('cell_budget')
        rows.append([_value(value) for value in row])
    return {'kind': 'csv', 'documentClass': 'spreadsheet', 'complete': True,
            '_text': text[:MAX_TEXT], '_grids': [{'name': '', 'rows': _trim(rows)}],
            'counts': {'sheets': 1, 'rows': len(rows), 'columns': max(map(len, rows), default=0)},
            'delimiter': choices[0]}


def _xlsx_summary(path, archive, budget):
    # Openpyxl eagerly loads metadata even in read_only mode. Inspect every
    # non-worksheet XML first, including nonstandard OPC shared string parts.
    declared_xml = set()
    if '[Content_Types].xml' in archive.namelist():
        with archive.open('[Content_Types].xml') as stream:
            declarations = _xml(budget.read(stream), budget)
        for entry in declarations:
            if entry.get('ContentType', '').endswith(('+xml', '/xml')):
                name = entry.get('PartName', '')
                if name.startswith('/'):
                    declared_xml.add(name[1:])
    parts = [item for item in archive.infolist() if item.filename in declared_xml or
             item.filename.lower().endswith(('.xml', '.rels'))]
    if sum(item.file_size for item in parts) > MAX_READ - budget.used:
        raise _Limited('xlsx_xml_budget')
    for item in parts:
        if item.filename != '[Content_Types].xml':
            with archive.open(item) as stream:
                _xml(budget.read(stream), budget)
    from openpyxl import load_workbook
    book = load_workbook(path, read_only=True, data_only=False, keep_links=False)
    try:
        sheets, cells, formulas, styles = [], 0, 0, False
        for sheet in book:
            if sheet.max_row and sheet.max_column and sheet.max_row * sheet.max_column > MAX_CELLS:
                raise _Limited('cell_budget')
            rows = []
            for row in sheet.iter_rows():
                budget.check()
                values = []
                for cell in row:
                    cells += 1
                    if cells > MAX_CELLS:
                        raise _Limited('cell_budget')
                    styles = styles or cell.has_style
                    value = cell.value
                    if cell.data_type == 'f':
                        formulas += 1
                        values.append(_value('', 'unknown', formula=str(value)))
                    elif value is None:
                        values.append(_value(''))
                    elif isinstance(value, bool):
                        values.append(_value(str(value).lower(), 'boolean'))
                    elif isinstance(value, (int, float, Decimal)):
                        values.append(_value(str(value), 'number'))
                    elif hasattr(value, 'isoformat'):
                        values.append(_value(value.isoformat(), 'date'))
                    else:
                        values.append(_value(value))
                rows.append(values)
            sheets.append({'name': sheet.title, 'rows': _trim(rows)})
        return {'kind': 'ooxml', 'documentClass': 'spreadsheet', 'complete': True,
                '_grids': sheets, '_text': '', 'counts': {'sheets': len(sheets), 'cells': cells,
                'formulas': formulas,
                'images': sum(item.filename.startswith('xl/media/') and not item.is_dir() for item in archive.infolist())},
                'hasStyles': styles}
    finally:
        book.close()


def _pdf_summary(path, budget):
    if path.stat().st_size > MAX_READ:
        raise _Limited('pdf_byte_budget')
    from pypdf import PdfReader
    reader = PdfReader(str(path), strict=False)
    try:
        if reader.is_encrypted:
            raise _Limited('encrypted_pdf_not_inspected')
        pages = len(reader.pages)
        if pages > MAX_PDF_PAGES:
            raise _Limited('pdf_page_budget')
        texts, text_size, images, seen, text_complete = [], 0, [], set(), True
        for page in reader.pages:
            budget.check()
            try:
                value = page.extract_text() or ''
                text_size += len(value)
                if text_size > MAX_TEXT:
                    raise _Limited('pdf_text_budget')
                texts.append(value)
            except _Limited:
                raise
            except Exception:
                text_complete = False
            resources = page.get('/Resources', {})
            resources = resources.get_object() if hasattr(resources, 'get_object') else resources
            objects = resources.get('/XObject', {})
            objects = objects.get_object() if hasattr(objects, 'get_object') else objects
            for reference in objects.values():
                budget.check()
                obj = reference.get_object()
                if obj.get('/Subtype') != '/Image':
                    continue
                key = getattr(reference, 'idnum', id(obj))
                if key in seen:
                    continue
                seen.add(key)
                if len(images) > 10000:
                    raise _Limited('pdf_image_budget')
                width, height = obj.get('/Width'), obj.get('/Height')
                if type(width) is int or isinstance(width, int):
                    images.append({'width': int(width), 'height': int(height or 0)})
        return {'kind': 'pdf', 'documentClass': 'rendered', 'complete': True,
                'counts': {'pages': pages, 'images': len(images)}, '_text': '\n'.join(texts),
                'pdfImages': images, '_text_complete': text_complete,
                'unknown': ['pdf_text_extraction', 'nested_or_inline_pdf_images']}
    finally:
        reader.close()


def collect_source_summary(source, *, cancel=None):
    """Return one private, bounded reusable summary for the current source."""
    source = Path(source)
    budget = _Budget(cancel)
    result = {'kind': 'unknown', 'documentClass': 'unknown', 'complete': False,
              'counts': {}, 'unknown': list(UNCHECKED), '_text': '', '_grids': []}
    try:
        identity = _identity(source)
        result['_identity'] = identity
        budget.check()
        with source.open('rb') as stream:
            magic = stream.read(32)
        if magic.startswith((b'\x89PNG\r\n\x1a\n', b'GIF87a', b'GIF89a', b'BM', b'\xff\xd8', b'II*\0', b'MM\0*')) or (
                magic.startswith(b'RIFF') and magic[8:12] == b'WEBP'):
            media = inspect_media(source, cancel=cancel)
            result.update(kind='raster', documentClass='image', complete=True, media=media,
                          counts={k: media[k] for k in ('width', 'height', 'frames', 'pages')})
        elif magic.startswith(b'%PDF-'):
            result.update(_pdf_summary(source, budget))
        elif magic.startswith(b'PK\x03\x04'):
            with zipfile.ZipFile(source) as archive:
                names = archive.namelist()
                if len(names) > 10000 or len(names) != len(set(names)):
                    raise _Limited('package_entry_budget_or_duplicates')
                if 'xl/workbook.xml' in names:
                    result.update(_xlsx_summary(source, archive, budget))
                elif 'word/document.xml' in names:
                    with archive.open('word/document.xml') as stream:
                        root = _xml(budget.read(stream), budget)
                    result.update(kind='ooxml', documentClass='text', complete=True,
                        _text=_paragraph_text(root, budget), _grids=_word_grids(root, budget),
                        hasStyles='word/styles.xml' in names,
                        counts={'tables': sum(1 for x in root.iter() if x.tag.rsplit('}', 1)[-1] == 'tbl'),
                                'images': sum(n.startswith('word/media/') and not n.endswith('/') for n in names),
                                'formulas': sum(x.tag.rsplit('}', 1)[-1] == 'oMath' for x in root.iter())})
                elif 'content.xml' in names and 'mimetype' in names:
                    with archive.open('mimetype') as stream:
                        mime = budget.read(stream, 256).decode('ascii')
                    if not mime.startswith('application/vnd.oasis.opendocument.'):
                        raise _Limited('unrecognized_package')
                    with archive.open('content.xml') as stream:
                        root = _xml(budget.read(stream), budget)
                    kind = mime.rsplit('.', 1)[-1]
                    result.update(kind='odf', documentClass='spreadsheet' if kind == 'spreadsheet' else 'text',
                        complete=True, _text=_paragraph_text(root, budget), _grids=_odf_grids(root, budget),
                        hasStyles=any(any(key.endswith('}style-name') for key in x.attrib) for x in root.iter()),
                        counts={'tables': len(root.findall('.//{%s}table' % TABLE)),
                                'images': sum(n.startswith('Pictures/') and not n.endswith('/') for n in names),
                                'formulas': sum('{%s}formula' % TABLE in x.attrib for x in root.iter())})
                    if result['documentClass'] == 'spreadsheet':
                        result['counts']['sheets'] = len(result['_grids'])
                else:
                    result['reason'] = 'unsupported_package'
        elif source.suffix.lower() in ('.fods', '.fodt', '.fodp', '.fodg'):
            with source.open('rb') as stream:
                root = _xml(budget.read(stream), budget)
            kind = root.get('{%s}mimetype' % OFFICE, '')
            result.update(kind='odf', documentClass='spreadsheet' if kind.endswith('.spreadsheet') else 'text',
                          complete=True, _text=_paragraph_text(root, budget), _grids=_odf_grids(root, budget),
                          counts={'tables': len(root.findall('.//{%s}table' % TABLE)),
                                  'formulas': sum('{%s}formula' % TABLE in x.attrib for x in root.iter())})
        elif source.suffix.lower() in ('.csv', '.tsv', '.tab'):
            result.update(_csv_summary(source, budget))
        elif source.suffix.lower() in ('.txt', '.text', '.md'):
            with source.open('rb') as stream:
                raw = budget.read(stream, MAX_TEXT * 4)
            value = raw.decode('utf-8-sig', 'strict')
            if '\0' in value:
                raise _Limited('text_encoding_not_inspected')
            if len(value) > MAX_TEXT:
                raise _Limited('text_budget')
            result.update(kind='text', documentClass='text', complete=True, _text=value)
        else:
            result['reason'] = 'unsupported_format'
        budget.check()
        if _identity(source) != identity:
            raise _Limited('source_changed_during_analysis')
        text = result.get('_text', '')
        result['textDigest'] = hashlib.sha256(_normal(text).encode()).hexdigest() if text else None
        result['counts']['characters'] = len(text)
    except GuardError as error:
        if error.code == 'CANCELLED':
            raise
        result.update(complete=False, reason=error.code)
    except (_Limited, UnicodeError, ET.ParseError, zipfile.BadZipFile, OSError, ValueError, KeyError) as error:
        result.update(complete=False, reason=str(error) if isinstance(error, _Limited) else 'inspection_failed')
    except Exception:
        result.update(complete=False, reason='inspection_failed')
    if not result.get('complete'):
        result['_text'], result['_grids'] = '', []
    return result


def _public(summary):
    return {key: summary[key] for key in ('kind', 'documentClass', 'complete', 'counts', 'reason', 'textDigest') if key in summary}


def _number(value):
    try:
        result = Decimal(value)
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _compare_grids(before, after, findings, checked, unknown):
    from collections import Counter
    a, b = before.get('_grids', []), after.get('_grids', [])
    if not a or not b:
        return
    # Mapping multiple sheets/tables merely by order would invent losses when
    # exporters rename/reorder them. Only a single unambiguous grid is proven.
    if len(a) != 1 or len(b) != 1:
        unknown.add('multiple_table_or_sheet_mapping')
        return
    rows_a, rows_b = a[0]['rows'], b[0]['rows']
    changed, representation, unknown_formula = [], [], False
    for ri, row in enumerate(rows_a):
        for ci, cell in enumerate(row):
            if cell.get('formula') and (cell['type'] == 'unknown' or not cell['value']):
                unknown_formula = True
                continue
            expected = cell['value']
            actual = rows_b[ri][ci]['value'] if ri < len(rows_b) and ci < len(rows_b[ri]) else ''
            if actual == expected:
                continue
            output_cell = rows_b[ri][ci] if ri < len(rows_b) and ci < len(rows_b[ri]) else None
            special_type = cell['type'] in ('date', 'time', 'boolean') or (
                output_cell is not None and output_cell['type'] in ('date', 'time', 'boolean'))
            if special_type or (actual and cell['type'] == 'number' and _number(actual) is None):
                # ISO/native typed values and locale-formatted CSV display
                # strings cannot be called lost without a verified dialect.
                representation.append({'row': ri + 1, 'column': ci + 1,
                                       'before': expected[:120], 'after': actual[:120]})
                continue
            number_a, number_b = _number(expected), _number(actual)
            if number_a is not None and number_b is not None and number_a == number_b:
                if cell['type'] == 'text':
                    representation.append({'row': ri + 1, 'column': ci + 1,
                                           'before': expected[:120], 'after': actual[:120]})
                continue
            if not expected and not actual:
                continue
            changed.append({'row': ri + 1, 'column': ci + 1,
                            'before': expected[:120], 'after': actual[:120]})
    if changed:
        # Different coordinates alone are not lost content: an exporter may
        # reorder rows/columns. Confirm only missing value multiplicity in
        # the complete, unambiguous inspected grid; keep moved values uncertain.
        def token(value):
            number = _number(value)
            return ('number', number) if number is not None else ('text', value)
        source_values = Counter(token(cell['value']) for row in rows_a for cell in row
                                if cell['value'] and not (cell.get('formula') and cell['type'] == 'unknown'))
        result_values = Counter(token(cell['value']) for row in rows_b for cell in row if cell['value'])
        missing = source_values - result_values
        lost, moved = [], []
        for entry in changed:
            key = token(entry['before'])
            if entry['before'] and missing[key]:
                lost.append(entry)
                missing[key] -= 1
            else:
                moved.append(entry)
        if lost:
            findings.append({'code': 'CELL_VALUES_CHANGED', 'category': 'confirmed_loss', 'feature': 'cell_values',
                'message': 'Исходные значения отсутствуют в проверенной таблице результата.', 'certainty': 'confirmed_subset',
                'before': len(lost), 'after': None, 'locations': lost[:25], 'totalAffected': len(lost)})
        if moved:
            findings.append({'code': 'CELL_POSITIONS_CHANGED', 'category': 'uncertain', 'feature': 'cell_relationships',
                'message': 'Расположение значений изменилось; потеря самих значений не доказана.',
                'certainty': 'requires_review', 'locations': moved[:25], 'totalAffected': len(moved)})
    elif not unknown_formula and not representation:
        checked.add('cell_values_single_grid')
    if representation:
        findings.append({'code': 'CELL_REPRESENTATION_CHANGED', 'category': 'uncertain', 'feature': 'cell_text',
            # This bucket includes verified numeric equivalence of TEXT cells,
            # but also typed dates, booleans and numeric strings whose locale is
            # unknown. Never assert equality for the entire bucket: "1,23" may
            # be a rounded export of 1.23456789012345, not the same value.
            'message': 'Представление значений изменилось; совпадение данных с учётом типа, локализации и точности требует проверки.',
            'certainty': 'requires_review', 'locations': representation[:25], 'totalAffected': len(representation)})
    if unknown_formula:
        unknown.add('cached_formula_values')


def _multi_csv_summary(paths, before, budget, findings, checked, unknown):
    """Prove all sheet VALUES by multiplicity without guessing sheet order.

    This does not certify names or map an ambiguous mismatch to a source row.
    Complete equal multiset evidence is useful for existing all-sheet exports.
    """
    from collections import Counter
    if len(paths) > 256:
        raise _Limited('sheet_output_budget')
    expected, observed, cells = Counter(), Counter(), 0

    def signature(grid):
        nonlocal cells
        values = [[cell['value'] for cell in row] for row in grid['rows']]
        cells += sum(map(len, values))
        if cells > MAX_CELLS * 2:
            raise _Limited('cell_budget')
        return hashlib.sha256(json.dumps(values, ensure_ascii=False,
                             separators=(',', ':')).encode()).hexdigest()

    grids = before.get('_grids', [])
    for grid in grids:
        if any(cell.get('formula') and (cell['type'] == 'unknown' or not cell['value'])
               for row in grid['rows'] for cell in row):
            raise _Limited('cached_formula_values')
        expected[signature(grid)] += 1
    for path in paths:
        budget.check()
        value = _csv_summary(path, budget)
        observed[signature(value['_grids'][0])] += 1
    if grids and len(grids) == len(paths) and expected == observed:
        checked.add('all_sheet_values_unordered')
        unknown.add('sheet_names_and_identity')
    else:
        findings.append({'code': 'MULTI_SHEET_VALUES_NOT_MATCHED', 'category': 'uncertain',
            'feature': 'cell_values', 'message': 'Не удалось сопоставить все исходные листы и текстовые результаты.',
            'before': len(grids), 'after': len(paths), 'certainty': 'requires_review'})
        unknown.add('all_sheet_outputs_mapping')
    return {'kind': 'csv', 'documentClass': 'spreadsheet', 'complete': True,
            'counts': {'sheetFiles': len(paths)}, '_text': '', '_grids': []}


def analyze_pair(source, paths, target, *, cancel=None, source_evidence=None):
    """Compare one retained source/target pair without replacing either file."""
    source, paths = Path(source), [Path(path) for path in paths]
    target = str(target).lower().lstrip('.')
    before = source_evidence or collect_source_summary(source, cancel=cancel)
    report = {'schemaVersion': 1, 'status': 'not_checked', 'qualityCompared': False,
              'scope': 'verified_properties_only', 'findings': [], 'checkedFeatures': [],
              'unknown': list(UNCHECKED), 'recipeCandidates': [], 'before': _public(before), 'after': {}}
    findings, checked, unknown = report['findings'], set(), set(UNCHECKED)
    budget = _Budget(cancel)
    budget.check()
    try:
        if tuple(before.get('_identity', ())) != _identity(source):
            raise _Limited('source_changed_or_unavailable')
        candidates = [p for p in paths if p.suffix.lower().lstrip('.') == target]
        if not candidates and target in RASTER:
            canonical = ALIASES.get(target, target)
            candidates = [p for p in paths if ALIASES.get(p.suffix.lower().lstrip('.'), p.suffix.lower().lstrip('.')) == canonical]
        multi_csv = len(candidates) > 1 and target in ('csv', 'tsv', 'tab')
        if len(candidates) != 1 and not multi_csv:
            raise _Limited('primary_output_mapping_not_determined')
        primary = candidates[0]
        after = (_multi_csv_summary(candidates, before, budget, findings, checked, unknown)
                 if multi_csv else collect_source_summary(primary, cancel=cancel))
        report['after'] = _public(after)
        unknown.update(before.get('unknown', ()))
        unknown.update(after.get('unknown', ()))
        if not before.get('complete') or not after.get('complete'):
            findings.append({'code': 'ANALYSIS_INCOMPLETE', 'category': 'not_checked',
                'message': 'Часть свойств не удалось надёжно проверить.',
                'certainty': 'unknown', 'reason': before.get('reason') or after.get('reason')})
        if not multi_csv and primary.suffix.lower() == source.suffix.lower() and primary.stat().st_size == source.stat().st_size:
            # Stream comparison is exact and bounded; do not retain whole files.
            identical = True
            with source.open('rb') as left, primary.open('rb') as right:
                while True:
                    budget.check()
                    a, b = left.read(65536), right.read(65536)
                    if a != b:
                        identical = False
                        break
                    if not a:
                        break
            if identical:
                checked.add('identical_file_bytes')
        media_a, media_b = before.get('media'), after.get('media')
        if target in RASTER:
            # Source probing tolerates recoverable inputs. Output container
            # verification must not accept an incomplete SOF/BMP header alone.
            verified = inspect_media(primary, cancel=cancel, validate_container=True)
            if verified is None or verified['format'] != ALIASES.get(target, target):
                raise GuardError('MEDIA_OUTPUT_INVALID', 'Неверный контейнер результата.', stage='comparison')
        if media_a and media_b:
            for field, supported in (('frames', target in ANIMATED_TARGETS), ('pages', target in ('tif', 'tiff'))):
                a, b = media_a.get(field, 1), media_b.get(field, 1)
                if a != b:
                    expected = a > 1 and not supported
                    findings.append({'code': 'FORMAT_' + field.upper() + '_LIMITATION' if expected else 'MEDIA_' + field.upper() + '_CHANGED',
                        'category': 'format_limitation' if expected else 'confirmed_loss', 'feature': field,
                        'message': ('Выбранный формат не сохраняет ' + ('анимацию.' if field == 'frames' else 'несколько страниц.')) if expected else
                                   ('Число кадров изменилось.' if field == 'frames' else 'Число страниц изменилось.'),
                        'before': a, 'after': b, 'certainty': 'format_contract' if expected else 'confirmed_subset'})
                else:
                    checked.add(field)
            if display_dimensions(media_a) != display_dimensions(media_b):
                findings.append({'code': 'IMAGE_DIMENSIONS_CHANGED', 'category': 'confirmed_loss', 'feature': 'image_dimensions',
                    'message': 'Размеры изображения изменились.', 'before': list(display_dimensions(media_a)),
                    'after': list(display_dimensions(media_b)), 'certainty': 'confirmed_subset'})
            else:
                checked.add('image_dimensions')
            if media_a.get('alpha') and not media_b.get('alpha'):
                expected = ALIASES.get(target, target) == 'jpeg'
                bmp_unknown = False
                if media_b['format'] == 'bmp':
                    with primary.open('rb') as stream:
                        head = stream.read(70)
                    depth = int.from_bytes(head[28:30], 'little') if len(head) >= 30 else None
                    # The generic BMP inspector does not resolve 32-bit alpha
                    # masks. Its default False is not proof of absent alpha.
                    bmp_unknown = depth == 32
                if bmp_unknown:
                    unknown.add('bmp_alpha_channel')
                else:
                    findings.append({'code': 'FORMAT_ALPHA_LIMITATION' if expected else 'IMAGE_ALPHA_LOST',
                        'category': 'format_limitation' if expected else 'confirmed_loss', 'feature': 'alpha_channel',
                        'message': 'JPEG не сохраняет канал прозрачности.' if expected else 'В результате нет канала прозрачности исходника.',
                        'before': True, 'after': False, 'certainty': 'format_contract' if expected else 'confirmed_subset'})
        if target in ('csv', 'tsv', 'tab', 'txt') and before.get('hasStyles'):
            findings.append({'code': 'FORMAT_STYLES_LIMITATION', 'category': 'format_limitation', 'feature': 'styles',
                'message': 'Текстовый формат не сохраняет оформление документа.', 'certainty': 'format_contract'})
        if before.get('counts', {}).get('formulas') and target in ('csv', 'tsv', 'tab', 'txt', 'pdf'):
            findings.append({'code': 'FORMAT_EDITABLE_FORMULAS_LIMITATION', 'category': 'format_limitation', 'feature': 'formulas',
                'message': 'В результате нет редактируемой модели формул исходной книги.', 'certainty': 'format_contract'})
        if before.get('complete') and after.get('complete'):
            if before.get('documentClass') == after.get('documentClass') and before.get('_grids') and after.get('_grids'):
                _compare_grids(before, after, findings, checked, unknown)
            text_a, text_b = _normal(before.get('_text', '')), _normal(after.get('_text', ''))
            text_complete = before.get('_text_complete', True) and after.get('_text_complete', True)
            if text_a and text_b and text_complete and before.get('documentClass') != 'spreadsheet':
                if text_a == text_b:
                    checked.add('normalized_text')
                else:
                    findings.append({'code': 'TEXT_REPRESENTATION_DIFFERS', 'category': 'uncertain', 'feature': 'text',
                        'message': 'Извлечённый текст отличается; это ещё не доказывает исчезновение текста в документе.',
                        'certainty': 'requires_review'})
            elif text_a and (not text_b or not text_complete) and after.get('kind') == 'pdf':
                findings.append({'code': 'PDF_TEXT_NOT_EXTRACTED', 'category': 'uncertain', 'feature': 'text',
                    'message': 'Текст PDF не извлечён. Это не доказательство потери видимого текста.', 'certainty': 'requires_review'})
            if after.get('kind') == 'pdf' and before.get('counts', {}).get('images', 0) > 0 and not media_a:
                findings.append({'code': 'PDF_IMAGE_PROPERTIES_UNVERIFIED', 'category': 'uncertain', 'feature': 'images',
                    'message': 'Встроенные изображения требуют проверки качества PDF; число объектов не доказывает сохранность пикселей.',
                    'certainty': 'requires_review'})
                report['recipeCandidates'].append('pdf_preserve_images')
            if target in ('csv', 'tsv', 'tab') and any(f['code'] in ('CELL_VALUES_CHANGED', 'CELL_REPRESENTATION_CHANGED') for f in findings):
                report['recipeCandidates'].append('csv_preserve_values')
        if (before.get('documentClass') == 'spreadsheet' and target in ('csv', 'tsv', 'tab')
                and len(before.get('_grids', [])) > 1 and 'all_sheet_values_unordered' not in checked):
            unknown.add('all_sheet_outputs_mapping')
        if 'identical_file_bytes' in checked:
            # Identical files have no conversion changes, even if a subset
            # inspector is unavailable. Unknown visual properties stay explicit.
            findings[:] = [f for f in findings if f['category'] == 'not_checked']
        budget.check()
        if tuple(before.get('_identity', ())) != _identity(source):
            raise _Limited('source_changed_during_analysis')
        if not multi_csv and after.get('_identity') and tuple(after['_identity']) != _identity(primary):
            raise _Limited('result_changed_during_analysis')
        categories = {f['category'] for f in findings}
        report['status'] = ('confirmed_loss' if 'confirmed_loss' in categories else
                            'uncertain' if 'uncertain' in categories else
                            'format_limitation' if 'format_limitation' in categories else
                            'checked' if checked else 'not_checked')
    except GuardError as error:
        if error.code == 'CANCELLED':
            raise
        findings.append({'code': error.code, 'category': 'confirmed_loss' if error.code == 'MEDIA_OUTPUT_INVALID' else 'not_checked',
            'message': 'Контейнер результата повреждён.' if error.code == 'MEDIA_OUTPUT_INVALID' else 'Проверка результата не завершена.',
            'certainty': 'confirmed_subset' if error.code == 'MEDIA_OUTPUT_INVALID' else 'unknown'})
        report['status'] = 'confirmed_loss' if error.code == 'MEDIA_OUTPUT_INVALID' else 'not_checked'
    except (_Limited, OSError, ValueError) as error:
        # A changed/deleted input or interrupted inspection invalidates the
        # comparison as a whole. Do not leave stale red claims or recipes.
        checked.clear()
        findings.clear()
        report['recipeCandidates'] = []
        report['status'] = 'not_checked'
        findings.append({'code': 'ANALYSIS_UNAVAILABLE', 'category': 'not_checked',
            'message': 'Анализ этого преобразования недоступен.', 'certainty': 'unknown',
            'reason': str(error) if isinstance(error, _Limited) else 'file_unavailable'})
    report['findings'] = findings[:MAX_FINDINGS]
    report['checkedFeatures'] = sorted(checked)
    report['unknown'] = sorted(unknown)
    report['qualityCompared'] = bool(checked)
    return report


def preview_text(path, *, max_chars=100000, cancel=None):
    """A labelled text representation, never a fake visual document preview."""
    if type(max_chars) is not int or not 1 <= max_chars <= MAX_TEXT:
        raise ValueError('invalid_preview_budget')
    evidence = collect_source_summary(Path(path), cancel=cancel)
    text = evidence.get('_text', '')
    if not text and evidence.get('_grids'):
        parts = []
        size = 0
        for sheet in evidence['_grids']:
            for row in sheet['rows']:
                value = '\t'.join(cell['value'] for cell in row)
                parts.append(value)
                size += len(value)
                if size > max_chars:
                    break
            if size > max_chars:
                break
        text = '\n'.join(parts)
    if not evidence.get('complete') or not text:
        return {'kind': 'unavailable', 'text': '', 'truncated': False,
                'note': 'Текстовый предпросмотр этого файла недоступен.',
                'reason': evidence.get('reason', 'no_text_representation')}
    return {'kind': 'text', 'text': text[:max_chars], 'truncated': len(text) > max_chars,
            'note': 'Текстовое представление: оформление и расположение объектов не показаны.'}
