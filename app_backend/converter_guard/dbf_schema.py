"""Bounded header gate for the legacy CLI route without a loaded UNO model."""
import re
from xml.etree import ElementTree as ET

from .errors import GuardError


def validate_cli_dbf_headers(snapshot, *, trusted_schema=False, cancel=None):
    """Never let CLI fallback bypass the UNO literal-heading protection.

    Only the first row of an already validated native FODS snapshot is read.
    Unsafe headings require the UNO mapping path; no XML or user file is
    rewritten here. This check is after native import, never Guard preflight.
    """
    office = '{urn:oasis:names:tc:opendocument:xmlns:office:1.0}'
    table = '{urn:oasis:names:tc:opendocument:xmlns:table:1.0}'
    text = '{urn:oasis:names:tc:opendocument:xmlns:text:1.0}'
    def incomplete():
        raise GuardError('ENGINE_OUTPUT_VALIDATION_INCOMPLETE',
            'Для безопасного сохранения названий полей DBF нужен нативный UNO-экспорт.',
            stage='export', diagnostic='dbf_cli_header_schema_unconfirmed')
    class Limited:
        used = 0
        def __init__(self, stream): self.stream = stream
        def read(self, size):
            if cancel is not None and cancel.is_set():
                raise GuardError('CANCELLED', 'Проверка заголовков DBF отменена.', stage='export')
            self.used += size
            if self.used > 1024 * 1024: incomplete()
            return self.stream.read(size)
    try:
        with open(snapshot, 'rb') as stream:
            in_row = False
            for event, node in ET.iterparse(Limited(stream), events=('start', 'end')):
                if event == 'start' and node.tag == table+'table-row':
                    in_row = True
                elif event == 'end' and node.tag == table+'table-row':
                    names, kinds = [], []
                    for cell in node:
                        if cell.tag != table+'table-cell': continue
                        paragraphs = cell.findall(text+'p')
                        value = cell.get(office+'string-value', '\n'.join(''.join(p.itertext()) for p in paragraphs))
                        if any(n.tag in (text+'s', text+'tab', text+'line-break') for n in cell.iter()):
                            value += ' '  # Such a heading cannot be a plain DBF identifier.
                        repeat = int(cell.get(table+'number-columns-repeated', '1'))
                        if repeat < 1 or repeat > 16384: incomplete()
                        if not value: continue  # Native styled empty tail is not a field name.
                        names.extend([value] * min(repeat, 2))
                        kinds.extend([cell.get(office+'value-type')] * min(repeat, 2))
                    if names and all(kind == 'string' for kind in kinds):
                        pattern = (r'[A-Za-z_][A-Za-z_0-9]{0,9}(?:,[CNDLM](?:,[0-9]+(?:,[0-9]+)?)?)?'
                                   if trusted_schema else r'[A-Za-z_][A-Za-z_0-9]{0,9}')
                        if (any(not re.fullmatch(pattern, name, re.I) for name in names)
                                or len({name.split(',')[0].upper() for name in names}) != len(names)):
                            incomplete()
                    return
                elif event == 'end' and not in_row:
                    node.clear()
    except (ET.ParseError, ValueError):
        incomplete()
    incomplete()
