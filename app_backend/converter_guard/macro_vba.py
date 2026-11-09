"""Bounded static VBA storage checks. Never load or execute macro code.

Byte-equal CFB streams prove storage preservation, not macro behavior in Office.
olefile reads directory/FAT metadata and streams; a wrapper bounds all reads,
seeks and cancellation polls, including work inside the third-party parser.
"""
import hashlib
import io
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit
import zipfile
import xml.etree.ElementTree as ET

from .errors import GuardError

OLE = bytes.fromhex('d0cf11e0a1b11ae1')
MAX_READ_BYTES = 64 * 1024 * 1024
MAX_STREAM_BYTES = 32 * 1024 * 1024
MAX_OPERATIONS = 200_000
MAX_ENTRIES = 100_000
MAX_OPC_METADATA = 4 * 1024 * 1024
VBA_TYPE = 'application/vnd.ms-office.vbaProject'
VBA_RELATIONSHIPS = frozenset((
    'http://schemas.microsoft.com/office/2006/relationships/vbaProject',
    'http://schemas.openxmlformats.org/officeDocument/2006/relationships/vbaProject',
    'http://purl.oclc.org/ooxml/officeDocument/relationships/vbaProject',
))
MACRO_TARGETS = frozenset(('docm', 'dotm', 'xlsm', 'xltm', 'xlsb', 'pptm', 'potm', 'ppsm',
                           'doc', 'dot', 'xls', 'xlt', 'ppt', 'pot', 'pps'))


class _Budget(Exception):
    pass


def _cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise GuardError('CANCELLED', 'Проверка макросов отменена.', stage='result')


class _Bounded:
    def __init__(self, stream, cancel):
        self.stream, self.cancel = stream, cancel
        self.remaining, self.operations = MAX_READ_BYTES, MAX_OPERATIONS

    def _poll(self):
        _cancel(self.cancel)
        self.operations -= 1
        if self.operations < 0:
            raise _Budget()

    def read(self, count=-1):
        self._poll()
        if count < 0:
            data = self.stream.read(self.remaining + 1)
            if len(data) > self.remaining:
                raise _Budget()
            self.remaining -= len(data)
            return data
        if count > self.remaining:
            raise _Budget()
        data = self.stream.read(count)
        self.remaining -= len(data)
        return data

    def seek(self, *args):
        self._poll()
        return self.stream.seek(*args)

    def tell(self):
        return self.stream.tell()

    def seekable(self):
        return True

    @property
    def closed(self):
        return self.stream.closed


def _cfb_projects(stream, cancel):
    import olefile
    # The parser can materialize CFB streams, so check their declared lengths
    # before openstream. All parser I/O additionally goes through _Bounded.
    with olefile.OleFileIO(_Bounded(stream, cancel), raise_defects=olefile.DEFECT_INCORRECT) as ole:
        if len(ole.direntries) > MAX_ENTRIES:
            raise _Budget()
        entries = ole.listdir()
        if len(entries) > MAX_ENTRIES:
            raise _Budget()
        roots = [path[:-2] for path in entries
                 if len(path) >= 2 and [s.lower() for s in path[-2:]] == ['vba', 'dir']]
        projects, work = [], MAX_OPERATIONS
        for root in roots:
            hashes = {}
            for path in entries:
                _cancel(cancel)
                work -= 1
                if work < 0:
                    raise _Budget()
                relative = path[len(root):]
                if path[:len(root)] != root or not relative:
                    continue
                if relative[0].lower() != 'vba' and relative not in (['PROJECT'], ['PROJECTwm']):
                    continue
                if ole.get_size(path) > MAX_STREAM_BYTES:
                    raise _Budget()
                key = '/'.join(relative).lower()
                if key in hashes:
                    raise ValueError('ambiguous_vba_stream')
                with ole.openstream(path) as value:
                    digest = hashlib.sha256()
                    while block := value.read(64 * 1024):
                        _cancel(cancel)
                        digest.update(block)
                hashes[key] = digest.hexdigest()
            # dir alone is not evidence of an actual VBA project.
            if 'vba/_vba_project' in hashes and len(hashes) > 2:
                projects.append(hashes)
        return projects


def _opc_name(value, base=()):
    """Resolve a package URI, never a filesystem path or network resource."""
    if (not isinstance(value,str) or not value or '\\' in value or
            any(ord(c)<32 for c in value) or re.search(r'%(?![0-9a-fA-F]{2})',value)):
        raise ValueError('vba_part_uri_invalid')
    uri=urlsplit(value)
    if uri.scheme or uri.netloc or uri.query or uri.fragment:
        raise ValueError('vba_part_uri_external_or_fragment')
    path=unquote(uri.path,errors='strict')
    if '\\' in path or any(ord(c)<32 for c in path):
        raise ValueError('vba_part_uri_invalid')
    parts=[] if path.startswith('/') else list(base)
    for part in path.split('/'):
        if part in ('','.'):continue
        if part=='..':
            if not parts:raise ValueError('vba_part_uri_outside_package')
            parts.pop()
        else:parts.append(part)
    if not parts:raise ValueError('vba_part_uri_empty')
    return '/'.join(parts).lower()


def _opc_vba_entries(archive, entries, cancel):
    """Use declared OPC types/relationships, not a guessed VBA filename.

    OPC Part URIs are resolved inside the existing ZIP only. Relationship
    targets are never fetched, opened on disk, or followed outside the package.
    """
    # ODF/EPUB and other ZIP containers do not use OPC Part URI rules. Do not
    # reinterpret every unrelated filename (for example a literal '%') there.
    has_types=any(entry.filename.lower()=='[content_types].xml' for entry in entries)
    if not has_types:
        by_name={}
        for entry in entries:
            _cancel(cancel)
            if entry.is_dir() or entry.filename.rsplit('/',1)[-1].lower()!='vbaproject.bin':continue
            key=_opc_name(entry.filename)
            if key in by_name:raise ValueError('ambiguous_vba_part')
            by_name[key]=entry
        return list(by_name.values())
    by_name={}
    for entry in entries:
        _cancel(cancel)
        if entry.is_dir():continue
        key=_opc_name(entry.filename)
        if key in by_name:raise ValueError('ambiguous_opc_part')
        by_name[key]=entry
    found={name for name in by_name if name.rsplit('/',1)[-1]=='vbaproject.bin'}
    remaining=MAX_OPC_METADATA
    def xml(entry):
        nonlocal remaining
        if entry.file_size>remaining or entry.file_size>1024*1024:raise _Budget()
        remaining-=entry.file_size
        data=archive.read(entry)
        _cancel(cancel)
        if data.startswith((b'\xff\xfe',b'\xfe\xff')):
            declaration_text=data.decode('utf-16')
        elif b'\x00' in data:
            raise ValueError('vba_opc_xml_encoding_unverified')
        else:
            declaration_text=data.decode('utf-8-sig')
        if '<!DOCTYPE' in declaration_text.upper() or '<!ENTITY' in declaration_text.upper():
            raise ValueError('vba_opc_xml_declaration')
        return ET.fromstring(data)
    types=by_name.get('[content_types].xml')
    if types is not None:
        namespace='{http://schemas.openxmlformats.org/package/2006/content-types}'
        root=xml(types)
        if root.tag!=namespace+'Types':raise ValueError('vba_content_types_root')
        overrides,defaults={},{}
        for node in root:
            _cancel(cancel)
            if node.tag==namespace+'Override':
                name=_opc_name(node.get('PartName'))
                if name in overrides:raise ValueError('vba_content_type_ambiguous')
                overrides[name]=node.get('ContentType')
            elif node.tag==namespace+'Default':
                extension=(node.get('Extension') or '').lower()
                if not extension or extension in defaults:raise ValueError('vba_content_type_ambiguous')
                defaults[extension]=node.get('ContentType')
        found.update(name for name,kind in overrides.items() if (kind or '').lower()==VBA_TYPE.lower())
        found.update(name for name in by_name if name not in overrides and
                     (defaults.get(name.rsplit('.',1)[-1]) or '').lower()==VBA_TYPE.lower())
        rel_namespace='{http://schemas.openxmlformats.org/package/2006/relationships}'
        for name,entry in by_name.items():
            if not name.endswith('.rels'):continue
            root=xml(entry)
            if root.tag!=rel_namespace+'Relationships':raise ValueError('vba_relationships_root')
            for node in root:
                if node.tag!=rel_namespace+'Relationship' or node.get('Type') not in VBA_RELATIONSHIPS:continue
                _cancel(cancel)
                if (node.get('TargetMode') or 'Internal')!='Internal':
                    raise ValueError('vba_relationship_external')
                parts=name.split('/')
                if name=='_rels/.rels':base=()
                elif len(parts)>=2 and parts[-2]=='_rels':base=parts[:-2]
                else:raise ValueError('vba_relationship_part_invalid')
                found.add(_opc_name(node.get('Target'),base))
    if any(name not in by_name for name in found):raise ValueError('vba_declared_part_missing')
    return [by_name[name] for name in sorted(found)]


def inspect_vba(source, *, cancel=None):
    """Return bounded structural storage evidence, never inferred from suffix."""
    _cancel(cancel)
    try:
        with Path(source).open('rb') as stream:
            magic = stream.read(8)
            stream.seek(0)
            if magic == OLE:
                projects = _cfb_projects(stream, cancel)
                return {'state': 'present' if projects else 'absent', 'container': 'ole',
                        'projects': projects, 'projectBytes': []}
            if not magic.startswith(b'PK'):
                return {'state': 'absent', 'projects': [], 'projectBytes': []}
            with zipfile.ZipFile(_Bounded(stream, cancel)) as archive:
                entries = archive.infolist()
                if len(entries) > MAX_ENTRIES:
                    raise _Budget()
                projects, project_bytes = [], []
                for entry in _opc_vba_entries(archive,entries,cancel):
                    _cancel(cancel)
                    if entry.file_size > MAX_STREAM_BYTES:
                        raise _Budget()
                    value = archive.read(entry)
                    if not value.startswith(OLE):
                        raise ValueError('vba_part_is_not_cfb')
                    found = _cfb_projects(io.BytesIO(value), cancel)
                    if not found:
                        raise ValueError('vba_part_has_no_project')
                    projects.extend(found)
                    project_bytes.append(hashlib.sha256(value).hexdigest())
                return {'state': 'present' if projects else 'absent', 'container': 'opc',
                        'projects': projects, 'projectBytes': project_bytes}
    except _Budget:
        return {'state': 'not_checked', 'reason': 'vba_inspection_budget'}
    except (ImportError, OSError, ValueError, EOFError, RecursionError, zipfile.BadZipFile, ET.ParseError) as error:
        return {'state': 'not_checked', 'reason': 'vba_inspection_' + type(error).__name__}


def check_vba_preservation(paths, target, source, *, cancel=None):
    before = inspect_vba(source, cancel=cancel)
    if before['state'] == 'absent':
        return None
    result = {'check': 'vba_storage', 'qualityCompared': False, 'macrosExecuted': False,
              'behaviorCompared': False, 'scope': 'vba_project_stream_bytes'}
    if before['state'] == 'not_checked':
        return {**result, 'status': 'not_checked', 'reason': before['reason'],
                'warnings': ['VBA_PRESERVATION_NOT_CHECKED']}
    result['sourceProjects'] = len(before['projects'])
    if target not in MACRO_TARGETS:
        return {**result, 'status': 'expected_loss', 'expectedLosses': ['vba_macros'],
                'warnings': ['VBA_MACROS_EXPECTED_LOSS']}
    primaries = [Path(path) for path in paths if Path(path).suffix.lower() == '.' + target]
    if len(primaries) != 1:
        raise GuardError('VBA_PRESERVATION_UNVERIFIED',
            'Не подтверждена сохранность VBA-макросов: основной результат не определён. '
            'Результат не отмечен готовым; исходный файл сохранён.',stage='result',
            diagnostic='vba_primary_not_unique')
    after = inspect_vba(primaries[0], cancel=cancel)
    if after['state'] == 'absent':
        raise GuardError('ENGINE_OUTPUT_MACROS_LOST',
            'LibreOffice не сохранил VBA-макросы в формате, поддерживающем макросы. '
            'Результат не отмечен готовым; исходный файл сохранён.', stage='result')
    if after['state'] == 'not_checked':
        raise GuardError('VBA_PRESERVATION_UNVERIFIED',
            'Не подтверждена сохранность VBA-макросов после преобразования. '
            'Результат не отмечен готовым; исходный файл сохранён.',stage='result',
            diagnostic=after['reason'])
    # Container sector allocation can change while every VBA stream remains
    # byte-identical. Compare complete project stream maps independently.
    canonical = lambda projects: sorted(tuple(sorted(project.items())) for project in projects)
    if canonical(before['projects']) == canonical(after['projects']):
        return {**result, 'status': 'streams_preserved', 'outputProjects': len(after['projects']),
                'projectContainersByteEqual': sorted(before['projectBytes']) == sorted(after['projectBytes'])
                                             if before['projectBytes'] and after['projectBytes'] else None,
                'warnings': ['VBA_STREAMS_PRESERVED']}
    raise GuardError('VBA_PRESERVATION_UNVERIFIED',
        'LibreOffice изменил потоки проекта VBA; сохранность кода и атрибутов макросов '
        'не подтверждена. Результат не отмечен готовым; исходный файл сохранён.',
        stage='result',diagnostic='vba_stream_bytes_changed')
