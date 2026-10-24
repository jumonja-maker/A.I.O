"""Native vector page views for Draw/Impress HTML exports.

LibreOffice's drawing HTML filter can produce an empty shell. This route
exports a fresh native flat ODF, retains every page in a private single-page
snapshot, and asks the installed SVG exporter to render each page. HTML only
packages those native SVG pages; it never redraws or rasterizes the document.
"""
import hashlib
import html
import json
import os
from pathlib import Path
import shutil
import time
from urllib.parse import quote
from xml.parsers import expat

from ..libreoffice_engine import convert_path, LibreOfficeError
from .errors import GuardError
from .legacy_sheets import (_scope, _restore, _expanded, _secure_parser,
                            _tag_end, _copy_range, BLOCK)

OFFICE = 'urn:oasis:names:tc:opendocument:xmlns:office:1.0'
DRAW = 'urn:oasis:names:tc:opendocument:xmlns:drawing:1.0'
TEXT = 'urn:oasis:names:tc:opendocument:xmlns:text:1.0'
PRESENTATION = 'urn:oasis:names:tc:opendocument:xmlns:presentation:1.0'
XLINK = 'http://www.w3.org/1999/xlink'
DRAW_SERVICE = 'com.sun.star.drawing.DrawingDocument'
PRESENTATION_SERVICE = 'com.sun.star.presentation.PresentationDocument'
_SHAPES = frozenset(('rect', 'line', 'polyline', 'polygon', 'regular-polygon',
    'path', 'circle', 'ellipse', 'connector', 'custom-shape', 'frame', 'caption',
    'measure', 'control', 'page-thumbnail'))


def _cancel(cancel):
    if cancel.is_set():
        raise GuardError('CANCELLED', 'Экспорт страниц отменён.', stage='export')


def _invalid(reason, code='NATIVE_DRAW_SNAPSHOT_INVALID'):
    raise GuardError(code, 'Не удалось сохранить все страницы рисунка или презентации.',
                     stage='result', diagnostic=reason)


def _digest(path, cancel):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while block := stream.read(BLOCK):
            _cancel(cancel); digest.update(block)
    return digest.hexdigest()


def native_draw_html_plan(native, proof, target):
    """Require installed native flat-ODF and SVG filters for the observed model."""
    if (target not in ('html', 'htm', 'xhtml') or not native.valid or not proof
            or proof.get('state') != 'detected'
            or not getattr(native, 'fingerprint', None)
            or proof.get('registryFingerprint') != native.fingerprint):
        return None
    uncertain = getattr(native, 'uncertain_extensions', ())
    services = proof.get('documentServices', ())
    if PRESENTATION_SERVICE in services:
        # The whole-document Impress SVG filter adds its own active slideshow
        # runtime. Only a probed native per-page encoder can provide inert SVG.
        if (proof.get('basis') != 'native_preflight_uno_document' or
                'svg' not in proof.get('nativeGraphicExports', ())):
            return None
        service, flat, name, svg = (PRESENTATION_SERVICE, 'fodp',
            'OpenDocument Presentation Flat XML', 'impress_svg_Export')
    elif DRAW_SERVICE in services:
        service, flat, name, svg = (DRAW_SERVICE, 'fodg',
            'OpenDocument Drawing Flat XML', 'draw_svg_Export')
    else:
        return None
    if any(extension in uncertain for extension in (target, flat, 'svg')):
        return None
    source_filter, export_filter = native.filters.get(name), native.filters.get(svg)
    if (not source_filter or source_filter[0] != service
            or not {'IMPORT', 'EXPORT'} <= source_filter[2]
            or flat not in source_filter[1] or not export_filter
            or export_filter[0] != service or 'EXPORT' not in export_filter[2]
            or 'svg' not in export_filter[1]):
        return None
    return {'service': service, 'flatFormat': flat, 'flatFilter': name,
            'svgFilter': svg, 'sourcePages': proof.get('drawPages')}


def _index_pages(stream, tags, cancel):
    parser = _secure_parser()
    namespaces, stack, pages = {}, [], []
    opened, model = None, None

    def start(name, attrs):
        nonlocal opened, model
        _cancel(cancel)
        changed = _scope(attrs, namespaces)
        expanded = _expanded(name, namespaces)
        if not stack:
            values = {_expanded(key, namespaces): value for key, value in attrs.items()}
            model = values.get((OFFICE, 'mimetype'))
            if expanded != (OFFICE, 'document') or model not in (
                    'application/vnd.oasis.opendocument.graphics',
                    'application/vnd.oasis.opendocument.presentation'):
                _invalid('not_native_flat_draw_or_presentation')
        if len(stack) > 256:
            _invalid('native_xml_depth')
        if expanded == (DRAW, 'page') and stack and stack[-1][0] in (
                (OFFICE, 'drawing'), (OFFICE, 'presentation')):
            if opened is not None: _invalid('overlapping_draw_pages')
            offset = parser.CurrentByteIndex
            end, empty = _tag_end(tags, offset, cancel)
            opened = {'start': offset, 'openEnd': end, 'selfClosing': empty,
                      'depth': len(stack) + 1, 'shapeElements': 0, 'imageElements': 0}
        if opened is not None and not any(item[0] == (PRESENTATION, 'notes') for item in stack):
            if expanded[0] == DRAW and expanded[1] in _SHAPES:
                opened['shapeElements'] += 1
            if expanded == (DRAW, 'image'):
                opened['imageElements'] += 1
        for key, value in attrs.items():
            if _expanded(key, namespaces) == (XLINK, 'href') and value and not value.startswith(('#', 'data:')):
                if expanded not in ((DRAW, 'a'), (TEXT, 'a')):
                    _invalid('native_flat_external_resource', 'NATIVE_DRAW_RESOURCE_UNSUPPORTED')
        stack.append((expanded, changed))

    def end(name):
        nonlocal opened
        _cancel(cancel)
        if opened is not None and opened['depth'] == len(stack):
            if stack[-1][0] != (DRAW, 'page'):
                _invalid('invalid_native_draw_page_stack')
            opened['end'] = (opened['openEnd'] if opened['selfClosing'] else
                             _tag_end(tags, parser.CurrentByteIndex, cancel)[0])
            pages.append(opened); opened = None
        _, changed = stack.pop()
        _restore(changed, namespaces)

    parser.StartElementHandler, parser.EndElementHandler = start, end
    try:
        while block := stream.read(BLOCK):
            _cancel(cancel); parser.Parse(block, False)
        parser.Parse(b'', True)
    except expat.ExpatError as error:
        _invalid('native_flat_parse:' + str(error.code))
    if not pages or opened is not None:
        _invalid('native_draw_pages_missing')
    return pages, model


def prepare_draw_page_snapshots(native_flat_odf, output, *, stem=None, cancel):
    """Copy native XML bytes, dropping only the other top-level draw:page nodes.

    Styles, master pages, geometry and embedded media remain unchanged. The
    original must be a fresh LibreOffice flat-ODF export, not a guessed source.
    """
    native_flat_odf, output = Path(native_flat_odf), Path(output)
    stem = native_flat_odf.stem if stem is None else stem
    if not isinstance(stem, str) or not stem or Path(stem).name != stem or stem in ('.', '..'):
        _invalid('snapshot_filename')
    _cancel(cancel)
    output.mkdir(parents=True, exist_ok=False)
    try:
        with native_flat_odf.open('rb') as source, native_flat_odf.open('rb') as scan, native_flat_odf.open('rb') as tags:
            initial = os.fstat(source.fileno())
            pages, model = _index_pages(scan, tags, cancel)
            extension = '.fodg' if model.endswith('graphics') else '.fodp'
            paths = []
            for index, selected in enumerate(pages, 1):
                _cancel(cancel)
                path = output / (stem + '__page_%04d' % index + extension)
                with path.open('xb') as destination:
                    position = 0
                    for page in pages:
                        _copy_range(source, destination, position, page['start'], cancel)
                        if page is selected:
                            _copy_range(source, destination, page['start'], page['end'], cancel)
                        position = page['end']
                    _copy_range(source, destination, position, initial.st_size, cancel)
                paths.append(path)
            final = os.fstat(source.fileno())
            if any(getattr(initial, field) != getattr(final, field)
                   for field in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')):
                _invalid('native_flat_file_changed')
        return paths, {'check': 'native_draw_all_pages', 'status': 'snapshot_prepared',
            'pageCount': len(pages), 'nativeSnapshotSha256': _digest(native_flat_odf, cancel),
            'pages': [{'page': index, 'contentExpected': bool(page['shapeElements']),
                       'shapeElements': page['shapeElements'], 'imageElements': page['imageElements']}
                      for index, page in enumerate(pages, 1)], 'qualityCompared': False}
    except BaseException as error:
        shutil.rmtree(output, ignore_errors=True)
        if isinstance(error, GuardError) and error.code == 'NATIVE_TABLE_SNAPSHOT_INVALID':
            _invalid('native_flat_xml_structure')
        raise


def wrap_svg_pages(svg_paths, target, output, *, stem, cancel, expected_pages=None):
    """Package verified native SVGs in order, retaining separate vector pages."""
    from .svg_page_validation import inspect_svg_page, sanitize_native_svg_doctype
    if target not in ('html', 'htm', 'xhtml') or not svg_paths:
        _invalid('html_page_wrapper_arguments')
    if not stem or Path(stem).name != stem or stem in ('.', '..'):
        _invalid('html_page_wrapper_filename')
    if expected_pages is not None and len(svg_paths) != len(expected_pages):
        _invalid('native_svg_page_count', 'NATIVE_DRAW_PAGE_COUNT_MISMATCH')
    output = Path(output)
    _cancel(cancel)
    output.mkdir(parents=True, exist_ok=False)
    try:
        assets = output / (stem + '_pages')
        assets.mkdir()
        paths, records = [], []
        for index, source in enumerate(svg_paths, 1):
            _cancel(cancel)
            expected = True if expected_pages is None else expected_pages[index - 1].get('contentExpected', True)
            destination = assets / ('page_%04d.svg' % index)
            copy_info = sanitize_native_svg_doctype(source, destination, cancel=cancel)
            page = inspect_svg_page(destination, cancel=cancel, content_expected=expected)
            digest = copy_info['packagedSha256']
            records.append({'page': index, 'path': destination.relative_to(output).as_posix(),
                            'sha256': digest, **copy_info, **page})
            paths.append(destination)
        primary = output / (stem + '.' + target)
        with primary.open('w', encoding='utf-8', newline='\n') as writer:
            writer.write('<?xml version="1.0" encoding="UTF-8"?>\n' if target == 'xhtml' else '<!DOCTYPE html>\n')
            writer.write('<html xmlns="http://www.w3.org/1999/xhtml"><head><meta charset="utf-8" />'
                '<title>' + html.escape(stem) + '</title><style>body{margin:0;background:#ddd}'
                '.aio-page{margin:1rem auto;background:white;page-break-after:always;max-width:100%}'
                '.aio-page object{display:block;width:100%;height:100%;border:0}'
                '@media print{body{background:white}.aio-page{margin:0}}</style></head><body>\n')
            for record in records:
                writer.write('<div class="aio-page" data-page="' + str(record['page']) + '"')
                # Geometry comes from the native SVG, preserving its page ratio.
                width, height = record.get('widthCss'), record.get('heightCss')
                if width and height:
                    writer.write(' style="width:' + html.escape(str(width), quote=True) + ';height:' +
                                 html.escape(str(height), quote=True) + '"')
                writer.write('><object type="image/svg+xml" data="' + quote(record['path'], safe='/') +
                    '" aria-label="' + str(record['page']) + '"></object></div>\n')
            writer.write('</body></html>\n')
        return [primary, *paths], {'check': 'native_draw_svg_html', 'status': 'all_pages_packaged',
            'pageCount': len(records), 'pages': records, 'rasterizedByAio': False,
            'externalResourcesFetched': False, 'qualityCompared': False}
    except BaseException:
        shutil.rmtree(output, ignore_errors=True)
        raise


def convert_native_draw_html(source, target, workspace, *, native, proof, executable,
                             prepare_profile, cancel, on_started, import_filter=None,
                             office=None, on_stage=lambda stage: None):
    """Run the same installed office for import, flat snapshots and page SVGs."""
    plan = native_draw_html_plan(native, proof, target)
    if plan is None:
        return None
    source, workspace = Path(source), Path(workspace)
    metrics, started = [], time.monotonic()
    if office is not None and not office.disabled and 'svg' in proof.get('nativeGraphicExports', ()):
        intermediate = workspace / 'native-svg-pages'
        intermediate.mkdir(parents=True, exist_ok=False)
        _cancel(cancel)
        on_started()
        deadline = getattr(cancel, 'deadline', None)
        remaining = None if deadline is None else max(.001, deadline - time.monotonic())
        result = office.request({'operation': 'export', 'source': str(source.resolve()),
            'format': 'svg', 'output': str(intermediate.resolve()),
            'exportFilter': plan['svgFilter'], 'graphicExport': 'svg_pages',
            'importFilter': import_filter, 'expectedImportFilter': import_filter,
            'requiredDrawPages': plan['sourcePages'], 'services': sorted(native.import_services),
            'expectedServices': [plan['service']],
            'expectedSourceServices': proof.get('sourceContract', {}).get('expectedServices', [])},
            cancel=cancel, timeout=remaining, on_stage=on_stage)
        if not result.get('ok'):
            _invalid('native_page_svg_export:' + str(result.get('code')), result.get('code') or 'ENGINE_FAILED')
        name = result.get('svgPageManifest')
        if not isinstance(name, str) or Path(name).name != name or not name.endswith('.aio-svg-pages.jsonl'):
            _invalid('native_page_manifest_missing')
        manifest = intermediate / name
        expected, svgs = [], []
        with manifest.open(encoding='utf-8') as stream:
            for line in stream:
                _cancel(cancel)
                if len(line) > 8192: _invalid('native_page_manifest_record')
                record = json.loads(line)
                if record.get('page') != len(expected) + 1:
                    _invalid('native_page_manifest_order')
                name = record.get('file')
                if not isinstance(name, str) or Path(name).name != name or Path(name).suffix.lower() != '.svg':
                    _invalid('native_page_manifest_path')
                svgs.append(intermediate / name); expected.append(record)
        expected_count = plan['sourcePages']
        if (not svgs or result.get('drawPagesCount') != len(svgs) or
                type(expected_count) is int and len(svgs) != expected_count):
            _invalid('native_page_manifest_count', 'NATIVE_DRAW_PAGE_COUNT_MISMATCH')
        paths, check = wrap_svg_pages(svgs, target, workspace / 'output', stem=source.stem,
                                      cancel=cancel, expected_pages=expected)
        check.update({'sourceSha256': _digest(source, cancel),
            'nativeFilters': ['com.sun.star.drawing.GraphicExportFilter:image/svg+xml'],
            'nativePageApi': True})
        timing = {**(result.get('timings') or {}), 'source': 'uno_document',
                  'adapterMs': round((time.monotonic() - started) * 1000)}
        return paths, check, timing
    if plan['service'] == PRESENTATION_SERVICE:
        _invalid('native_page_svg_api_unavailable', 'NATIVE_PAGE_SVG_API_UNAVAILABLE')

    def export(path, fmt, name, folder, hint):
        _cancel(cancel)
        prepare_profile(folder / 'profile')
        try:
            return convert_path(path, fmt, folder, executable=executable, cancel=cancel,
                on_started=on_started, import_filter=hint, convert_spec=fmt + ':' + name,
                metrics_sink=lambda metric: metrics.append(metric))
        except LibreOfficeError as error:
            raise GuardError(error.code, str(error), stage='export', diagnostic=error.diagnostic) from error

    initial = export(source, plan['flatFormat'], plan['flatFilter'],
                     workspace / 'native-draw-snapshot', import_filter)
    snapshot = next((path for path in initial if path.suffix.lower() == '.' + plan['flatFormat']), None)
    if snapshot is None: _invalid('native_flat_snapshot_missing')
    pages, snapshot_check = prepare_draw_page_snapshots(snapshot, workspace / 'native-page-inputs',
                                                       stem=source.stem, cancel=cancel)
    if type(plan['sourcePages']) is int and plan['sourcePages'] > 0 and len(pages) != plan['sourcePages']:
        _invalid('observed_vs_snapshot_page_count', 'NATIVE_DRAW_PAGE_COUNT_MISMATCH')
    svgs = []
    for index, page in enumerate(pages, 1):
        outputs = export(page, 'svg', plan['svgFilter'], workspace / ('native-svg-%04d' % index), plan['flatFilter'])
        primary = [path for path in outputs if path.suffix.lower() == '.svg']
        if len(primary) != 1: _invalid('native_svg_primary_ambiguous')
        svgs.append(primary[0])
    paths, check = wrap_svg_pages(svgs, target, workspace / 'output', stem=source.stem,
        cancel=cancel, expected_pages=snapshot_check['pages'])
    check['nativeSnapshotSha256'] = snapshot_check['nativeSnapshotSha256']
    check['nativeFilters'] = [plan['flatFilter'], plan['svgFilter']]
    timing = {'source': 'cli_process', 'adapterMs': round((time.monotonic() - started) * 1000),
              'processMs': sum(item.get('processMs', 0) for item in metrics)}
    return paths, check, timing
