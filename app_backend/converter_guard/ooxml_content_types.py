"""Private, namespace-only OPC metadata compatibility after native import fails.

Some native importers reject a valid prefixed content-types root. Only this
finite metadata grammar is reserialized; every other ZIP member stays byte
equal. Signed, encrypted, ambiguous, unbounded and unknown packages are left
untouched. This helper never authorizes a route or executes document content.
"""
from pathlib import Path, PurePosixPath
import copy
import hashlib
import re
import stat
import unicodedata
from xml.parsers import expat
import xml.etree.ElementTree as ET
from xml.sax.saxutils import quoteattr
import zipfile

from .errors import GuardError

CT = 'http://schemas.openxmlformats.org/package/2006/content-types'
RELS = 'http://schemas.openxmlformats.org/package/2006/relationships'
KIND = 'ooxml_content_types_namespace'
NATIVE_FAILURES = frozenset(('PREFLIGHT_IMPORT_UNDETERMINED', 'PREFLIGHT_IMPORT_FAILED', 'SOURCE_IMPORT_FAILED'))
MAX_PACKAGE = 128 * 1024 * 1024
MAX_EXPANDED = 256 * 1024 * 1024
MAX_METADATA = 1024 * 1024
MAX_METADATA_TOTAL = 4 * 1024 * 1024
MAX_MEMBERS = 8192


class _Decline(Exception):
    pass


def _check(cancel):
    if cancel.is_set():
        raise GuardError('CANCELLED', 'Подготовка совместимости OOXML отменена.', stage='import')


def _stamp(path):
    value = path.lstat()
    if not stat.S_ISREG(value.st_mode) or getattr(value, 'st_file_attributes', 0) & 0x400:
        raise _Decline('not_regular_source')
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _hash(path, cancel):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            _check(cancel)
            result.update(chunk)
    return result.hexdigest()


def _xml(raw, namespace, cancel):
    if len(raw) > MAX_METADATA or b'\0' in raw:
        raise _Decline('xml_budget_or_encoding')
    parser = expat.ParserCreate(namespace_separator='}')
    depth, count, namespaces = 0, 0, 0
    def decline(*args):
        raise _Decline('xml_unknown_declaration')
    def start(name, attrs):
        nonlocal depth, count
        _check(cancel)
        depth += 1
        count += 1
        if depth > 3 or count > MAX_MEMBERS + 1 or not name.startswith(namespace+'}'):
            raise _Decline('xml_unknown_structure')
    def end(name):
        nonlocal depth
        depth -= 1
    def declared(prefix, uri):
        nonlocal namespaces
        namespaces += 1
        if uri != namespace or namespaces > 1:
            raise _Decline('xml_unknown_namespace')
    parser.StartElementHandler, parser.EndElementHandler = start, end
    parser.StartNamespaceDeclHandler = declared
    parser.StartDoctypeDeclHandler = parser.EntityDeclHandler = decline
    parser.ExternalEntityRefHandler = parser.ProcessingInstructionHandler = decline
    parser.CommentHandler = decline
    try:
        for offset in range(0, len(raw), 16384):
            _check(cancel)
            parser.Parse(raw[offset:offset+16384], False)
        parser.Parse(b'', True)
        return ET.fromstring(raw)
    except (expat.ExpatError, ET.ParseError) as error:
        raise _Decline('xml_parse') from error


def _facts(node):
    return (node.tag, sorted(node.attrib.items()), node.text, node.tail, [_facts(c) for c in node])


def _replacement(raw, members, cancel):
    root = _xml(raw, CT, cancel)
    if root.tag != '{'+CT+'}Types' or root.attrib or root.text and root.text.strip():
        raise _Decline('content_types_root')
    # Only a genuinely prefixed root needs this compatibility operation.
    if not re.search(rb'<[A-Za-z_][A-Za-z0-9_.-]*:Types(?:\s|>)', raw):
        raise _Decline('already_default_namespace')
    defaults, overrides = {}, {}
    for node in root:
        _check(cancel)
        local = node.tag.removeprefix('{'+CT+'}')
        expected = {'Extension','ContentType'} if local == 'Default' else {'PartName','ContentType'}
        if (local not in ('Default','Override') or set(node.attrib) != expected or len(node)
                or node.text and node.text.strip() or node.tail and node.tail.strip()):
            raise _Decline('content_types_unknown_metadata')
        mime = node.get('ContentType')
        if not re.fullmatch(r'[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+', mime or ''):
            raise _Decline('content_type_unconfirmed')
        if 'signature' in mime.lower():
            raise _Decline('signed_package')
        if local == 'Default':
            extension = node.get('Extension')
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', extension or '') or extension.casefold() in defaults:
                raise _Decline('ambiguous_default')
            defaults[extension.casefold()] = mime
        else:
            part = node.get('PartName')
            if not part or not part.startswith('/') or part[1:] not in members or part.casefold() in overrides:
                raise _Decline('ambiguous_override')
            overrides[part.casefold()] = mime
    for name in members:
        if name == '[Content_Types].xml' or name.endswith('/'):
            continue
        if '/'+name.casefold() not in overrides and name.rsplit('.',1)[-1].casefold() not in defaults:
            raise _Decline('undeclared_package_part')
    chunks = ['<?xml version="1.0" encoding="utf-8"?>\n<Types xmlns='+quoteattr(CT)+'>', root.text or '']
    for node in root:
        local = node.tag.split('}',1)[1]
        attrs = ''.join(' '+key+'='+quoteattr(value) for key,value in node.attrib.items())
        chunks.append('<'+local+attrs+'>'+node.text+'</'+local+'>' if node.text else '<'+local+attrs+'/>')
        chunks.append(node.tail or '')
    chunks.append('</Types>')
    replacement = ''.join(chunks).encode('utf-8')
    if _facts(root) != _facts(ET.fromstring(replacement)):
        raise _Decline('expanded_semantics_changed')
    return replacement


def prepare_content_types_source(source, workspace, *, cancel, native_failure_code=None, expected=None,
                                 copy_budget=None):
    """Return a new owned copy only after an eligible native failure/proof.

    At conversion time expected binds the operation to the original preflight
    bytes. The adapter must still require a fresh loaded-model/registry proof.
    No temporary output is created for declined or already canonical input.
    """
    source, workspace = Path(source), Path(workspace)
    archive = None
    prepared = None
    succeeded = False
    try:
        _check(cancel)
        if expected is None and native_failure_code not in NATIVE_FAILURES:
            return source, None
        if expected is not None and (expected.get('kind') != KIND or expected.get('state') != 'prepared'
                                     or expected.get('originalNativeCode') not in NATIVE_FAILURES):
            raise GuardError('PREFLIGHT_ROUTE_UNDETERMINED', 'Не подтверждена подготовка OOXML. Повторите проверку.', stage='preflight')
        before = _stamp(source)
        if before[2] > MAX_PACKAGE:
            raise _Decline('package_budget')
        if not zipfile.is_zipfile(source):
            raise _Decline('not_zip')
        archive = zipfile.ZipFile(source)
        entries = archive.infolist()
        if len(entries) > MAX_MEMBERS or archive.comment:
            raise _Decline('package_metadata_budget')
        members, folded, total = set(), set(), 0
        for entry in entries:
            _check(cancel)
            name, path = entry.filename, PurePosixPath(entry.filename)
            alias = unicodedata.normalize('NFC',name).casefold()
            if (name in members or alias in folded or not name or path.is_absolute()
                    or '..' in path.parts or '\\' in name or ':' in name or '%' in name
                    or entry.orig_filename != name or any(ord(c)<32 for c in name)
                    or any(part.endswith((' ','.')) for part in path.parts)
                    or name != path.as_posix()+('/' if entry.is_dir() else '')
                    or entry.flag_bits & 1 or entry.extra or entry.comment
                    or entry.compress_type not in (zipfile.ZIP_STORED,zipfile.ZIP_DEFLATED)
                    or stat.S_ISLNK(entry.external_attr >> 16)):
                raise _Decline('ambiguous_or_unknown_zip_metadata')
            if name.casefold().startswith('_xmlsignatures/') or 'signature' in path.name.casefold():
                raise _Decline('signed_package')
            members.add(name); folded.add(alias); total += entry.file_size
            if total > MAX_EXPANDED:
                raise _Decline('expanded_budget')
        if '[Content_Types].xml' not in members or archive.getinfo('[Content_Types].xml').file_size > MAX_METADATA:
            raise _Decline('content_types_missing_or_budget')
        raw = archive.read('[Content_Types].xml')
        replacement = _replacement(raw, members, cancel)
        metadata_total = len(raw)
        # Do not move relative external relationships to a different base URL,
        # and do not accidentally modify a package carrying any signature.
        for entry in entries:
            if not entry.filename.endswith('.rels'):
                continue
            metadata_total += entry.file_size
            if entry.file_size > MAX_METADATA or metadata_total > MAX_METADATA_TOTAL:
                raise _Decline('relationship_budget')
            rels = _xml(archive.read(entry), RELS, cancel)
            if rels.tag != '{'+RELS+'}Relationships' or rels.attrib:
                raise _Decline('relationship_metadata')
            for rel in rels:
                if (rel.tag != '{'+RELS+'}Relationship' or len(rel)
                        or set(rel.attrib) != {'Id','Type','Target'}
                        or 'signature' in rel.get('Type','').lower()):
                    raise _Decline('external_or_unknown_relationship')
        source_sha = _hash(source, cancel)
        ct_sha = hashlib.sha256(raw).hexdigest()
        if expected is not None and (source_sha != expected.get('originalSha256')
                                     or ct_sha != expected.get('contentTypesOriginalSha256')):
            raise GuardError('SOURCE_CHANGED_DURING_PREPARATION', 'Исходник OOXML изменился. Повторите проверку.', stage='import')
        def check_copy_budget(target=None):
            _check(cancel)
            if copy_budget is not None:
                if target is not None:
                    target.fp.flush()
                used = prepared.stat().st_size if prepared is not None and prepared.exists() else 0
                if used > copy_budget() or not used and copy_budget() <= 0:
                    raise GuardError('SPOOL_QUOTA', 'Превышена квота временной копии OOXML.', stage='import', status=507)
        check_copy_budget()
        workspace.mkdir(parents=True, exist_ok=False)
        prepared = workspace/source.name
        with zipfile.ZipFile(prepared, 'x') as target:
            for entry in entries:
                _check(cancel)
                with target.open(copy.copy(entry), 'w') as output:
                    if entry.filename == '[Content_Types].xml':
                        output.write(replacement)
                        check_copy_budget(target)
                    else:
                        with archive.open(entry) as stream:
                            while chunk := stream.read(1024*1024):
                                _check(cancel)
                                output.write(chunk)
                                check_copy_budget(target)
        check_copy_budget()
        # CRCs were checked while copying every member. Reopen and compare
        # hashes so the summary describes actual written bytes, not intent.
        with zipfile.ZipFile(prepared) as written:
            if written.namelist() != [entry.filename for entry in entries]:
                raise GuardError('SOURCE_PREPARATION_FAILED', 'Не удалось проверить временную копию OOXML.', stage='import')
            for entry in entries:
                _check(cancel)
                if entry.filename == '[Content_Types].xml':
                    if written.read(entry.filename) != replacement:
                        raise GuardError('SOURCE_PREPARATION_FAILED', 'Изменена подготовленная часть OOXML.', stage='import')
                    continue
                with archive.open(entry) as left, written.open(entry.filename) as right:
                    while True:
                        _check(cancel)
                        a,b = left.read(1024*1024),right.read(1024*1024)
                        if a != b:
                            raise GuardError('SOURCE_PREPARATION_FAILED', 'Не совпадают части временной копии OOXML.', stage='import')
                        if not a: break
        if _stamp(source) != before or _hash(source, cancel) != source_sha:
            raise GuardError('SOURCE_CHANGED_DURING_PREPARATION', 'Исходник OOXML изменился во время подготовки.', stage='import')
        prepared_sha = _hash(prepared,cancel)
        if expected is not None and prepared_sha != expected.get('preparedSha256'):
            raise GuardError('PREFLIGHT_ROUTE_UNDETERMINED', 'Временная копия OOXML не совпала с проверенной.', stage='preflight')
        summary = {'kind':KIND,'state':'prepared','originalNativeCode':native_failure_code or expected['originalNativeCode'],
            'originalSha256':source_sha,'preparedSha256':prepared_sha,
            'contentTypesOriginalSha256':ct_sha,'contentTypesPreparedSha256':hashlib.sha256(replacement).hexdigest(),
            'changedParts':['[Content_Types].xml'],'expandedXmlSemanticsEqual':True,
            'otherPartBytesPreserved':True,'packageSignaturesAbsent':True,
            'warnings':['OOXML_PRIVATE_CONTENT_TYPES_NAMESPACE'],'qualityCompared':False}
        succeeded = True
        return prepared, summary
    except _Decline:
        if expected is not None:
            raise GuardError('PREFLIGHT_ROUTE_UNDETERMINED', 'Не удалось повторить проверенную подготовку OOXML. Повторите проверку.', stage='preflight')
        return source, None
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError, UnicodeError, ValueError):
        if expected is not None:
            raise GuardError('PREFLIGHT_ROUTE_UNDETERMINED', 'Не удалось проверить временную копию OOXML.', stage='preflight')
        return source, None
    finally:
        if archive is not None:
            archive.close()
        if prepared is not None and not succeeded:
            try:
                prepared.unlink()
            except FileNotFoundError:
                pass
