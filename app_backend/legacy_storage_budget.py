"""Measured legacy disk budget; never evict an unsaved workspace.

Count logical file bytes, including inputs, native profiles, archives and
unverified/foreign-owned roots. This is cooperative admission/live monitoring,
not an OS filesystem quota: native writers can overshoot between polls.
"""
import os
import stat
import time

from .libreoffice_engine import LibreOfficeError

DEFAULT_STORAGE_BYTES = 8 * 1024 ** 3
MAX_SCAN_ENTRIES = 100_000
MAX_SCAN_DEPTH = 32


# Windows lstat obtains handle metadata that descent does not need. A fresh
# attributes lookup still detects a directory replaced by a link/reparse point,
# without paying for the full stat on every directory of every quota scan.
# Cache only the API binding, never filesystem attributes or byte counts.
_get_file_attributes = None
if os.name == 'nt':
    import ctypes
    _get_file_attributes = ctypes.WinDLL('kernel32', use_last_error=True).GetFileAttributesW
    _get_file_attributes.argtypes = [ctypes.c_wchar_p]
    _get_file_attributes.restype = ctypes.c_uint32


def _check_directory(path):
    attributes = _get_file_attributes(os.fsdecode(path))
    if attributes == 0xffffffff:
        raise ctypes.WinError(ctypes.get_last_error())
    if attributes & 0x400:
        raise OSError('Linked legacy storage cannot be measured safely')
    if not attributes & 0x10:
        raise OSError('Legacy storage directory changed type during inspection')


def _check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise LibreOfficeError('Проверка занятого места отменена.', 'CANCELLED')


def measured_bytes(roots, *, cancel=None):
    """Full bounded scans, without buffering a DirEntry for every file.

    No stale byte cache can hide active writers or foreign-owned files.
    """
    _check_cancel(cancel)
    total = 0
    pending = [(os.fspath(root), 0, False) for root in roots]
    visited = len(pending)
    if visited > MAX_SCAN_ENTRIES:
        raise OSError('Legacy storage inspection budget exceeded')
    # Most explicit admission/publication checks have no cancellation token.
    # Avoid a Python helper call for every file in those complete scans.
    cancelled = cancel.is_set if cancel is not None else None
    while pending:
        _check_cancel(cancel)
        path, depth, directory = pending.pop()
        try:
            if directory and _get_file_attributes is not None:
                _check_directory(path)
                info = None
            else:
                info = os.lstat(path)
        except FileNotFoundError:
            continue
        if info is not None:
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise OSError('Linked legacy storage cannot be measured safely')
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
                continue
            if not stat.S_ISDIR(info.st_mode):
                raise OSError('Unsupported legacy storage entry')
        try:
            child_depth = depth + 1
            with os.scandir(path) as entries:
                for item in entries:
                    if cancelled is not None and cancelled():
                        raise LibreOfficeError('Проверка занятого места отменена.', 'CANCELLED')
                    visited += 1
                    if visited > MAX_SCAN_ENTRIES or child_depth > MAX_SCAN_DEPTH:
                        raise OSError('Legacy storage inspection budget exceeded')
                    try:
                        info = item.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    kind = stat.S_IFMT(info.st_mode)
                    if kind == stat.S_IFLNK or getattr(info, 'st_file_attributes', 0) & 0x400:
                        raise OSError('Linked legacy storage cannot be measured safely')
                    if kind == stat.S_IFREG:
                        total += info.st_size
                    elif kind == stat.S_IFDIR:
                        # Only directories need deferred traversal. Regular
                        # files are charged immediately using the same fresh
                        # non-following stat required by the quota contract.
                        pending.append((item.path, child_depth, True))
                    else:
                        raise OSError('Unsupported legacy storage entry')
        except FileNotFoundError:
            continue
    _check_cancel(cancel)
    return total


class LegacyRunControl:
    """Use native/upload cancellation polls without a watcher thread."""
    def __init__(self, cancel, check):
        self.cancel, self.check = cancel, check
        self.next_scan = 0

    def is_set(self):
        if self.cancel.is_set():
            return True
        now = time.monotonic()
        if now >= self.next_scan:
            self.check()
            self.next_scan = time.monotonic() + .5
        return self.cancel.is_set()

    def wait(self, seconds):
        self.cancel.wait(seconds)
        return self.is_set()


def storage_error(code):
    return LibreOfficeError(
        'Недостаточно настроенной квоты старого конвертера. '
        'Сохраните и явно закройте прежние результаты. '
        'Несохранённые файлы не удалены.' if code == 'LEGACY_STORAGE_BYTES' else
        'Не удалось проверить занятое место старого конвертера. '
        'Проверьте доступ к диску; существующие файлы не удалены.', code)
