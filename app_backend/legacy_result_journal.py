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


def recover_workspaces(parent):
    """Claim only complete, byte-verified journals; leave every other root intact."""
    parent = Path(parent)
    parent.mkdir(parents=True, exist_ok=True)
    if path_is_link(parent):
        raise OSError('Legacy recovery parent is linked')
    recovered, issues = [], []
    for root in sorted(parent.iterdir()):
        if not ROOT_NAME.fullmatch(root.name):
            continue
        workspace = None
        try:
            if path_is_link(root) or not root.is_dir():
                raise ValueError('Invalid legacy workspace')
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
