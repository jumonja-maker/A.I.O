"""Bounded static VBA storage checks. Never load or execute macro code.

Byte-equal CFB streams prove storage preservation, not macro behavior in Office.
olefile reads directory/FAT metadata and streams; a wrapper bounds all reads,
seeks and cancellation polls, including work inside the third-party parser.
"""
import hashlib
import io
from pathlib import Path
import zipfile

from .errors import GuardError

OLE = bytes.fromhex('d0cf11e0a1b11ae1')
MAX_READ_BYTES = 64 * 1024 * 1024
MAX_STREAM_BYTES = 32 * 1024 * 1024
MAX_OPERATIONS = 200_000
MAX_ENTRIES = 100_000
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
        projects = []
        for root in roots:
            hashes = {}
            for path in entries:
                _cancel(cancel)
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
                projects, project_bytes, seen = [], [], set()
                for entry in entries:
                    _cancel(cancel)
                    if entry.filename.rsplit('/', 1)[-1].lower() != 'vbaproject.bin':
                        continue
                    name = entry.filename.lower()
                    if name in seen:
                        raise ValueError('ambiguous_vba_part')
                    seen.add(name)
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
    except (ImportError, OSError, ValueError, EOFError, RecursionError, zipfile.BadZipFile) as error:
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
        return {**result, 'status': 'not_checked', 'reason': 'vba_primary_not_unique',
                'warnings': ['VBA_PRESERVATION_NOT_CHECKED']}
    after = inspect_vba(primaries[0], cancel=cancel)
    if after['state'] == 'absent':
        raise GuardError('ENGINE_OUTPUT_MACROS_LOST',
            'LibreOffice не сохранил VBA-макросы в формате, поддерживающем макросы. '
            'Результат не отмечен готовым; исходный файл сохранён.', stage='result')
    if after['state'] == 'not_checked':
        return {**result, 'status': 'not_checked', 'reason': after['reason'],
                'warnings': ['VBA_PRESERVATION_NOT_CHECKED']}
    # Container sector allocation can change while every VBA stream remains
    # byte-identical. Compare complete project stream maps independently.
    canonical = lambda projects: sorted(tuple(sorted(project.items())) for project in projects)
    if canonical(before['projects']) == canonical(after['projects']):
        return {**result, 'status': 'streams_preserved', 'outputProjects': len(after['projects']),
                'projectContainersByteEqual': sorted(before['projectBytes']) == sorted(after['projectBytes'])
                                             if before['projectBytes'] and after['projectBytes'] else None,
                'warnings': ['VBA_STREAMS_PRESERVED']}
    return {**result, 'status': 'not_checked', 'reason': 'vba_stream_bytes_changed',
            'outputProjects': len(after['projects']), 'warnings': ['VBA_PRESERVATION_NOT_CHECKED']}
