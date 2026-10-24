"""Readback verification of saved bytes. This does not certify conversion quality."""
import hashlib
import os
from pathlib import Path
import re
import stat

from .errors import GuardError
from .result_store import path_is_link
from .html_resources import resource_path


def sha256_stream(stream, *, cancel=None):
    digest, size = hashlib.sha256(), 0
    while block := stream.read(64 * 1024):
        if cancel and cancel.is_set():
            raise GuardError('SAVE_CANCELLED', 'Проверка сохранённых файлов отменена.', stage='save')
        size += len(block)
        digest.update(block)
    if cancel and cancel.is_set():
        raise GuardError('SAVE_CANCELLED', 'Проверка сохранённых файлов отменена.', stage='save')
    return size, digest.hexdigest()


def verify_folder(folder, entries, *, cancel=None, on_progress=lambda current, total: None):
    """Read saved files independently; retain each observed hash in the manifest.

    The copy hash is the expected value, never evidence of a successful readback.
    Entries are annotated even on a byte mismatch so the caller can persist the
    failed stage without discarding the application-owned original results.
    """
    folder = Path(folder)
    if path_is_link(folder):
        raise GuardError('SAVE_VERIFY_FAILED', 'Ссылка вместо папки сохранения.', stage='save')
    folder = folder.resolve(strict=True)
    seen = set()
    bytes_verified = 0
    for number, entry in enumerate(entries, 1):
        if cancel and cancel.is_set():
            raise GuardError('SAVE_CANCELLED', 'Проверка сохранённых файлов отменена.', stage='save')
        try:
            relative = resource_path(entry.get('path'))
        except (AttributeError, GuardError) as error:
            raise GuardError('SAVE_VERIFY_FAILED', 'Недопустимый путь результата.', stage='save') from error
        if relative.casefold() in seen:
            raise GuardError('SAVE_VERIFY_FAILED', 'Повторный путь в списке результатов.', stage='save')
        seen.add(relative.casefold())
        if (type(entry.get('size')) is not int or entry['size'] < 0
                or not re.fullmatch(r'[0-9a-f]{64}', str(entry.get('sha256')))):
            raise GuardError('SAVE_VERIFY_FAILED', 'Недопустимое описание результата.', stage='save')
        path = folder / relative
        for part in (path, *path.parents):
            if part == folder.parent:
                break
            if path_is_link(part):
                raise GuardError('SAVE_VERIFY_FAILED', 'Ссылка вместо сохранённого файла.', stage='save')
        if not path.resolve(strict=True).is_relative_to(folder):
            raise GuardError('SAVE_VERIFY_FAILED', 'Результат вне папки сохранения.', stage='save')
        # O_NOFOLLOW closes the final-component race on platforms supporting it;
        # Windows links/reparse points are rejected above and through fstat.
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0) |
                             getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(descriptor, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or
                    getattr(before, 'st_file_attributes', 0) &
                    getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)):
                raise GuardError('SAVE_VERIFY_FAILED', 'Результат не является обычным файлом.', stage='save')
            size, digest = sha256_stream(stream, cancel=cancel)
            after = os.fstat(stream.fileno())
        current = path.stat(follow_symlinks=False)
        unchanged = (not path_is_link(path) and
                     (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                     (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) ==
                     (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns))
        matches = unchanged and size == entry['size'] and digest == entry['sha256']
        entry.update(readbackSize=size, readbackSha256=digest, readbackVerified=matches)
        if not matches:
            raise GuardError('SAVE_VERIFY_FAILED',
                             'Сохранённый файл отличается от результата AIO. Оригинал результата оставлен в Guard.',
                             stage='save')
        bytes_verified += size
        try:
            on_progress(number, len(entries))
        except Exception:
            pass
    return {'readbackVerified': True, 'verificationScope':'manifest_outputs',
            'verificationMethod':'independent_read_sha256',
            'outputsVerified':len(entries), 'bytesVerified':bytes_verified}

