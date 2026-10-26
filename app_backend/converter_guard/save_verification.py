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


def readback_fingerprint(path, *, cancel=None):
    """Read a final disk path and report whether it still names those bytes.

    A blocked Windows replacement or another read/stat error is a failed
    verification, never an acknowledgement. Cancellation keeps its own code.
    """
    path = Path(path)
    descriptor = None
    try:
        if path_is_link(path):
            raise GuardError('SAVE_VERIFY_FAILED', 'Ссылка вместо сохранённого файла.', stage='save')
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0) |
                             getattr(os, 'O_NOFOLLOW', 0))
        stream = os.fdopen(descriptor, 'rb')
        descriptor = None  # The stream now owns and closes the descriptor.
        with stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or
                    getattr(before, 'st_file_attributes', 0) &
                    getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)):
                raise GuardError('SAVE_VERIFY_FAILED', 'Результат не является обычным файлом.', stage='save')
            size, digest = sha256_stream(stream, cancel=cancel)
            after = os.fstat(stream.fileno())
        current = path.stat(follow_symlinks=False)
        def identity(info):
            return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        unchanged = (not path_is_link(path) and
                     identity(before) == identity(after) == identity(current))
        return size, digest, unchanged
    except OSError as error:
        raise GuardError('SAVE_VERIFY_FAILED',
                         'Не удалось независимо прочитать сохранённый файл. Результат оставлен в AIO.',
                         stage='save') from error
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass  # Preserve the verification error from the failed open.


def verify_folder(folder, entries, *, cancel=None, on_progress=lambda current, total: None):
    """Read saved files independently; retain each observed hash in the manifest.

    The copy hash is the expected value, never evidence of a successful readback.
    Entries are annotated even on a byte mismatch so the caller can persist the
    failed stage without discarding the application-owned original results.
    """
    folder = Path(folder)
    try:
        if path_is_link(folder):
            raise GuardError('SAVE_VERIFY_FAILED', 'Ссылка вместо папки сохранения.', stage='save')
        folder = folder.resolve(strict=True)
    except OSError as error:
        raise GuardError('SAVE_VERIFY_FAILED', 'Папка сохранения недоступна для проверки.', stage='save') from error
    seen = set()
    bytes_verified = 0
    for number, entry in enumerate(entries, 1):
        if cancel and cancel.is_set():
            raise GuardError('SAVE_CANCELLED', 'Проверка сохранённых файлов отменена.', stage='save')
        try:
            relative = entry.get('path')
            canonical = resource_path(relative)
        except (AttributeError, GuardError) as error:
            raise GuardError('SAVE_VERIFY_FAILED', 'Недопустимый путь результата.', stage='save') from error
        # Validate portable/canonical collisions without rewriting a native
        # filename: NTFS and Linux preserve decomposed Unicode byte-for-byte.
        if canonical.casefold() in seen:
            raise GuardError('SAVE_VERIFY_FAILED', 'Повторный путь в списке результатов.', stage='save')
        seen.add(canonical.casefold())
        if (type(entry.get('size')) is not int or entry['size'] < 0
                or not re.fullmatch(r'[0-9a-f]{64}', str(entry.get('sha256')))):
            raise GuardError('SAVE_VERIFY_FAILED', 'Недопустимое описание результата.', stage='save')
        entry['readbackVerified'] = False
        path = folder / relative
        try:
            for part in (path, *path.parents):
                if part == folder.parent:
                    break
                if path_is_link(part):
                    raise GuardError('SAVE_VERIFY_FAILED', 'Ссылка вместо сохранённого файла.', stage='save')
            if not path.resolve(strict=True).is_relative_to(folder):
                raise GuardError('SAVE_VERIFY_FAILED', 'Результат вне папки сохранения.', stage='save')
            size, digest, unchanged = readback_fingerprint(path, cancel=cancel)
        except OSError as error:
            raise GuardError('SAVE_VERIFY_FAILED', 'Не удалось проверить путь сохранённого файла.',
                             stage='save') from error
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

