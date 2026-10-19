"""Direct, isolated LibreOffice engine for A.I.O (NOT Converter Guard).

The route table only describes LO Writer/Calc/Impress import/export families.
It cannot veto a route on behalf of the future Guard. All actual conversions
are executed through soffice, and verified for a nonempty physical output.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from zipfile import is_zipfile

MAX_SOURCE = 20 * 1024 * 1024
MAX_BATCH = 50 * 1024 * 1024
MAX_RESULT = 100 * 1024 * 1024
MAX_FILES = 100
TIMEOUT_SECONDS = 90
SOURCE_GROUP = {
    **dict.fromkeys(('docx', 'doc', 'odt', 'rtf', 'txt', 'html', 'htm'), 'writer'),
    **dict.fromkeys(('xlsx', 'xls', 'ods', 'csv'), 'calc'),
    **dict.fromkeys(('pptx', 'ppt', 'odp'), 'impress'),
}
# Only native same-family LO export filters. Cross-family, images and PDF imports
# require separate real-file tests; never silently use the old A.I.O converters.
EXPORT_FILTERS = {
    'writer': {
        'pdf': 'writer_pdf_Export', 'docx': 'Office Open XML Text',
        'odt': 'writer8', 'doc': 'MS Word 97', 'rtf': 'Rich Text Format',
        'txt': 'Text (encoded)', 'html': 'HTML (StarWriter)',
    },
    'calc': {
        'pdf': 'calc_pdf_Export', 'xlsx': 'Calc MS Excel 2007 XML',
        'ods': 'calc8', 'xls': 'MS Excel 97',
        'csv': 'Text - txt - csv (StarCalc)',
    },
    'impress': {
        'pdf': 'impress_pdf_Export', 'pptx': 'Impress MS PowerPoint 2007 XML',
        'odp': 'impress8', 'ppt': 'MS PowerPoint 97',
    },
}
OUTPUT_META = {
    'pdf': ('documents', 'Переносимый документ PDF', 'application/pdf'),
    'docx': ('documents', 'Документ Word DOCX', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
    'doc': ('documents', 'Документ Word DOC', 'application/msword'),
    'odt': ('documents', 'Текстовый документ OpenDocument', 'application/vnd.oasis.opendocument.text'),
    'rtf': ('documents', 'Документ Rich Text', 'application/rtf'),
    'txt': ('documents', 'Текстовый документ', 'text/plain'),
    'html': ('documents', 'Веб-страница HTML', 'text/html'),
    'xlsx': ('spreadsheets', 'Книга Excel XLSX', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
    'xls': ('spreadsheets', 'Таблица Excel XLS', 'application/vnd.ms-excel'),
    'ods': ('spreadsheets', 'Таблица OpenDocument', 'application/vnd.oasis.opendocument.spreadsheet'),
    'csv': ('spreadsheets', 'Значения CSV', 'text/csv'),
    'pptx': ('documents', 'Презентация PowerPoint PPTX', 'application/vnd.openxmlformats-officedocument.presentationml.presentation'),
    'ppt': ('documents', 'Презентация PowerPoint PPT', 'application/vnd.ms-powerpoint'),
    'odp': ('documents', 'Презентация OpenDocument', 'application/vnd.oasis.opendocument.presentation'),
}
ORDER = tuple(OUTPUT_META)
ZIP_BASED = frozenset(('docx', 'odt', 'xlsx', 'ods', 'pptx', 'odp'))
SCREENS = frozenset(('docx', 'odt', 'xlsx', 'ods', 'pptx', 'odp'))


class LibreOfficeError(ValueError):
    def __init__(self, message: str, code: str = 'LIBREOFFICE_ERROR'):
        super().__init__(message)
        self.code = code


def source_format(name: str) -> str:
    if (not isinstance(name, str) or not 0 < len(name) <= 240 or
        '/' in name or '\\' in name or ':' in name or '\0' in name or
        name in ('.', '..') or re.search(r'[\r\n\t]', name)):
        raise LibreOfficeError('Недопустимое имя исходного файла.', 'INVALID_FILENAME')
    suffix = Path(name).suffix.lower().lstrip('.')
    if suffix not in SOURCE_GROUP:
        raise LibreOfficeError('Этот входной формат пока не подключён к LibreOffice.', 'INPUT_NOT_SUPPORTED')
    return suffix


def compatible(source: str, target: str) -> bool:
    group = SOURCE_GROUP.get(source)
    return bool(group and target in EXPORT_FILTERS[group] and target != source)


def engine_binary() -> Path | None:
    frozen = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent.parent))
    places = []
    configured = os.environ.get('AIO_LIBREOFFICE_ROOT')
    if configured:
        places.append(Path(configured))
    places.extend((frozen / 'libreoffice', frozen / '_internal' / 'libreoffice',
                   Path('D:/LBR/LibreOffice'), Path('D:/LBR'),
                   Path('C:/Program Files/LibreOffice')))
    for root in places:
        if root.is_file() and root.name.lower() in ('soffice.exe', 'soffice'):
            return root.resolve()
        for candidate in (root / 'program' / 'soffice.exe',
                          root / 'program' / 'soffice', root / 'soffice'):
            if candidate.is_file():
                return candidate.resolve()
    which = shutil.which('soffice') or shutil.which('libreoffice')
    return Path(which).resolve() if which else None


def validate_source(name: str, blob: bytes) -> str:
    ext = source_format(name)
    if type(blob) is not bytes or not 0 < len(blob) <= MAX_SOURCE:
        raise LibreOfficeError('Файл пустой или превышает 20 МБ.', 'INPUT_TOO_LARGE')
    if ext in ZIP_BASED and not is_zipfile(__import__('io').BytesIO(blob)):
        raise LibreOfficeError('Файл не является корректным офисным ZIP-документом.', 'INVALID_PACKAGE')
    if ext in ('pdf',):  # no PDF inputs yet
        raise LibreOfficeError('Импорт PDF пока не подключён.', 'INPUT_NOT_SUPPORTED')
    return ext


def inspect(files: list[tuple[str, bytes]], ids: list[str]) -> dict:
    if len(files) != len(ids) or not 1 <= len(files) <= MAX_FILES:
        raise LibreOfficeError('Требуется от 1 до 100 файлов.', 'INVALID_BATCH')
    engine = engine_binary()
    details = []
    available = []
    for (name, blob), identity in zip(files, ids):
        info = {'id': identity}
        try:
            fmt = validate_source(name, blob)
            info['format'] = fmt.upper()
            available.append(fmt)
        except LibreOfficeError as exc:
            info['error'] = str(exc)
            available.append(None)
        details.append(info)
    formats = []
    for target in ORDER:
        category, description, _mime = OUTPUT_META[target]
        enabled = bool(engine) and all(x is not None and compatible(x, target) for x in available)
        reasons = {}
        for identity, origin in zip(ids, available):
            if origin is None:
                reasons[identity] = 'Исходный файл не прошёл проверку.'
            elif not engine:
                reasons[identity] = 'LibreOffice не найден.'
            elif not compatible(origin, target):
                reasons[identity] = 'Формат не экспортируется этим модулем LibreOffice.'
        formats.append({
            'id':target, 'label':target.upper(), 'category':category,
            'description':description, 'enabled':enabled,
            'compatibleFileIds':list(ids) if enabled else [],
            'incompatibleReasons':reasons,
            'help':[
                {'title':'Движок', 'text':'Непосредственный LibreOffice Writer, Calc или Impress.'},
                {'title':'Потери', 'text':'При смене формата могут измениться оформление и свойства. Проверьте результат.'},
            ],
            'options':[],
        })
    return {'files':details,'formats':formats,
            'message':('LibreOffice готов к конвертации.' if engine else
                       'LibreOffice не найден. Проверьте установку движка.')}


def convert(name: str, blob: bytes, target: str, *, executable: Path | None = None) -> bytes:
    source = validate_source(name, blob)
    if not compatible(source, target):
        raise LibreOfficeError('LibreOffice не подтверждает это направление в текущем адаптере.',
                               'UNSUPPORTED_DIRECTION')
    executable = executable or engine_binary()
    if executable is None or not executable.is_file():
        raise LibreOfficeError('LibreOffice не найден в системе или комплекте A.I.O.', 'ENGINE_NOT_FOUND')
    filter_name = EXPORT_FILTERS[SOURCE_GROUP[source]][target]
    with tempfile.TemporaryDirectory(prefix='aio-lo-') as temp:
        work = Path(temp)
        incoming = work / 'source'
        output = work / 'output'
        incoming.mkdir()
        output.mkdir()
        src_path = incoming / ('input.' + source)
        src_path.write_bytes(blob)
        profile = work / 'profile'
        profile.mkdir()
        cmd = [
            str(executable), '-env:UserInstallation=' + profile.as_uri(),
            '--headless', '--nologo', '--nodefault', '--norestore',
            '--convert-to', target + ':' + filter_name,
            '--outdir', str(output), str(src_path),
        ]
        kwargs = {'capture_output':True, 'timeout':TIMEOUT_SECONDS, 'check':False}
        if sys.platform == 'win32':
            kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
        try:
            process = subprocess.run(cmd, **kwargs)
        except subprocess.TimeoutExpired as exc:
            raise LibreOfficeError('LibreOffice превысил лимит времени обработки.',
                                   'ENGINE_TIMEOUT') from exc
        except OSError as exc:
            raise LibreOfficeError('Не удалось запустить LibreOffice.',
                                   'ENGINE_LAUNCH_FAILED') from exc
        result = output / ('input.' + target)
        if process.returncode != 0 or not result.is_file():
            raise LibreOfficeError('LibreOffice не создал запрошенный результат.',
                                   'ENGINE_FAILED')
        if result.stat().st_size == 0 or result.stat().st_size > MAX_RESULT:
            raise LibreOfficeError('LibreOffice создал пустой или слишком большой результат.',
                                   'INVALID_OUTPUT')
        content = result.read_bytes()
        if target == 'pdf' and not content.startswith(b'%PDF-'):
            raise LibreOfficeError('Результат не является PDF.', 'INVALID_OUTPUT')
        if target in ZIP_BASED and not is_zipfile(result):
            raise LibreOfficeError('Результат не является офисным ZIP-документом.',
                                   'INVALID_OUTPUT')
        return content
