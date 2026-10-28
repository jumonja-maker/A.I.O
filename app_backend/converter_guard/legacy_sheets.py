"""Stream private single-sheet snapshots of a native-produced Flat Calc file.

This is the CLI counterpart of XSpreadsheets2.importSheet: LibreOffice first
imports the original and exports a FODS with evaluated values and native
styles. Each snapshot retains those bytes except other top-level sheets and
formula attributes; only native LibreOffice performs the DBF/DIF conversion.
No document-size ceiling or whole-document XML tree is needed.
"""
from pathlib import Path
import hashlib
import os
import shutil
from xml.parsers import expat
from xml.sax.saxutils import quoteattr

from .errors import GuardError

OFFICE = 'urn:oasis:names:tc:opendocument:xmlns:office:1.0'
TABLE = 'urn:oasis:names:tc:opendocument:xmlns:table:1.0'
CALCEXT = 'urn:org:documentfoundation:names:experimental:calc:xmlns:calcext:1.0'
BLOCK = 65536


def _check(cancel):
    if cancel.is_set():
        raise GuardError('CANCELLED', 'Подготовка листов отменена.', stage='export')


def _invalid(reason):
    raise GuardError('NATIVE_TABLE_SNAPSHOT_INVALID',
                     'Не удалось безопасно подготовить все листы для DBF/DIF.',
                     stage='export', diagnostic=reason)


def _expanded(name, namespaces):
    prefix, separator, local = name.partition(':')
    return (namespaces.get(prefix, ''), local) if separator else (namespaces.get('', ''), prefix)


def _scope(attrs, namespaces):
    changed = []
    for name, value in attrs.items():
        if name == 'xmlns' or name.startswith('xmlns:'):
            prefix = name[6:] if name.startswith('xmlns:') else ''
            changed.append((prefix, namespaces.get(prefix)))
            namespaces[prefix] = value
    return changed


def _restore(changed, namespaces):
    for prefix, previous in reversed(changed):
        if previous is None:
            namespaces.pop(prefix, None)
        else:
            namespaces[prefix] = previous


def _tag_end(stream, offset, cancel):
    """Return the byte after an XML tag and whether it is self-closing."""
    stream.seek(offset)
    quote, previous = None, None
    position = offset
    while block := stream.read(BLOCK):
        _check(cancel)
        for byte in block:
            position += 1
            if quote is not None:
                if byte == quote:
                    quote = None
            elif byte in (34, 39):
                quote = byte
            elif byte == 62:
                return position, previous == 47
            if byte not in (9, 10, 13, 32):
                previous = byte
    _invalid('unfinished_native_xml_tag')


def _secure_parser():
    parser = expat.ParserCreate()
    def decline(*_):
        _invalid('native_xml_declarations')
    parser.StartDoctypeDeclHandler = decline
    parser.EntityDeclHandler = decline
    parser.ExternalEntityRefHandler = decline
    def declaration(version, encoding, standalone):
        if encoding and encoding.lower().replace('-', '') not in ('utf8', 'ascii', 'usascii'):
            _invalid('native_flat_xml_encoding')
    parser.XmlDeclHandler = declaration
    return parser


def _index(stream, tags, cancel):
    parser = _secure_parser()
    namespaces, stack, sheets = {}, [], []
    opened = None
    formula_cells = 0
    error_cell = None

    def start(name, attrs):
        nonlocal opened, formula_cells, error_cell
        _check(cancel)
        changed = _scope(attrs, namespaces)
        expanded = _expanded(name, namespaces)
        if not stack and (expanded != (OFFICE, 'document') or
                not any(_expanded(key, namespaces) == (OFFICE, 'mimetype') and
                        value == 'application/vnd.oasis.opendocument.spreadsheet'
                        for key, value in attrs.items())):
            _invalid('not_native_flat_calc')
        if len(stack) > 256:
            _invalid('native_xml_depth')
        if expanded == (TABLE, 'table') and stack and stack[-1][0] == (OFFICE, 'spreadsheet'):
            if opened is not None:
                _invalid('overlapping_native_sheets')
            offset = parser.CurrentByteIndex
            tag_end, self_closing = _tag_end(tags, offset, cancel)
            opened = {'start': offset, 'openEnd': tag_end, 'selfClosing': self_closing,
                      'depth': len(stack) + 1, 'namespaces': dict(namespaces),
                      'name': next((v for k,v in attrs.items() if _expanded(k,namespaces) == (TABLE,'name')), '')}
        if opened and expanded == (TABLE, 'table-cell'):
            attributes = {_expanded(key, namespaces): value for key, value in attrs.items()}
            if (TABLE, 'formula') in attributes:
                kind = attributes.get((OFFICE, 'value-type'))
                required = {'float': 'value', 'currency': 'value', 'percentage': 'value',
                            'date': 'date-value', 'time': 'time-value', 'boolean': 'boolean-value'}
                if kind not in {*required, 'string'} or (kind in required and
                        (OFFICE, required[kind]) not in attributes):
                    _invalid('native_formula_cache_missing')
                formula_cells += 1
                if attributes.get((CALCEXT, 'value-type')) == 'error':
                    error_cell = {'depth': len(stack) + 1, 'text': False}
        stack.append((expanded, changed))

    def text(value):
        if error_cell is not None and value.strip():
            error_cell['text'] = True

    def end(name):
        nonlocal opened, error_cell
        _check(cancel)
        if error_cell is not None and error_cell['depth'] == len(stack):
            if not error_cell['text']:
                _invalid('native_formula_error_display_missing')
            error_cell = None
        if opened and opened['depth'] == len(stack):
            if stack[-1][0] != (TABLE, 'table'):
                _invalid('invalid_native_sheet_stack')
            opened['end'] = (opened['openEnd'] if opened['selfClosing'] else
                             _tag_end(tags, parser.CurrentByteIndex, cancel)[0])
            sheets.append(opened)
            opened = None
        _expanded_name, changed = stack.pop()
        _restore(changed, namespaces)

    parser.StartElementHandler, parser.EndElementHandler = start, end
    parser.CharacterDataHandler = text
    try:
        while block := stream.read(BLOCK):
            _check(cancel)
            parser.Parse(block, False)
        parser.Parse(b'', True)
    except expat.ExpatError as error:
        _invalid('native_xml_parse:' + str(error.code))
    if not sheets or opened is not None:
        _invalid('native_sheets_missing')
    return sheets, formula_cells


def _copy_range(source, destination, begin, end, cancel):
    source.seek(begin)
    while begin < end:
        _check(cancel)
        data = source.read(min(BLOCK, end-begin))
        if not data:
            _invalid('native_flat_file_changed')
        destination.write(data)
        begin += len(data)


def _copy_sheet(source, scan, tags, destination, sheet, cancel):
    """Strip formula attrs using native XML token offsets, keeping cached data."""
    parser = _secure_parser()
    namespaces = dict(sheet['namespaces'])
    scopes = []
    position = sheet['start']

    def start(name, attrs):
        nonlocal position
        _check(cancel)
        scopes.append(_scope(attrs, namespaces))
        if (_expanded(name, namespaces) == (TABLE, 'table-cell') and
                any(_expanded(key, namespaces) == (TABLE, 'formula') for key in attrs)):
            offset = sheet['start'] + parser.CurrentByteIndex
            end, self_closing = _tag_end(tags, offset, cancel)
            _copy_range(source, destination, position, offset, cancel)
            attributes = {_expanded(key, namespaces): value for key, value in attrs.items()}
            excluded = {(TABLE, 'formula')}
            if attributes.get((CALCEXT, 'value-type')) == 'error':
                # Native Calc writes error caches as string-value="" plus
                # the displayed error in text:p. Without a formula the empty
                # string attribute wins over text:p; retain the visible text
                # as an ordinary string rather than producing a blank cell.
                excluded.update({(CALCEXT, 'value-type'), (OFFICE, 'string-value')})
            kept = [(key, value) for key, value in attrs.items()
                    if _expanded(key, namespaces) not in excluded]
            tag = '<' + name + ''.join(' ' + key + '=' + quoteattr(value) for key, value in kept)
            destination.write((tag + ('/>' if self_closing else '>')).encode('utf-8'))
            position = end

    def end(name):
        _restore(scopes.pop(), namespaces)

    parser.StartElementHandler, parser.EndElementHandler = start, end
    scan.seek(sheet['start'])
    remaining = sheet['end'] - sheet['start']
    try:
        while remaining:
            _check(cancel)
            block = scan.read(min(BLOCK, remaining))
            if not block:
                _invalid('native_flat_file_changed')
            parser.Parse(block, False)
            remaining -= len(block)
        parser.Parse(b'', True)
    except expat.ExpatError as error:
        _invalid('native_sheet_xml_parse:' + str(error.code))
    _copy_range(source, destination, position, sheet['end'], cancel)


def prepare_calc_sheet_snapshots(native_fods, output, *, stem=None, cancel):
    """Return (private FODS paths, metadata), after native import+FODS export.

    Caller must use an independently loaded, freshly native-exported FODS.
    Existing output directories are refused; originals and styles stay intact.
    Formula attributes alone are removed because legacy files store values.
    """
    native_fods, output = Path(native_fods), Path(output)
    stem = native_fods.stem if stem is None else stem
    if not isinstance(stem, str) or not stem or Path(stem).name != stem or stem in ('.', '..'):
        _invalid('snapshot_filename')
    # A valid 255-character source filename can exceed Windows component
    # length when "__sheet_NNN.fods" is appended. The temporary snapshot
    # stem is not user-facing: shorten it deterministically without moving
    # or renaming the source or changing its recorded sheet provenance.
    if len((stem + '__sheet_999.fods').encode('utf-16-le')) // 2 > 200:
        stem = 'aio-snapshot-' + hashlib.sha256(stem.encode('utf-8')).hexdigest()[:20]
    output.mkdir(parents=True, exist_ok=False)
    paths = []
    try:
        with native_fods.open('rb') as source, native_fods.open('rb') as scan, native_fods.open('rb') as tags:
            initial = os.fstat(source.fileno())
            sheets, formula_cells = _index(scan, tags, cancel)
            for index, selected in enumerate(sheets):
                _check(cancel)
                name = stem if index == 0 else stem + '__sheet_%03d' % (index + 1)
                path = output / (name + '.fods')
                with path.open('xb') as destination:
                    position = 0
                    for sheet in sheets:
                        _copy_range(source, destination, position, sheet['start'], cancel)
                        if sheet is selected:
                            _copy_sheet(source, scan, tags, destination, sheet, cancel)
                        position = sheet['end']
                    _copy_range(source, destination, position, initial.st_size, cancel)
                paths.append(path)
            final = os.fstat(source.fileno())
            if any(getattr(initial, field) != getattr(final, field)
                   for field in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')):
                _invalid('native_flat_file_changed')
        return paths, {'check': 'native_calc_all_sheets', 'status': 'snapshot_prepared',
                       'sheetCount': len(sheets), 'sheetNames': [s['name'] for s in sheets],
                       'cachedFormulaCells': formula_cells,
                       'valuesOnly': True, 'qualityCompared': False}
    except BaseException:
        shutil.rmtree(output, ignore_errors=True)
        raise
