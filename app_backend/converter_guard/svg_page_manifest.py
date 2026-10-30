"""M21: bind a multi-page SVG result to its saved page manifest.

This verifies only the already-produced, bounded JSON and file metadata.
It does not parse source documents, rescan SVG graphics, or claim fidelity.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat

from .errors import GuardError

_MAX_MANIFEST = 1024 * 1024
_MAX_PAGES = 4096


def _fail(reason):
    raise GuardError('ENGINE_OUTPUT_VALIDATION_INCOMPLETE',
                     'Не удалось подтвердить все страницы SVG и их порядок.',
                     stage='result', diagnostic='svg_pages:' + reason)


def _cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise GuardError('CANCELLED', 'Проверка страниц SVG отменена.', stage='result')


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate page-manifest key')
        result[key] = value
    return result


def _identity(value):
    return (value.st_dev, value.st_ino, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns)


def verify_svg_page_manifest(paths, output_dir, *, cancel=None):
    """Return a scoped receipt or None when a single SVG needs no manifest.

    The caller has already gathered native results; only direct children of
    its owned output folder can be counted as primary SVG page outputs.
    Other image resources and unrelated output formats are not affected.
    """
    _cancel(cancel)
    root = Path(output_dir)
    found = [Path(item) for item in paths]
    pages = [p for p in found if p.parent == root and p.suffix.lower() == '.svg']
    if not pages:
        return None
    manifests = [p for p in found if p.parent == root and p.name == 'AIO-pages.json']
    if not manifests:
        if len(pages) == 1:
            return None  # Native single-page routes retain their own receipt.
        _fail('missing_manifest')
    if len(manifests) != 1 or not 1 <= len(pages) <= _MAX_PAGES:
        _fail('manifest_or_page_count')
    manifest = manifests[0]
    try:
        before = manifest.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_MANIFEST:
            _fail('manifest_type_or_size')
        with manifest.open('rb') as stream:
            if _identity(os.fstat(stream.fileno())) != _identity(before):
                _fail('manifest_changed_before_read')
            raw = stream.read(_MAX_MANIFEST + 1)
        if len(raw) > _MAX_MANIFEST or _identity(manifest.lstat()) != _identity(before):
            _fail('manifest_changed_or_oversized')
    except OSError:
        _fail('manifest_unavailable')
    _cancel(cancel)
    try:
        info = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError):
        _fail('manifest_not_strict_json')
    if (not isinstance(info, dict) or set(info) != {'schema', 'pageCount', 'pages'}
            or type(info['schema']) is not int or info['schema'] != 1
            or type(info['pageCount']) is not int or info['pageCount'] != len(pages)
            or not isinstance(info['pages'], list) or len(info['pages']) != len(pages)):
        _fail('manifest_schema_or_count')

    names = [p.name for p in pages]
    if len(names) != len(set(name.casefold() for name in names)):
        _fail('duplicate_output_names')
    mapped = set()
    first = None
    for index, row in enumerate(info['pages'], 1):
        _cancel(cancel)
        if (not isinstance(row, dict) or set(row) != {'page', 'file'}
                or type(row['page']) is not int or row['page'] != index
                or not isinstance(row['file'], str)
                or not row['file'] or any(c in row['file'] for c in '/\\:')):
            _fail('manifest_page_mapping')
        name = row['file']
        if first is None:
            first = Path(name)
            if first.name != name or first.suffix.lower() != '.svg':
                _fail('manifest_primary_name')
        expected = (first.name if index == 1 else
                    first.stem + '__page_%04d' % index + first.suffix)
        if name != expected or name not in names or name in mapped:
            _fail('missing_repeated_or_reordered_page')
        mapped.add(name)
    if mapped != set(names):
        _fail('orphan_page_output')
    for page in pages:
        _cancel(cancel)
        try:
            item = page.lstat()
        except OSError:
            _fail('svg_page_unavailable')
        if not stat.S_ISREG(item.st_mode) or item.st_size <= 0:
            _fail('svg_page_missing_or_empty')
    return {'check': 'svg_page_manifest', 'status': 'checked_subset',
            'schemaVersion': 1, 'expectedUnits': len(pages),
            'observedOutputs': len(mapped),
            'pageManifest': manifest.name,
            'checkedFeatures': ['page_count', 'ordered_page_filenames', 'nonempty_page_files'],
            'notChecked': ['original_import_equivalence', 'svg_visual_equivalence',
                           'embedded_resource_integrity'],
            'qualityCompared': False}
