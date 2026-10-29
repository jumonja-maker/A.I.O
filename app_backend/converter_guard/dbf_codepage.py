"""Attach explicit UTF-8 metadata to LibreOffice's native dBase outputs.

The native dBase FilterOptions=76 selects UTF-8, but its DBF header can still
advertise language-driver ID zero. A matching .cpg sidecar lets consumers
identify the codepage without interpreting UTF-8 bytes as Windows-1251.

No second conversion, input scan, document model, locale change or Guard probe.
Only the fixed DBF header is read; source and DBF bytes remain untouched.
"""
from pathlib import Path

from .errors import GuardError


_DBASE_VERSIONS = frozenset((0x03, 0x04, 0x30, 0x83, 0x8b, 0xf5))


def attach_utf8_codepages(paths, *, filter_name, cancel=None):
    """Return newly created/existing sidecars for verified native dBase UTF-8.

    When the exporter already declares a language driver, do not overwrite it
    with an unproved encoding claim. Never overwrite a conflicting sidecar.
    The caller adds returned files to the normal manifest/save transaction.
    """
    if filter_name != 'dBase':
        return []
    files = [Path(path) for path in paths if Path(path).suffix.lower() == '.dbf']
    sidecars = []
    for path in files:
        if cancel is not None and cancel.is_set():
            raise GuardError('CANCELLED', 'Создание метаданных DBF отменено.', stage='result')
        if path.is_symlink() or not path.is_file():
            raise GuardError('ENGINE_OUTPUT_VALIDATION_INCOMPLETE',
                'DBF недоступен для подтверждения кодировки.', stage='result')
        with path.open('rb') as stream:
            head = stream.read(32)
        if (len(head) != 32 or head[0] not in _DBASE_VERSIONS
                or int.from_bytes(head[8:10], 'little') < 33
                or int.from_bytes(head[10:12], 'little') < 1):
            raise GuardError('ENGINE_OUTPUT_VALIDATION_INCOMPLETE',
                'Неверный заголовок DBF: кодировка не подтверждена.', stage='result')
        if head[29] != 0:
            continue  # Native DBF header already provides a codepage marker.
        cpg = path.with_suffix('.cpg')
        if cpg.is_symlink():
            raise GuardError('ENGINE_OUTPUT_PATH',
                'Недопустимая ссылка на файл кодировки DBF.', stage='result')
        if cpg.exists():
            try:
                label = cpg.read_text(encoding='ascii').strip()
            except (OSError, UnicodeError) as error:
                raise GuardError('ENGINE_OUTPUT_VALIDATION_INCOMPLETE',
                    'Кодировка DBF в существующем CPG не подтверждена.', stage='result') from error
            if label.upper().replace('_', '-') not in ('UTF-8', 'UTF8'):
                raise GuardError('ENGINE_OUTPUT_VALIDATION_INCOMPLETE',
                    'Существующий CPG противоречит кодировке UTF-8.', stage='result')
        else:
            with cpg.open('x', encoding='ascii', newline='\n') as stream:
                stream.write('UTF-8\n')
        sidecars.append(cpg)
    return sidecars
