"""Attach matching codepage metadata to LibreOffice's native dBase outputs.

The native dBase FilterOptions=76 selects UTF-8, but its DBF header can still
advertise language-driver ID zero. A matching .cpg sidecar lets consumers
identify the codepage without interpreting UTF-8 bytes as Windows-1251.

Both native UNO and validated CLI snapshots can explicitly export Windows-1251
and declare LDID C9. Other Unicode and memo sheets remain UTF-8. Sidecar
attachment reads only the fixed header. The explicit CP1251 declaration helper
can update byte 29; it never changes records, field widths or memo bytes.
"""
import codecs
import os
from pathlib import Path
import stat

from .errors import GuardError


_DBASE_VERSIONS = frozenset((0x03, 0x04, 0x30, 0x83, 0x8b, 0xf5))
_LDID_CODECS = {0x57: 'cp1252', 0xC9: 'cp1251'}


def _cpg_codec(path):
    try:
        with path.open('rb') as stream:
            raw = stream.read(129)
        if len(raw) > 128:
            raise ValueError('cpg_label_budget')
        label = raw.decode('ascii').strip()
        if label.isdigit():
            label = 'utf-8' if label == '65001' else 'cp' + label
        return codecs.lookup(label).name
    except (OSError, UnicodeError, LookupError, ValueError) as error:
        raise GuardError('ENGINE_OUTPUT_VALIDATION_INCOMPLETE',
            'Кодировка DBF в существующем CPG не подтверждена.', stage='result') from error


def attach_utf8_codepages(paths, *, filter_name, cancel=None):
    """Return newly created/existing sidecars for verified native dBase UTF-8.

    When the exporter already declares a language driver, do not overwrite it
    with an unproved encoding claim. Never overwrite a conflicting sidecar.
    The caller adds returned files to the normal manifest/save transaction.
    """
    return _attach_codepages(paths, filter_name=filter_name, cancel=cancel)


def attach_native_codepages(paths, *, filter_name, cancel=None):
    """Include native Windows-1251 companions as well as UTF-8 companions.

    LDID C9 is set only after a native sheet exporter selected explicit
    Windows-1251 for strictly representable non-Memo text. Unknown drivers stay.
    """
    return _attach_codepages(paths, filter_name=filter_name, cancel=cancel, include_cp1251=True)


def declare_native_cp1251(path):
    """Declare only the known native FilterOptions=34 output, never transcode.

    Called after strict text representability and a completed native export.
    LibreOffice can leave LDID unset even with this explicit encoding option.
    """
    path = Path(path)
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or getattr(metadata, 'st_file_attributes', 0) & 0x400:
        raise GuardError('ENGINE_OUTPUT_PATH', 'Недопустимая ссылка на результат DBF.', stage='result')
    # Windows CRT O_TEXT can remove terminal Ctrl-Z already at open time.
    descriptor = os.open(path, os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
    with os.fdopen(descriptor, 'r+b') as stream:
        head = stream.read(32)
        if (len(head) != 32 or head[0] != 3
                or head[29] not in (0, 0xC9)
                or int.from_bytes(head[8:10], 'little') < 33
                or int.from_bytes(head[10:12], 'little') < 1):
            raise GuardError('ENGINE_OUTPUT_VALIDATION_INCOMPLETE',
                'Нативная кодировка Windows-1251 DBF не подтверждена.', stage='result')
        declaration = stream.read(int.from_bytes(head[8:10], 'little')-32)
        if (not declaration or declaration[-1] != 13 or (len(declaration)-1) % 32
                or any(declaration[index+11] == ord('M') for index in range(0, len(declaration)-1, 32))):
            raise GuardError('ENGINE_OUTPUT_VALIDATION_INCOMPLETE',
                'Схема DBF Windows-1251 требует неподтверждённого импорта Memo.', stage='result')
        if head[29] == 0:
            stream.seek(29)
            stream.write(b'\xc9')


def _attach_codepages(paths, *, filter_name, cancel, include_cp1251=False):
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
        cpg = path.with_suffix('.cpg')
        if cpg.is_symlink():
            raise GuardError('ENGINE_OUTPUT_PATH',
                'Недопустимая ссылка на файл кодировки DBF.', stage='result')
        codec = _cpg_codec(cpg) if cpg.exists() else None
        if head[29] != 0:
            # Do not let an LDID bypass conflicting sidecar validation. The
            # small confirmed legacy map is not a general encoding detector.
            if codec is not None and codec != _LDID_CODECS.get(head[29]):
                raise GuardError('ENGINE_OUTPUT_VALIDATION_INCOMPLETE',
                    'LDID и CPG DBF противоречат друг другу или не подтверждены.', stage='result')
            if not (include_cp1251 and head[29] == 0xC9):
                continue  # Preserve other declared legacy metadata.
            if codec is None:
                with cpg.open('x', encoding='ascii', newline='\n') as stream:
                    stream.write('1251\n')
            sidecars.append(cpg)
            continue
        if codec is not None:
            if codec != 'utf-8':
                raise GuardError('ENGINE_OUTPUT_VALIDATION_INCOMPLETE',
                    'Существующий CPG противоречит кодировке UTF-8.', stage='result')
        else:
            with cpg.open('x', encoding='ascii', newline='\n') as stream:
                stream.write('UTF-8\n')
        sidecars.append(cpg)
    return sidecars
