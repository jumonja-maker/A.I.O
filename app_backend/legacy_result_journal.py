"""Durable published legacy tickets, separate from Guard jobs and protocols.

Only an explicit release retires a published workspace. An incomplete/corrupt
journal is retained but never promoted to a successful conversion.
"""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
from zipfile import ZipFile, ZIP_STORED

from .converter_guard.result_store import ResultStore, atomic_json, path_is_link
from .libreoffice_catalog import mime_for

ROOT_NAME = re.compile(r'aio-legacy-[a-f0-9]{32}')
MAX_JOURNAL_BYTES = 16 * 1024 * 1024


def _check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise InterruptedError('Legacy recovery stopped')


def file_hash(path, *, cancel=None):
    _check_cancel(cancel)
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(1024 * 1024):
            _check_cancel(cancel)
            digest.update(block)
    _check_cancel(cancel)
    return digest.hexdigest()


class LegacyWorkspace:
    def __init__(self, root):
        self.name = str(root)
        owner = Path(root) / 'owner.lock'
        if os.path.lexists(owner) and path_is_link(owner):
            raise ValueError('Linked legacy owner file')
        self.store = ResultStore(root)  # Reuse only the tested OS ownership/retirement primitive.
        self.preserve_outputs = False
        if not self.store.acquire():
            raise BlockingIOError('Legacy workspace is owned by another instance')

    @classmethod
    def create(cls, parent):
        parent = Path(parent)
        parent.mkdir(parents=True, exist_ok=True)
        if path_is_link(parent):
            raise OSError('Legacy recovery parent is linked')
        root = parent / ('aio-legacy-' + secrets.token_hex(16))
        root.mkdir()
        return cls(root)

    def close(self):
        self.store.close()

    def cleanup(self):
        # The sibling admission barrier outlives a partial rmtree and is
        # already covered by the common Windows/POSIX ownership tests.
        self.store.retire()
        self.store.cleanup_retired()

    def record(self, result):
        root = Path(self.name)
        path = result.path
        relative = path.relative_to(root).as_posix()
        checked = self._path(relative)
        # Flush our native output before publishing its journal. No source
        # document is opened for writing and no file bytes are modified.
        with checked.open('r+b') as stream:
            stream.flush()
            os.fsync(stream.fileno())
        value = {'schema': 1, 'kind': 'aio_legacy_result',
                 'requestId': result.request_id, 'path': relative,
                 'size': checked.stat().st_size, 'sha256': file_hash(checked),
                 'targetReport': result.target_report, 'formatIds': list(result.format_ids)}
        value['checksum'] = ResultStore._digest(value)
        raw = json.dumps(value, ensure_ascii=True).encode('ascii')
        if len(raw) > MAX_JOURNAL_BYTES:
            raise OSError('Legacy result journal exceeds inspection budget')
        atomic_json(root / 'legacy-result.json', value)

    def _path(self, relative):
        if (not isinstance(relative, str) or not relative or
                any(value in relative for value in ('\\', ':', '\0'))):
            raise ValueError('Invalid legacy output path')
        parts = PurePosixPath(relative)
        if parts.is_absolute() or parts.as_posix() != relative or any(p in ('', '.', '..') for p in parts.parts):
            raise ValueError('Invalid legacy output path')
        current = Path(self.name)
        if path_is_link(current):
            raise ValueError('Linked legacy root')
        for part in parts.parts:
            current = current / part
            if path_is_link(current):
                raise ValueError('Linked legacy output')
        if not current.is_file():
            raise ValueError('Legacy output missing')
        return current

    def load(self, *, cancel=None):
        _check_cancel(cancel)
        root = Path(self.name)
        if self.store.retirement_pending():
            raise ValueError('Legacy result was explicitly released')
        path = root / 'legacy-result.json'
        if path_is_link(path) or path.stat().st_size > MAX_JOURNAL_BYTES:
            raise ValueError('Invalid legacy journal')
        value = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise ValueError('Invalid legacy journal object')
        checksum = value.pop('checksum', None)
        if (checksum != ResultStore._digest(value) or value.get('schema') != 1 or
                value.get('kind') != 'aio_legacy_result' or
                not isinstance(value.get('path'), str) or
                not isinstance(value.get('requestId'), str) or
                not re.fullmatch(r'[a-f0-9]{32}', value['requestId']) or
                type(value.get('size')) is not int or value['size'] < 0 or
                not isinstance(value.get('sha256'), str) or
                not re.fullmatch(r'[a-f0-9]{64}', value['sha256']) or
                not isinstance(value.get('targetReport'), dict) or
                not isinstance(value.get('formatIds'), list) or
                any(not isinstance(item, str) or not re.fullmatch(r'[a-z0-9_-]{1,32}', item)
                    for item in value['formatIds'])):
            raise ValueError('Legacy journal integrity failed')
        output = self._path(value['path'])
        if output.stat().st_size != value['size'] or file_hash(output, cancel=cancel) != value['sha256']:
            raise ValueError('Legacy output bytes changed')
        value.update(path=output, name=output.name,
                     mime=mime_for(output.suffix.lower().lstrip('.')))
        self.preserve_outputs = True
        return value


def recover_workspaces(parent, *, skip_roots=(), cancel=None):
    """Claim complete journals, closing every untransferred owner on abort."""
    parent = Path(parent)
    _check_cancel(cancel)
    parent.mkdir(parents=True, exist_ok=True)
    if path_is_link(parent):
        raise OSError('Legacy recovery parent is linked')
    recovered, issues = [], []
    try:
        for root in sorted(parent.iterdir()):
            _check_cancel(cancel)
            if not ROOT_NAME.fullmatch(root.name) or root.name in skip_roots:
                continue
            workspace = None
            try:
                if path_is_link(root) or not root.is_dir():
                    raise ValueError('Invalid legacy workspace')
                if ResultStore(root).retirement_pending():
                    try:
                        workspace = LegacyWorkspace(root)
                        try:
                            workspace.store.cleanup_retired()
                        finally:
                            workspace.close()
                    except (OSError, ValueError, TypeError, RecursionError):
                        pass
                    continue
                workspace = LegacyWorkspace(root)
                value = workspace.load(cancel=cancel)
                _check_cancel(cancel)
                recovered.append((workspace, value))
                workspace = None
            except InterruptedError:
                raise
            except (OSError, ValueError, TypeError, RecursionError) as error:
                issues.append({'workspaceId': root.name, 'code': (
                    'LEGACY_RECOVERY_BUSY' if isinstance(error, BlockingIOError) else 'LEGACY_RECOVERY_UNVERIFIED')})
            finally:
                if workspace is not None:
                    workspace.close()
        _check_cancel(cancel)
        return recovered, issues
    except BaseException:
        for workspace, _value in recovered:
            workspace.close()
        raise

def salvage_unverified_workspace(parent, workspace_id, destination, *, check_budget=None, cancel=None):
    """Copy an unverified snapshot. One independent CRC/SHA read per member."""
    _check_cancel(cancel)
    if not isinstance(workspace_id, str) or not ROOT_NAME.fullmatch(workspace_id):
        raise ValueError('Invalid legacy workspace identity')
    root = Path(parent) / workspace_id
    if path_is_link(root) or not root.is_dir():
        raise ValueError('Legacy workspace is not an ordinary directory')
    workspace = LegacyWorkspace(root)
    try:
        if workspace.store.retirement_pending():
            raise ValueError('Previously retired workspace must not be salvaged')
        try:
            workspace.load(cancel=cancel)
        except InterruptedError:
            raise
        except (OSError, ValueError, TypeError, RecursionError):
            pass
        else:
            raise ValueError('A verified legacy result does not need salvage')
        native = root / 'native'
        if not native.is_dir() or path_is_link(native):
            raise ValueError('No safe native outputs in legacy workspace')
        files, total, visited = [], 0, 0
        for candidate in native.glob('*/*/output/**/*'):
            visited += 1
            if visited > 100_000:
                raise ValueError('Legacy salvage inspection limit exceeded')
            _check_cancel(cancel)
            relative = candidate.relative_to(root).as_posix()
            parts = PurePosixPath(relative).parts
            if (len(parts) < 5 or parts[0] != 'native' or
                    not parts[1].isdecimal() or not parts[2].isdecimal() or parts[3] != 'output'):
                continue
            if path_is_link(candidate):
                raise ValueError('Linked native output cannot be salvaged')
            if candidate.is_dir():
                continue
            output = workspace._path(relative)
            size = output.stat().st_size
            total += size
            files.append((relative, output, size))
            if len(files) > 4096 or total > 8 * 1024 ** 3:
                raise ValueError('Legacy salvage inspection limit exceeded')
        files.sort(key=lambda item: item[0])
        if not files:
            raise ValueError('No surviving native outputs were found')
        if check_budget is not None:
            check_budget(total + 1024 * 1024 + 512 * len(files))
        _check_cancel(cancel)
        destination = Path(destination)
        entries = []
        with ZipFile(destination, 'x', compression=ZIP_STORED, allowZip64=True) as archive:
            for relative, output, size in files:
                _check_cancel(cancel)
                digest = hashlib.sha256()
                copied = 0
                with output.open('rb') as source, archive.open(relative, 'w', force_zip64=True) as target:
                    while block := source.read(128 * 1024):
                        _check_cancel(cancel)
                        if copied + len(block) > size:
                            raise OSError('Native output grew during salvage')
                        target.write(block)
                        digest.update(block)
                        copied += len(block)
                if copied != size or output.stat().st_size != size:
                    raise OSError('Native output changed during salvage')
                entries.append({'path': relative, 'size': size, 'sha256': digest.hexdigest()})
            report = json.dumps({'schema': 1, 'status': 'RECOVERED_UNVERIFIED',
                'conversionSuccess': False, 'qualityCompared': False,
                'completenessVerified': False, 'sourceWorkspaceId': workspace_id,
                'files': entries}, ensure_ascii=False).encode('utf-8')
            archive.writestr('AIO-salvage-report.json', report)
        _check_cancel(cancel)
        with ZipFile(destination) as archive:
            names = [entry['path'] for entry in entries] + ['AIO-salvage-report.json']
            if sorted(archive.namelist()) != sorted(names):
                raise OSError('Salvaged ZIP inventory changed')
            if archive.read('AIO-salvage-report.json') != report:
                raise OSError('Salvaged ZIP report changed')
            for entry in entries:
                _check_cancel(cancel)
                digest, size = hashlib.sha256(), 0
                with archive.open(entry['path']) as stream:
                    while block := stream.read(128 * 1024):
                        _check_cancel(cancel)
                        digest.update(block)
                        size += len(block)
                if size != entry['size'] or digest.hexdigest() != entry['sha256']:
                    raise OSError('Salvaged ZIP bytes changed')
        _check_cancel(cancel)
        if check_budget is not None:
            check_budget()
        return len(entries)
    finally:
        workspace.close()
