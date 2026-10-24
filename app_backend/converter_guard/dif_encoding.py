"""Declare native DIF's UTF-8 encoding without altering its data bytes.

DIF has no in-band charset declaration. Adding a UTF-8 BOM makes some native
type detectors miss its TABLE header, so the file stays unchanged and a
companion describes the encoding and the matching LibreOffice import options.
This validates the encoding, not the fidelity of the converted document.
"""
import codecs
import hashlib
import json
import os
from pathlib import Path
import stat

from .errors import GuardError

BLOCK = 65536


def _cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise GuardError('CANCELLED', 'Проверка кодировки DIF отменена.', stage='result')


def _token(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _inspect(path, cancel):
    """Hash exact bytes while validating a complete, strict UTF-8 stream."""
    _cancel(cancel)
    initial = path.lstat()
    if not stat.S_ISREG(initial.st_mode) or path.is_symlink():
        raise GuardError('ENGINE_OUTPUT_PATH', 'Результат DIF не является обычным файлом.', stage='result')
    digest = hashlib.sha256()
    decoder = codecs.getincrementaldecoder('utf-8')('strict')
    read = 0
    try:
        with path.open('rb') as source:
            opened = os.fstat(source.fileno())
            if _token(initial) != _token(opened):
                raise GuardError('DIF_ENCODING_CHECK_FAILED', 'Результат DIF изменился во время проверки.', stage='result')
            first = source.read(BLOCK)
            if not first.startswith((b'TABLE\n', b'TABLE\r')):
                raise GuardError('DIF_ENCODING_INVALID',
                    'Основной результат не содержит ожидаемый заголовок DIF без BOM.', stage='result')
            block = first
            while block:
                _cancel(cancel)
                digest.update(block)
                decoder.decode(block, final=False)
                read += len(block)
                block = source.read(BLOCK)
            decoder.decode(b'', final=True)
            final = os.fstat(source.fileno())
        if _token(initial) != _token(final) or _token(final) != _token(path.lstat()) or read != final.st_size:
            raise GuardError('DIF_ENCODING_CHECK_FAILED', 'Результат DIF изменился во время проверки.', stage='result')
    except UnicodeError as error:
        raise GuardError('DIF_ENCODING_INVALID',
            'Результат нативного экспорта DIF не соответствует заявленной кодировке UTF-8.',
            stage='result', diagnostic='dif_utf8:' + type(error).__name__) from error
    _cancel(cancel)
    return {'name': path.name, 'bytes': read, 'sha256': digest.hexdigest()}


def describe_dif_encoding(paths, *, cancel):
    """Add one exclusive companion for same-folder native DIF output files.

    Call only after selecting the installed DIF exporter with FilterOptions 76.
    Other artifacts are retained. Every DIF is fully validated before a sidecar
    is created, and failures leave the original files and prior work untouched.
    """
    originals = list(paths)
    difs = [Path(path) for path in originals if Path(path).suffix.lower() == '.dif']
    if not difs:
        return originals, None
    _cancel(cancel)
    folder = difs[0].parent.resolve(strict=True)
    if any(path.parent.resolve(strict=True) != folder for path in difs):
        raise GuardError('ENGINE_OUTPUT_PATH', 'Файлы DIF должны находиться в одном каталоге результата.', stage='result')
    if any(not Path(path).resolve(strict=True).is_relative_to(folder) for path in originals):
        raise GuardError('ENGINE_OUTPUT_PATH', 'Недопустимое расположение результата DIF.', stage='result')
    if len({path.name.casefold() for path in difs}) != len(difs):
        raise GuardError('ENGINE_OUTPUT_COLLISION', 'Совпали имена результатов DIF.', stage='result')
    sidecar = folder / (difs[0].stem + '.aio-encoding.json')
    partial = sidecar.with_name(sidecar.name + '.part')
    if sidecar.exists() or sidecar.is_symlink():
        raise GuardError('ENGINE_OUTPUT_COLLISION', 'Файл описания кодировки DIF уже существует.', stage='result')
    records = [_inspect(path, cancel) for path in difs]
    metadata = {
        'schemaVersion': 1, 'format': 'DIF', 'encoding': 'UTF-8', 'bom': False,
        'libreOfficeImport': {'FilterName': 'DIF', 'FilterOptions': '76'},
        'instructions': {
            'ru': 'Файлы DIF сохранены в UTF-8. При импорте в другую программу выберите UTF-8; '
                  'кодировка ANSI по умолчанию может искажать кириллицу и другие символы.',
            'en': 'These DIF files use UTF-8. Select UTF-8 when importing them in another application; '
                  'an ANSI default may misread Cyrillic and other characters.'},
        'files': records, 'dataBytesChanged': False, 'qualityCompared': False}
    payload = (json.dumps(metadata, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    partial_created = sidecar_created = False
    try:
        _cancel(cancel)
        with partial.open('xb') as output:
            partial_created = True
            for offset in range(0, len(payload), BLOCK):
                _cancel(cancel)
                output.write(payload[offset:offset + BLOCK])
        _cancel(cancel)
        # link() provides an exclusive, atomic publish on both Windows/NTFS
        # and POSIX. A concurrent existing file can never be overwritten.
        os.link(partial, sidecar)
        sidecar_created = True
        partial.unlink()
        partial_created = False
    except BaseException:
        if partial_created:
            partial.unlink(missing_ok=True)
        if sidecar_created:
            sidecar.unlink(missing_ok=True)
        raise
    return [*originals, sidecar], {'check': 'dif_encoding', 'status': 'encoding_declared',
        'encoding': 'UTF-8', 'bom': False, 'files': len(difs), 'sidecar': sidecar.name,
        'dataBytesChanged': False, 'qualityCompared': False}
