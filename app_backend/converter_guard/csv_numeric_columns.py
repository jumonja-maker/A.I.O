"""Conservative decimal-dot hints from an already bounded CSV sample.

No file IO, locale/profile mutation or additional native import. The caller
still owns admission and leading-zero-ID protection. Type 10 is Calc's
per-column US-English parser; it is NOT a global document language change.
"""
import re

_NUMBER = re.compile(r'[+-]?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z')
MAX_COLUMNS = 256
MAX_HINTS = 64
MAX_SAMPLE_ROWS = 256


def decimal_column_hints(rows, width, text_hints=''):
    """Extend Text hints only for consistent, unambiguous decimal-dot columns.

    Grouping-only forms such as 1.234, mixed separators, dates, exponents,
    formulas, zero-padded or >15-significant-digit numbers are left alone.
    A conventional all-text header may precede data. This is a bounded import
    hint, NOT evidence that every row or every user-defined type was checked.
    """
    if (type(width) is not int or not 1 <= width <= MAX_COLUMNS or
            not 2 <= len(rows) <= MAX_SAMPLE_ROWS or
            any(len(row) != width for row in rows)):
        return text_hints
    formats = {}
    if text_hints:
        tokens = text_hints.split('/')
        if len(tokens) % 2:
            return text_hints
        last = 0
        for column, code in zip(tokens[::2], tokens[1::2]):
            if (not column.isascii() or not column.isdecimal() or code != '2' or
                    not last < int(column) <= width):
                return text_hints
            last = int(column)
            formats[last] = '2'
        if len(formats) > MAX_HINTS:
            return text_hints
    # Do not invent a header from a missing/invalid first numeric value.
    header = all(isinstance(v, str) and v and len(v) <= 128 and
                 not v.startswith('=') and any(c.isalpha() for c in v)
                 for v in rows[0])
    data = rows[1:] if header else rows
    for index in range(width):
        if index + 1 in formats or len(formats) >= MAX_HINTS:
            continue
        values = [row[index] for row in data if row[index] != '']
        if len(values) < 2 or any(not isinstance(v, str) or
                                  _NUMBER.fullmatch(v) is None for v in values):
            continue
        # Calc stores doubles. Do not proactively coerce high-precision IDs.
        if any(len(v.lstrip('+-').replace('.', '').lstrip('0')) > 15 for v in values):
            continue
        decimals = [v for v in values if '.' in v]
        if len(decimals) < 2 or all(len(v.rsplit('.', 1)[1]) == 3 for v in decimals):
            continue
        formats[index + 1] = '10'
    return '/'.join(str(column) + '/' + formats[column] for column in sorted(formats))
