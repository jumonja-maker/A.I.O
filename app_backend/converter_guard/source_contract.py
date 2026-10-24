"""Check native container declarations before trusting an imported model.

This is a bounded structural check, not a document importer or schema validator.
It neither rewrites a source nor admits a route. Unknown content and exhausted
inspection budgets still require LibreOffice's own import proof.
"""
from pathlib import Path
import posixpath
import xml.etree.ElementTree as ET
import zipfile

from .errors import GuardError

OFFICE = 'urn:oasis:names:tc:opendocument:xmlns:office:1.0'
LEGACY_OFFICE = 'http://openoffice.org/2000/office'
WORD_2003 = 'http://schemas.microsoft.com/office/word/2003/wordml'
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
                if len(stack) == 3 and stack[1].endswith('}body'):
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
        if expected is None:
            if flat and kind in ('text-flat-xml', 'spreadsheet-flat-xml',
                                 'presentation-flat-xml', 'graphics-flat-xml'):
                return _invalid('flat_mimetype_requires_document_family', 'flat_odf')
            return {'state': 'not_checked', 'reason': 'source_mimetype_unmapped', 'mimetype': mime}
        if tag != '{' + OFFICE + '}' + ('document' if flat else 'document-content'):
            return _invalid('mimetype_namespace_mismatch', 'odf')
        if body is not None and body != '{' + OFFICE + '}' + expected[0]:
            return _invalid('mimetype_body_mismatch', 'odf')
        return {'state': 'recognized', 'contentClass': 'flat_odf' if flat else 'odf_package',
                'mimetype': mime, 'expectedServices': [expected[1]],
                'bodyObserved': body is not None}
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


def _ooxml_contract(archive, names, *, cancel, budget):
    ct = '[Content_Types].xml'
    rel = '_rels/.rels'
    if names.count(ct) != 1 or names.count(rel) != 1:
        return _invalid('missing_or_duplicate_ooxml_declarations', 'ooxml_package')
    if max(archive.getinfo(ct).file_size, archive.getinfo(rel).file_size) > budget:
        raise _Budget()
    ct_data, rel_data = archive.read(ct), archive.read(rel)
    _safe_xml_data(ct_data)
    _safe_xml_data(rel_data)
    content_types = ET.fromstring(ct_data)
    relationships = ET.fromstring(rel_data)
    if (content_types.tag != '{http://schemas.openxmlformats.org/package/2006/content-types}Types'
            or relationships.tag != '{http://schemas.openxmlformats.org/package/2006/relationships}Relationships'):
        return _invalid('ooxml_declaration_namespace', 'ooxml_package')
    main = [item for item in relationships if item.get('Type', '').endswith('/officeDocument')]
    if len(main) != 1 or main[0].get('TargetMode', 'Internal') != 'Internal':
        return _invalid('ooxml_main_relationship', 'ooxml_package')
    part = main[0].get('Target', '').lstrip('/')
    if '%' in part:
        raise _Budget()  # URI escaping needs OPC resolution, not a guessed ZIP path.
    if (not part or '\\' in part or posixpath.normpath(part) != part
            or part.startswith('../') or names.count(part) != 1):
        return _invalid('ooxml_main_part_missing_or_ambiguous', 'ooxml_package')
    content_ns = '{http://schemas.openxmlformats.org/package/2006/content-types}'
    declarations = [item.get('ContentType', '') for item in content_types
                    if item.tag == content_ns + 'Override' and item.get('PartName') == '/' + part]
    if not declarations:
        # OPC resolves an exact part Override first, then the Default for its
        # extension. Real Calc packages may use only the latter for workbook.xml.
        # Extension matching is case insensitive; duplicate matches are ambiguous.
        extension = posixpath.splitext(part)[1][1:].lower()
        declarations = [item.get('ContentType', '') for item in content_types
                        if item.tag == content_ns + 'Default' and extension
                        and item.get('Extension', '').lower() == extension]
    if len(declarations) != 1:
        return _invalid('ooxml_main_content_type', 'ooxml_package')
    declared = declarations[0]
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
            'mainContentType': declared, 'expectedServices': [service]}


def _missing_ooxml_content_types(archive, names, *, cancel, budget):
    """Identify an incomplete OPC office package without relying on its suffix.

    A generic ZIP is not an office document merely because it contains XML.
    Require both the package's officeDocument relationship and a content-confirmed
    Word, Calc or Impress main root before diagnosing the missing declaration.
    """
    relationship_part = '_rels/.rels'
    if names.count(relationship_part) != 1:
        return None
    if archive.getinfo(relationship_part).file_size > budget:
        raise _Budget()
    data = archive.read(relationship_part)
    _safe_xml_data(data)
    relationships = ET.fromstring(data)
    package_ns = '{http://schemas.openxmlformats.org/package/2006/relationships}'
    if relationships.tag != package_ns + 'Relationships':
        return None
    office_types = {
        'http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument',
        'http://purl.oclc.org/ooxml/officeDocument/relationships/officeDocument',
    }
    main = [item for item in relationships
            if item.tag == package_ns + 'Relationship' and item.get('Type') in office_types]
    if len(main) != 1 or main[0].get('TargetMode', 'Internal') != 'Internal':
        return None
    part = main[0].get('Target', '').lstrip('/')
    if (not part or '%' in part or '\\' in part or posixpath.normpath(part) != part
            or part.startswith('../') or names.count(part) != 1):
        return None
    with archive.open(part) as stream:
        root = _xml_root(stream, cancel=cancel, budget=budget)
    known = {
        '{' + namespace + '}' + local
        for family, local in (('wordprocessingml', 'document'), ('spreadsheetml', 'workbook'),
                              ('presentationml', 'presentation'))
        for namespace in ('http://schemas.openxmlformats.org/' + family + '/2006/main',
                          'http://purl.oclc.org/ooxml/' + family + '/main')
    }
    if root in known:
        return _invalid('missing_ooxml_content_types', 'ooxml_package')
    return None


def inspect_source_contract(source, *, cancel=None, budget=4 * 1024 * 1024):
    """Return only content-confirmed declarations, independent of filename."""
    recognized = False
    try:
        path = Path(source)
        with path.open('rb') as stream:
            prefix = stream.read(4096)
            packed = prefix.startswith(b'PK\x03\x04')
            if not packed:
                if OFFICE.encode() not in prefix and WORD_2003.encode() not in prefix:
                    return {'state': 'not_applicable'}
                stream.seek(0)
                root, body = _root_and_body(stream, cancel=cancel, budget=budget)
                if root and root[0] == '{' + WORD_2003 + '}wordDocument':
                    return {'state': 'recognized', 'contentClass': 'word2003_xml',
                            'expectedServices': [WRITER]}
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
            incomplete = _missing_ooxml_content_types(archive, names, cancel=cancel, budget=budget)
            if incomplete:
                return incomplete
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
