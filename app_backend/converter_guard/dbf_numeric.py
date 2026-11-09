"""Shared bounded numeric sizing for native DBF model/snapshot exports."""

DBF_NUMERIC_SOURCE = r'''
from decimal import Decimal, InvalidOperation
from pathlib import Path
import struct

class DbfNumericColumn:
    def __init__(self):
        self.count = 0
        self.integer_width = 1
        self.scale = 0

    def add(self, value):
        if isinstance(value, bool):
            raise ValueError('DBF_NUMERIC_TYPE_UNCONFIRMED')
        try:
            number = Decimal(str(value))
        except InvalidOperation as error:
            raise ValueError('DBF_NUMERIC_VALUE_UNCONFIRMED') from error
        if not number.is_finite():
            raise ValueError('DBF_NUMERIC_VALUE_UNCONFIRMED')
        _sign, digits, exponent = number.as_tuple()
        # PyUNO returns integral doubles as 42.0. Their trailing zero is not
        # a declared decimal scale; trusted DBF declarations are kept below.
        digits = list(digits)
        if not any(digits):
            digits, exponent = [0], 0
        while len(digits) > 1 and digits[-1] == 0:
            digits.pop()
            exponent += 1
        self.integer_width = max(self.integer_width,
            max(1, len(digits) + exponent) + int(number.is_signed()))
        self.scale = max(self.scale, max(0, -exponent))
        self.count += 1

    def schema(self, *, minimum_width=0, minimum_scale=0):
        if not self.count:
            return None
        scale = max(self.scale, minimum_scale)
        width = max(minimum_width, self.integer_width + (1 + scale if scale else 0))
        # These native dBASE numeric limits are verified by native QA. Values
        # outside them must never be rounded to make an export succeed.
        if width > 19 or scale > 15 or width < 1 or scale < 0:
            raise ValueError('DBF_NUMERIC_SCHEMA_UNREPRESENTABLE')
        return (width, scale)

def verify_dbf_numeric_rows(path, fields, expected_rows):
    """Check native field declarations and every numeric value before release."""
    checked = 0
    with Path(path).open('rb') as stream:
        head = stream.read(32)
        if len(head) != 32:
            raise ValueError('DBF_NUMERIC_OUTPUT_HEADER')
        count, header, stride = struct.unpack_from('<IHH', head, 4)
        declaration = stream.read(header - 32)
        if header < 33 or (header - 33) % 32 or declaration[-1:] != b'\r':
            raise ValueError('DBF_NUMERIC_OUTPUT_HEADER')
        offsets, position = {}, 1
        for column, offset in enumerate(range(0, len(declaration)-1, 32)):
            descriptor = declaration[offset:offset+32]
            offsets[column] = (position, descriptor)
            position += descriptor[16]
        if position != stride:
            raise ValueError('DBF_NUMERIC_OUTPUT_STRIDE')
        for field in fields:
            _offset, descriptor = offsets[field['column']]
            if (descriptor[:11].split(b'\0')[0].decode('ascii').upper() != field['fieldName'].upper()
                    or descriptor[11] != ord('N') or descriptor[16] != field['width']
                    or descriptor[17] != field['scale']):
                raise ValueError('DBF_NUMERIC_OUTPUT_SCHEMA')
        observed = 0
        for expected in expected_rows:
            record = stream.read(stride)
            if observed >= count or len(record) != stride or record[:1] != b' ':
                raise ValueError('DBF_NUMERIC_OUTPUT_ROW_COUNT')
            observed += 1
            for field in fields:
                start, _descriptor = offsets[field['column']]
                raw = record[start:start+field['width']].strip()
                value = expected[field['column']]
                if value is None or value == '':
                    if raw:
                        raise ValueError('DBF_NUMERIC_BLANK_CHANGED')
                elif not raw or Decimal(raw.decode('ascii')) != Decimal(str(value)):
                    raise ValueError('DBF_NUMERIC_VALUE_CHANGED')
                checked += 1
        if observed != count:
            raise ValueError('DBF_NUMERIC_OUTPUT_ROW_COUNT')
    return {'check': 'dbf_numeric_values', 'status': 'checked_subset',
            'checkedCells': checked, 'numericColumns': len(fields), 'qualityCompared': False}
'''

exec(compile(DBF_NUMERIC_SOURCE, __file__, 'exec'))
