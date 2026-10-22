"""Thin LibreOffice CLI adapter. Conversion policy belongs to Converter Guard.

No file-size, batch-size, page-count, elapsed-time or source-format policy here.
Sources stay on disk; LibreOffice decides whether it can import/export them.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time

from .libreoffice_catalog import EXTENSION, catalog
from .native_process import ProcessTree


class LibreOfficeError(ValueError):
    def __init__(self, message: str, code: str = 'LIBREOFFICE_ERROR', *, diagnostic=''):
        super().__init__(message)
        self.code = code
        self.diagnostic = diagnostic


def source_format(name: str) -> str:
    # Structural path validation is not a format allowlist.
    if (not isinstance(name, str) or not name or name in ('.', '..') or
            any(c in name for c in '/\\\0') or re.search(r'[\r\n]', name)):
        raise LibreOfficeError('Недопустимое имя исходного файла.', 'INVALID_FILENAME')
    return Path(name).suffix.lower().lstrip('.')



def validated_outputs(source: Path, target: str, output: Path, diagnostic: str = '') -> list[Path]:
    """Require a nonempty native primary; HTML/image sidecars are not results."""
    results = sorted(p for p in output.rglob('*') if p.is_file() and not p.is_symlink())
    aliases = {'jpeg': {'jpg', 'jpeg'}, 'jpg': {'jpg', 'jpeg'},
               'tif': {'tif', 'tiff'}, 'tiff': {'tif', 'tiff'},
               'htm': {'htm', 'html'}, 'html': {'htm', 'html'}}
    extensions = aliases.get(target, {target})
    primaries = [p for p in results if p.parent == output and
                 p.suffix.lower().lstrip('.') in extensions]
    if not primaries:
        raise LibreOfficeError('LibreOffice не создал основной файл запрошенного формата.',
                               'ENGINE_OUTPUT_MISSING', diagnostic=diagnostic)
    # Prefer the document's own name. Multiple page images can have
    # numbered stems, so extension + native output root is authoritative.
    primary = next((p for p in primaries if p.stem.casefold() == source.stem.casefold()),
                   primaries[0])
    try:
        if primary.stat().st_size == 0:
            raise LibreOfficeError('LibreOffice создал пустой основной файл.',
                                   'ENGINE_OUTPUT_EMPTY', diagnostic=diagnostic)
        with primary.open('rb') as stream:
            magic = stream.read(32)
    except OSError as exc:
        raise LibreOfficeError('Основной результат недоступен для проверки.',
                               'ENGINE_OUTPUT_MISSING', diagnostic=diagnostic) from exc
    signatures = {
        'pdf': (b'%PDF-',), 'png': (b'\x89PNG\r\n\x1a\n',),
        'jpg': (b'\xff\xd8\xff',), 'jpeg': (b'\xff\xd8\xff',),
        'gif': (b'GIF87a', b'GIF89a'), 'bmp': (b'BM',),
        'webp': (b'RIFF',), 'zip': (b'PK\x03\x04', b'PK\x05\x06'),
        'docx': (b'PK\x03\x04',), 'xlsx': (b'PK\x03\x04',),
        'pptx': (b'PK\x03\x04',), 'odt': (b'PK\x03\x04',),
        'ods': (b'PK\x03\x04',), 'odp': (b'PK\x03\x04',),
        'odg': (b'PK\x03\x04',), 'odc': (b'PK\x03\x04',),
        'epub': (b'PK\x03\x04',)
    }
    if target in signatures and not magic.startswith(signatures[target]):
        raise LibreOfficeError('Структура основного результата не соответствует формату.',
                               'ENGINE_OUTPUT_INVALID', diagnostic=diagnostic)
    if target == 'webp' and magic[8:12] != b'WEBP':
        raise LibreOfficeError('Некорректный WEBP результат.',
                               'ENGINE_OUTPUT_INVALID', diagnostic=diagnostic)
    return results


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
                 convert_spec: str | None = None, stdout=None, log_sink=None, import_filter=None,
                 metrics_sink=None) -> list[Path]:
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
    if import_filter:
        command.insert(6, '--infilter=' + import_filter)
    diagnostic, logs, return_code = '', None, None
    native_tick = time.monotonic()
    launch_ms = None
    try:
        from .converter_guard.native_logs import NativeLogs
        with ProcessTree(command, cwd=workspace, on_started=on_started,
                         stdout=subprocess.PIPE if stdout is None else stdout,
                         stderr=subprocess.PIPE) as tree:
            launch_ms = round((time.monotonic()-native_tick)*1000)
            logs = NativeLogs(tree.process)
            while tree.process.poll() is None:
                if cancel.is_set():
                    raise LibreOfficeError('Конвертация отменена.', 'CANCELLED')
                try:
                    # Wake when the process actually exits, instead of adding
                    # a full 100ms sleep to every short conversion. The same
                    # bounded interval still checks cancellation/Guard policy.
                    tree.process.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    pass
            if cancel.is_set():
                raise LibreOfficeError('Конвертация отменена.', 'CANCELLED')
            return_code = tree.process.returncode
    except OSError as exc:
        raise LibreOfficeError('Не удалось запустить LibreOffice.', 'ENGINE_LAUNCH_FAILED') from exc
    finally:
        process_ms = round((time.monotonic()-native_tick)*1000)
        diagnostic = logs.finish() if logs else ''
        if metrics_sink and launch_ms is not None:
            try:
                metrics_sink({'source':'cli_process', 'launchMs':launch_ms, 'processMs':process_ms})
            except Exception:
                pass  # Observation must never change an export outcome.
        # Diagnostics are bounded and do not expose private storage paths.
        for private in (str(source.resolve()), str(workspace.resolve()), str(profile.resolve()), str(binary)):
            diagnostic = diagnostic.replace(private, '[local]')
        diagnostic = diagnostic[-8192:]
        if log_sink:
            log_sink(diagnostic)
    if return_code != 0:
        source_failed = bool(re.search(r'(?im)^Error:\s*source file could not be loaded\.?\s*$', diagnostic))
        raise LibreOfficeError('LibreOffice не смог открыть исходный документ.' if source_failed else
                               'LibreOffice завершился с кодом ' + str(return_code) + '.',
                               'SOURCE_IMPORT_FAILED' if source_failed else 'ENGINE_FAILED',
                               diagnostic=diagnostic)
    try:
        return validated_outputs(source, target, output, diagnostic)
    except LibreOfficeError as error:
        source_failed = bool(re.search(r'(?im)^Error:\s*source file could not be loaded\.?\s*
, diagnostic))
        if source_failed:
            raise LibreOfficeError('LibreOffice не смог открыть исходный документ.',
                                   'SOURCE_IMPORT_FAILED', diagnostic=diagnostic) from error
        raise
