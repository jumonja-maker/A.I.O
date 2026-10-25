"""Conservative text preservation checks for ambiguous Writer ODF tables.

LibreOffice can import a table without column declarations and silently keep
only its first column.  Missing declarations are not classified as invalid.
Instead, compare actual nonempty cell text with an inspectable text export.
This is a content subset check, not a visual or semantic fidelity certificate.
"""
import codecs
from collections import Counter
from contextlib import contextmanager
from html.parser import HTMLParser
from pathlib import Path
import re
import xml.etree.ElementTree as ET
import zipfile

from .errors import GuardError

_TABLE = 'urn:oasis:names:tc:opendocument:xmlns:table:1.0'
_TEXT = 'urn:oasis:names:tc:opendocument:xmlns:text:1.0'
_WORD = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
_STRICT_WORD = 'http://purl.oclc.org/ooxml/wordprocessingml/main'
_ODF_OUTPUTS = frozenset(('odt', 'ott', 'odm', 'oth', 'otm'))
_WORD_OUTPUTS = frozenset(('docx', 'docm', 'dotx', 'dotm'))
_HTML_OUTPUTS = frozenset(('html', 'htm', 'xhtml'))
_TARGETS = _ODF_OUTPUTS | _WORD_OUTPUTS | _HTML_OUTPUTS | {'txt', 'fodt'}
_PARAGRAPHS = {'{' + _TEXT + '}p', '{' + _TEXT + '}h',
               '{' + _WORD + '}p', '{' + _STRICT_WORD + '}p'}
_SPACES = {'{' + _TEXT + '}s', '{' + _TEXT + '}tab', '{' + _TEXT + '}line-break',
           '{' + _WORD + '}tab', '{' + _WORD + '}br',
           '{' + _STRICT_WORD + '}tab', '{' + _STRICT_WORD + '}br'}
_OFFICE = 'urn:oasis:names:tc:opendocument:xmlns:office:1.0'
_NONBODY = {'{' + _OFFICE + '}annotation', '{' + _TEXT + '}note',
            '{' + _TEXT + '}tracked-changes'}
# These bound this optional inspection, not admissible conversion file sizes.
MAX_XML_READ = 64 * 1024 * 1024
MAX_XML_DEPTH = 128
MAX_UNIQUE_CELL_TEXTS = 10000
MAX_CELL_TEXT_BYTES = 4 * 1024 * 1024
MAX_STRUCTURAL_CELLS = 20000
MAX_TEXT_MATCH_WORK = 64 * 1024 * 1024


class _InspectionLimited(Exception):
    pass


class _InspectionReader:
    def __init__(self, stream, cancel):
        self.stream, self.cancel = stream, cancel
        self.used = 0
        self.tail = b''

    def read(self, size=-1):
        _cancel(self.cancel)
        block = self.stream.read(min(size, 64 * 1024) if size >= 0 else 64 * 1024)
        self.used += len(block)
        if self.used > MAX_XML_READ:
            raise _InspectionLimited('xml_read_budget')
        # NUL removal also catches UTF-16/32 XML declarations before ElementTree
        # can expand an internal entity; these encodings remain parseable.
        declarations = (self.tail + block).replace(b'\x00', b'').lower()
        if b'<!doctype' in declarations or b'<!entity' in declarations:
            raise _InspectionLimited('xml_declarations_not_inspected')
        self.tail = declarations[-16:]
        return block


def _cancel(cancel):
    if cancel.is_set():
        raise GuardError('CANCELLED', 'Проверка содержимого отменена.', stage='result')


def _normalize(text):
    return ' '.join(text.split())


def _element_text(element):
    pieces = [element.text or '']
    for child in element:
        if child.tag in _NONBODY:
            pass
        elif child.tag in _SPACES:
            pieces.append(' ')
        else:
            pieces.append(_element_text(child))
        if child.tag in _PARAGRAPHS:
            pieces.append(' ')
        pieces.append(child.tail or '')
    return ''.join(pieces)


@contextmanager
def _odf_xml(path):
    """Yield Writer ODF XML only; filename alone is never content evidence."""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            if 'mimetype' not in archive.namelist() or 'content.xml' not in archive.namelist():
                yield None
                return
            with archive.open('mimetype') as stream:
                mime = stream.read(256).strip()
            if not mime.startswith(b'application/vnd.oasis.opendocument.text'):
                yield None
                return
            with archive.open('content.xml') as stream:
                yield stream
    elif path.suffix.lower() == '.fodt':
        with path.open('rb') as stream:
            yield stream
    else:
        yield None


def _source_cells(source, cancel):
    """Inspect only ODF Writer tables with implicit column declarations.

    Preserve the row/cell grid and multiplicity rather than a unique-token
    set. Bounded inspection may decline complex spans and repeated rows; it
    never modifies the original and never vetoes a document based on size.
    """
    tokens, tables, grids = set(), [], []
    physical_cells = 0
    token_bytes = 0
    total_cells = 0
    values = Counter()
    with _odf_xml(source) as stream:
        if stream is None:
            return None
        stack = []
        for event, element in ET.iterparse(_InspectionReader(stream, cancel), events=('start', 'end')):
            _cancel(cancel)
            if event == 'start':
                stack.append(element)
                if len(stack) > MAX_XML_DEPTH:
                    raise _InspectionLimited('xml_depth_budget')
                if element.tag == '{' + _TABLE + '}table':
                    if tables:
                        tables[-1]['ambiguous'] = True
                    tables.append({'columns': False, 'tokens': set(), 'values': Counter(),
                                   'cells': 0, 'rows': [], 'row': None, 'ambiguous': False})
                elif element.tag == '{' + _TABLE + '}table-column' and tables:
                    tables[-1]['columns'] = True
                elif element.tag == '{' + _TABLE + '}table-row' and tables:
                    table = tables[-1]
                    if table['row'] is not None or element.get('{' + _TABLE + '}number-rows-repeated', '1') != '1':
                        table['ambiguous'] = True
                    table['row'] = []
                elif element.tag in ('{' + _TABLE + '}table-cell',
                                      '{' + _TABLE + '}covered-table-cell') and tables:
                    if (element.tag.endswith('}covered-table-cell') or
                            any(element.get('{' + _TABLE + '}' + attr, '1') != '1'
                                for attr in ('number-columns-repeated', 'number-columns-spanned',
                                             'number-rows-spanned'))):
                        tables[-1]['ambiguous'] = True
                continue
            if element.tag == '{' + _TABLE + '}table-cell' and tables:
                table = tables[-1]
                text = _normalize(_element_text(element))
                if table['row'] is None:
                    table['ambiguous'] = True
                else:
                    table['row'].append(text)
                total_cells += 1
                if total_cells > MAX_STRUCTURAL_CELLS:
                    raise _InspectionLimited('structural_cell_budget')
                if text:
                    if text not in table['tokens']:
                        token_bytes += len(text.encode('utf-8'))
                        if (token_bytes > MAX_CELL_TEXT_BYTES or
                                len(tokens) + sum(len(t['tokens']) for t in tables)
                                >= MAX_UNIQUE_CELL_TEXTS):
                            raise _InspectionLimited('cell_text_budget')
                        table['tokens'].add(text)
                    table['values'][text] += 1
                    table['cells'] += 1
                element.clear()
            elif element.tag == '{' + _TABLE + '}table-row' and tables:
                table = tables[-1]
                if table['row'] is not None:
                    table['rows'].append(tuple(table['row']))
                    table['row'] = None
                element.clear()
            elif element.tag == '{' + _TABLE + '}table' and tables:
                table = tables.pop()
                if not table['columns'] and table['cells']:
                    if table['ambiguous']:
                        raise _InspectionLimited('complex_table_layout')
                    tokens.update(table['tokens'])
                    values.update(table['values'])
                    grids.append(tuple(table['rows']))
                    physical_cells += table['cells']
                element.clear()
            elif not tables:
                element.clear()
            if len(stack) > 1 and (not tables or element.tag in (
                    '{' + _TABLE + '}table-cell', '{' + _TABLE + '}table-row')):
                stack[-2].remove(element)
            stack.pop()
    return {'tokens': tokens, 'values': values, 'tables': grids,
            'cells': physical_cells} if tokens else None

class _Presence:
    """Match normalized cell text across streamed element/chunk boundaries."""
    def __init__(self, tokens):
        self.missing = set(tokens)
        self.keep = max(map(len, self.missing), default=1)
        self.tail = ''
        self.pending_space = False
        self.started = False

    def feed(self, text):
        if not self.missing:
            return
        pieces = []
        for part in re.finditer(r'\s+|\S+', text):
            value = part.group()
            if value.isspace():
                self.pending_space = True
            else:
                if self.pending_space and self.started:
                    pieces.append(' ')
                pieces.append(value)
                self.started = True
                self.pending_space = False
        window = self.tail + ''.join(pieces)
        self.missing.difference_update(token for token in tuple(self.missing) if token in window)
        self.tail = window[-self.keep:]


def _xml_text(stream, matcher, cancel):
    stack, paragraph_depth = [], 0
    for event, element in ET.iterparse(_InspectionReader(stream, cancel), events=('start', 'end')):
        _cancel(cancel)
        if event == 'start':
            stack.append(element)
            if len(stack) > MAX_XML_DEPTH:
                raise _InspectionLimited('xml_depth_budget')
            if element.tag in _PARAGRAPHS:
                paragraph_depth += 1
            continue
        if element.tag in _PARAGRAPHS:
            matcher.feed(_element_text(element))
            matcher.feed(' ')
            paragraph_depth -= 1
        if not paragraph_depth:
            element.clear()
            if len(stack) > 1:
                stack[-2].remove(element)
        stack.pop()


class _HtmlText(HTMLParser):
    def __init__(self, matcher):
        super().__init__(convert_charrefs=True)
        self.matcher = matcher
        self.ignored = []

    def handle_starttag(self, tag, attrs):
        if tag in ('head', 'style', 'script', 'template'):
            self.ignored.append(tag)
        if not self.ignored and tag in ('p', 'div', 'br', 'tr', 'td', 'th', 'li'):
            self.matcher.feed(' ')

    def handle_endtag(self, tag):
        if self.ignored and self.ignored[-1] == tag:
            self.ignored.pop()
        if not self.ignored and tag in ('p', 'div', 'tr', 'td', 'th', 'li'):
            self.matcher.feed(' ')

    def handle_data(self, text):
        if not self.ignored:
            self.matcher.feed(text)


def _output_text(path, target, matcher, cancel):
    if target in _ODF_OUTPUTS | _WORD_OUTPUTS:
        member = 'content.xml' if target in _ODF_OUTPUTS else 'word/document.xml'
        with zipfile.ZipFile(path) as archive, archive.open(member) as stream:
            _xml_text(stream, matcher, cancel)
    elif target == 'fodt':
        with path.open('rb') as stream:
            _xml_text(stream, matcher, cancel)
    else:
        parser = _HtmlText(matcher) if target in _HTML_OUTPUTS else None
        # Share the bounded inspection transform, never alter exported bytes.
        from .postconditions import _AttributeLimiter
        limiter = _AttributeLimiter() if parser else None
        decoder = codecs.getincrementaldecoder('utf-8-sig')('strict')
        with path.open('rb') as stream:
            while block := stream.read(64 * 1024):
                _cancel(cancel)
                text = decoder.decode(block)
                if parser:
                    parser.feed(limiter.feed(text))
                    if len(parser.rawdata) > 256 * 1024:
                        raise ValueError('html_parser_budget')
                else:
                    matcher.feed(text)
            text = decoder.decode(b'', final=True)
            if parser:
                parser.feed(limiter.feed(text))
                parser.close()
            else:
                matcher.feed(text)


def check_content_invariants(paths, target, source, *, cancel, source_evidence=None):
    """Reject missing table text only where both sides can be inspected.

    Unsupported output families (including PDF and legacy binary Office) are
    explicitly uninspected, since text extraction failure is not proof of loss.
    """
    source = Path(source)
    if not source.is_file():
        return None
    try:
        expected = (_source_cells(source, cancel) if source_evidence is None else
                    source_evidence.get('odf_table_text', source,
                                        lambda: _source_cells(source, cancel)))
    except _InspectionLimited as error:
        return {'check': 'odf_table_text', 'status': 'not_checked',
                'reason': 'source_inspection_budget', 'inspectionLimit': str(error),
                'qualityCompared': False}
    except (ET.ParseError, zipfile.BadZipFile, KeyError, OSError, ValueError):
        # Import/source contracts own malformed input diagnosis, but this
        # inspection must not disappear from provenance as a successful check.
        return {'check': 'odf_table_text', 'status': 'not_checked',
                'reason': 'source_inspection_unavailable', 'qualityCompared': False}
    if not expected:
        return None
    result = {'check': 'odf_table_text', 'sourceNonemptyCells': expected['cells'],
              'uniqueCellTexts': len(expected['tokens']), 'qualityCompared': False}
    if target not in _TARGETS:
        return {**result, 'status': 'not_checked', 'reason': 'target_text_not_inspectable'}
    candidates = [Path(p) for p in paths if Path(p).suffix.lower() == '.' + target]
    if len(candidates) != 1:
        raise GuardError('ENGINE_OUTPUT_VALIDATION_INCOMPLETE',
            'Не удалось проверить текст таблиц в результате. Он не отмечен готовым.',
            stage='result', diagnostic='odf_table_primary_ambiguous')
    matcher = _Presence(expected['tokens'])
    try:
        _output_text(candidates[0], target, matcher, cancel)
    except (_InspectionLimited, ET.ParseError, zipfile.BadZipFile, KeyError,
            UnicodeError, OSError, ValueError) as error:
        raise GuardError('ENGINE_OUTPUT_VALIDATION_INCOMPLETE',
            'Не удалось проверить текст таблиц в результате. Он не отмечен готовым.',
            stage='result', diagnostic='odf_table_output:' + type(error).__name__) from error
    _cancel(cancel)
    if matcher.missing:
        raise GuardError('ENGINE_OUTPUT_CONTENT_LOSS',
            'В результате отсутствует текст исходных ячеек таблицы. Он не отмечен готовым.',
            stage='result', diagnostic='odf_table_missing_unique_cell_texts:' + str(len(matcher.missing)))
    return {**result, 'status': 'checked_subset', 'missingUniqueCellTexts': 0}
