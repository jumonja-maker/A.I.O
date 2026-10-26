"""Opt-in, job-owned samples of failed native output; never ready artifacts.

Only ``job/work/<index>/output`` is read. Originals, profiles, source preparation
and arbitrary caller paths are never sampled. All budgets include on-disk JSON;
large files receive bounded metadata without a potentially expensive full hash.
No reader executes an output, schedules a retry, or creates a save ticket.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time

_MAX_BYTES = 8 * 1024 * 1024
_MAX_TASKS = 16
_MAX_ENTRIES = 16
_OWNER = 'aio-guard-failed-output-v1'
_TASK_NAME = re.compile(r'task-[0-9]+-[0-9a-f]{16}\Z')
_FILE_NAME = re.compile(r'(?:[0-9]{3}\.capture|\.partial-[0-9a-f]{16}|owner\.json|receipt\.json)\Z')
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')


class _Unsafe(Exception):
    pass


class _Cancelled(Exception):
    pass


def _encoded(value):
    return json.dumps(value, ensure_ascii=True, separators=(',', ':')).encode('ascii')


def _linked(stamp):
    return (stat.S_ISLNK(stamp.st_mode) or bool(getattr(stamp, 'st_file_attributes', 0)
            & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)))


def _safe(path, root, *, directory=False):
    """Reject links/reparse points in every component, including the job root."""
    path, root = Path(path).absolute(), Path(root).absolute()
    if '..' in path.parts or '..' in root.parts or not path.is_relative_to(root):
        raise _Unsafe()
    for ancestor in (path, *path.parents):
        info = ancestor.lstat()
        if _linked(info):
            raise _Unsafe()
        if ancestor == path:
            if directory and not stat.S_ISDIR(info.st_mode):
                raise _Unsafe()
        elif not stat.S_ISDIR(info.st_mode):
            raise _Unsafe()
    return path.lstat()


def _regular(path, root):
    info = _safe(path, root)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise _Unsafe()
    return info


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _check(cancel):
    if cancel is not None and cancel.is_set():
        raise _Cancelled()


def _text(value, limit):
    # Reports may contain absolute source paths. Retain useful native codes and
    # short prose, replacing quoted, drive, UNC and POSIX absolute paths.
    value = value[:2048] if isinstance(value, str) else ''
    value = re.sub(r'([\"\'])(?:[A-Za-z]:[\\/]|/|\\\\)[^\"\'\r\n]*\1', '[path]', value)
    value = re.sub(r'(?:[A-Za-z]:[\\/]|\\\\)[^\s\"\'<>|]*', '[path]', value)
    value = re.sub(r'(?<![A-Za-z0-9])/(?:[^\s\"\'<>|])+', '[path]', value)
    return ''.join(c if c.isprintable() else ' ' for c in value)[:limit]


def _owned_entries(folder, root, job_key):
    """Bounded metadata walk; foreign or excessive content stops capture."""
    _safe(folder, root, directory=True)
    rows, used = [], 0
    with os.scandir(folder) as entries:
        for index, item in enumerate(entries):
            if index >= _MAX_TASKS or not _TASK_NAME.fullmatch(item.name):
                raise _Unsafe()
            task = folder / item.name
            _safe(task, root, directory=True)
            owner_path = task / 'owner.json'
            owner_info = _regular(owner_path, root)
            if owner_info.st_size > 512:
                raise _Unsafe()
            with owner_path.open('rb') as stream:
                owner = json.loads(stream.read(513))
            if (owner.get('owner') != _OWNER or owner.get('jobKey') != job_key
                    or type(owner.get('createdAt')) not in (int, float)):
                raise _Unsafe()
            members, size = [], 0
            with os.scandir(task) as children:
                for number, child in enumerate(children):
                    if number >= _MAX_ENTRIES + 2 or not _FILE_NAME.fullmatch(child.name):
                        raise _Unsafe()
                    path = task / child.name
                    info = _regular(path, root)
                    members.append(path)
                    size += info.st_size
            used += size
            rows.append((task, members, size, owner['createdAt']))
    return rows, used


def _remove_owned(folder, members, root):
    # Keep the owner marker until every payload is gone, so a locked file
    # can be cleaned on the next attempt without becoming an unowned orphan.
    for path in sorted(members, key=lambda item: item.name == 'owner.json'):
        _regular(path, root)
        path.unlink()
    _safe(folder, root, directory=True)
    folder.rmdir()


def _outputs(output, root, maximum):
    """At most maximum entries are inspected; no eager rglob/listdir."""
    visited = 0
    def walk(folder, depth):
        nonlocal visited
        if depth > 8:
            return
        _safe(folder, root, directory=True)
        with os.scandir(folder) as entries:
            for item in entries:
                if visited >= maximum:
                    return
                visited += 1
                path = folder / item.name
                info = path.lstat()
                if _linked(info):
                    yield path, None, 'link_or_reparse'
                elif stat.S_ISDIR(info.st_mode):
                    yield from walk(path, depth + 1)
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    yield path, info, None
                else:
                    yield path, None, 'non_regular_or_hardlinked'
    yield from walk(output, 0)


def capture_failed_output(job_root, workspace, *, task_index, error_code,
                          source_sha256=None, diagnostic='', max_bytes=0,
                          max_tasks=16, max_entries=16, max_age_seconds=86400,
                          cancel=None):
    """Best-effort bounded receipt; failure never replaces conversion error.

    ``max_bytes=0`` disables even filesystem inspection. Positive limits are
    capped at 8 MiB, 16 captures and 16 visited output entries per owning job.
    Returned ``relativePath`` values point into diagnostics, not downloadable
    ready outputs. Job deletion removes this directory; a later capture also
    purges verified owned samples older than ``max_age_seconds`` (up to 7 days).
    Caller serializes captures for one job, as its conversion queue already does.
    """
    receipt = {'schema': 1, 'kind': 'failed_output_diagnostic', 'status': 'disabled',
               'taskIndex': task_index if type(task_index) is int and 0 <= task_index <= 2**63 - 1 else None,
               'errorCode': re.sub(r'[^A-Za-z0-9_.-]', '_', error_code[:64] if isinstance(error_code, str) else 'UNKNOWN'),
               'sourceSha256': source_sha256 if isinstance(source_sha256, str)
                   and len(source_sha256) == 64 and _DIGEST.fullmatch(source_sha256) else None,
               'diagnostic': _text(diagnostic, 256), 'files': [], 'capturedBytes': 0,
               'qualityCompared': False, 'ready': False}
    owned = None
    owned_members = []
    try:
        if type(max_bytes) is not int or max_bytes <= 0:
            return receipt
        if (type(task_index) is not int or task_index < 0 or task_index > 2**63 - 1
                or type(max_tasks) is not int or max_tasks <= 0
                or type(max_entries) is not int or max_entries <= 0
                or type(max_age_seconds) not in (int, float)
                or not 0 < max_age_seconds <= 7 * 86400):
            receipt['status'] = 'invalid_policy'
            return receipt
        max_bytes, max_tasks = min(max_bytes, _MAX_BYTES), min(max_tasks, _MAX_TASKS)
        max_entries = min(max_entries, _MAX_ENTRIES)
        root = Path(job_root).absolute()
        expected = root / 'work' / str(task_index)
        if Path(workspace).absolute() != expected:
            raise _Unsafe()
        _safe(expected, root, directory=True)
        output = expected / 'output'
        _safe(output, root, directory=True)
        _check(cancel)
        parent = root / 'diagnostics'
        if not parent.exists():
            parent.mkdir()
        _safe(parent, root, directory=True)
        quarantine = parent / 'failed-outputs'
        if not quarantine.exists():
            quarantine.mkdir()
        _safe(quarantine, root, directory=True)
        job_key = hashlib.sha256(str(root).encode('utf-8')).hexdigest()
        now = time.time()
        existing, used = _owned_entries(quarantine, root, job_key)
        kept = 0
        for folder, members, size, created in existing:
            _check(cancel)
            if 0 <= now - created and now - created > max_age_seconds:
                _remove_owned(folder, members, root)
                used -= size
            else:
                kept += 1
        if kept >= max_tasks:
            receipt['status'] = 'task_limit'
            return receipt
        available = max_bytes - used
        owner = {'owner': _OWNER, 'jobKey': job_key, 'taskIndex': task_index, 'createdAt': now}
        owner_bytes = _encoded(owner)
        receipt.update(status='metadata_only', entryLimit=max_entries, inspectionBounded=True)
        if len(owner_bytes) + len(_encoded(receipt)) > available:
            receipt['status'] = 'byte_limit'
            return receipt
        owned = quarantine / ('task-%d-%s' % (task_index, secrets.token_hex(8)))
        owned.mkdir()
        _safe(owned, root, directory=True)
        owner_path = owned / 'owner.json'
        owned_members.append(owner_path)
        with owner_path.open('xb') as stream:
            stream.write(owner_bytes)
        available -= len(owner_bytes)
        for number, (path, info, reason) in enumerate(_outputs(output, root, max_entries), 1):
            _check(cancel)
            entry = {'name': _text(path.relative_to(output).as_posix(), 160),
                     'size': info.st_size if info else None, 'sha256': None,
                     'captured': False, 'reason': reason or 'byte_budget'}
            receipt['files'].append(entry)
            # Account for final receipt before spending any file bytes. A large
            # file is never opened or hashed just to add a diagnostic record.
            planned = {**entry, 'captured': True, 'sha256': '0' * 64, 'reason': None,
                       'relativePath': (owned.relative_to(root) / ('%03d.capture' % number)).as_posix()}
            receipt['files'][-1] = planned
            next_bytes = receipt['capturedBytes'] + (info.st_size if info else 0)
            planned_receipt = {**receipt, 'capturedBytes': next_bytes, 'status': 'captured'}
            fits = info is not None and next_bytes + len(_encoded(planned_receipt)) <= available
            receipt['files'][-1] = entry
            if not fits:
                if receipt['capturedBytes'] + len(_encoded(receipt)) > available:
                    receipt['files'].pop()
                    break
                continue
            before = _regular(path, root)
            if _stamp(before) != _stamp(info):
                entry['reason'] = 'changed_before_capture'
                continue
            temporary = owned / ('.partial-' + secrets.token_hex(8))
            destination = owned / ('%03d.capture' % number)
            owned_members.append(temporary)
            digest, copied = hashlib.sha256(), 0
            flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
            with os.fdopen(os.open(path, flags), 'rb') as source, temporary.open('xb') as target:
                opened = os.fstat(source.fileno())
                if _linked(opened) or not stat.S_ISREG(opened.st_mode) or _stamp(opened) != _stamp(before):
                    raise _Unsafe()
                while block := source.read(min(65536, before.st_size - copied + 1)):
                    _check(cancel)
                    copied += len(block)
                    if copied > before.st_size:
                        raise _Unsafe()
                    target.write(block)
                    digest.update(block)
                after = os.fstat(source.fileno())
            _check(cancel)
            _safe(owned, root, directory=True)
            if (copied != before.st_size or _stamp(after) != _stamp(before)
                    or _stamp(_regular(path, root)) != _stamp(before)):
                temporary.unlink()
                owned_members.remove(temporary)
                entry['reason'] = 'changed_during_capture'
                continue
            # Exclusive hardlink publication cannot replace an existing sample.
            os.link(temporary, destination)
            owned_members.append(destination)
            temporary.unlink()
            owned_members.remove(temporary)
            receipt['files'][-1] = {**planned, 'sha256': digest.hexdigest()}
            receipt['capturedBytes'] += copied
        _check(cancel)
        receipt['status'] = 'captured' if any(row['captured'] for row in receipt['files']) else 'metadata_only'
        # These final fixed fields also participate in the quota. If the budget
        # was exactly exhausted, omit optional prose rather than exceed it.
        while receipt['capturedBytes'] + len(_encoded(receipt)) > available:
            if receipt.get('diagnostic'):
                receipt['diagnostic'] = ''
            elif 'inspectionBounded' in receipt:
                receipt.pop('inspectionBounded'); receipt.pop('entryLimit', None)
            else:
                raise _Unsafe()
        receipt_path = owned / 'receipt.json'
        owned_members.append(receipt_path)
        with receipt_path.open('xb') as stream:
            stream.write(_encoded(receipt))
        return receipt
    except _Cancelled:
        receipt.update(status='cancelled', files=[], capturedBytes=0)
    except Exception:
        receipt.update(status='unavailable', files=[], capturedBytes=0)
    if owned is not None:
        # Delete only paths this invocation created. On a hostile replacement,
        # stop rather than walking/deleting anything outside the owning job.
        try:
            _safe(owned, Path(job_root).absolute(), directory=True)
            for path in sorted(owned_members, key=lambda item: item.name == 'owner.json'):
                if path.exists():
                    path.unlink()
            owned.rmdir()
        except OSError:
            pass
        except _Unsafe:
            pass
    return receipt
