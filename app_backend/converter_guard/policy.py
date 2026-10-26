"""Guard deadlines and optional resource policy; no extension, page or MB veto."""
from dataclasses import dataclass
import math
from pathlib import Path
import re

from ..libreoffice_catalog import EXTENSION
from .errors import GuardError

IDENTIFIER = re.compile(r'[A-Za-z0-9_-]{1,64}\Z')


@dataclass(frozen=True)
class GuardLimits:
    max_jobs: int = 16
    max_files: int | None = None
    max_file_bytes: int | None = None
    max_spool_bytes: int | None = None
    # A queue must eventually move past a wedged native operation. This is
    # Guard policy, configurable per explicit run; never a format/size veto.
    file_timeout: float | None = 1800
    retention_seconds: float = 1800
    max_metadata_bytes: int = 16 * 1024 * 1024
    upload_idle_timeout: float = 30
    preflight_timeout: float = 120
    session_max_tasks: int = 128
    native_import_timeout: float = 60
    native_close_timeout: float = 15
    native_chart_timeout: float = 60
    # Optional diagnostic samples of rejected native output; never a conversion cap.
    rejected_output_bytes: int = 0

    def __post_init__(self):
        if (type(self.rejected_output_bytes) is not int or
                not 0 <= self.rejected_output_bytes <= 8 * 1024 * 1024):
            raise ValueError('rejected_output_bytes must be between zero and 8 MiB')
        for key in ('max_jobs', 'max_metadata_bytes', 'session_max_tasks'):
            value = getattr(self, key)
            if type(value) is not int or value <= 0:
                raise ValueError(key + ' must be a positive integer')
        for key in ('max_files', 'max_file_bytes', 'max_spool_bytes'):
            value = getattr(self, key)
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError(key + ' must be None or a positive integer')
        for key in ('file_timeout', 'retention_seconds', 'upload_idle_timeout', 'preflight_timeout',
                    'native_import_timeout', 'native_close_timeout', 'native_chart_timeout'):
            value = getattr(self, key)
            if value is None and key == 'file_timeout':
                continue
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value <= 0):
                raise ValueError(key + ' must be finite and positive')


def validate_files(files, limits):
    if not isinstance(files, list) or not files:
        raise GuardError('FILE_COUNT', 'Выберите хотя бы один файл.')
    if limits.max_files is not None and len(files) > limits.max_files:
        raise GuardError('FILE_COUNT', 'Превышен явно настроенный лимит числа файлов.')
    rows, ids = [], set()
    for item in files:
        if not isinstance(item, dict):
            raise GuardError('INPUT_METADATA', 'Неверное описание файла.')
        identity, name, size = item.get('id'), item.get('name'), item.get('size')
        if not isinstance(identity, str) or not IDENTIFIER.fullmatch(identity) or identity in ids:
            raise GuardError('INPUT_ID', 'Идентификаторы файлов должны быть уникальными.')
        if (not isinstance(name, str) or not name.strip() or len(name) > 255
                or any(ord(c) < 32 for c in name) or '/' in name or '\\' in name
                or name in ('.', '..')):
            raise GuardError('INPUT_NAME', 'Недопустимое имя файла.')
        try:
            name.encode('utf-8')
        except UnicodeError as error:
            raise GuardError('INPUT_NAME', 'Недопустимая кодировка имени файла.') from error
        if type(size) is not int or size < 0:
            raise GuardError('INPUT_SIZE', 'Некорректный размер файла.')
        if limits.max_file_bytes is not None and size > limits.max_file_bytes:
            raise GuardError('INPUT_SIZE', 'Превышен явно настроенный лимит размера Guard.', status=413)
        ids.add(identity)
        rows.append({'id': identity, 'name': name, 'size': size})
    return rows


def validate_formats(formats):
    if formats is None:
        formats = ['pdf']
    if not isinstance(formats, list) or not formats:
        raise GuardError('OUTPUT_FORMAT', 'Выберите хотя бы один формат результата.')
    result, seen = [], set()
    for value in formats:
        if not isinstance(value, str) or not EXTENSION.fullmatch(value):
            raise GuardError('OUTPUT_FORMAT', 'Некорректное расширение результата.')
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def validate_plan(files, formats, plan):
    """One explicit target list per source; never infer type compatibility."""
    if plan is None:
        targets = validate_formats(formats)
        return {item['id']: targets for item in files}
    if formats is not None or not isinstance(plan, list):
        raise GuardError('CONVERSION_PLAN', 'Укажите общий список formats или индивидуальный plan.',
                         stage='plan')
    expected = {item['id'] for item in files}
    result = {}
    for entry in plan:
        if not isinstance(entry, dict):
            raise GuardError('CONVERSION_PLAN', 'Некорректное описание плана.', stage='plan')
        identity = entry.get('fileId')
        if (not isinstance(identity, str) or identity not in expected or identity in result):
            raise GuardError('CONVERSION_PLAN', 'План содержит неизвестный или повторный файл.',
                             stage='plan')
        result[identity] = validate_formats(entry.get('formats', []))
    if set(result) != expected:
        raise GuardError('CONVERSION_PLAN', 'Для каждого исходника нужны выбранные форматы.',
                         stage='plan')
    return result
