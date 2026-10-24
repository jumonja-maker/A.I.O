"""Use installed PNG/JPEG exporters for their compatible output aliases."""
import os
from pathlib import Path
import struct
import zlib

from .errors import GuardError
from .media_fidelity import inspect_media

CANONICAL = {'apng': 'png', 'jfif': 'jpg', 'jif': 'jpg', 'jpe': 'jpg'}


def native_alias_target(native, target, proof):
    canonical = CANONICAL.get(target)
    if (canonical and native.valid and proof
            and native.observed_decision(target, proof) == 'registered'
            and native.observed_decision(canonical, proof) == 'registered'):
        return canonical
    return None


def _check(cancel):
    if cancel.is_set():
        raise GuardError('CANCELLED', 'Подготовка формата изображения отменена.', stage='result')


def _chunk(kind, data):
    return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)


def _static_apng(source, output, *, cancel):
    info = inspect_media(source, cancel=cancel)
    if not info or info['format'] != 'png' or info['frames'] != 1:
        raise GuardError('NATIVE_ALIAS_INVALID', 'Не создано статичное изображение PNG для APNG.', stage='result')
    partial = output.with_name(output.name + '.part')
    created = False
    destination_written = False
    try:
        with source.open('rb') as reader, partial.open('xb') as writer:
            created = True
            writer.write(reader.read(33))  # PNG signature and unchanged IHDR.
            writer.write(_chunk(b'acTL', struct.pack('>II', 1, 0)))
            writer.write(_chunk(b'fcTL', struct.pack('>IIIIIHHBB',
                0, info['width'], info['height'], 0, 0, 0, 1000, 0, 0)))
            while header := reader.read(8):
                _check(cancel)
                if len(header) != 8:
                    raise GuardError('NATIVE_ALIAS_INVALID', 'Повреждён основной PNG.', stage='result')
                length, kind = struct.unpack('>I4s', header)
                if kind in (b'acTL', b'fcTL', b'fdAT'):
                    raise GuardError('NATIVE_ALIAS_INVALID', 'Ожидался статичный PNG.', stage='result')
                writer.write(header)
                remaining = length + 4  # Original payload and original CRC.
                while remaining:
                    _check(cancel)
                    block = reader.read(min(65536, remaining))
                    if not block:
                        raise GuardError('NATIVE_ALIAS_INVALID', 'Основной PNG обрезан.', stage='result')
                    writer.write(block)
                    remaining -= len(block)
        if output.exists():
            raise GuardError('ENGINE_OUTPUT_COLLISION', 'Совпали имена результатов.', stage='result')
        os.replace(partial, output)
        destination_written = True
        # Inspect the final requested container, including both new CRCs.
        inspect_media(output, cancel=cancel)
        source.unlink()
        return output
    except BaseException:
        if destination_written:
            output.unlink(missing_ok=True)
        raise
    finally:
        if created:
            partial.unlink(missing_ok=True)


def wrap_native_aliases(paths, target, *, cancel):
    canonical = CANONICAL.get(target)
    if not canonical:
        return paths, None
    changed = []
    count = 0
    for item in paths:
        _check(cancel)
        path = Path(item)
        if path.suffix.lower() not in (('.png',) if canonical == 'png' else ('.jpg', '.jpeg')):
            changed.append(path)
            continue
        output = path.with_suffix('.' + target)
        if output.exists():
            raise GuardError('ENGINE_OUTPUT_COLLISION', 'Совпали имена результатов.', stage='result')
        if target == 'apng':
            path = _static_apng(path, output, cancel=cancel)
        else:
            info = inspect_media(path, cancel=cancel)
            if not info or info['format'] != 'jpeg':
                raise GuardError('NATIVE_ALIAS_INVALID', 'Не создано изображение JPEG.', stage='result')
            path.rename(output)
            path = output
        changed.append(path)
        count += 1
    if not count:
        raise GuardError('ENGINE_OUTPUT_MISSING', 'Не создан основной результат изображения.', stage='result')
    return changed, {'check': 'native_image_alias', 'status': 'validated',
        'requestedFormat': target, 'nativeFormat': canonical, 'files': count,
        'pixelPayloadPreserved': True, 'singleFrameAnimationContainer': target == 'apng',
        'qualityCompared': False}
