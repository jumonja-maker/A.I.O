"""Wrap complete native EMF/WMF exports with a verified GZIP container.

Some installed LibreOffice builds emit an incomplete DEFLATE payload for a
large EMZ/WMZ although the trailer describes the complete metafile. Never
recalculate that corrupt trailer: request the existing uncompressed native
export, validate its record boundaries, then compress those exact bytes.
"""
import gzip
import hashlib
import os
from pathlib import Path
import struct

from .errors import GuardError

COMPRESSED_METAFILES = {'emz': 'emf', 'wmz': 'wmf'}


def _cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise GuardError('CANCELLED', 'Обработка метафайла отменена.', stage='result')


def _invalid(reason):
    raise GuardError('ENGINE_OUTPUT_CONTAINER_INVALID',
        'LibreOffice создал неполный метафайл. Результат не отмечен готовым.',
        stage='result', diagnostic='native_metafile:' + reason)


def _stat_token(stat):
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def native_metafile_target(native, target, proof):
    """Use a native plain exporter only for the same admitted document model."""
    plain = COMPRESSED_METAFILES.get(target)
    if (plain and native.valid and proof and
            native.observed_decision(target, proof) == 'registered' and
            native.observed_decision(plain, proof) == 'registered'):
        return plain
    return None


def inspect_metafile(path, kind, *, cancel=None):
    """Validate bounded-memory record structure, declared size, count and EOF."""
    path = Path(path)
    if path.is_symlink():
        _invalid('symlink')
    with path.open('rb') as stream:
        initial = os.fstat(stream.fileno())
        size = initial.st_size
        header = stream.read(108)
        count, position = 0, 0
        declared_count = None
        if kind == 'emf':
            if (size < 108 or len(header) < 88 or
                    struct.unpack_from('<I', header)[0] != 1 or header[40:44] != b' EMF'):
                _invalid('emf_header')
            header_size = struct.unpack_from('<I', header, 4)[0]
            declared_size, declared_count = struct.unpack_from('<II', header, 48)
            if declared_size != size or header_size < 88 or header_size % 4:
                _invalid('emf_size')
            terminal = False
            while position < size:
                _cancel(cancel)
                stream.seek(position)
                record = stream.read(8)
                if len(record) != 8:
                    _invalid('emf_record_header')
                record_type, length = struct.unpack('<II', record)
                if length < 8 or length % 4 or position + length > size:
                    _invalid('emf_record_size')
                if count == 0 and (record_type != 1 or length != header_size):
                    _invalid('emf_first_record')
                count += 1
                position += length
                if record_type == 14:
                    if length < 20 or position != size:
                        _invalid('emf_eof')
                    terminal = True
            # Native LO counts records following EMR_HEADER in nRecords.
            # Record boundaries, complete declared byte count and terminal EOF
            # independently prove completeness; retain its bytes unchanged.
            if not terminal or declared_count not in (count, count - 1):
                _invalid('emf_count_or_eof')
        elif kind == 'wmf':
            placeable = header[:4] == b'\xd7\xcd\xc6\x9a'
            offset = 22 if placeable else 0
            if size < offset + 24 or len(header) < offset + 18:
                _invalid('wmf_header')
            if placeable:
                words = struct.unpack('<11H', header[:22])
                checksum = 0
                for word in words[:10]:
                    checksum ^= word
                if checksum != words[10]:
                    _invalid('wmf_placeable_checksum')
            file_type, header_words, version, file_words, objects, maximum, params = struct.unpack_from(
                '<HHHIHIH', header, offset)
            if (file_type not in (1, 2) or header_words != 9 or
                    version not in (0x100, 0x300) or params != 0 or
                    file_words * 2 != size - offset):
                _invalid('wmf_size_or_header')
            position = offset + 18
            terminal = False
            largest = 0
            while position < size:
                _cancel(cancel)
                stream.seek(position)
                record = stream.read(6)
                if len(record) != 6:
                    _invalid('wmf_record_header')
                words, function = struct.unpack('<IH', record)
                if words < 3 or position + words * 2 > size:
                    _invalid('wmf_record_size')
                position += words * 2
                largest = max(largest, words)
                count += 1
                if function == 0:
                    if words != 3 or position != size:
                        _invalid('wmf_eof')
                    terminal = True
            if not terminal or largest > maximum:
                _invalid('wmf_max_record_or_eof')
        else:
            raise ValueError('Unsupported plain metafile kind')
        end = os.fstat(stream.fileno())
        if _stat_token(initial) != _stat_token(end) or _stat_token(end) != _stat_token(path.stat()):
            _invalid('source_changed')
    return {'format': kind, 'bytes': size, 'records': count,
            **({'declaredRecords': declared_count,
                'nativeHeaderCountExcludesHeader': declared_count == count - 1}
               if declared_count is not None else {})}


def wrap_native_metafiles(paths, target, *, cancel=None):
    """Return compressed output paths and independent exact-byte evidence.

    Call only for the private output of the native plain export. Source files
    and corrupt compressed results are never inputs to this operation.
    """
    plain = COMPRESSED_METAFILES.get(target)
    if not plain:
        raise ValueError('Unsupported compressed metafile target')
    inputs = [Path(p) for p in paths]
    primaries = [p for p in inputs if p.suffix.lower() == '.' + plain]
    if not primaries:
        _invalid('primary_missing')
    outputs, evidence = list(inputs), []
    staged = []
    try:
        for source in primaries:
            snapshot = source.lstat()
            facts = inspect_metafile(source, plain, cancel=cancel)
            if _stat_token(snapshot) != _stat_token(source.lstat()):
                _invalid('source_changed')
            output = source.with_suffix('.' + target)
            temporary = output.with_name(output.name + '.partial')
            if output.exists() or temporary.exists():
                _invalid('output_collision')
            staged.append(temporary)
            original_hash = hashlib.sha256()
            original_size = 0
            with source.open('rb') as incoming, temporary.open('xb') as raw:
                initial = os.fstat(incoming.fileno())
                if _stat_token(initial) != _stat_token(snapshot):
                    _invalid('source_changed')
                with gzip.GzipFile(filename='', fileobj=raw, mode='wb', mtime=0) as compressed:
                    while True:
                        _cancel(cancel)
                        block = incoming.read(64 * 1024)
                        if not block:
                            break
                        original_hash.update(block)
                        original_size += len(block)
                        compressed.write(block)
                end = os.fstat(incoming.fileno())
                if (_stat_token(initial) != _stat_token(end) or
                        _stat_token(end) != _stat_token(source.lstat()) or
                        original_size != facts['bytes']):
                    _invalid('source_changed')
            expanded_hash = hashlib.sha256()
            expanded_size = 0
            with gzip.open(temporary, 'rb') as verified:
                while True:
                    _cancel(cancel)
                    block = verified.read(64 * 1024)
                    if not block:
                        break
                    expanded_hash.update(block)
                    expanded_size += len(block)
            if expanded_size != original_size or expanded_hash.digest() != original_hash.digest():
                _invalid('gzip_readback_mismatch')
            _cancel(cancel)
            os.replace(temporary, output)
            staged.remove(temporary)
            outputs[outputs.index(source)] = output
            evidence.append({**facts, 'sha256': original_hash.hexdigest()})
        # Remove only private intermediate plain exports after all containers
        # have passed readback; return no unrequested duplicate plain files.
        for source in primaries:
            source.unlink()
        return outputs, {'check': 'native_metafile_gzip', 'status': 'byte_preserved',
            'plainFormat': plain, 'containerFormat': target, 'files': len(evidence),
            'metafiles': evidence, 'qualityCompared': False}
    finally:
        for temporary in staged:
            temporary.unlink(missing_ok=True)
