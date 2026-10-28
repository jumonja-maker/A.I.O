"""Private OOXML preparation for the native Writer plain-text exporter.

Some LO builds loop on the last table cell when a Word document ends in a
body-level table. Add ONE empty body paragraph in a disposable input copy,
not a renderer, text extraction or a modification to the retained original.
Only the caller's confirmed Writer Text/Text (encoded) recipe may use this.
"""
from contextlib import contextmanager
import copy
import hashlib
import os
from pathlib import Path
import shutil
from xml.etree import ElementTree as ET
from xml.parsers import expat
import zipfile

from .errors import GuardError

TEXT_FILTERS = frozenset(('Text', 'Text (encoded)'))
WORD = frozenset(('http://schemas.openxmlformats.org/wordprocessingml/2006/main',
                  'http://purl.oclc.org/ooxml/wordprocessingml/main'))
REL = 'http://schemas.openxmlformats.org/package/2006/relationships'
OFFICE_REL = frozenset((
    'http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument',
    'http://purl.oclc.org/ooxml/officeDocument/relationships/officeDocument'))
CHUNK = 65536
# These are only limits of the optional structure probe, not format/import limits.
PROBE_BYTES = 64 * 1024 * 1024


class _NotApplicable(ValueError):
    pass


def _check(cancel):
    if cancel.is_set():
        raise GuardError('CANCELLED', 'Подготовка текстового экспорта отменена.', stage='import')


def _stamp(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _main_part(archive, cancel):
    names = archive.namelist()
    if len(names) > 10000 or len(names) != len(set(names)) or '_rels/.rels' not in names:
        raise _NotApplicable()
    if any(name.lower().startswith('_xmlsignatures/') for name in names):
        raise _NotApplicable()  # Do not invalidate a document signature, even in a private copy.
    info = archive.getinfo('_rels/.rels')
    if info.file_size > 1024 * 1024:
        raise _NotApplicable()
    _check(cancel)
    data = archive.read(info)
    if b'\0' in data or b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise _NotApplicable()
    root = ET.fromstring(data)
    if root.tag != '{' + REL + '}Relationships':
        raise _NotApplicable()
    mains = [r for r in root if r.tag == '{' + REL + '}Relationship'
             and r.get('Type') in OFFICE_REL]
    if len(mains) != 1 or mains[0].get('TargetMode', 'Internal') != 'Internal':
        raise _NotApplicable()
    target = mains[0].get('Target', '')
    # Never extract archive member names or resolve an external URI. Complex
    # escaped aliases are left to the existing native importer, not guessed.
    if not target or any(c in target for c in '\\%?#:'):
        raise _NotApplicable()
    part = target[1:] if target.startswith('/') else target
    if any(x in ('', '.', '..') for x in part.split('/')) or part not in names:
        raise _NotApplicable()
    return part


def _terminal_table(stream, cancel):
    parser = expat.ParserCreate(namespace_separator='}')
    stack, state = [], {'body': 0, 'last': None, 'section': None, 'end': None, 'ns': None}
    digest, size = hashlib.sha256(), 0

    def reject(*_):
        raise _NotApplicable()

    def declaration(version, encoding, standalone):
        if encoding and encoding.lower().replace('-', '') not in ('utf8', 'usascii', 'ascii'):
            reject()

    def start(tag, attributes):
        stack.append(tag)
        if len(stack) > 128:
            reject()
        if len(stack) == 1:
            namespace, _, local = tag.rpartition('}')
            if namespace not in WORD or local != 'document':
                reject()
            state['ns'] = namespace
        elif len(stack) == 2 and tag == state['ns'] + '}body':
            state['body'] += 1
            if state['body'] != 1:
                reject()
        elif len(stack) == 3 and stack[-2] == state['ns'] + '}body':
            if state['section'] is not None:
                reject()  # Ambiguous/nonterminal section properties: do not edit.
            if tag == state['ns'] + '}sectPr':
                state['section'] = parser.CurrentByteIndex
            else:
                state['last'] = tag

    def end(tag):
        if len(stack) == 2 and tag == state['ns'] + '}body':
            state['end'] = parser.CurrentByteIndex
        stack.pop()

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.XmlDeclHandler = declaration
    parser.StartDoctypeDeclHandler = reject
    parser.EntityDeclHandler = reject
    parser.ExternalEntityRefHandler = reject
    while block := stream.read(CHUNK):
        _check(cancel)
        size += len(block)
        if size > PROBE_BYTES or b'\0' in block:
            reject()
        digest.update(block)
        parser.Parse(block, False)
    parser.Parse(b'', True)
    if state['body'] != 1 or state['last'] != state['ns'] + '}tbl' or state['end'] is None:
        return None
    offset = state['section'] if state['section'] is not None else state['end']
    addition = ('<aioTextTail:p xmlns:aioTextTail="' + state['ns'] + '"/>').encode('ascii')
    return offset, addition, digest.hexdigest()


def _prepare(source, workspace, cancel, ownership):
    _check(cancel)
    if source.is_symlink():
        raise GuardError('SOURCE_CHANGED_DURING_PREPARATION',
                         'Исходник перенаправлен во время подготовки.', stage='import')
    try:
        before = _stamp(source.stat())
        original = source.open('rb')
    except OSError:
        # Optional preparation must not replace the native missing-file error.
        return source, None
    with original:
        if original.read(4) != b'PK\x03\x04':
            return source, None
        original.seek(0)
        try:
            archive = zipfile.ZipFile(original)
        except zipfile.BadZipFile:
            return source, None
        with archive:
            try:
                main = _main_part(archive, cancel)
                with archive.open(main) as stream:
                    terminal = _terminal_table(stream, cancel)
            except (_NotApplicable, ET.ParseError, expat.ExpatError, zipfile.BadZipFile,
                    KeyError, NotImplementedError, RuntimeError):
                terminal = None
            if terminal is None:
                return source, None
            offset, addition, expected = terminal
            workspace.mkdir(parents=True, exist_ok=False)
            ownership[0] = True
            prepared = workspace / source.name
            digest, prepared_digest = hashlib.sha256(), hashlib.sha256()
            with zipfile.ZipFile(prepared, 'x') as output:
                output.comment = archive.comment
                for entry in archive.infolist():
                    _check(cancel)
                    info = copy.copy(entry)
                    is_main = entry.filename == main
                    if is_main:
                        info.file_size += len(addition)
                    with archive.open(entry) as src, output.open(info, 'w',
                            force_zip64=info.file_size >= zipfile.ZIP64_LIMIT) as dst:
                        position, inserted = 0, False
                        while block := src.read(CHUNK):
                            _check(cancel)
                            if is_main:
                                digest.update(block)
                                if not inserted and position <= offset <= position + len(block):
                                    split = offset - position
                                    payload = block[:split] + addition + block[split:]
                                    inserted = True
                                else:
                                    payload = block
                                position += len(block)
                                prepared_digest.update(payload)
                                dst.write(payload)
                            else:
                                dst.write(block)
                        if is_main and (not inserted or digest.hexdigest() != expected):
                            raise GuardError('SOURCE_CHANGED_DURING_PREPARATION',
                                'Исходник изменился во время подготовки TXT.', stage='import')
            if before != _stamp(os.fstat(original.fileno())) or before != _stamp(source.stat()):
                raise GuardError('SOURCE_CHANGED_DURING_PREPARATION',
                                 'Исходник изменился во время подготовки TXT.', stage='import')
    _check(cancel)
    return prepared, {'kind': 'writer_text_terminal_table', 'state': 'prepared',
        'operation': 'append_one_empty_body_paragraph', 'mainPart': main,
        'originalMainXmlSha256': expected,
        'preparedMainXmlSha256': prepared_digest.hexdigest(),
        'addedXmlBytes': len(addition), 'originalModified': False,
        'qualityCompared': False, 'warnings': ['NATIVE_WRITER_TEXT_TAIL_PARAGRAPH']}


@contextmanager
def prepare_writer_text_source(source, workspace, *, cancel):
    """Yield (private_source, preparation) with no open source streams.

    Non-OOXML, existing terminal paragraphs and unsupported optional probes
    retain their native path. Once a terminal table is confirmed, staging
    errors propagate: never silently fall back to the known looping input.
    """
    source, workspace = Path(source), Path(workspace)
    ownership = [False]
    try:
        yield _prepare(source, workspace, cancel, ownership)
    finally:
        if ownership[0]:
            # Closed native models/processes no longer need this private input.
            # Windows scanner locks defer leftovers to the owning job cleanup.
            shutil.rmtree(workspace, ignore_errors=True)
