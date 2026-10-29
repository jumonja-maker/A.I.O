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

from .csv_id_contract import text_columns_in_sample
from .csv_numeric_columns import decimal_column_hints


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
    # Only a confirmed Calc Text importer supports documented per-column
    # text types. Never force a whole sheet to text: money/dates need their
    # normal native parsing. The bounded preview is an import hint, not a
    # claim that every row was inspected; final output has an independent
    # source-ID contract.
    columns = ''
    if selected == 'Text - txt - csv (StarCalc)':
        try:
            reader = csv.reader(io.StringIO(text, newline=''),
                                delimiter=choices[0], quotechar='"', strict=True)
            sample_rows = []
            for record in reader:
                if record:
                    sample_rows.append(record)
                if len(sample_rows) >= 256:
                    break
            if sample_rows:
                columns = text_columns_in_sample(sample_rows, len(sample_rows[0]))
                # Reuse these same rows; no full-file scan or extra LO load.
                # Text IDs win; only unambiguous decimal-dot columns get type 10.
                columns = decimal_column_hints(sample_rows, len(sample_rows[0]), columns)
        except (csv.Error, ValueError):
            pass
    options = f'{separator},34,76,1' + (',' + columns if columns else '')
    return {'filter': selected, 'options': options}


def inspect_csv_stream(source, *, delimiter=None, cancel=None, seconds=5, max_bytes=64*1024*1024,
                       analyze_values=True):
    """Full UTF-8 record scan when within the diagnostic budget, never a veto.

    Reports exact strings, including zeros/dates/formula-like literals, by a
    digest; it never guesses types or rewrites values. A limit means incomplete
    evidence, not damaged input. No global csv.field_size_limit mutation.
    """
    import hashlib
    import json
    import re
    import time
    started = time.monotonic()
    result = {'schema':1, 'status':'not_checked', 'complete':False, 'encoding':None,
              'delimiter':delimiter, 'valuesAnalyzed':analyze_values, 'rows':0, 'columns':None, 'raggedRows':0,
              'emptyRows':0, 'typeRisks':{'leadingZeros':0, 'formulaLike':0,
                  'dateLike':0, 'scientificLike':0}, 'valuesDigest':None}
    class Budget(Exception):
        pass
    class Lines:
        def __init__(self, stream):
            self.stream = stream
            self.read = 0
        def __iter__(self):
            return self
        def __next__(self):
            if (cancel is not None and cancel.is_set()) or time.monotonic()-started > seconds:
                raise Budget('cancelled_or_deadline')
            line = self.stream.readline(256*1024+1)
            if not line:
                raise StopIteration
            self.read += len(line.encode('utf-8'))
            if len(line)>256*1024 or self.read>max_bytes:
                raise Budget('inspection_budget')
            if '\0' in line:
                raise UnicodeError('binary text')
            return line
    if delimiter not in DELIMITERS:
        result['reason']='dialect_undetermined'
        return result
    digest = hashlib.sha256()
    try:
        with Path(source).open('r',encoding='utf-8-sig',errors='strict',newline='') as stream:
            for row in csv.reader(Lines(stream),delimiter=delimiter,quotechar='"',strict=True):
                result['rows'] += 1
                if not row:
                    result['emptyRows'] += 1
                elif result['columns'] is None:
                    result['columns'] = len(row)
                elif len(row)!=result['columns']:
                    result['raggedRows'] += 1
                if not analyze_values:
                    continue
                digest.update(json.dumps(row,ensure_ascii=True,separators=(',',':')).encode('ascii')+b'\n')
                for value in row:
                    risks=result['typeRisks']
                    if re.match(r'^[+-]?0\d+',value):risks['leadingZeros']+=1
                    if value.startswith(('=','+','-','@')):risks['formulaLike']+=1
                    if re.fullmatch(r'\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}',value):risks['dateLike']+=1
                    if re.fullmatch(r'[+-]?\d+(?:[.,]\d+)?[eE][+-]?\d+',value):risks['scientificLike']+=1
        result.update(status='inconsistent' if result['raggedRows'] else 'checked',
                      complete=True, encoding='utf-8', valuesDigest=digest.hexdigest() if analyze_values else None)
    except Budget as error:
        result.update(status='incomplete',reason=str(error))
    except UnicodeError:
        result.update(status='undetermined',reason='encoding_or_binary')
    except csv.Error:
        result.update(status='incomplete',reason='csv_syntax_or_field_limit')
    except OSError:
        result.update(status='unavailable',reason='read_failed')
    result['durationMs']=round((time.monotonic()-started)*1000)
    return result
