"""Wrap complete native EMF/WMF exports with a verified GZIP container.

Some installed LibreOffice builds emit an incomplete DEFLATE payload for a
large EMZ/WMZ although the trailer describes the complete metafile. Never
recalculate that corrupt trailer: request the existing uncompressed native
export, validate its record boundaries, then compress those exact bytes.
"""
import gzip
import hashlib
import json
import stat
import tempfile
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
        contents = hashlib.sha256()
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
                # The structure walk already visits each record. Hash its
                # body here to detect same-size in-place mutations even on
                # filesystems whose mtime has coarse resolution.
                contents.update(record)
                unread = length - 8
                while unread:
                    _cancel(cancel)
                    part = stream.read(min(unread, 64 * 1024))
                    if not part:
                        _invalid('emf_record_truncated')
                    contents.update(part)
                    unread -= len(part)
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
            contents.update(header[:position])
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
                contents.update(record)
                unread = words * 2 - 6
                while unread:
                    _cancel(cancel)
                    part = stream.read(min(unread, 64 * 1024))
                    if not part:
                        _invalid('wmf_record_truncated')
                    contents.update(part)
                    unread -= len(part)
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
            'sha256': contents.hexdigest(),
            **({'declaredRecords': declared_count,
                'nativeHeaderCountExcludesHeader': declared_count == count - 1}
               if declared_count is not None else {})}


# Matches the native page producer's existing bounds, not a new Guard scan.
_PAGE_MANIFEST_LIMIT = 1024 * 1024
_METAFILE_PAGE_LIMIT = 4096


def _unique_manifest_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate manifest key')
        value[key] = item
    return value


def _retarget_page_manifest(inputs, primaries, replacements):
    """Bind the existing schema-1 page list to supplied outputs, never its paths."""
    manifests = [p for p in inputs if p.name == 'AIO-pages.json']
    if not manifests:
        if len(primaries) > 1:
            _invalid('page_manifest_missing')
        return None
    if len(manifests) != 1:
        _invalid('page_manifest_duplicate')
    path = manifests[0]
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > _PAGE_MANIFEST_LIMIT:
        _invalid('page_manifest_type_or_budget')
    with path.open('rb') as stream:
        if _stat_token(os.fstat(stream.fileno())) != _stat_token(before):
            _invalid('page_manifest_changed')
        raw = stream.read(_PAGE_MANIFEST_LIMIT + 1)
    if len(raw) > _PAGE_MANIFEST_LIMIT or _stat_token(path.lstat()) != _stat_token(before):
        _invalid('page_manifest_changed_or_budget')
    try:
        data = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_manifest_object)
    except (ValueError, UnicodeError, RecursionError):
        _invalid('page_manifest_json')
    if (not isinstance(data, dict) or set(data) != {'schema', 'pageCount', 'pages'}
            or type(data['schema']) is not int or data['schema'] != 1
            or type(data['pageCount']) is not int or data['pageCount'] != len(primaries)
            or not isinstance(data['pages'], list) or len(data['pages']) != len(primaries)):
        _invalid('page_manifest_schema_or_count')
    parent = os.path.abspath(path.parent)
    names = {p.name: replacements[p].name for p in primaries}
    if (len(names) != len(primaries)
            or any(os.path.abspath(p.parent) != parent for p in primaries)):
        _invalid('page_manifest_output_scope')
    seen, rows = set(), []
    for number, row in enumerate(data['pages'], 1):
        if (not isinstance(row, dict) or set(row) != {'page', 'file'}
                or type(row['page']) is not int or row['page'] != number
                or not isinstance(row['file'], str) or row['file'] not in names
                or row['file'] in seen or any(c in row['file'] for c in '/\\:')):
            _invalid('page_manifest_mapping')
        seen.add(row['file'])
        rows.append({'page': number, 'file': names[row['file']]})
    return path, before, json.dumps(
        {'schema': 1, 'pageCount': len(rows), 'pages': rows}, ensure_ascii=False).encode('utf-8')


def _remove_created(entries):
    """Best-effort rollback of files exclusively created by this invocation."""
    for path, identity in reversed(entries):
        try:
            current = path.lstat()
            if stat.S_ISREG(current.st_mode) and (current.st_dev, current.st_ino) == identity:
                path.unlink()
        except OSError:
            pass  # Any locked leftovers stay charged to the owning workspace.


def wrap_native_metafiles(paths, target, *, cancel=None):
    """Compress complete native vectors and retarget their page list together.

    Files live in a private, unpublished task workspace. Keep every native
    intermediate and the old manifest until ALL gzip readbacks pass. On an
    earlier failure, remove only our exclusively created containers. Final
    manifest replacement/intermediate deletion is not a filesystem transaction:
    a cleanup error is still an error, and valid containers remain in the owned
    workspace rather than being destroyed after their plain originals are gone.
    """
    plain = COMPRESSED_METAFILES.get(target)
    if not plain:
        raise ValueError('Unsupported compressed metafile target')
    _cancel(cancel)
    inputs = [Path(p) for p in paths]
    keys = [os.path.abspath(p).casefold() for p in inputs]
    if len(keys) != len(set(keys)):
        _invalid('duplicate_input')
    primaries = [p for p in inputs if p.suffix.lower() == '.' + plain]
    if not primaries:
        _invalid('primary_missing')
    if len(primaries) > _METAFILE_PAGE_LIMIT:
        _invalid('page_budget')
    replacements = {p: p.with_suffix('.' + target) for p in primaries}
    manifest = _retarget_page_manifest(inputs, primaries, replacements)
    outputs = [replacements.get(p, p) for p in inputs]
    evidence, owned, snapshots = [], [], []
    manifest_temp = []
    committing = False
    try:
        for source, output in replacements.items():
            _cancel(cancel)
            snapshot = source.lstat()
            facts = inspect_metafile(source, plain, cancel=cancel)
            if _stat_token(snapshot) != _stat_token(source.lstat()):
                _invalid('source_changed')
            snapshots.append((source, snapshot))
            # Exclusive final names eliminate the old exists()+replace() race.
            # These private bytes are not returned to Guard before readback.
            if os.path.lexists(output.with_name(output.name + '.partial')):
                _invalid('output_collision')
            original_hash = hashlib.sha256()
            original_size = 0
            with source.open('rb') as incoming:
                initial = os.fstat(incoming.fileno())
                if _stat_token(initial) != _stat_token(snapshot):
                    _invalid('source_changed')
                try:
                    raw = output.open('xb')
                except FileExistsError:
                    _invalid('output_collision')
                with raw:
                    created = os.fstat(raw.fileno())
                    owned.append((output, (created.st_dev, created.st_ino)))
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
                if (_stat_token(initial) != _stat_token(end)
                        or _stat_token(end) != _stat_token(source.lstat())
                        or original_size != facts['bytes']
                        or original_hash.hexdigest() != facts['sha256']):
                    _invalid('source_changed')
            expanded_hash = hashlib.sha256()
            expanded_size = 0
            with gzip.open(output, 'rb') as verified:
                while True:
                    _cancel(cancel)
                    block = verified.read(64 * 1024)
                    if not block:
                        break
                    expanded_hash.update(block)
                    expanded_size += len(block)
                    if expanded_size > original_size:
                        _invalid('gzip_readback_mismatch')
            if expanded_size != original_size or expanded_hash.digest() != original_hash.digest():
                _invalid('gzip_readback_mismatch')
            evidence.append({**facts, 'file': output.name, 'sha256': original_hash.hexdigest()})
        if manifest:
            path, before, payload = manifest
            fd, name = tempfile.mkstemp(prefix='.aio-pages-', suffix='.partial', dir=path.parent)
            temporary = Path(name)
            with os.fdopen(fd, 'wb') as stream:
                created = os.fstat(stream.fileno())
                manifest_temp.append((temporary, (created.st_dev, created.st_ino)))
                stream.write(payload)
            if temporary.read_bytes() != payload:
                _invalid('page_manifest_readback')
        # Cheap identity checks only; no additional native import or full scan.
        for source, snapshot in snapshots:
            _cancel(cancel)
            if _stat_token(source.lstat()) != _stat_token(snapshot):
                _invalid('source_changed')
        if manifest:
            if _stat_token(path.lstat()) != _stat_token(before):
                _invalid('page_manifest_changed')
            _cancel(cancel)
            os.replace(temporary, path)  # Replace only the validated native sidecar.
        committing = True
        for source in primaries:
            _cancel(cancel)
            source.unlink()
        return outputs, {'check': 'native_metafile_gzip', 'status': 'byte_preserved',
            'plainFormat': plain, 'containerFormat': target, 'files': len(evidence),
            'metafiles': evidence, 'qualityCompared': False,
            **({'pageManifest': {'file': path.name, 'pageCount': len(primaries)}} if manifest else {})}
    finally:
        _remove_created(manifest_temp)
        if not committing:
            _remove_created(owned)
