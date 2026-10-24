"""Preserve mixed DBF columns in private, native-produced Calc snapshots."""
import json
import io
from pathlib import Path
import tempfile
import struct
from xml.parsers import expat
from xml.sax.saxutils import XMLGenerator

from .legacy_sheets import _secure_parser, _expanded, _scope, _restore, _check, _invalid, BLOCK

O = 'urn:oasis:names:tc:opendocument:xmlns:office:1.0'
T = 'urn:oasis:names:tc:opendocument:xmlns:table:1.0'
X = 'urn:oasis:names:tc:opendocument:xmlns:text:1.0'


class _Cells:
    def __init__(self, cancel, *, output=None, mapping=None, profile=None):
        self.cancel, self.output, self.mapping, self.profile = cancel, output, mapping, profile
        self.gen = XMLGenerator(output, encoding='utf-8') if output else None
        self.namespaces, self.stack = {}, []
        self.row = 0
        self.current_row = self.cell = None
        self.sheets = 0
        self.sheet_depth = None
        self.first = None
        self.first_cells, self.kinds = {}, {}
        self.minimum, self.maximum = 16384, -1
        self.skip = None
        if self.gen:
            self.gen.startDocument()

    def _number(self, attrs, key, default=1):
        value = int(attrs.get(key, default))
        if value < 1 or value > 1048576:
            _invalid('native_repeat_count')
        return value

    def start(self, name, attrs):
        _check(self.cancel)
        scope = _scope(attrs, self.namespaces)
        tag = _expanded(name, self.namespaces)
        self.stack.append((tag, scope))
        depth = len(self.stack)
        expanded = {_expanded(key, self.namespaces): value for key, value in attrs.items()}
        if depth == 1 and (tag != (O, 'document') or
                expanded.get((O, 'mimetype')) != 'application/vnd.oasis.opendocument.spreadsheet'):
            _invalid('memo_requires_native_flat_calc')
        if tag == (T, 'table') and len(self.stack) > 1 and self.stack[-2][0] == (O, 'spreadsheet'):
            self.sheets += 1
            self.sheet_depth = depth
        if self.skip is not None:
            return
        if (tag == (T, 'table-row') and self.current_row is None and self.sheet_depth is not None
                and sum(item[0] == (T, 'table') for item in self.stack) == 1):
            repeat = self._number(expanded, (T, 'number-rows-repeated'))
            self.current_row = {'depth': depth, 'start': self.row, 'repeat': repeat, 'column': 0, 'cells': []}
            if self.gen and self.row == self.profile['firstRow']:
                self._header()
                if self.profile['hasHeader']:
                    if repeat == 1:
                        self.skip = depth
                        return
                    attrs = {key: value for key, value in attrs.items()
                             if _expanded(key, self.namespaces) != (T, 'number-rows-repeated')}
                    attrs = {**attrs, 'xmlns:aio_dbt': T, 'aio_dbt:number-rows-repeated': str(repeat-1)}
        if (tag in ((T, 'table-cell'), (T, 'covered-table-cell')) and self.current_row
                and depth == self.current_row['depth'] + 1):
            count = self._number(expanded, (T, 'number-columns-repeated'))
            column = self.current_row['column']
            if column + count > 16384:
                _invalid('native_column_count')
            self.current_row['column'] += count
            self.cell = {'depth': depth, 'name': name, 'attrs': dict(attrs), 'expanded': expanded,
                         'column': column, 'repeat': count, 'text': [], 'paragraphs': 0, 'paragraphDepth': None}
            if self.gen:
                self.cell['inner'] = io.TextIOWrapper(
                    tempfile.SpooledTemporaryFile(max_size=1024*1024, mode='w+b'), encoding='utf-8')
                self.cell['writer'] = XMLGenerator(self.cell['inner'], encoding='utf-8')
            return
        if self.cell:
            if tag == (X, 'p'):
                if self.cell['paragraphs']:
                    self.cell['text'].append('\n')
                self.cell['paragraphs'] += 1
                self.cell['paragraphDepth'] = depth
            elif tag == (X, 's'):
                count = int(expanded.get((X, 'c'), 1))
                if count < 1 or count > 32767:
                    _invalid('native_text_space_count')
                self.cell['text'].append(' ' * count)
            elif tag in ((X, 'tab'), (X, 'line-break')):
                self.cell['text'].append('\t' if tag[1] == 'tab' else '\n')
            if self.gen:
                self.cell['writer'].startElement(name, attrs)
        elif self.gen:
            self.gen.startElement(name, attrs)

    def text(self, value):
        if self.skip is not None:
            return
        if self.cell:
            if self.cell['paragraphDepth'] is not None:
                self.cell['text'].append(value)
            if self.gen:
                self.cell['writer'].characters(value)
        elif self.gen:
            self.gen.characters(value)

    def _value(self, cell):
        attrs = cell['expanded']
        kind = attrs.get((O, 'value-type'), 'empty')
        raw = next((attrs[(O, key)] for key in ('value', 'date-value', 'time-value', 'boolean-value', 'string-value')
                    if (O, key) in attrs), None)
        display = ''.join(cell['text'])
        if kind == 'string' and (O, 'string-value') in attrs:
            display = attrs[(O, 'string-value')]
        if not display and raw is not None:
            display = raw
        if kind == 'string' and raw is None:
            raw = display
        scalar = 'text' if kind == 'string' and display else 'number' if kind not in ('empty', 'string') else None
        return kind, raw, display, scalar

    def _header(self):
        p = self.profile
        self.gen.startElement('aio_dt:table-row', {'xmlns:aio_dt': T, 'xmlns:aio_do': O, 'xmlns:aio_dx': X})
        if p['minimum']:
            self.gen.startElement('aio_dt:table-cell', {'aio_dt:number-columns-repeated': str(p['minimum'])})
            self.gen.endElement('aio_dt:table-cell')
        for col in range(p['minimum'], p['maximum'] + 1):
            text = p['headers'].get(col, 'FIELD' + str(col + 1)) if p['hasHeader'] else 'FIELD' + str(col + 1)
            if col in p['mixed']:
                text = text.split(',')[0] + ',M'
            self.gen.startElement('aio_dt:table-cell', {'aio_do:value-type': 'string', 'aio_do:string-value': text})
            self.gen.startElement('aio_dx:p', {});self.gen.characters(text);self.gen.endElement('aio_dx:p')
            self.gen.endElement('aio_dt:table-cell')
        self.gen.endElement('aio_dt:table-row')

    def _emit_cell(self, cell):
        kind, raw, display, scalar = self._value(cell)
        begin, end = cell['column'], cell['column'] + cell['repeat']
        column = begin
        while column < end:
            memo = column in self.profile['mixed']
            limit = column + 1
            while limit < end and (limit in self.profile['mixed']) == memo:
                limit += 1
            attrs = {key: value for key, value in cell['attrs'].items()
                     if _expanded(key, self.namespaces) != (T, 'number-columns-repeated')}
            attrs.update({'xmlns:aio_dbt': T, 'aio_dbt:number-columns-repeated': str(limit-column)})
            if memo:
                attrs = {key: value for key, value in attrs.items()
                         if _expanded(key, self.namespaces) != (T, 'formula') and
                         not (_expanded(key, self.namespaces)[0] == O and
                                 _expanded(key, self.namespaces)[1] in ('value-type', 'value', 'date-value',
                                     'time-value', 'boolean-value', 'string-value', 'currency'))}
                attrs.update({'xmlns:aio_dbo': O, 'xmlns:aio_dbx': X,
                              'aio_dbo:value-type': 'string', 'aio_dbo:string-value': display})
            self.gen.startElement(cell['name'], attrs)
            if memo:
                self.gen.startElement('aio_dbx:p', {});self.gen.characters(display);self.gen.endElement('aio_dbx:p')
                source_row = self.current_row['start']
                repeat = self.current_row['repeat']
                if self.profile['hasHeader'] and source_row == self.profile['firstRow']:
                    source_row, repeat = source_row + 1, repeat - 1
                self.mapping.write(json.dumps({'row': source_row, 'rowRepeat': repeat,
                    'column': column, 'columnRepeat': limit-column, 'sourceType': kind,
                    'sourceValue': raw, 'exportedText': display}, ensure_ascii=True) + '\n')
            else:
                cell['writer'].endDocument()
                cell['inner'].seek(0)
                while block := cell['inner'].read(BLOCK):
                    _check(self.cancel)
                    self.output.write(block)
            self.gen.endElement(cell['name'])
            column = limit

    def end(self, name):
        _check(self.cancel)
        depth = len(self.stack)
        tag, scope = self.stack[-1]
        if self.skip is not None:
            if depth == self.skip:
                self.row += self.current_row['repeat']
                self.current_row, self.skip = None, None
        elif self.cell and depth == self.cell['depth']:
            kind, raw, display, scalar = self._value(self.cell)
            if self.gen:
                try:
                    self._emit_cell(self.cell)
                finally:
                    self.cell['inner'].close()
            else:
                self.current_row['cells'].append((self.cell['column'], self.cell['repeat'], kind, display, scalar))
            self.cell = None
        elif self.cell:
            if self.gen:
                self.cell['writer'].endElement(name)
            if self.cell['paragraphDepth'] == depth:
                self.cell['paragraphDepth'] = None
        else:
            if self.gen:
                self.gen.endElement(name)
            if self.current_row and depth == self.current_row['depth']:
                if not self.gen:
                    self._profile_row()
                self.row += self.current_row['repeat']
                self.current_row = None
        self.stack.pop()
        _restore(scope, self.namespaces)
        if depth == self.sheet_depth:
            self.sheet_depth = None

    def _profile_row(self):
        cells = self.current_row['cells']
        nonempty = [item for item in cells if item[4] is not None]
        if not nonempty:
            return
        first = self.first is None
        if first:
            self.first = self.current_row['start']
        for col, repeat, kind, display, scalar in cells:
            if scalar is None:
                continue
            self.minimum, self.maximum = min(self.minimum, col), max(self.maximum, col+repeat-1)
            for column in range(col, col+repeat):
                if first:
                    self.first_cells[column] = (kind, display, scalar)
                if not first or self.current_row['repeat'] > 1:
                    self.kinds.setdefault(column, set()).add(scalar)

    def finish(self):
        if self.sheets != 1:
            _invalid('memo_requires_single_native_sheet')
        if self.gen:
            self.gen.endDocument()
            return None
        header = self.first is not None and all(self.first_cells.get(c, ('', '', ''))[0] == 'string'
            and self.first_cells.get(c, ('', '', ''))[1] for c in range(self.minimum, self.maximum+1))
        mixed = []
        for col in range(self.minimum, self.maximum+1):
            kinds = set(self.kinds.get(col, ()))
            if not header and col in self.first_cells:
                kinds.add(self.first_cells[col][2])
            declaration = self.first_cells.get(col, ('', '', ''))[1].split(',')[1:2] if header else []
            if len(kinds) > 1 or ('text' in kinds and declaration and declaration[0].strip().upper() in ('N', 'D', 'L')):
                mixed.append(col)
        return {'firstRow': self.first, 'minimum': self.minimum, 'maximum': self.maximum,
                'hasHeader': header, 'headers': {c: value[1] for c, value in self.first_cells.items()} if header else {},
                'mixed': mixed}


def _parse(path, visitor, cancel):
    parser = _secure_parser()
    parser.StartElementHandler, parser.EndElementHandler = visitor.start, visitor.end
    parser.CharacterDataHandler = visitor.text
    try:
        with Path(path).open('rb') as stream:
            while block := stream.read(BLOCK):
                _check(cancel)
                parser.Parse(block, False)
        parser.Parse(b'', True)
    except expat.ExpatError as error:
        _invalid('memo_native_xml_parse:' + str(error.code))
    return visitor.finish()


def prepare_dbf_memo_snapshot(native_fods, output, *, cancel):
    native_fods, output = Path(native_fods), Path(output)
    profile = _parse(native_fods, _Cells(cancel), cancel)
    if not profile['mixed']:
        return native_fods, None, None
    output.mkdir(parents=True, exist_ok=False)
    prepared = output/native_fods.name
    sidecar = output/(native_fods.stem + '.dbf.aio-columns.jsonl')
    try:
        with prepared.open('x', encoding='utf-8') as result, sidecar.open('x', encoding='utf-8') as mapping:
            mapping.write(json.dumps({'schemaVersion': 1, 'format': 'dBase', 'encoding': 'UTF-8',
                'nativeFilterOptions': '76', 'sourceHasHeader': profile['hasHeader'],
                'originalHeaders': profile['headers'], 'memoColumns': profile['mixed'],
                'numericColumnsRemainTyped': True, 'warning': 'NATIVE_DBF_MIXED_COLUMN_TEXT'},
                ensure_ascii=True) + '\n')
            _parse(native_fods, _Cells(cancel, output=result, mapping=mapping, profile=profile), cancel)
        return prepared, sidecar, {'check': 'native_dbf_memo_columns', 'status': 'prepared',
            'memoColumnCount': len(profile['mixed']), 'qualityCompared': False}
    except BaseException:
        prepared.unlink(missing_ok=True);sidecar.unlink(missing_ok=True)
        raise


def append_native_dbf_mapping(dbf, sidecar, *, cancel):
    fields = []
    with Path(dbf).open('rb') as stream:
        header = stream.read(32)
        if len(header) != 32:
            _invalid('memo_native_dbf_header')
        size = struct.unpack_from('<H', header, 8)[0]
        if size < 33 or (size-33) % 32:
            _invalid('memo_native_dbf_header')
        for index in range((size-33)//32):
            _check(cancel)
            field = stream.read(32)
            if len(field) != 32:
                _invalid('memo_native_dbf_fields')
            fields.append({'column': index, 'name': field[:11].split(b'\0', 1)[0].decode('ascii'),
                           'type': chr(field[11]), 'length': field[16], 'decimals': field[17]})
    with Path(sidecar).open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({'kind': 'native_field_mapping', 'fields': fields}, ensure_ascii=True) + '\n')
