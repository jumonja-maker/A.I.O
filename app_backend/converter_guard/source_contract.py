"""Check native container declarations before trusting an imported model.

This is a bounded structural check, not a document importer or schema validator.
It neither rewrites a source nor admits a route. Unknown content and exhausted
inspection budgets still require LibreOffice's own import proof.
"""
from pathlib import Path
import re
from urllib.parse import quote, unquote, urlsplit
import xml.etree.ElementTree as ET
from xml.parsers import expat
import zipfile
import zlib

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


# Stop on the declaration we need, not after building a tree for a 64 KiB
# block. This is deliberately NOT a whole-document well-formedness verdict.
class _DeclarationFound(Exception):
    pass


_BODY_ROOTS = frozenset(('{' + OFFICE + '}document', '{' + OFFICE + '}document-content',
                         '{' + LEGACY_OFFICE + '}document-content'))
_BODY_TAGS = frozenset(('{' + OFFICE + '}body', '{' + LEGACY_OFFICE + '}body'))
_UTF32_PREFIXES = (b'\x00\x00\xfe\xff', b'\xff\xfe\x00\x00',
                   b'\x00\x00\x00<', b'<\x00\x00\x00')


def _deny_dtd(*args):
    # Parser callbacks distinguish real declarations from harmless strings in
    # comments/CDATA. Reject before any entity expansion or external access.
    raise _UntrustedXML()


def _clark(name):
    return '{' + name if '}' in name else name


def _probe_xml(stream, *, cancel, budget, body=False, initial=b''):
    parser = expat.ParserCreate(namespace_separator='}')
    parser.StartDoctypeDeclHandler = _deny_dtd
    parser.EntityDeclHandler = _deny_dtd
    parser.ExternalEntityRefHandler = _deny_dtd
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    # Retain Expat's default reparse-deferral protection for long XML tokens.
    stack, root, found_body = [], None, None
    events = 0

    def start(name, attributes):
        nonlocal root, found_body, events
        _check_cancel(cancel, events)
        events += 1
        tag = _clark(name)
        stack.append(tag)
        if root is None:
            root = (tag, {_clark(key): value for key, value in attributes.items()})
            if not body or tag not in _BODY_ROOTS:
                raise _DeclarationFound()
        if len(stack) > 256:
            raise _Budget()
        if len(stack) == 3 and stack[1] in _BODY_TAGS:
            found_body = tag
            raise _DeclarationFound()

    def end(name):
        stack.pop()

    parser.StartElementHandler, parser.EndElementHandler = start, end
    consumed, chunk = 0, 4096
    try:
        while True:
            _check_cancel(cancel)
            if consumed >= budget:
                raise _Budget()
            block = initial or stream.read(min(chunk, budget - consumed))
            initial = b''
            if len(block) > budget - consumed:
                raise _Budget()
            if consumed == 0 and block.startswith(_UTF32_PREFIXES):
                # Expat cannot inspect UTF-32. Unsupported is not corrupt.
                raise _UntrustedXML()
            consumed += len(block)
            parser.Parse(block, not block)
            if not block:
                break
            chunk = 65536
    except _DeclarationFound:
        pass
    except expat.ExpatError as error:
        if error.code in (expat.errors.codes[expat.errors.XML_ERROR_UNKNOWN_ENCODING],
                          expat.errors.codes[expat.errors.XML_ERROR_INCORRECT_ENCODING]):
            raise _UntrustedXML() from error
        raise ET.ParseError(str(error)) from error
    except (LookupError, ValueError) as error:
        raise _UntrustedXML() from error
    return root, found_body


class _MetadataBuilder(ET.TreeBuilder):
    def doctype(self, name, pubid, system):
        _deny_dtd()


def _metadata_xml(data):
    if data.startswith(_UTF32_PREFIXES):
        raise _UntrustedXML()
    try:
        return ET.fromstring(data, parser=ET.XMLParser(target=_MetadataBuilder()))
    except ET.ParseError as error:
        if getattr(error, 'code', None) in (
                expat.errors.codes[expat.errors.XML_ERROR_UNKNOWN_ENCODING],
                expat.errors.codes[expat.errors.XML_ERROR_INCORRECT_ENCODING]):
            raise _UntrustedXML() from error
        raise
    except (LookupError, ValueError) as error:
        raise _UntrustedXML() from error


def _root_and_body(stream, *, cancel, budget):
    return _probe_xml(stream, cancel=cancel, budget=budget, body=True)


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
    root, _ = _probe_xml(stream, cancel=cancel, budget=budget)
    return root[0] if root else None


_CONTENT_TYPES = 'http://schemas.openxmlformats.org/package/2006/content-types'
_RELATIONSHIPS = 'http://schemas.openxmlformats.org/package/2006/relationships'
_OFFICE_RELATIONSHIPS = frozenset((
    'http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument',
    'http://purl.oclc.org/ooxml/officeDocument/relationships/officeDocument',
))
_PERCENT = re.compile(r'%([0-9a-fA-F]{2})')
_UNRESERVED = frozenset('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~')
_SIMPLE_PART = re.compile(r'/?[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\Z')

# Exact main-part types, not substring guesses (e.g. worksheet/styles != workbook).
# Unknown future types still go to native import; this is not an export allowlist.
_XML_MAIN_TYPES = {
    **{'application/vnd.openxmlformats-officedocument.' + name + '.main+xml': family
       for family, names in (
           ('wordprocessingml', ('wordprocessingml.document', 'wordprocessingml.template')),
           ('spreadsheetml', ('spreadsheetml.sheet', 'spreadsheetml.template')),
           ('presentationml', ('presentationml.presentation', 'presentationml.template',
                               'presentationml.slideshow')))
       for name in names},
    **{'application/vnd.' + name + '.macroenabled.main+xml': family
       for family, names in (
           ('wordprocessingml', ('ms-word.document',)),
           ('spreadsheetml', ('ms-excel.sheet', 'ms-excel.template', 'ms-excel.addin')),
           ('presentationml', ('ms-powerpoint.presentation', 'ms-powerpoint.template',
                               'ms-powerpoint.slideshow', 'ms-powerpoint.addin')))
       for name in names},
    # Word macro-enabled templates (DOTM) use a distinct real main-part MIME:
    # macroEnabledTemplate, not the macroEnabled document/template spelling.
    'application/vnd.ms-word.template.macroenabledtemplate.main+xml': 'wordprocessingml',
}
_BINARY_CALC_MAIN = 'application/vnd.ms-excel.sheet.binary.macroenabled.main'
_STANDALONE_XML = {
    '{' + MATHML + '}math': ('mathml', FORMULA),
    '{urn:schemas-microsoft-com:office:spreadsheet}Workbook': ('spreadsheetml_2003', CALC),
    '{http://schemas.microsoft.com/office/word/2003/wordml}wordDocument': ('wordml_2003', WRITER),
}


def _opc_part_key(value, *, relationship=False):
    """Resolve root relationships without extracting or guessing ZIP paths.

    OPC part URIs are package-relative, not local filesystem paths. A package
    relationship may contain dot segments; its resolved destination must remain
    inside the package. Part declarations/items themselves cannot have them.
    Escaped separators/unreserved characters are forbidden by OPC. Percent
    escapes are retained (ZIP mapping), normalized in case, and Unicode IRIs
    receive UTF-8 percent encoding. Case aliases are treated as ambiguous.
    """
    # Most ZIP entries use simple ASCII paths. Keep the original strict URI
    # path for escapes, Unicode, reserved characters and dot-segment resolution.
    if isinstance(value, str) and _SIMPLE_PART.fullmatch(value):
        if not relationship and not value.startswith('/'):
            raise ValueError('part URI must be absolute')
        simple = value[1:] if value.startswith('/') else value
        if all(not segment.endswith('.') for segment in simple.split('/')):
            return simple.lower()
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
    content_types = _metadata_xml(ct_data)
    relationships = _metadata_xml(rel_data)
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
    if declared.casefold() == _BINARY_CALC_MAIN:
        # XLSB's workbook.bin is not malformed XML: it is not XML at all.
        # Preserve the declared model constraint, but do not certify BIFF12.
        return {'state': 'not_checked', 'reason': 'source_contract_binary_main',
                'contentClass': 'ooxml_package', 'mainContentType': declared,
                'contentTypeBasis': basis, 'expectedServices': [CALC]}
    family = _XML_MAIN_TYPES.get(declared.casefold())
    if family is None:
        return {'state': 'not_checked', 'reason': 'source_main_type_unmapped',
                'contentClass': 'ooxml_package', 'mainContentType': declared}
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


def _odf_content_encrypted(archive, names, *, cancel, budget):
    """Failure-only fallback: ordinary ODF scans do not read an extra member."""
    name = 'META-INF/manifest.xml'
    if names.count(name) != 1:
        return False
    _check_cancel(cancel)
    if archive.getinfo(name).file_size > budget:
        raise _Budget()
    root = _metadata_xml(archive.read(name))
    ns = '{urn:oasis:names:tc:opendocument:xmlns:manifest:1.0}'
    if root.tag != ns + 'manifest':
        return False
    for index, entry in enumerate(root):
        _check_cancel(cancel, index)
        if (entry.tag == ns + 'file-entry' and entry.get(ns + 'full-path') == 'content.xml'
                and entry.find(ns + 'encryption-data') is not None):
            return True
    return False


def inspect_source_contract(source, *, cancel=None, budget=4 * 1024 * 1024):
    """Return only content-confirmed declarations, independent of filename.

    recognized confirms a declaration, not a complete, safe or lossless file.
    No source verdict here can bypass the native import/model proof.
    """
    _check_cancel(cancel)
    recognized = False
    try:
        if budget <= 0:
            raise _Budget()
        path = Path(source)
        with path.open('rb') as stream:
            prefix = stream.read(min(4096, budget))
            if len(prefix) == budget and budget < 4:
                raise _Budget()
            packed = prefix.startswith(b'PK\x03\x04')
            if not packed:
                # UTF-8/BOM, UTF-16 and XML with a long prolog are examined by
                # their root, not a namespace substring in the first 4 KiB.
                candidate = prefix.removeprefix(b'\xef\xbb\xbf').lstrip()
                if not (candidate.startswith(b'<') or prefix.startswith(
                        (b'\xff\xfe', b'\xfe\xff', b'\x00<', b'\x00\x00'))):
                    return {'state': 'not_applicable'}
                root, body = _probe_xml(stream, cancel=cancel, budget=budget,
                                        body=True, initial=prefix)
                if not root:
                    return {'state': 'not_applicable'}
                if root[0] != '{' + OFFICE + '}document':
                    standalone = _STANDALONE_XML.get(root[0])
                    if standalone:
                        return {'state': 'recognized', 'contentClass': standalone[0],
                                'expectedServices': [standalone[1]], 'rootObserved': True}
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
                    if names.count('mimetype') != 1:
                        return _invalid('duplicate_mimetype', 'epub_package')
                    return {'state': 'recognized', 'contentClass': 'epub_package',
                            'mimetype': mime, 'expectedServices': []}
                if not mime.startswith(('application/vnd.oasis.opendocument.', 'application/vnd.sun.xml.')):
                    # An unrelated extra member must not mask real OPC declarations.
                    if '[Content_Types].xml' in names:
                        recognized = True
                        return _ooxml_contract(archive, names, cancel=cancel, budget=budget)
                    return {'state': 'not_applicable'}
                recognized = True
                if names.count('mimetype') != 1:
                    return _invalid('duplicate_mimetype', 'odf_package')
                if names.count('content.xml') != 1:
                    return _invalid('missing_or_duplicate_content_xml', 'odf_package')
                try:
                    with archive.open('content.xml') as stream:
                        root, body = _root_and_body(stream, cancel=cancel, budget=budget)
                except (ET.ParseError, _UntrustedXML):
                    if _odf_content_encrypted(archive, names, cancel=cancel, budget=budget):
                        return {'state': 'not_checked', 'reason': 'source_contract_encrypted',
                                'contentClass': 'odf_package', 'mimetype': mime}
                    raise
                return _odf_contract(mime, root, body)
            if '[Content_Types].xml' in names:
                recognized = True
                return _ooxml_contract(archive, names, cancel=cancel, budget=budget)
    except _Budget:
        return {'state': 'not_checked', 'reason': 'source_contract_budget'}
    except _UntrustedXML:
        return {'state': 'not_checked', 'reason': 'source_contract_dtd_entities_or_encoding'}
    except (RuntimeError, NotImplementedError):
        # A password-protected ZIP or unavailable decompressor is not evidence
        # of corruption. The owned native importer still has to open the file.
        return {'state': 'not_checked', 'reason': 'source_contract_archive_unavailable'}
    except (ET.ParseError, UnicodeError, zipfile.BadZipFile, KeyError, ValueError,
            EOFError, zlib.error):
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
