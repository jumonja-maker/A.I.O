"""Thin LibreOffice CLI adapter. Conversion policy belongs to Converter Guard.

No file-size, batch-size, page-count, elapsed-time or source-format policy here.
Sources stay on disk; LibreOffice decides whether it can import/export them.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import sys
import threading

from .libreoffice_catalog import EXTENSION, catalog
from .native_process import ProcessTree


class LibreOfficeError(ValueError):
    def __init__(self, message: str, code: str = 'LIBREOFFICE_ERROR'):
        super().__init__(message)
        self.code = code


def source_format(name: str) -> str:
    # Structural path validation is not a format allowlist.
    if (not isinstance(name, str) or not name or name in ('.', '..') or
            any(c in name for c in '/\\\0') or re.search(r'[\r\n]', name)):
        raise LibreOfficeError('Недопустимое имя исходного файла.', 'INVALID_FILENAME')
    return Path(name).suffix.lower().lstrip('.')


def engine_binary() -> Path | None:
    configured = os.environ.get('AIO_LIBREOFFICE') or os.environ.get('AIO_LIBREOFFICE_ROOT')
    if configured:
        roots = [Path(configured).expanduser()]
    else:
        frozen = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent.parent))
        roots = [frozen / 'libreoffice', frozen / '_internal/libreoffice',
                 Path('D:/LBR/LibreOffice'), Path('D:/LBR')]
        roots.extend(Path(os.environ[key]) / 'LibreOffice'
                     for key in ('ProgramFiles', 'ProgramFiles(x86)') if os.environ.get(key))
        roots.extend((Path('/Applications/LibreOffice.app/Contents'),
                      Path('/usr/lib/libreoffice')))
    for root in roots:
        if root.is_file():
            return root.resolve()
        for leaf in ('program/soffice.com', 'program/soffice.exe', 'program/soffice',
                     'MacOS/soffice', 'soffice'):
            candidate = root / leaf
            if candidate.is_file():
                return candidate.resolve()
    if configured:  # An explicit engine setting is authoritative.
        return None
    candidate = shutil.which('soffice.com') or shutil.which('soffice') or shutil.which('libreoffice')
    return Path(candidate).resolve() if candidate else None


def inspect_metadata(files: list[dict]) -> dict:
    if not isinstance(files, list):
        raise LibreOfficeError('Некорректный список файлов.', 'INVALID_BATCH')
    rows, ids, seen = [], [], set()
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get('id'), str):
            raise LibreOfficeError('Некорректные сведения о файле.', 'INVALID_BATCH')
        name = item.get('name')
        extension = source_format(name)
        if item['id'] in seen or not item['id']:
            raise LibreOfficeError('Идентификаторы файлов должны отличаться.', 'INVALID_IDS')
        size = item.get('size')
        if type(size) is not int or size < 0:
            raise LibreOfficeError('Некорректный размер файла.', 'INVALID_METADATA')
        ids.append(item['id'])
        seen.add(item['id'])
        rows.append({'id': item['id'], 'format': extension.upper(), 'name': name,
                     'size': size, 'compatibility': 'native_decision'})
    binary = engine_binary()
    formats = []
    for extension, category, description, mime, registered in catalog(binary):
        formats.append({
            'id': extension, 'label': extension.upper(), 'category': category,
            'description': description, 'enabled': bool(binary),
            'compatibleFileIds': list(ids), 'options': [],
            'compatibility': 'native_decision', 'registeredExport': registered,
            'mime': mime,
            'help': [
                {'title': 'Движок', 'text': 'Исходный файл целиком обрабатывает установленный LibreOffice.'},
                {'title': 'Обработка', 'text': 'Возможность преобразования выбранных файлов определит LibreOffice при запуске.'},
                {'title': 'Результат', 'text': ('Графический экспорт сохраняет представление страницы. Могут измениться размер полотна и число страниц; анимация и редактируемость могут не сохраниться.'
                    if category == 'images' else 'При смене формата оформление и свойства могут измениться. Полнота содержимого автоматически не сравнивается.')},
            ],
        })
    return {'files': rows, 'formats': formats,
            'acceptedExtensions': '', 'policy': None,
            'message': ('LibreOffice готов. Выберите формат результата.' if binary else
                        'LibreOffice не найден. Проверьте установку движка.')}


def inspect(files, ids):
    """Compatibility for callers; never parses or copies document contents."""
    return inspect_metadata([{'id': identity, 'name': name, 'size': len(blob)}
                             for (name, blob), identity in zip(files, ids)])


def convert_path(source: Path, target: str, workspace: Path, *,
                 executable: Path | None = None, cancel=None, on_started=lambda: None,
                 convert_spec: str | None = None) -> list[Path]:
    source_format(source.name)
    if not isinstance(target, str) or not EXTENSION.fullmatch(target):
        raise LibreOfficeError('Некорректное расширение результата.', 'INVALID_FORMAT')
    binary = executable or engine_binary()
    if binary is None or not binary.is_file():
        raise LibreOfficeError('LibreOffice не найден.', 'ENGINE_NOT_FOUND')
    cancel = cancel or threading.Event()
    if cancel.is_set():
        raise LibreOfficeError('Конвертация отменена.', 'CANCELLED')
    output, profile = workspace / 'output', workspace / 'profile'
    output.mkdir(parents=True)
    # A fresh profile prevents a running personal office from taking the job.
    # Native defaults are retained; Guard can supply explicit export settings.
    command = [str(binary), '-env:UserInstallation=' + profile.resolve().as_uri(),
               '--headless', '--nologo', '--nodefault', '--norestore',
               '--convert-to', convert_spec or target,
               '--outdir', str(output.resolve()), str(source.resolve())]
    try:
        with ProcessTree(command, cwd=workspace, on_started=on_started) as tree:
            while tree.process.poll() is None:
                if cancel.wait(0.1):
                    raise LibreOfficeError('Конвертация отменена.', 'CANCELLED')
            if cancel.is_set():
                raise LibreOfficeError('Конвертация отменена.', 'CANCELLED')
            if tree.process.returncode != 0:
                raise LibreOfficeError('LibreOffice завершился с кодом ' +
                                       str(tree.process.returncode) + '.', 'ENGINE_FAILED')
    except OSError as exc:
        raise LibreOfficeError('Не удалось запустить LibreOffice.', 'ENGINE_LAUNCH_FAILED') from exc
    results = sorted(p for p in output.rglob('*')
                     if p.is_file() and not p.is_symlink())
    if not results:
        raise LibreOfficeError('LibreOffice не создал запрошенный результат.', 'ENGINE_OUTPUT_MISSING')
    return results
