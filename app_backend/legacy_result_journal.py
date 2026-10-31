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


def file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
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

    def load(self):
        root = Path(self.name)
        if self.store.retirement_pending():
            raise ValueError('Legacy result was explicitly released')
        path = root / 'legacy-result.json'
        if path_is_link(path) or path.stat().st_size > MAX_JOURNAL_BYTES:
            raise ValueError('Invalid legacy journal')
        value = json.loads(path.read_text(encoding='utf-8'))
        checksum = value.pop('checksum', None)
        if (checksum != ResultStore._digest(value) or value.get('schema') != 1 or
                value.get('kind') != 'aio_legacy_result' or
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
        if output.stat().st_size != value['size'] or file_hash(output) != value['sha256']:
            raise ValueError('Legacy output bytes changed')
        value.update(path=output, name=output.name,
                     mime=mime_for(output.suffix.lower().lstrip('.')))
        self.preserve_outputs = True
        return value


def recover_workspaces(parent, *, skip_roots=()):
    """Claim only complete, byte-verified journals; leave every other root intact."""
    parent = Path(parent)
    parent.mkdir(parents=True, exist_ok=True)
    if path_is_link(parent):
        raise OSError('Legacy recovery parent is linked')
    recovered, issues = [], []
    for root in sorted(parent.iterdir()):
        if not ROOT_NAME.fullmatch(root.name) or root.name in skip_roots:
            continue
        workspace = None
        try:
            if path_is_link(root) or not root.is_dir():
                raise ValueError('Invalid legacy workspace')
            # A previously confirmed refusal uses ResultStore's durable
            # identity-bound retirement barrier. An interrupted deletion must
            # never become a new "unsaved" result or prompt on next launch.
            if ResultStore(root).retirement_pending():
                try:
                    workspace = LegacyWorkspace(root)
                    try:
                        workspace.store.cleanup_retired()
                    finally:
                        workspace.close()
                except (OSError, ValueError, TypeError, RecursionError):
                    pass  # Keep the barrier for a later safe cleanup retry.
                continue
            workspace = LegacyWorkspace(root)
            value = workspace.load()
            recovered.append((workspace, value))
        except (OSError, ValueError, TypeError, RecursionError) as error:
            if workspace is not None:
                workspace.close()
            issues.append({'workspaceId': root.name, 'code': (
                'LEGACY_RECOVERY_BUSY' if isinstance(error, BlockingIOError)
                else 'LEGACY_RECOVERY_UNVERIFIED')})
    return recovered, issues

def salvage_unverified_workspace(parent, workspace_id, destination, *, check_budget=None):
    """Copy surviving native outputs into a *separate*, unverified ZIP.

    Only a manual request can call this. The original workspace and its broken
    journal are never modified or retired, even when the ZIP is later released.
    A verified published result must be downloaded via its ordinary ticket.
    """
    if not isinstance(workspace_id, str) or not ROOT_NAME.fullmatch(workspace_id):
        raise ValueError('Invalid legacy workspace identity')
    root = Path(parent) / workspace_id
    if path_is_link(root) or not root.is_dir():
        raise ValueError('Legacy workspace is not an ordinary directory')
    workspace = LegacyWorkspace(root)  # Nonblocking exclusive ownership.
    try:
        # An explicit deletion barrier must never become recoverable data.
        if workspace.store.retirement_pending():
            raise ValueError('Previously retired workspace must not be salvaged')
        try:
            workspace.load()
        except (OSError, ValueError, TypeError, RecursionError):
            pass
        else:
            raise ValueError('A verified legacy result does not need salvage')
        native = root / 'native'
        if not native.is_dir() or path_is_link(native):
            raise ValueError('No safe native outputs in legacy workspace')
        files, total = [], 0
        for candidate in sorted(native.glob('*/*/output/**/*')):
            relative = candidate.relative_to(root).as_posix()
            parts = PurePosixPath(relative).parts
            if (len(parts) < 5 or parts[0] != 'native' or
                    not parts[1].isdecimal() or not parts[2].isdecimal() or
                    parts[3] != 'output'):
                continue
            # Reject links and exotic file types; never follow paths outside
            # the owned output tree or use the original user-upload directory.
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
        if not files:
            raise ValueError('No surviving native outputs were found')
        # ZIP_STORED duplicates the bytes, so reserve before any disk writes.
        if check_budget is not None:
            check_budget(total + 1024 * 1024 + 512 * len(files))
        destination = Path(destination)
        entries = []
        with ZipFile(destination, 'w', compression=ZIP_STORED, allowZip64=True) as archive:
            for relative, output, size in files:
                digest = hashlib.sha256()
                copied = 0
                with output.open('rb') as source, archive.open(relative, 'w', force_zip64=True) as target:
                    while block := source.read(128 * 1024):
                        target.write(block)
                        digest.update(block)
                        copied += len(block)
                if copied != size or output.stat().st_size != size:
                    raise OSError('Native output changed during salvage')
                entries.append({'path': relative, 'size': size, 'sha256': digest.hexdigest()})
            archive.writestr('AIO-salvage-report.json', json.dumps({
                'schema': 1, 'status': 'RECOVERED_UNVERIFIED',
                'conversionSuccess': False, 'qualityCompared': False,
                'completenessVerified': False, 'sourceWorkspaceId': workspace_id,
                'files': entries,
            }, ensure_ascii=False).encode('utf-8'))
        # Read the ZIP, not only the original files, before publishing a ticket.
        with ZipFile(destination) as archive:
            if archive.testzip() is not None:
                raise OSError('Salvaged ZIP failed CRC validation')
            for entry in entries:
                digest = hashlib.sha256()
                with archive.open(entry['path']) as stream:
                    while block := stream.read(128 * 1024):
                        digest.update(block)
                if digest.hexdigest() != entry['sha256']:
                    raise OSError('Salvaged ZIP bytes changed')
        if check_budget is not None:
            check_budget()
        return len(entries)
    finally:
        workspace.close()
