"""Check native container declarations before trusting an imported model.

This is a bounded structural check, not a document importer or schema validator.
It neither rewrites a source nor admits a route. Unknown content and exhausted
inspection budgets still require LibreOffice's own import proof.
"""
from pathlib import Path
import re
from urllib.parse import quote, unquote, urlsplit
import xml.etree.ElementTree as ET
import zipfile

from .errors import GuardError

OFFICE = 'urn:oasis:names:tc:opendocument:xmlns:office:1.0'
MATHML = 'http://www.w3.org/1998/Math/MathML'
LEGACY_OFFICE = 'http://openoffice.org/2000/office'
WRITER = 'com.sun.star.text.TextDocument'
CALC = 'com.sun.star.sheet.SpreadsheetDocument'
IMPRESS = 'com.sun.star.presentation.PresentationDocument'
DRAW = 'com.sun.star.drawing.DrawingDocument'
FORMULA = 'com.sun.star.formula.FormulaProperties'
CHART = 'com.sun.star.chart2.ChartDocument'
BASE = 'com.sun.star.sdb.OfficeDatabaseDocument'
ODF = {
    'text': ('text', WRITER), 'text-template': ('text', WRITER),
    'text-master': ('text', 'com.sun.star.text.GlobalDocument'),
    'text-master-template': ('text', 'com.sun.star.text.GlobalDocument'),
    'text-web': ('text', 'com.sun.star.text.WebDocument'),
    'spreadsheet': ('spreadsheet', CALC), 'spreadsheet-template': ('spreadsheet', CALC),
    'presentation': ('presentation', IMPRESS), 'presentation-template': ('presentation', IMPRESS),
    'graphics': ('drawing', DRAW), 'graphics-template': ('drawing', DRAW),
    'formula': ('formula', FORMULA), 'chart': ('chart', CHART), 'database': ('database', BASE),
}
LEGACY = {
    'writer': WRITER, 'writer.template': WRITER, 'writer.global': 'com.sun.star.text.GlobalDocument',
    'writer.web': 'com.sun.star.text.WebDocument', 'chart': CHART,
    'calc': CALC, 'calc.template': CALC, 'impress': IMPRESS, 'impress.template': IMPRESS,
    'draw': DRAW, 'draw.template': DRAW, 'math': FORMULA,
}


class _Budget(Exception):
    pass


class _UntrustedXML(Exception):
    pass


def _safe_xml_data(data, tail=b''):
    # This metadata check does not need DTDs or entities. Do not feed their
    # declarations to ElementTree; leave these documents to the owned native
    # importer without asserting that a diagnostic budget makes them invalid.
    # UTF-16/32 needs a separate encoding-aware scanner, so defer it too.
    combined = (tail + data).upper()
    if b'\0' in data or b'<!DOCTYPE' in combined or b'<!ENTITY' in combined:
        raise _UntrustedXML()
    return combined[-16:]


def _root_and_body(stream, *, cancel, budget):
    parser = ET.XMLPullParser(events=('start', 'end'))
    stack = []
    root = None
    size = 0
    tail = b''
    while block := stream.read(65536):
        if cancel is not None and cancel.is_set():
            raise GuardError('CANCELLED', 'Проверка исходного контейнера отменена.', stage='preflight')
        size += len(block)
        if size > budget:
            raise _Budget()
        tail = _safe_xml_data(block, tail)
        parser.feed(block)
        for event, item in parser.read_events():
            if event == 'start':
                stack.append(item.tag)
                if root is None:
                    root = (item.tag, dict(item.attrib))
                if len(stack) == 3 and stack[1] in ('{' + OFFICE + '}body', '{' + LEGACY_OFFICE + '}body'):
                    return root, item.tag
            else:
                stack.pop()
                item.clear()
    parser.close()
    return root, None


def _invalid(reason, content_class):
    return {'state': 'invalid', 'code': 'SOURCE_CONTAINER_INCONSISTENT',
            'reason': reason, 'contentClass': content_class, 'expectedServices': []}


def _odf_contract(mime, root, body, *, flat=False):
    if not root:
        return _invalid('missing_xml_root', 'odf')
    tag, attrs = root
    prefix = 'application/vnd.oasis.opendocument.'
    if mime.startswith(prefix):
        kind = mime[len(prefix):]
        expected = ODF.get(kind)
        flat_alias_family = {'presentation-flat-xml': 'presentation', 'text-flat-xml': 'text'}.get(kind) if flat else None
        if flat_alias_family:
            # These deployed aliases have installed native Flat XML importers.
            # Accept only its actual presentation body; the installed explicit
            # importer and matching live model remain mandatory afterwards.
            expected = ODF[flat_alias_family]
            if body != '{' + OFFICE + '}' + expected[0]:
                return _invalid('mimetype_body_mismatch', 'flat_odf')
        if expected is None:
            if flat and kind in ('text-flat-xml', 'spreadsheet-flat-xml',
                                 'presentation-flat-xml', 'graphics-flat-xml'):
                return _invalid('flat_mimetype_requires_document_family', 'flat_odf')
            return {'state': 'not_checked', 'reason': 'source_mimetype_unmapped', 'mimetype': mime}
        # Formula packages have a MathML content.xml rather than the
        # office:document-content wrapper used by Writer/Calc/Impress. This
        # recognizes the declaration only: native import must still prove the
        # FormulaProperties service through expectedServices/model matching.
        if kind == 'formula' and not flat and tag == '{' + MATHML + '}math':
            return {'state': 'recognized', 'contentClass': 'odf_formula_package',
                    'mimetype': mime, 'expectedServices': [FORMULA],
                    'mathmlRootObserved': True, 'bodyObserved': False}
        if tag != '{' + OFFICE + '}' + ('document' if flat else 'document-content'):
            return _invalid('mimetype_namespace_mismatch', 'odf')
        if body is not None and body != '{' + OFFICE + '}' + expected[0]:
            return _invalid('mimetype_body_mismatch', 'odf')
        return {'state': 'recognized', 'contentClass': 'flat_odf' if flat else 'odf_package',
                'mimetype': mime, 'expectedServices': [expected[1]],
                'bodyObserved': body is not None,
                **({'warnings': ['SOURCE_MIMETYPE_ALIAS'],
                    'nativeImportRequired': True,
                    'importProfile': 'flat_' + flat_alias_family + '_mimetype_alias'}
                   if flat_alias_family else {})}
    if mime.startswith('application/vnd.sun.xml.'):
        expected = LEGACY.get(mime[len('application/vnd.sun.xml.'):])
        if expected is None:
            return {'state': 'not_applicable'}
        if tag != '{' + LEGACY_OFFICE + '}document-content':
            return _invalid('legacy_mimetype_namespace_mismatch', 'staroffice_package')
        return {'state': 'recognized', 'contentClass': 'staroffice_package',
                'mimetype': mime, 'expectedServices': [expected]}
    return {'state': 'not_applicable'}


def _xml_root(stream, *, cancel, budget):
    """Read just a root; OOXML body structures vary by document family."""
    parser = ET.XMLPullParser(events=('start',))
    size = 0
    tail = b''
    while block := stream.read(65536):
        if cancel is not None and cancel.is_set():
            raise GuardError('CANCELLED', 'Проверка исходного контейнера отменена.', stage='preflight')
        size += len(block)
        if size > budget:
            raise _Budget()
        tail = _safe_xml_data(block, tail)
        parser.feed(block)
        for _, element in parser.read_events():
            return element.tag
    return None


_CONTENT_TYPES = 'http://schemas.openxmlformats.org/package/2006/content-types'
_RELATIONSHIPS = 'http://schemas.openxmlformats.org/package/2006/relationships'
_OFFICE_RELATIONSHIPS = frozenset((
    'http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument',
    'http://purl.oclc.org/ooxml/officeDocument/relationships/officeDocument',
))
_PERCENT = re.compile(r'%([0-9a-fA-F]{2})')
_UNRESERVED = frozenset('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~')


def _opc_part_key(value, *, relationship=False):
    """Resolve root relationships without extracting or guessing ZIP paths.

    OPC part URIs are package-relative, not local filesystem paths. A package
    relationship may contain dot segments; its resolved destination must remain
    inside the package. Part declarations/items themselves cannot have them.
    Escaped separators/unreserved characters are forbidden by OPC. Percent
    escapes are retained (ZIP mapping), normalized in case, and Unicode IRIs
    receive UTF-8 percent encoding. Case aliases are treated as ambiguous.
    """
    if (not isinstance(value, str) or not value or '\\' in value
            or any(ord(c) <= 32 or ord(c) == 127 for c in value)):
        raise ValueError('unsafe OPC URI')
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or '?' in value or '#' in value:
        raise ValueError('non-package OPC URI')
    path = parsed.path
    if not relationship and not path.startswith('/'):
        raise ValueError('part URI must be absolute')
    if path.startswith('//'):
        raise ValueError('authority in OPC URI')
    # Reject malformed escapes rather than allowing zipfile/URL consumers to
    # interpret the same name differently. Decode only to validate characters;
    # read the exact encoded ZIP item after resolving its unique identity.
    if re.search(r'%(?![0-9a-fA-F]{2})', path):
        raise ValueError('malformed OPC URI escape')
    for escaped in _PERCENT.finditer(path):
        char = chr(int(escaped.group(1), 16))
        if (char in _UNRESERVED or char in '/\\'
                or ord(char) < 32 or ord(char) == 127):
            raise ValueError('forbidden OPC URI escape')
    decoded = unquote(path, encoding='utf-8', errors='strict')
    if any(ord(c) < 32 or ord(c) == 127 for c in decoded):
        raise ValueError('control character in OPC URI')
    segments = []
    for segment in path.lstrip('/').split('/'):
        if relationship and segment == '.':
            continue
        if relationship and segment == '..':
            if not segments:
                raise ValueError('OPC URI escapes package root')
            segments.pop()
            continue
        if not segment or segment in ('.', '..') or segment.endswith('.'):
            raise ValueError('invalid OPC part segment')
        segments.append(segment)
    if not segments:
        raise ValueError('empty OPC part URI')
    canonical = quote('/'.join(segments), safe="/%:@!$&'()*+,;=-._~")
    canonical = _PERCENT.sub(lambda match: match.group(0).upper(), canonical)
    return canonical.casefold()


def _check_cancel(cancel, index=0):
    if index % 64 == 0 and cancel is not None and cancel.is_set():
        raise GuardError('CANCELLED', 'Проверка исходного контейнера отменена.', stage='preflight')


def _ooxml_contract(archive, names, *, cancel, budget):
    ct = '[Content_Types].xml'
    rel = '_rels/.rels'
    if names.count(ct) != 1 or names.count(rel) != 1:
        return _invalid('missing_or_duplicate_ooxml_declarations', 'ooxml_package')
    if max(archive.getinfo(ct).file_size, archive.getinfo(rel).file_size) > budget:
        raise _Budget()
    _check_cancel(cancel)
    ct_data, rel_data = archive.read(ct), archive.read(rel)
    _safe_xml_data(ct_data)
    _safe_xml_data(rel_data)
    content_types = ET.fromstring(ct_data)
    relationships = ET.fromstring(rel_data)
    if (content_types.tag != '{' + _CONTENT_TYPES + '}Types'
            or relationships.tag != '{' + _RELATIONSHIPS + '}Relationships'):
        return _invalid('ooxml_declaration_namespace', 'ooxml_package')
    relation_ids = set()
    for index, item in enumerate(relationships):
        _check_cancel(cancel, index)
        if item.tag == '{' + _RELATIONSHIPS + '}Relationship':
            identity = item.get('Id')
            if not identity or identity in relation_ids:
                return _invalid('ooxml_relationship_ids', 'ooxml_package')
            relation_ids.add(identity)
    main = [item for item in relationships
            if item.tag == '{' + _RELATIONSHIPS + '}Relationship'
            and item.get('Type') in _OFFICE_RELATIONSHIPS]
    if len(main) != 1 or main[0].get('TargetMode', 'Internal') != 'Internal':
        return _invalid('ooxml_main_relationship', 'ooxml_package')
    try:
        part_key = _opc_part_key(main[0].get('Target'), relationship=True)
    except (ValueError, UnicodeError):
        return _invalid('ooxml_main_part_missing_or_ambiguous', 'ooxml_package')
    # Never pass a URI directly to an archive or a filesystem. Match one exact
    # stored member, rejecting duplicate entries and case/escape aliases.
    matched = []
    for index, name in enumerate(names):
        _check_cancel(cancel, index)
        if name.endswith('/'):
            continue
        try:
            if _opc_part_key('/' + name) == part_key:
                matched.append(name)
        except (ValueError, UnicodeError):
            continue  # Unrelated non-part entries cannot establish the model.
    if len(matched) != 1:
        return _invalid('ooxml_main_part_missing_or_ambiguous', 'ooxml_package')
    part = matched[0]
    overrides, defaults = {}, {}
    try:
        for index, item in enumerate(content_types):
            _check_cancel(cancel, index)
            if item.tag == '{' + _CONTENT_TYPES + '}Override':
                key = _opc_part_key(item.get('PartName'))
                table = overrides
            elif item.tag == '{' + _CONTENT_TYPES + '}Default':
                extension = item.get('Extension', '')
                if (not extension or any(c in extension for c in './\\%?#:')
                        or any(ord(c) <= 32 for c in extension)):
                    raise ValueError('invalid content-type extension')
                key, table = extension.casefold(), defaults
            else:
                # Foreign namespace elements/attributes must not impersonate
                # a Default/Override declaration.
                continue
            declared_type = item.get('ContentType', '')
            if not declared_type or key in table:
                raise ValueError('missing or duplicate content-type declaration')
            table[key] = declared_type
    except (ValueError, UnicodeError):
        return _invalid('ooxml_content_type_declarations', 'ooxml_package')
    # OPC lookup precedence is Override for this part, then Default for its
    # extension. A conflicting Default does not override a valid Override.
    extension = part_key.rsplit('/', 1)[-1].rsplit('.', 1)
    declared = overrides.get(part_key)
    basis = 'override'
    if declared is None:
        declared = defaults.get(extension[-1]) if len(extension) == 2 else None
        basis = 'default'
    if not declared:
        return _invalid('ooxml_main_content_type', 'ooxml_package')
    family = next((name for name in ('wordprocessingml', 'spreadsheetml', 'presentationml')
                   if name in declared), None)
    if family is None:
        family = next((family for name, family in (
            ('ms-word.', 'wordprocessingml'), ('ms-excel.', 'spreadsheetml'),
            ('ms-powerpoint.', 'presentationml')) if name in declared), None)
    if family is None:
        return {'state': 'not_applicable'}
    with archive.open(part) as stream:
        root = _xml_root(stream, cancel=cancel, budget=budget)
    local, service = {'wordprocessingml': ('document', WRITER),
                      'spreadsheetml': ('workbook', CALC),
                      'presentationml': ('presentation', IMPRESS)}[family]
    accepted = {'{http://schemas.openxmlformats.org/' + family + '/2006/main}' + local,
                '{http://purl.oclc.org/ooxml/' + family + '/main}' + local}
    if root not in accepted:
        return _invalid('ooxml_main_type_namespace_mismatch', 'ooxml_package')
    return {'state': 'recognized', 'contentClass': 'ooxml_package',
            'mainContentType': declared, 'contentTypeBasis': basis,
            'expectedServices': [service]}


def inspect_source_contract(source, *, cancel=None, budget=4 * 1024 * 1024):
    """Return only content-confirmed declarations, independent of filename."""
    recognized = False
    try:
        path = Path(source)
        with path.open('rb') as stream:
            prefix = stream.read(4096)
            packed = prefix.startswith(b'PK\x03\x04')
            if not packed:
                if OFFICE.encode() not in prefix:
                    return {'state': 'not_applicable'}
                stream.seek(0)
                root, body = _root_and_body(stream, cancel=cancel, budget=budget)
                if not root or root[0] != '{' + OFFICE + '}document':
                    return {'state': 'not_applicable'}
                mime = root[1].get('{' + OFFICE + '}mimetype', '')
                if not mime:
                    return _invalid('flat_mimetype_missing', 'flat_odf')
                return _odf_contract(mime, root, body, flat=True)
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if 'mimetype' in names:
                if archive.getinfo('mimetype').file_size > 256:
                    return {'state': 'not_applicable'}
                mime = archive.read('mimetype').decode('ascii', 'strict')
                if mime == 'application/epub+zip':
                    return {'state': 'recognized', 'contentClass': 'epub_package',
                            'mimetype': mime, 'expectedServices': []}
                if not mime.startswith(('application/vnd.oasis.opendocument.', 'application/vnd.sun.xml.')):
                    return {'state': 'not_applicable'}
                recognized = True
                if names.count('mimetype') != 1:
                    return _invalid('duplicate_mimetype', 'odf_package')
                if names.count('content.xml') != 1:
                    return _invalid('missing_or_duplicate_content_xml', 'odf_package')
                with archive.open('content.xml') as stream:
                    root, body = _root_and_body(stream, cancel=cancel, budget=budget)
                return _odf_contract(mime, root, body)
            if '[Content_Types].xml' in names:
                recognized = True
                return _ooxml_contract(archive, names, cancel=cancel, budget=budget)
    except _Budget:
        return {'state': 'not_checked', 'reason': 'source_contract_budget'}
    except _UntrustedXML:
        return {'state': 'not_checked', 'reason': 'source_contract_dtd_entities_or_encoding'}
    except (ET.ParseError, UnicodeError, zipfile.BadZipFile, KeyError, ValueError,
            RuntimeError, NotImplementedError):
        return (_invalid('container_declaration_unreadable', 'office_package') if recognized
                else {'state': 'not_applicable'})
    except OSError:
        return {'state': 'not_checked', 'reason': 'source_contract_unavailable'}
    return {'state': 'not_applicable'}


def source_model_matches(contract, services):
    expected = contract.get('expectedServices', ())
    return not expected or bool(set(expected).intersection(services or ()))


def source_import_hint(contract, native):
    """A declared flat-ODF body may select its installed native importer.

    This does not establish compatibility: the subsequently loaded model and
    its actual FilterName must still agree with this content declaration.
    """
    if contract.get('state') != 'recognized' or contract.get('contentClass') != 'flat_odf':
        return None
    services = contract.get('expectedServices')
    if not services or len(services) != 1 or not native.valid:
        return None
    name = {WRITER: 'OpenDocument Text Flat XML', CALC: 'OpenDocument Spreadsheet Flat XML',
            IMPRESS: 'OpenDocument Presentation Flat XML', DRAW: 'OpenDocument Drawing Flat XML'}.get(services[0])
    item = native.filters.get(name)
    if item and item[0] == services[0] and 'IMPORT' in item[2]:
        return {'filter': name, 'options': None, 'basis': 'content_confirmed_flat_odf'}
    return None
