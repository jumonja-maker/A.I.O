"""Bounded DIF numeric evidence, shared verbatim with native office Python.

DIF uses a decimal point in its numeric records. Some office locales import
such records as literal ``#ERR`` strings. This inspector does not repair those
strings or render documents: it verifies original numeric coordinates before
an export can be committed. An unsupported grammar stays explicitly unknown.
"""

DIF_CORE_SOURCE = r'''
import math as _dif_math
import re as _dif_re
from pathlib import Path as _DifPath

_DIF_LINE_LIMIT = 1024 * 1024
_DIF_NUMBER = _dif_re.compile(rb'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[Ee][+-]?[0-9]+)?\Z')

class DifInspectionError(ValueError):
    pass

class DifNumericMismatch(RuntimeError):
    pass


def _dif_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise DifInspectionError('dif_cancelled')


def _dif_line(stream, cancel):
    _dif_cancel(cancel)
    line = stream.readline(_DIF_LINE_LIMIT + 1)
    if len(line) > _DIF_LINE_LIMIT:
        raise DifInspectionError('dif_record_inspection_budget')
    return line.rstrip(b'\r\n') if line else None


def is_dif_source(source):
    try:
        with _DifPath(source).open('rb') as stream:
            return stream.read(10).removeprefix(b'\xef\xbb\xbf').startswith((b'TABLE\r', b'TABLE\n'))
    except OSError:
        # This cheap optional evidence probe never takes over native source
        # admission/error handling (or an adapter's controlled native double).
        return False


def iter_dif_numeric_rows(source, *, cancel=None):
    """Yield only actual numeric V cells, with their original zero-based row.

    Rows are streamed; no document-sized list or expanded declared grid is
    allocated. Unknown records, nonfinite values, enormous single records and
    ambiguous multiline strings are not silently accepted as numeric proof.
    The inspector's line budget never changes source admission by extension.
    """
    with _DifPath(source).open('rb') as stream:
        tag = _dif_line(stream, cancel)
        tag = tag.removeprefix(b'\xef\xbb\xbf') if tag is not None else None
        if tag != b'TABLE':
            raise DifInspectionError('dif_header_unrecognized')
        columns = rows = None
        # Topic names are ASCII and have exactly two following records.
        while tag != b'DATA':
            pair = _dif_line(stream, cancel)
            value = _dif_line(stream, cancel)
            if pair is None or value is None or not _dif_re.fullmatch(rb'-?[0-9]+,-?[0-9]+', pair):
                raise DifInspectionError('dif_topic_structure')
            if tag in (b'VECTORS', b'TUPLES'):
                fields = pair.split(b',')
                if fields[0] != b'0':
                    raise DifInspectionError('dif_dimensions_ambiguous')
                try: count = int(fields[1])
                except ValueError:
                    raise DifInspectionError('dif_dimension_integer_budget')
                if count < 0:
                    raise DifInspectionError('dif_dimensions_ambiguous')
                if tag == b'VECTORS': columns = count
                else: rows = count
            tag = _dif_line(stream, cancel)
            if tag is None:
                raise DifInspectionError('dif_data_missing')
        if _dif_line(stream, cancel) != b'0,0' or _dif_line(stream, cancel) != b'""':
            raise DifInspectionError('dif_data_header')
        row = -1
        column = 0
        numeric = []
        ended = False
        while True:
            token = _dif_line(stream, cancel)
            if token is None:
                raise DifInspectionError('dif_eod_missing')
            payload = _dif_line(stream, cancel)
            if payload is None:
                raise DifInspectionError('dif_record_truncated')
            if token == b'-1,0':
                if payload == b'BOT':
                    if row >= 0:
                        if columns is not None and column != columns:
                            raise DifInspectionError('dif_column_count')
                        yield row, numeric
                    row += 1
                    column = 0
                    numeric = []
                elif payload == b'EOD':
                    if row >= 0:
                        if columns is not None and column != columns:
                            raise DifInspectionError('dif_column_count')
                        yield row, numeric
                    if rows is not None and row + 1 != rows:
                        raise DifInspectionError('dif_row_count')
                    ended = True
                    break
                else:
                    raise DifInspectionError('dif_control_record')
                continue
            if row < 0:
                raise DifInspectionError('dif_row_missing')
            if token.startswith(b'0,'):
                number = token[2:]
                if not _DIF_NUMBER.fullmatch(number):
                    raise DifInspectionError('dif_numeric_grammar')
                if payload == b'V':
                    try: value = float(number)
                    except (ValueError, OverflowError):
                        raise DifInspectionError('dif_numeric_range')
                    if not _dif_math.isfinite(value):
                        raise DifInspectionError('dif_numeric_range')
                    numeric.append((column, value))
                elif payload in (b'TRUE', b'FALSE'):
                    # Logical DIF records may retain a native boolean number
                    # format. This check only proves ordinary V-numbers.
                    pass
                elif payload not in (b'NA', b'ERROR'):
                    raise DifInspectionError('dif_numeric_status')
            elif token == b'1,0':
                if not (len(payload) >= 2 and payload[:1] == payload[-1:] == b'"'):
                    raise DifInspectionError('dif_multiline_or_unquoted_string')
            else:
                raise DifInspectionError('dif_record_type')
            column += 1
            # The native Calc model cannot have a wider input row. Stop the
            # inspector before retaining attacker-controlled unbounded rows.
            if column > 16384:
                raise DifInspectionError('dif_native_column_range')
        if ended:
            while True:
                trailing = _dif_line(stream, cancel)
                if trailing is None: break
                if trailing.strip():
                    raise DifInspectionError('dif_trailing_data')


def check_dif_import_model(source, model, *, cancel=None):
    """Compare every source numeric coordinate with the actually loaded model.

    XCellRangeData distinguishes text/void from doubles; ``#ERR`` therefore
    cannot masquerade as a numeric value. Comparison is exact in Calc's IEEE
    double domain. This says nothing about final presentation or other cells.
    """
    if not is_dif_source(source):
        return None
    try:
        sheet = model.getSheets().getByIndex(0)
        count = 0
        for row, values in iter_dif_numeric_rows(source, cancel=cancel):
            for index in range(0, len(values), 128):
                _dif_cancel(cancel)
                batch = values[index:index + 128]
                if not batch: continue
                first, last = batch[0][0], batch[-1][0]
                observed = sheet.getCellRangeByPosition(first, row, last, row).getDataArray()[0]
                for column, expected in batch:
                    actual = observed[column - first]
                    if (isinstance(actual, bool) or not isinstance(actual, (float, int))
                            or not _dif_math.isfinite(actual) or actual != expected):
                        raise DifNumericMismatch('DIF_NUMERIC_IMPORT_MISMATCH:row=' +
                            str(row + 1) + ':column=' + str(column + 1))
                    count += 1
        return {'check': 'dif_numeric_import', 'status': 'checked_subset',
                'numericCells': count, 'valuesCompared': True, 'typesCompared': True,
                'comparisonDomain': 'ieee754_double', 'qualityCompared': False}
    except DifInspectionError as error:
        if str(error) == 'dif_cancelled':
            raise
        return {'check': 'dif_numeric_import', 'status': 'not_checked',
                'reason': str(error), 'qualityCompared': False}
'''

exec(DIF_CORE_SOURCE, globals())


def _dif_output_rows(path, target, cancel):
    """Read typed numeric row segments from a native flat/packaged Calc file."""
    from contextlib import ExitStack
    import xml.etree.ElementTree as ET
    import zipfile
    from .content_invariants import _InspectionReader
    office = 'urn:oasis:names:tc:opendocument:xmlns:office:1.0'
    table = 'urn:oasis:names:tc:opendocument:xmlns:table:1.0'
    q = lambda name: '{' + table + '}' + name
    with ExitStack() as cleanup:
        if target in ('ods', 'ots'):
            archive = cleanup.enter_context(zipfile.ZipFile(path))
            stream = cleanup.enter_context(archive.open('content.xml'))
        elif target == 'fods':
            stream = cleanup.enter_context(_DifPath(path).open('rb'))
        else:
            raise DifInspectionError('dif_target_not_typed_calc_xml')
        stack, active, finished, row_index = [], None, False, 0
        for event, element in ET.iterparse(_InspectionReader(stream, cancel), events=('start', 'end')):
            _dif_cancel(cancel)
            if event == 'start':
                stack.append(element)
                if len(stack) > 128:
                    raise DifInspectionError('dif_output_xml_depth')
                if element.tag == q('table') and len(stack) > 1 and stack[-2].tag == '{' + office + '}spreadsheet':
                    if active is None and not finished:
                        active = element
                continue
            if active is not None and element.tag == q('table-row'):
                if any(ancestor.tag == q('table-cell') for ancestor in stack[:-1]):
                    raise DifInspectionError('dif_nested_output_table')
                repeat = int(element.get(q('number-rows-repeated'), '1'))
                if repeat < 1 or row_index + repeat > 1048576:
                    raise DifInspectionError('dif_output_row_range')
                segments, column = [], 0
                for cell in element:
                    if cell.tag not in (q('table-cell'), q('covered-table-cell')):
                        continue
                    count = int(cell.get(q('number-columns-repeated'), '1'))
                    if count < 1 or column + count > 16384:
                        raise DifInspectionError('dif_output_column_range')
                    kind = cell.get('{' + office + '}value-type')
                    value = cell.get('{' + office + '}value')
                    if kind == 'float' and value is not None:
                        value = float(value)
                    else:
                        value = None
                    segments.append((column, count, kind, value))
                    column += count
                yield row_index, repeat, segments
                row_index += repeat
                element.clear()
                if len(stack) > 1:
                    stack[-2].remove(element)
            elif element is active:
                finished, active = True, None
                element.clear()
            elif active is None:
                element.clear()
                if len(stack) > 1:
                    stack[-2].remove(element)
            stack.pop()
        if not finished:
            raise DifInspectionError('dif_output_sheet_missing')


def check_dif_output(paths, target, source, *, cancel):
    """Verify a native Calc snapshot's numeric coordinates before CLI exports.

    The caller can create one native FODS snapshot using the SAME DIF import
    filter/options and isolated profile policy, then cache this source proof
    only for the unchanged source identity. The check never derives numbers
    from damaged output strings and never treats an uninspectable target as
    numeric proof. FODS/ODS/OTS are the supported *evidence* containers.
    """
    from .errors import GuardError
    from .content_invariants import _InspectionLimited
    import xml.etree.ElementTree as ET
    import zipfile
    if not is_dif_source(source):
        return None
    target = target.lower()
    result = {'check': 'dif_numeric_values', 'qualityCompared': False,
              'comparisonDomain': 'ieee754_double', 'typesCompared': True}
    if target not in ('fods', 'ods', 'ots'):
        return {**result, 'status': 'not_checked', 'reason': 'target_numeric_not_inspectable'}
    candidates = [_DifPath(path) for path in paths if _DifPath(path).suffix.lower() == '.' + target]
    if len(candidates) != 1:
        raise GuardError('ENGINE_OUTPUT_VALIDATION_INCOMPLETE',
            'Не удалось однозначно проверить числовые данные DIF.', stage='result',
            diagnostic='dif_numeric_primary_ambiguous')
    try:
        observed = iter(_dif_output_rows(candidates[0], target, cancel))
        current = next(observed, None)
        count = 0
        for row, values in iter_dif_numeric_rows(source, cancel=cancel):
            while current is not None and current[0] + current[1] <= row:
                current = next(observed, None)
            if not values:
                continue
            if current is None or not current[0] <= row < current[0] + current[1]:
                raise DifNumericMismatch('DIF_NUMERIC_OUTPUT_MISMATCH:row=' + str(row + 1))
            segments = iter(current[2])
            segment = next(segments, None)
            for column, expected in values:
                while segment is not None and segment[0] + segment[1] <= column:
                    segment = next(segments, None)
                if (segment is None or segment[2] != 'float' or
                        not _dif_math.isfinite(segment[3]) or segment[3] != expected):
                    raise DifNumericMismatch('DIF_NUMERIC_OUTPUT_MISMATCH:row=' +
                        str(row + 1) + ':column=' + str(column + 1))
                count += 1
        # Exhaust the evidence container to catch XML errors after the last
        # numeric coordinate; an early prefix cannot certify complete output.
        for _ in observed:
            pass
    except DifNumericMismatch as error:
        raise GuardError('ENGINE_OUTPUT_CONTENT_LOSS',
            'Числовые значения DIF изменились или стали текстом. Результат не отмечен готовым.',
            stage='result', diagnostic=str(error)) from error
    except (DifInspectionError, _InspectionLimited) as error:
        if cancel.is_set():
            raise GuardError('CANCELLED', 'Проверка чисел DIF отменена.', stage='result') from error
        # Unsupported inspector grammar / bounded inspection is uncertainty,
        # never proof of corruption and never a native route prohibition.
        return {**result, 'status': 'not_checked', 'reason': str(error)[:128],
                'warning': 'DIF_NUMERIC_CONTENT_NOT_CHECKED'}
    except (ET.ParseError, zipfile.BadZipFile, KeyError, OSError, ValueError, OverflowError) as error:
        if cancel.is_set():
            raise GuardError('CANCELLED', 'Проверка чисел DIF отменена.', stage='result') from error
        raise GuardError('ENGINE_OUTPUT_VALIDATION_INCOMPLETE',
            'Не удалось полностью прочитать нативный снимок чисел DIF. Результат не подтверждён.',
            stage='result', diagnostic='dif_numeric_inspection:' + type(error).__name__) from error
    if cancel.is_set():
        raise GuardError('CANCELLED', 'Проверка чисел DIF отменена.', stage='result')
    return {**result, 'status': 'checked_subset', 'numericCells': count,
            'valuesCompared': True, 'sourceNumericImportCompared': True}
