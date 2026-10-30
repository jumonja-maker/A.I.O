"""Measured legacy disk budget; never evict an unsaved workspace.

Count logical file bytes, including inputs, native profiles, archives and
unverified/foreign-owned roots. This is cooperative admission/live monitoring,
not an OS filesystem quota: native writers can overshoot between polls.
"""
import os
from pathlib import Path
import stat
import time

from .libreoffice_engine import LibreOfficeError

DEFAULT_STORAGE_BYTES = 8 * 1024 ** 3
MAX_SCAN_ENTRIES = 100_000
MAX_SCAN_DEPTH = 32


def measured_bytes(roots):
    total, visited = 0, 0
    pending = [(Path(root), 0) for root in roots]
    while pending:
        path, depth = pending.pop()
        visited += 1
        if visited > MAX_SCAN_ENTRIES or depth > MAX_SCAN_DEPTH:
            raise OSError('Legacy storage inspection budget exceeded')
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue  # Explicitly released storage may disappear during a scan.
        if (stat.S_ISLNK(info.st_mode) or
                getattr(info, 'st_file_attributes', 0) & 0x400):
            raise OSError('Linked legacy storage cannot be measured safely')
        if stat.S_ISREG(info.st_mode):
            total += info.st_size
        elif stat.S_ISDIR(info.st_mode):
            try:
                with os.scandir(path) as entries:
                    for item in entries:
                        pending.append((Path(item.path), depth + 1))
                        if visited + len(pending) > MAX_SCAN_ENTRIES:
                            raise OSError('Legacy storage inspection budget exceeded')
            except FileNotFoundError:
                continue
        else:
            raise OSError('Unsupported legacy storage entry')
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
            self.next_scan = now + .5
            self.check()
        return False

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
