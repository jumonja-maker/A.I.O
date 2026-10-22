"""Content-aware optional noninteractive native Calc text-import hint.

Not a routing decision: only a successfully loaded UNO spreadsheet with an
installed importer can establish compatibility. Unknown and binary inputs
continue through ordinary Guard preflight, without new source-size limits.
"""
from __future__ import annotations

import codecs
import csv
import io
from pathlib import Path


CALC = 'com.sun.star.sheet.SpreadsheetDocument'
DELIMITERS = (',', ';', '\t', '|')


def csv_import_hint(source: Path, native):
    """Return a validated UNO filter/options pair, or None.

    Use a bounded sample; no suffix-based admission, and no new rejection.
    UTF-8 is chosen only when actual sampled bytes decode as strict UTF-8.
    """
    if not getattr(native, 'valid', False):
        return None
    eligible = [name for name, (service, extensions, flags) in native.filters.items()
                if service == CALC and 'csv' in extensions and 'IMPORT' in flags]
    preferred = set(getattr(native, 'preferred_filters', {}).get('csv', ())).intersection(eligible)
    if len(preferred) == 1:
        selected = next(iter(preferred))
    elif 'Text - txt - csv (StarCalc)' in eligible:
        # LO 26.8 also registers Orcus CSV. Prefer the installed *Calc Text*
        # importer because it supports explicit FilterOptions and no UI.
        # This does not authorize a route: actual loaded Calc model must
        # still confirm this exact native filter.
        selected = 'Text - txt - csv (StarCalc)'
    elif len(eligible) == 1:
        selected = eligible[0]
    else:
        return None  # No registered, unambiguous text-CSV importer.
    try:
        with Path(source).open('rb') as stream:
            # One look-ahead byte distinguishes a bounded prefix from EOF.
            # An incomplete final UTF-8 character in a prefix is not damage.
            prefix = stream.read(65537)
        truncated = len(prefix) > 65536
        sample = prefix[:65536]
        if not sample or b'\0' in sample:
            return None
        text = codecs.getincrementaldecoder('utf-8-sig')('strict').decode(sample, final=not truncated)
        # A partial last row in a bounded sample must not change the dialect.
        if truncated and '\n' in text:
            text = text[:text.rfind('\n') + 1]
        if not text or '\n' not in text:
            return None
        choices = []
        for delimiter in DELIMITERS:
            try:
                reader = csv.reader(io.StringIO(text, newline=''),
                                    delimiter=delimiter, quotechar='"', strict=True)
                rows = []
                for record in reader:
                    if record:
                        rows.append(record)
                    if len(rows) >= 8:
                        break
                if len(rows) < 2:
                    continue
                width = len(rows[0])
                if width < 2 or any(len(row) != width for row in rows):
                    continue
                choices.append(delimiter)
            except (csv.Error, ValueError, UnicodeError):
                continue
        if len(choices) != 1:
            return None
    except (OSError, UnicodeError, ValueError):
        return None
    # Native Calc CSV import syntax: separator code, text delimiter code,
    # UTF-8 charset 76, and first data row 1. Never depend on UI dialogs.
    separator = ord(choices[0])
    return {'filter': selected, 'options': f'{separator},34,76,1',
            'sampleOnly': True, 'sampleBytes': len(sample), 'sampleRows': 8}
