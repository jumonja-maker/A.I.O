"""Numeric schema on an owned native FODS copy; original data stays untouched."""
from pathlib import Path
import re
from xml.sax.saxutils import escape

from .dbf_numeric import DbfNumericColumn, verify_dbf_numeric_rows
from .errors import GuardError
from .legacy_sheets import (_secure_parser, _scope, _restore, _expanded,
                            _tag_end, _copy_range, BLOCK, OFFICE, TABLE)

TEXT = 'urn:oasis:names:tc:opendocument:xmlns:text:1.0'


def native_rows(path, cancel):
    """Yield compact typed rows without retaining the document or body text."""
    parser = _secure_parser()
    namespaces, scopes, queue = {}, [], []
    row, cell = None, None
    row_index, column = 0, 0
    with Path(path).open('rb') as stream, Path(path).open('rb') as tags:
        def start(name, attrs):
            nonlocal row, cell, column
            if cancel.is_set():
                raise GuardError('CANCELLED', 'Подготовка числовой схемы DBF отменена.', stage='export')
            scopes.append(_scope(attrs, namespaces))
            tag = _expanded(name, namespaces)
            values = {_expanded(key, namespaces): value for key, value in attrs.items()}
            if tag == (TABLE, 'table-row'):
                if row is not None:
                    raise ValueError('DBF_NESTED_NATIVE_ROW')
                repeat = int(values.get((TABLE, 'number-rows-repeated'), '1'))
                if not 1 <= repeat <= 1048576:
                    raise ValueError('DBF_NATIVE_ROW_REPEAT')
                row, column = {'cells': [], 'repeat': repeat}, 0
            elif row is not None and tag in ((TABLE, 'table-cell'), (TABLE, 'covered-table-cell')):
                repeated = int(values.get((TABLE, 'number-columns-repeated'), '1'))
                if not 1 <= repeated <= 16384 or column+repeated > 16384:
                    raise ValueError('DBF_NATIVE_COLUMN_REPEAT')
                offset = parser.CurrentByteIndex
                opened, empty = _tag_end(tags, offset, cancel)
                kind = values.get((OFFICE, 'value-type'))
                text = values.get((OFFICE, 'string-value'))
                cell = {'kind': kind, 'value': values.get((OFFICE, 'value')),
                        'text': text if row_index == 0 else bool(text), 'shown': False,
                        'column': column, 'repeat': repeated, 'start': offset,
                        'opened': opened, 'empty': empty}
                column += repeated
            elif cell is not None and tag == (TEXT, 'p'):
                cell['paragraphDepth'] = len(scopes)

        def characters(value):
            if cell is not None and value and cell.get('paragraphDepth') is not None:
                cell['shown'] = cell['shown'] or bool(value.strip())
                if row_index == 0 and cell['text'] is None:
                    cell.setdefault('parts', []).append(value)

        def end(name):
            nonlocal row, cell, row_index
            tag = _expanded(name, namespaces)
            if cell is not None and tag == (TEXT, 'p'):
                cell.pop('paragraphDepth', None)
            if cell is not None and tag in ((TABLE, 'table-cell'), (TABLE, 'covered-table-cell')):
                if row_index == 0 and cell['text'] is None:
                    cell['text'] = ''.join(cell.pop('parts', []))
                cell['end'] = (cell['opened'] if cell['empty'] else
                               _tag_end(tags, parser.CurrentByteIndex, cancel)[0])
                row['cells'].append(cell)
                cell = None
            elif row is not None and tag == (TABLE, 'table-row'):
                queue.append(row)
                row_index += row['repeat']
                row = None
            _restore(scopes.pop(), namespaces)

        parser.StartElementHandler, parser.CharacterDataHandler, parser.EndElementHandler = start, characters, end
        while chunk := stream.read(BLOCK):
            if cancel.is_set():
                raise GuardError('CANCELLED', 'Подготовка числовой схемы DBF отменена.', stage='export')
            parser.Parse(chunk, False)
            while queue:
                yield queue.pop(0)
        parser.Parse(b'', True)
        while queue:
            yield queue.pop(0)


def prepare_numeric_snapshot(snapshot, destination, *, trusted_schema=False, cancel):
    """Return an immutable schema copy only for confirmed numeric columns.

    Literal heading admission already happened on the unmodified snapshot.
    Only header cells are changed; native data, styles and date/logical fields
    retain their original bytes. Headerless sheets keep the existing route.
    """
    snapshot, destination = Path(snapshot), Path(destination)
    rows = native_rows(snapshot, cancel)
    first = next(rows, None)
    if first is None or first['repeat'] != 1:
        return snapshot, None
    cells = [cell for cell in first['cells'] if cell['kind'] or cell['shown'] or cell['text']]
    if not cells or any(cell['kind'] != 'string' or not cell['text'] or cell['repeat'] != 1 for cell in cells):
        return snapshot, None
    headers = [cell['text'] for cell in cells]
    grammar = (r'[A-Za-z_][A-Za-z_0-9]{0,9}(?:,[CNDLM](?:,[0-9]+(?:,[0-9]+)?)?)?'
               if trusted_schema else r'[A-Za-z_][A-Za-z_0-9]{0,9}')
    if (any(not re.fullmatch(grammar, name, re.I) for name in headers)
            or len({name.split(',')[0].upper() for name in headers}) != len(headers)):
        raise ValueError('DBF_LITERAL_HEADER_SCHEMA_UNCONFIRMED')
    if [cell['column'] for cell in cells] != list(range(len(headers))):
        return snapshot, None
    columns = [DbfNumericColumn() for _ in headers]
    allowed = [True] * len(headers)
    position, used = 1, 1
    for row in rows:
        populated = False
        for cell in row['cells']:
            nonempty = (cell['kind'] is not None and cell['kind'] != 'string') or cell['shown'] or bool(cell['text'])
            populated = populated or nonempty
            for column in range(cell['column'], min(len(headers), cell['column']+cell['repeat'])):
                if cell['kind'] == 'float' and cell['value'] is not None and allowed[column]:
                    columns[column].add(cell['value'])
                elif nonempty:
                    allowed[column] = False
        if populated:
            used = position+row['repeat']
        position += row['repeat']
    fields, replacements = [], []
    for index, profile in enumerate(columns):
        declaration = headers[index].split(',') if trusted_schema else []
        if (not allowed[index] or not profile.count or
                declaration and len(declaration) > 1 and declaration[1].upper() != 'N'):
            continue
        width, scale = profile.schema(
            minimum_width=int(declaration[2]) if len(declaration) > 2 else 0,
            minimum_scale=int(declaration[3]) if len(declaration) > 3 else 0)
        name = headers[index].split(',')[0] if trusted_schema else headers[index]
        fields.append({'column': index, 'fieldName': name.upper(), 'width': width, 'scale': scale})
        heading = '%s,N,%d,%d' % (name, width, scale)
        raw = ('<aio:table-cell xmlns:aio="'+TABLE+'" xmlns:o="'+OFFICE+'" o:value-type="string">'
               '<t:p xmlns:t="'+TEXT+'">'+escape(heading)+'</t:p></aio:table-cell>').encode('utf-8')
        replacements.append((cells[index]['start'], cells[index]['end'], raw))
    if not fields:
        return snapshot, None
    destination.parent.mkdir(parents=True, exist_ok=True)
    with snapshot.open('rb') as source, destination.open('xb') as output:
        position = 0
        for start, end, raw in replacements:
            _copy_range(source, output, position, start, cancel)
            output.write(raw)
            position = end
        _copy_range(source, output, position, snapshot.stat().st_size, cancel)
    return destination, {'numericFields': fields, 'originalHeaders': headers,
                         'fieldNames': [name.split(',')[0].upper() if trusted_schema else name.upper() for name in headers],
                         'sourceRows': used, 'sourceHasHeader': True}


def verify_numeric_snapshot(snapshot, dbf, plan, *, cancel):
    def expected():
        first, remaining = True, plan['sourceRows']-1
        for row in native_rows(snapshot, cancel):
            if first:
                first = False
                continue
            values = [None] * len(plan['fieldNames'])
            for cell in row['cells']:
                for column in range(cell['column'], min(len(values), cell['column']+cell['repeat'])):
                    values[column] = cell['value'] if cell['kind'] == 'float' else None
            for _index in range(min(row['repeat'], remaining)):
                if cancel.is_set():
                    raise GuardError('CANCELLED', 'Проверка чисел DBF отменена.', stage='result')
                yield values
                remaining -= 1
            if remaining == 0:
                return
    return verify_dbf_numeric_rows(dbf, plan['numericFields'], expected())
