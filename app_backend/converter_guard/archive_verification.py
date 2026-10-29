"""Read back an AIO-built ZIP before publishing its ticket; never extract it.

Expected hashes come from retained exports, not from the ZIP's own manifest.
The receipt proves archive bytes only, not a user's external save or fidelity.
"""
import hashlib
import os
from pathlib import Path
import re
import stat
import zipfile

from .errors import GuardError
from .html_resources import resource_path
from .result_store import path_is_link

CHUNK = 64 * 1024
METADATA = frozenset(('AIO-conversion-report.json', 'AIO-results-manifest.json'))


def _stop(cancel):
    if cancel is not None and cancel.is_set():
        raise GuardError('BUNDLE_CANCELLED', 'Проверка архива отменена.', stage='bundle')


def _invalid(reason):
    raise GuardError('BUNDLE_VERIFY_FAILED',
        'Архив не прошёл проверку. Отдельные результаты сохранены в AIO.',
        stage='bundle', diagnostic='archive_readback:' + reason)


def _token(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _hash(stream, cancel):
    digest, size = hashlib.sha256(), 0
    while True:
        _stop(cancel)
        block = stream.read(CHUNK)
        if not block:
            break
        digest.update(block)
        size += len(block)
    return size, digest.hexdigest()


def verify_built_archive(path, entries, metadata, snapshot, *, cancel=None):
    """Return (size, SHA-256, receipt) only for the exact expected ZIP members.

    Payload reads are streamed, including ZIP64 and empty outputs. zipfile's
    central directory and the expected inventory remain proportional to the
    number of outputs. No source document is reopened or converted here.
    """
    _stop(cancel)
    if (not isinstance(metadata, dict) or set(metadata) != METADATA or
            any(not isinstance(value, bytes) for value in metadata.values()) or
            not isinstance(snapshot, dict) or
            not re.fullmatch('[0-9a-f]{32}', str(snapshot.get('snapshotId'))) or
            type(snapshot.get('snapshotRevision')) is not int or snapshot['snapshotRevision'] < 0):
        _invalid('snapshot_metadata')
    expected, canonical = {}, set()
    for entry in entries:
        _stop(cancel)
        if not isinstance(entry, dict):
            _invalid('entry_metadata')
        name = entry.get('path')
        try:
            key = resource_path(name).casefold()
        except GuardError:
            _invalid('entry_path')
        if (key in canonical or key in {n.casefold() for n in METADATA} or
                type(entry.get('size')) is not int or entry['size'] < 0 or
                not re.fullmatch('[0-9a-f]{64}', str(entry.get('sha256'))) or
                entry.get('exportSha256') not in (None, entry['sha256']) or
                entry.get('copySha256') not in (None, entry['sha256'])):
            _invalid('entry_metadata')
        canonical.add(key)
        expected[name] = (entry['size'], entry['sha256'])
    names = canonical | {n.casefold() for n in METADATA}
    if not expected or any('/'.join(name.split('/')[:i]) in names
                           for name in names for i in range(1, len(name.split('/')))):
        _invalid('entry_collision')
    output_count, output_bytes = len(expected), sum(value[0] for value in expected.values())
    for name, content in metadata.items():
        expected[name] = (len(content), hashlib.sha256(content).hexdigest())

    path = Path(path)
    descriptor = None
    try:
        if path_is_link(path):
            _invalid('container_link')
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0) |
                             getattr(os, 'O_NOFOLLOW', 0))
        stream = os.fdopen(descriptor, 'rb')
        descriptor = None
        with stream:
            initial = os.fstat(stream.fileno())
            if (not stat.S_ISREG(initial.st_mode) or
                    getattr(initial, 'st_file_attributes', 0) &
                    getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)):
                _invalid('container_type')
            with zipfile.ZipFile(stream, 'r') as archive:
                infos = archive.infolist()
                if len(infos) != len(expected) or {info.filename for info in infos} != set(expected):
                    _invalid('member_set')
                for info in infos:
                    _stop(cancel)
                    size, digest = expected[info.filename]
                    if (info.is_dir() or info.flag_bits & 1 or
                            info.compress_type != zipfile.ZIP_STORED or
                            stat.S_ISLNK(info.external_attr >> 16) or
                            info.file_size != size or info.compress_size != size):
                        _invalid('member_header')
                    with archive.open(info, 'r') as member:
                        actual = _hash(member, cancel)
                    if actual != (size, digest):
                        _invalid('member_bytes')
            # Keep the prior whole-container digest, on the SAME handle whose
            # members were read back; a second pathname open cannot substitute
            # a different ZIP between entry verification and fingerprinting.
            stream.seek(0)
            actual_size, digest = _hash(stream, cancel)
            _stop(cancel)
            final = os.fstat(stream.fileno())
            current = path.stat(follow_symlinks=False)
            if (path_is_link(path) or _token(initial) != _token(final) or
                    _token(final) != _token(current) or actual_size != initial.st_size):
                _invalid('container_changed')
        return actual_size, digest, {
            'check': 'archive_contents', 'status': 'byte_verified',
            'snapshotId': snapshot['snapshotId'],
            'snapshotRevision': snapshot['snapshotRevision'],
            'verificationScope': 'owned_zip_members_before_download',
            'verificationMethod': 'independent_zip_entry_sha256',
            'outputsVerified': output_count, 'bytesVerified': output_bytes,
            'metadataVerified': len(metadata), 'qualityCompared': False,
            'externalSaveVerified': False}
    except (OSError, EOFError, ValueError, RuntimeError, zipfile.BadZipFile) as error:
        _invalid('container_unreadable')
    finally:
        if descriptor is not None:
            os.close(descriptor)
