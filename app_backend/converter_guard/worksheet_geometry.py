"""Bound actual XLSX cells before a read-only reader trusts dimension hints.

The worksheet dimension is advisory metadata and can omit real cells.  These
limits apply only to inspection, never to import/export or route admission.
"""
from __future__ import annotations

import re

SPREADSHEET_NAMESPACES = {
    'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
    'http://purl.oclc.org/ooxml/spreadsheetml/main',
}
MAX_INSPECTION_CELLS = 50000
_COORDINATE = re.compile(r'([A-Za-z]{1,3})([1-9][0-9]{0,6})\Z')


class GeometryUnavailable(ValueError):
    pass


def _positive_row(value):
    if not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]{0,6}', value):
        raise GeometryUnavailable('xlsx_cell_coordinate_inconsistent')
    return int(value)


def tree_events(root):
    """Iterate a previously bounded XML tree without retaining another tree."""
    yield 'start', root
    for child in root:
        yield from tree_events(child)
    yield 'end', root


def worksheet_geometry(events, *, check, max_cells=MAX_INSPECTION_CELLS, clear=False):
    """Resolve real row/column bounds; reject ambiguous mappings before reading.

    ``events`` are start/end XML events from an entity-safe bounded parser.
    ``clear`` lets the streaming caller immediately release consumed elements.
    """
    stack = []
    namespace = None
    rows = columns = previous_row = current_row = current_column = nodes = data_parts = 0
    for event, element in events:
        check()
        if event == 'start':
            nodes += 1
            stack.append(element)
            if len(stack) > 128 or nodes > 200000:
                raise GeometryUnavailable('xlsx_structure_budget')
            if len(stack) == 1:
                namespace, _, local = element.tag[1:].partition('}') if element.tag.startswith('{') else ('', '', '')
                if namespace not in SPREADSHEET_NAMESPACES or local != 'worksheet':
                    raise GeometryUnavailable('xlsx_worksheet_not_recognized')
            prefix = '{' + namespace + '}'
            if element.tag == prefix + 'sheetData' and len(stack) == 2:
                data_parts += 1
                if data_parts != 1:
                    raise GeometryUnavailable('xlsx_cell_coordinate_inconsistent')
            elif element.tag == prefix + 'row' and len(stack) == 3 and stack[-2].tag == prefix + 'sheetData':
                current_row = _positive_row(element.get('r', str(previous_row + 1)))
                if current_row <= previous_row:
                    raise GeometryUnavailable('xlsx_cell_coordinate_inconsistent')
                previous_row, current_column = current_row, 0
                rows = max(rows, current_row)
            elif (element.tag == prefix + 'c' and len(stack) == 4 and
                  stack[-2].tag == prefix + 'row' and stack[-3].tag == prefix + 'sheetData'):
                reference = element.get('r')
                if reference is None:
                    column = current_column + 1
                else:
                    match = _COORDINATE.fullmatch(reference)
                    if not match or int(match[2]) != current_row:
                        raise GeometryUnavailable('xlsx_cell_coordinate_inconsistent')
                    column = 0
                    for letter in match[1].upper():
                        column = column * 26 + ord(letter) - ord('A') + 1
                if column <= current_column or column > 16384:
                    raise GeometryUnavailable('xlsx_cell_coordinate_inconsistent')
                current_column = column
                columns = max(columns, column)
            if rows > max_cells or rows * columns > max_cells:
                raise GeometryUnavailable('xlsx_cell_geometry_budget')
        else:
            if clear:
                element.clear()
                if len(stack) > 1:
                    stack[-2].remove(element)
            stack.pop()
    if namespace is None or data_parts != 1:
        raise GeometryUnavailable('xlsx_worksheet_not_recognized')
    return rows, columns
