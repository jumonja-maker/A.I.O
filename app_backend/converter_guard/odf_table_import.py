"""Preserve unambiguous Writer tables when LO omits implicit columns.

Only a private import copy changes. The source XML is byte-for-byte preserved
apart from explicit column declarations whose count follows every physical
row. No content, styles, namespace bindings or ZIP resources are rewritten.
Ambiguous spanning/ragged tables remain unchanged and retain result checks.
"""
from pathlib import Path
import copy
from xml.parsers import expat
import zipfile

from .errors import GuardError

TABLE = 'urn:oasis:names:tc:opendocument:xmlns:table:1.0'
OFFICE = 'urn:oasis:names:tc:opendocument:xmlns:office:1.0'
MAX_XML_BYTES = 64 * 1024 * 1024
MAX_XML_DEPTH = 128
MAX_COLUMNS = 16384
MAX_TABLES = 10000
MAX_PACKAGE_BYTES = 512 * 1024 * 1024


def _check(cancel):
    if cancel.is_set():
        raise GuardError('CANCELLED', 'Подготовка таблиц отменена.', stage='import')


def _key(local):
    return TABLE + '}' + local


class _Uninspectable(Exception):
    pass


def _insertions(data, cancel):
    if len(data) > MAX_XML_BYTES:
        raise _Uninspectable('xml_budget')
    # Byte offsets below are valid only for UTF-8/single-byte ASCII-compatible
    # XML. UTF-16/32 and unusual encodings remain the native importer's work.
    if b'\0' in data:
        raise _Uninspectable('xml_encoding')
    parser = expat.ParserCreate(namespace_separator='}')
    parser.buffer_text = True
    stack, tables, completed = [], [], []
    roots = []

    def decline(*args):
        raise _Uninspectable('xml_declarations')

    def positive(attrs, local):
        raw = attrs.get(_key(local), '1')
        if not raw.isascii() or not raw.isdigit() or not 0 < int(raw) <= MAX_COLUMNS:
            raise _Uninspectable('logical_grid_budget')
        return int(raw)

    def start(name, attrs):
        _check(cancel)
        if len(stack) >= MAX_XML_DEPTH:
            raise _Uninspectable('xml_depth')
        if not stack:
            roots.append(name)
        stack.append(name)
        if name == _key('table'):
            if len(completed) + len(tables) >= MAX_TABLES:
                raise _Uninspectable('table_budget')
            tables.append({'depth':len(stack), 'columns':False, 'width':None,
                           'rows':0, 'uniform':True,
                           'row':None, 'insert':None, 'ambiguous':False})
        elif tables:
            table = tables[-1]
            depth = len(stack) - table['depth']
            if name == _key('table-column'):
                table['columns'] = True
            if (depth == 1 and name in {_key('table-row'), _key('table-rows'),
                                       _key('table-header-rows'), _key('table-row-group')}
                    and table['insert'] is None):
                table['insert'] = parser.CurrentByteIndex
            if name == _key('table-row'):
                if table['row'] is not None:
                    table['ambiguous'] = True
                table['row'] = 0
                positive(attrs, 'number-rows-repeated')
            elif name in {_key('table-cell'), _key('covered-table-cell')}:
                if table['row'] is None:
                    table['ambiguous'] = True
                else:
                    table['row'] += positive(attrs, 'number-columns-repeated')
                    if table['row'] > MAX_COLUMNS:
                        raise _Uninspectable('logical_grid_budget')
                if (name == _key('covered-table-cell') or
                        positive(attrs, 'number-columns-spanned') != 1 or
                        positive(attrs, 'number-rows-spanned') != 1):
                    table['ambiguous'] = True

    def end(name):
        _check(cancel)
        if tables:
            table = tables[-1]
            if name == _key('table-row'):
                if table['row'] is not None:
                    if table['rows'] == 0:
                        table['width'] = table['row']
                    elif table['width'] != table['row']:
                        table['uniform'] = False
                    table['rows'] += 1
                table['row'] = None
            elif name == _key('table'):
                completed.append(tables.pop())
        stack.pop()

    parser.StartElementHandler, parser.EndElementHandler = start, end
    parser.StartDoctypeDeclHandler = decline
    parser.EntityDeclHandler = decline
    parser.ExternalEntityRefHandler = decline
    try:
        for offset in range(0, len(data), 65536):
            _check(cancel)
            parser.Parse(data[offset:offset + 65536], False)
        parser.Parse(b'', True)
    except expat.ExpatError as error:
        raise _Uninspectable('xml_parse') from error
    if not roots or roots[0] not in {OFFICE + '}document-content', OFFICE + '}document'}:
        return []
    insertions = []
    for table in completed:
        width = table['width']
        if (table['columns'] or table['ambiguous'] or not table['rows'] or
                not width or not table['uniform'] or table['insert'] is None):
            continue
        # A local prefix makes the insertion independent of source prefixes,
        # including documents using a default namespace or shadowing bindings.
        declaration = ('<aio_guard_columns:table-column '
            'xmlns:aio_guard_columns="%s" '
            'aio_guard_columns:number-columns-repeated="%d"/>' % (TABLE, width)).encode('ascii')
        insertions.append((table['insert'], declaration, width, table['rows']))
    return sorted(insertions)


def _apply(data, insertions):
    chunks, previous = [], 0
    for offset, declaration, _, _ in insertions:
        chunks.extend((data[previous:offset], declaration))
        previous = offset
    chunks.append(data[previous:])
    return b''.join(chunks)


def prepare_odf_table_source(source, workspace, *, cancel):
    """Return private prepared path and bounded provenance, or original path.

    Budgets defer optional normalization without imposing a conversion-size
    limit. Invalid/non-Writer containers are left to source contracts/native
    import. All result postconditions must still compare to original source.
    """
    source, workspace = Path(source), Path(workspace)
    _check(cancel)
    archive = None
    try:
        if zipfile.is_zipfile(source):
            archive = zipfile.ZipFile(source)
            names = archive.namelist()
            if (names.count('mimetype') != 1 or names.count('content.xml') != 1 or
                    archive.getinfo('mimetype').file_size > 256):
                return source, None
            mime = archive.read('mimetype').strip()
            if mime not in {b'application/vnd.oasis.opendocument.text',
                            b'application/vnd.oasis.opendocument.text-template',
                            b'application/vnd.oasis.opendocument.text-master',
                            b'application/vnd.oasis.opendocument.text-web'}:
                return source, None
            if archive.getinfo('content.xml').file_size > MAX_XML_BYTES:
                raise _Uninspectable('xml_budget')
            data = archive.read('content.xml')
        elif source.suffix.lower() == '.fodt':
            if source.stat().st_size > MAX_XML_BYTES:
                raise _Uninspectable('xml_budget')
            data = source.read_bytes()
            # Require the Writer body/mimetype contract rather than suffix.
            from .source_contract import inspect_source_contract
            contract = inspect_source_contract(source, cancel=cancel)
            if contract.get('expectedServices') != ['com.sun.star.text.TextDocument']:
                return source, None
        else:
            return source, None
        insertions = _insertions(data, cancel)
        if not insertions:
            return source, None
        if archive and any(n.lower().startswith('meta-inf/') and 'signature' in n.lower() for n in names):
            raise _Uninspectable('signed_package')
        if archive and (len(names) != len(set(names)) or
                        sum(i.file_size for i in archive.infolist()) > MAX_PACKAGE_BYTES):
            raise _Uninspectable('package_budget_or_duplicate_members')
        _check(cancel)
        prepared = workspace / 'odf-table-import' / source.name
        prepared.parent.mkdir(parents=True, exist_ok=True)
        if prepared.exists():
            raise OSError('Private ODF table import path already exists')
        rewritten = _apply(data, insertions)
        try:
            if archive:
                # No extraction: odd member paths cannot write outside the
                # new package. Copy all resources, methods and attributes.
                with zipfile.ZipFile(prepared, 'x') as output:
                    output.comment = archive.comment
                    for entry in archive.infolist():
                        _check(cancel)
                        with output.open(copy.copy(entry), 'w') as destination:
                            if entry.filename == 'content.xml':
                                destination.write(rewritten)
                            else:
                                with archive.open(entry) as original:
                                    while block := original.read(65536):
                                        _check(cancel)
                                        destination.write(block)
            else:
                with prepared.open('xb') as output:
                    output.write(rewritten)
        except BaseException:
            prepared.unlink(missing_ok=True)
            raise
        return prepared, {'kind':'odf_explicit_table_columns', 'state':'prepared',
            'tables':len(insertions), 'columns':[i[2] for i in insertions],
            'rows':[i[3] for i in insertions], 'originalPreserved':True,
            'contentRewritten':False, 'qualityCompared':False}
    except _Uninspectable as error:
        return source, {'kind':'odf_explicit_table_columns', 'state':'not_prepared',
                        'reason':str(error), 'originalPreserved':True, 'qualityCompared':False}
    except (zipfile.BadZipFile, KeyError, RuntimeError, UnicodeError) as error:
        return source, {'kind':'odf_explicit_table_columns', 'state':'not_prepared',
                        'reason':'source_inspection_unavailable', 'originalPreserved':True,
                        'qualityCompared':False}
    finally:
        if archive:
            archive.close()
