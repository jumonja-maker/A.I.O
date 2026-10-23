"""Readback verification of saved bytes. This does not certify conversion quality."""
import hashlib
from pathlib import Path
import re

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
    folder = Path(folder).resolve(strict=True)
    seen = set()
    for number, entry in enumerate(entries, 1):
        relative = resource_path(entry.get('path'))
        if relative.casefold() in seen:
            raise GuardError('SAVE_VERIFY_FAILED', 'Повторный путь в списке результатов.', stage='save')
        seen.add(relative.casefold())
        path = folder / relative
        for part in (path, *path.parents):
            if part == folder.parent:
                break
            if path_is_link(part):
                raise GuardError('SAVE_VERIFY_FAILED', 'Ссылка вместо сохранённого файла.', stage='save')
        if not path.resolve(strict=True).is_relative_to(folder):
            raise GuardError('SAVE_VERIFY_FAILED', 'Результат вне папки сохранения.', stage='save')
        with path.open('rb') as stream:
            size, digest = sha256_stream(stream, cancel=cancel)
        if (type(entry.get('size')) is not int or not re.fullmatch(r'[0-9a-f]{64}', str(entry.get('sha256')))
                or size != entry['size'] or digest != entry['sha256']):
            raise GuardError('SAVE_VERIFY_FAILED',
                             'Сохранённый файл отличается от результата AIO. Оригинал результата оставлен в Guard.',
                             stage='save')
        try:
            on_progress(number, len(entries))
        except Exception:
            pass
    return {'readbackVerified': True, 'verificationScope':'manifest_outputs', 'outputsVerified':len(entries)}
