"""Real Guard preflight/queue/save acceptance for the v17 repaired routes.

Uses the owner's original subset when supplied and generated controls with
independent content or container checks. Unsupported requested routes remain
INCOMPLETE. This is bounded native acceptance, not a full visual certification.
"""
from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import gzip
import hashlib
import json
from pathlib import Path
import re
import sys
import threading
import time
import zipfile
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
from app_backend.converter_guard import ConverterGuard, GuardLimits, LibreOfficeAdapter
from app_backend.converter_guard.adapter import prepare_profile, _RunControl
from app_backend.converter_guard.media_fidelity import inspect_media
from app_backend.converter_guard.native_metafile import inspect_metafile
from app_backend.libreoffice_engine import convert_path
from guard_v16_bugbag_acceptance import writer_seed, output_text, OFFICE, TABLE


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def normal(text):
    return re.sub(r'\s+', ' ', text).strip()


def odf_xml(path):
    if path.suffix == '.fods':
        return ET.fromstring(path.read_bytes())
    with zipfile.ZipFile(path) as archive:
        return ET.fromstring(archive.read('content.xml'))


def writer_markers(path):
    cells = odf_xml(path).findall('.//{%s}table-cell' % TABLE)
    return [normal(''.join(cell.itertext())) for cell in cells if normal(''.join(cell.itertext()))]


def table_markers(path):
    tables = odf_xml(path).findall('.//{%s}table' % TABLE)
    result = []
    for table in tables:
        markers = []
        for row in table.findall('{%s}table-row' % TABLE)[1:]:
            for cell in row.findall('{%s}table-cell' % TABLE):
                if cell.get('{%s}value-type' % OFFICE) == 'string':
                    text = normal(''.join(cell.itertext()))
                    if text:
                        markers.append(text)
        result.append(markers)
    return result


def numeric_pairs(path):
    """Cached numeric controls keyed by an adjacent nonempty string cell."""
    result = []
    for table in odf_xml(path).findall('.//{%s}table' % TABLE):
        pairs = []
        for row in table.findall('{%s}table-row' % TABLE)[1:]:
            cells = row.findall('{%s}table-cell' % TABLE)
            if (len(cells) >= 2 and cells[0].get('{%s}value-type' % OFFICE) == 'string' and
                    cells[1].get('{%s}value-type' % OFFICE) == 'float'):
                marker = normal(''.join(cells[0].itertext()))
                value = cells[1].get('{%s}value' % OFFICE)
                if marker and value is not None:
                    pairs.append((marker, Decimal(value)))
        result.append(pairs)
    return result


def primary_table_outputs(paths, target, expected_count):
    """Memo/index companions are retained outputs, not additional sheets."""
    primaries = [path for path in paths if path.suffix.lower() == '.' + target]
    if len(primaries) != expected_count:
        raise AssertionError('Expected %d %s sheet primaries, received %d; all retained outputs=%r' %
                             (expected_count, target, len(primaries), [path.name for path in paths]))
    return primaries, [path for path in paths if path not in primaries]


def mixed_numeric_control_column(path, sheet_index):
    table = odf_xml(path).findall('.//{%s}table' % TABLE)[sheet_index]
    kinds = set()
    for row in table.findall('{%s}table-row' % TABLE)[1:]:
        cells = row.findall('{%s}table-cell' % TABLE)
        if len(cells) < 2:
            continue
        kind = cells[1].get('{%s}value-type' % OFFICE)
        if kind == 'string' and normal(''.join(cells[1].itertext())):
            kinds.add('string')
        elif kind in ('float', 'currency', 'percentage'):
            kinds.add('numeric')
    return kinds == {'string', 'numeric'}


def cell_display(cell):
    paragraphs = cell.findall('.//{urn:oasis:names:tc:opendocument:xmlns:text:1.0}p')
    return '\n'.join(''.join(paragraph.itertext()) for paragraph in paragraphs)


def dbf_numeric_controls(source, sheet_index, primary):
    """Compare typed recovery metadata against the original source cells."""
    table = odf_xml(source).findall('.//{%s}table' % TABLE)[sheet_index]
    controls, source_displays = {}, {}
    row_index = 0
    for row in table.findall('{%s}table-row' % TABLE):
        cells = row.findall('{%s}table-cell' % TABLE)
        if (len(cells) >= 2 and cells[0].get('{%s}value-type' % OFFICE) == 'string' and
                cells[1].get('{%s}value-type' % OFFICE) == 'float'):
            marker = normal(''.join(cells[0].itertext()))
            if marker:
                controls[(row_index, 1)] = (marker, Decimal(cells[1].get('{%s}value' % OFFICE)))
                source_displays[marker] = cell_display(cells[1])
        row_index += int(row.get('{%s}number-rows-repeated' % TABLE, '1'))
    sidecar = primary.with_name(primary.name + '.aio-columns.jsonl')
    if not sidecar.is_file():
        return {}, source_displays
    selected = {}
    with sidecar.open(encoding='utf-8') as stream:
        header = json.loads(next(stream))
        if (header.get('schemaVersion') != 1 or header.get('format') != 'dBase' or
                1 not in header.get('memoColumns', [])):
            raise AssertionError('Mixed DBF typed recovery sidecar header differs: ' + sidecar.name)
        for line in stream:
            row = json.loads(line, parse_float=Decimal)
            control = controls.get((row.get('row'), row.get('column')))
            if control is None:
                continue
            marker, expected = control
            value = row.get('sourceValue')
            try:
                numeric = Decimal(str(value))
            except InvalidOperation as error:
                raise AssertionError('DBF recovery sidecar has invalid sourceValue=%r for %r' % (value, marker)) from error
            if row.get('sourceType') != 'number' or not numeric.is_finite() or numeric != expected:
                raise AssertionError('DBF recovery sidecar changed source value for %r: expected=%s observed=%r' %
                                     (marker, expected, value))
            if marker in selected or not isinstance(row.get('exportedText'), str):
                raise AssertionError('Invalid or duplicate mixed DBF recovery entry for ' + marker)
            selected[marker] = {'sourceValue': str(numeric), 'exportedText': row['exportedText'],
                                'sidecar': sidecar.name, 'row': row['row'], 'column': row['column']}
    if len(selected) != len(controls):
        raise AssertionError('Mixed DBF recovery sidecar is missing original numeric controls')
    return selected, source_displays


def check_native_table_readback(path, markers, pairs, *, allow_dbf_mixed_numeric_text=False,
                                dbf_memo_controls=None, source_numeric_displays=None):
    """Use native typed values, never locale-formatted CSV display strings."""
    tables = odf_xml(path).findall('.//{%s}table' % TABLE)
    if len(tables) != 1:
        raise AssertionError('A single exported sheet reopened as %d native tables' % len(tables))
    text = normal(' '.join(''.join(cell.itertext()) for cell in tables[0].iter('{%s}table-cell' % TABLE)))
    missing = [marker for marker in markers if normal(marker) not in text]
    if missing:
        raise AssertionError('Native table reimport lost string markers: %r' % missing)
    rows = tables[0].findall('.//{%s}table-row' % TABLE)
    observed = []
    for marker, expected in pairs:
        matches = []
        for row in rows:
            cells = row.findall('{%s}table-cell' % TABLE)
            if cells and normal(''.join(cells[0].itertext())) == marker:
                matches.append(cells)
        if len(matches) != 1 or len(matches[0]) < 2:
            raise AssertionError('Native numeric control %r: expected exactly one row and second cell; observed rows=%d' %
                                 (marker, len(matches)))
        cell = matches[0][1]
        value_type = cell.get('{%s}value-type' % OFFICE)
        value = cell.get('{%s}value' % OFFICE)
        display = cell_display(cell)
        diagnostic = 'marker=%r; expected=%s; observedType=%r; observedOfficeValue=%r; observedDisplayText=%r' % (
            marker, expected, value_type, value, display)
        coerced = value_type == 'string' and allow_dbf_mixed_numeric_text
        coercion = None
        if coerced:
            recovery = (dbf_memo_controls or {}).get(marker)
            if recovery is not None:
                if display != recovery['exportedText'] or Decimal(recovery['sourceValue']) != expected:
                    raise AssertionError('DBF native Memo differs from typed recovery metadata; ' + diagnostic)
                value = recovery['sourceValue']
                coercion = 'native_DBF_mixed_column_memo_with_typed_recovery_sidecar'
            elif ((source_numeric_displays or {}).get(marker) == display and
                  display and not display.lstrip().startswith('#')):
                value = str(expected)
                coercion = 'native_DBF_mixed_column_preserves_source_display'
            else:
                value = normal(display)
                if not re.fullmatch(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?', value):
                    raise AssertionError('Native mixed DBF numeric text is not canonical; ' + diagnostic)
                coercion = 'native_DBF_mixed_column_numeric_as_text'
        elif value_type not in ('float', 'currency', 'percentage') or value is None:
            raise AssertionError('Native table reimport changed a numeric cell type; ' + diagnostic)
        try:
            actual = Decimal(value)
        except InvalidOperation as error:
            raise AssertionError('Native numeric value is not canonical; ' + diagnostic) from error
        if not actual.is_finite() or actual != expected:
            raise AssertionError('Native table reimport changed a cached numeric value; ' + diagnostic)
        observed.append({'marker': marker, 'expected': str(expected), 'actual': value,
                         'valueType': value_type, 'displayText': display,
                         'typeCoercion': coercion,
                         'typedRecovery': (dbf_memo_controls or {}).get(marker) if coerced else None})
    return {'stringMarkers': len(markers), 'numericValues': len(pairs),
            'numericReadback': observed, 'allFound': True}


def seed_controls(folder):
    folder.mkdir()
    xml = writer_seed().replace('<table:table-column table:number-columns-repeated="4"/>', '')
    xml = xml.replace('<office:document ', '<office:document-content ', 1).replace(
        '</office:document>', '</office:document-content>')
    odt = folder / 'missing-columns.odt'
    with zipfile.ZipFile(odt, 'x') as archive:
        archive.writestr('mimetype', 'application/vnd.oasis.opendocument.text')
        archive.writestr('content.xml', xml)
        archive.writestr('META-INF/manifest.xml',
            '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0">'
            '<manifest:file-entry manifest:full-path="/" manifest:media-type="application/vnd.oasis.opendocument.text"/>'
            '<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>'
            '</manifest:manifest>')
    from test_guard_epub_v17 import create_epub, MARKERS
    epub = folder / 'ordered-spine.epub'
    create_epub(epub)
    sheets = []
    for index in range(1, 4):
        sheets.append('<table:table table:name="S%d"><table:table-column table:number-columns-repeated="2"/>'
            '<table:table-row><table:table-cell office:value-type="string"><text:p>ID</text:p></table:table-cell>'
            '<table:table-cell office:value-type="string"><text:p>Value</text:p></table:table-cell></table:table-row>'
            '<table:table-row><table:table-cell office:value-type="string"><text:p>AIO_SHEET_%d_FIRST</text:p></table:table-cell>'
            '<table:table-cell office:value-type="float" office:value="%d"><text:p>%d</text:p></table:table-cell></table:table-row>'
            '<table:table-row><table:table-cell office:value-type="string"><text:p>AIO_SHEET_%d_LAST</text:p></table:table-cell>'
            '<table:table-cell office:value-type="float" office:value="%d"><text:p>%d</text:p></table:table-cell></table:table-row>'
            '</table:table>' % (index, index, index * 10, index * 10, index, index * 10 + 1, index * 10 + 1))
    fods = folder / 'three-sheets.fods'
    fods.write_text('<office:document xmlns:office="%s" xmlns:table="%s" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" office:version="1.3" '
        'office:mimetype="application/vnd.oasis.opendocument.spreadsheet">'
        '<office:body><office:spreadsheet>%s</office:spreadsheet></office:body></office:document>'
        % (OFFICE, TABLE, ''.join(sheets)), encoding='utf-8')
    return odt, epub, tuple(MARKERS), fods


def make_cases(seeds, corpus):
    odt, epub, epub_markers, fods = seed_controls(seeds)
    cases = [dict(source=odt, targets=['docx', 'txt', 'pdf'], kind='writer'),
             dict(source=epub, targets=['docx', 'odt', 'pdf', 'txt'], kind='epub', markers=epub_markers),
             dict(source=fods, targets=['dbf', 'dif'], kind='table')]
    wanted = [('05_LEVEL_2.odt', ['docx', 'txt', 'pdf'], 'writer'),
              ('05_LEVEL_2.epub', ['odt', 'pdf'], 'epub'),
              ('05_LEVEL_2.gif', ['png', 'webp'], 'media'),
              ('05_LEVEL_2.png', ['webp', 'emz', 'wmz', 'svm'], 'image'),
              ('05_LEVEL_2.ods', ['dbf', 'dif'], 'table')]
    absent = []
    for name, targets, kind in wanted:
        path = corpus / name if corpus else None
        if path is None or not path.is_file():
            absent.append({'source': name, 'status': 'INCOMPLETE', 'reason': 'original_sample_not_available'})
            continue
        row = dict(source=path, targets=targets, kind=kind)
        if kind == 'epub':
            row['markers'] = ('S2-EPUB-L2',)
        cases.append(row)
    return cases, absent


def text_check(paths, markers, *, ordered=False):
    text = normal('\n'.join(output_text(path) for path in paths))
    missing = [marker for marker in markers if normal(marker) not in text]
    positions = [text.find(normal(marker)) for marker in markers]
    order_ok = not ordered or positions == sorted(positions)
    if missing or not order_ok:
        raise AssertionError('Missing text markers or OPF spine order changed: ' + repr(missing))
    return {'method': 'independent_document_text', 'markers': len(markers), 'spineOrder': order_ok}


def check_case(case, target, paths, work, executable, seconds):
    source = case['source']
    if case['kind'] == 'writer':
        markers = writer_markers(source)
        if len(markers) != 48:
            raise AssertionError('Writer acceptance source must contain all 48 control cells')
        return text_check(paths, markers)
    if case['kind'] == 'epub':
        result = text_check(paths, case['markers'], ordered=source.name == 'ordered-spine.epub')
        if target == 'docx':
            with zipfile.ZipFile(paths[0]) as archive:
                images = [name for name in archive.namelist() if name.startswith('word/media/')]
                if not images:
                    raise AssertionError('EPUB image was not embedded in DOCX')
                rels = ET.fromstring(archive.read('word/_rels/document.xml.rels'))
                if any(node.get('Type', '').endswith('/image') and node.get('TargetMode') == 'External' for node in rels):
                    raise AssertionError('EPUB DOCX retains external image references')
                result['embeddedImages'] = len(images)
        return result
    if target in ('emz', 'wmz'):
        plain = 'emf' if target == 'emz' else 'wmf'
        checks = []
        for index, path in enumerate(paths):
            expanded = work / ('expanded-%d.' % index + plain)
            with gzip.open(path, 'rb') as input_stream, expanded.open('xb') as out:
                while block := input_stream.read(64 * 1024):
                    out.write(block)
            checks.append({**inspect_metafile(expanded, plain), 'sha256': digest(expanded)})
        return {'method': 'independent_full_gzip_and_native_records', 'files': checks}
    if target == 'svm':
        for path in paths:
            if not path.read_bytes().startswith(b'VCLMTF'):
                raise AssertionError('Missing native SVM signature')
        return {'method': 'native_svm_signature', 'pages': len(paths)}
    if case['kind'] in ('media', 'image'):
        before, after = inspect_media(source), inspect_media(paths[0])
        keys = ['width', 'height', 'frames', 'pages', 'loopCount']
        for key in keys:
            if before.get(key) != after.get(key):
                raise AssertionError('Media property changed: ' + key)
        if before['alpha'] and not after['alpha']:
            raise AssertionError('Alpha channel was lost')
        if before['frames'] > 1 and before['durationsMs'] != after['durationsMs']:
            raise AssertionError('Animation timing changed')
        return {'method': 'independent_container_structure', 'sourceMedia': before, 'resultMedia': after}
    if case['kind'] == 'table':
        expected_sheets = table_markers(source)
        paths, companions = primary_table_outputs(paths, target, len(expected_sheets))
        sheet_checks = []
        # Map the original stem first and then the numbered additional sheets.
        paths = sorted(paths, key=lambda path: (path.stem != source.stem, path.name))
        for index, (path, markers) in enumerate(zip(paths, expected_sheets)):
            reopen = work / ('reopen-sheet-%d' % index)
            prepare_profile(reopen / 'profile')
            result = convert_path(path, 'fods', reopen, executable=executable,
                convert_spec='fods:OpenDocument Spreadsheet Flat XML',
                import_filter='dBase:76' if target == 'dbf' else 'DIF:76',
                cancel=_RunControl(threading.Event(), reopen, seconds, None))
            flat_path = next(candidate for candidate in result if candidate.suffix == '.fods')
            mixed = target == 'dbf' and mixed_numeric_control_column(source, index)
            recovery, source_displays = dbf_numeric_controls(source, index, path) if mixed else ({}, {})
            sheet_checks.append({'sheet': index + 1,
                **check_native_table_readback(flat_path, markers, numeric_pairs(source)[index],
                    allow_dbf_mixed_numeric_text=mixed, dbf_memo_controls=recovery,
                    source_numeric_displays=source_displays)})
        return {'method': 'independent_native_fods_reimport_per_sheet', 'sheets': sheet_checks,
                'companionFiles': [path.name for path in companions]}
    raise AssertionError('Unknown acceptance case')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--executable', type=Path, required=True)
    parser.add_argument('--corpus', type=Path)
    parser.add_argument('--seconds', type=float, default=60)
    args = parser.parse_args(argv)
    if not args.executable.is_file() or args.seconds <= 0 or args.corpus and not args.corpus.is_dir():
        parser.error('Existing native executable/corpus and positive deadline required')
    args.output.mkdir(parents=True, exist_ok=False)
    cases, missing_sources = make_cases(args.output / 'seeds', args.corpus)
    before = {str(case['source']): digest(case['source']) for case in cases}
    evidence = {'schema': 1, 'status': 'FAIL', 'scope': 'v17 real Guard native targeted routes',
                'missingSources': missing_sources, 'checks': [], 'unplannedPairs': [],
                'platformUnsupported': [],
                'fulltest510': 'NOT_RUN', 'visibleGui': 'NOT_RUN'}
    guard = ConverterGuard(adapter=LibreOfficeAdapter(args.executable.resolve()),
        limits=GuardLimits(preflight_timeout=args.seconds, file_timeout=args.seconds),
        recovery_parent=args.output / 'owned')
    try:
        files = [{'id': 'case%d' % index, 'name': case['source'].name,
                  'size': case['source'].stat().st_size} for index, case in enumerate(cases)]
        identity = guard.create(files, prepare_only=True)['jobId']
        for metadata, case in zip(files, cases):
            with case['source'].open('rb') as stream:
                guard.upload(identity, metadata['id'], stream, metadata['size'])
        print('AIO_V17_NATIVE_PREFLIGHT=' + json.dumps({'files': len(files)}), flush=True)
        inspection = guard.preflight(identity)
        formats = {row['id']: row for row in inspection['formats']}
        plan = []
        for metadata, case in zip(files, cases):
            targets = []
            for target in case['targets']:
                fmt = formats.get(target, {})
                if metadata['id'] in fmt.get('selectableFileIds', []):
                    targets.append(target)
                else:
                    proof = next(row['preflight'] for row in inspection['files'] if row['id'] == metadata['id'])
                    rejected = {'source': case['source'].name, 'target': target,
                        'status': 'INCOMPLETE', 'reason': fmt.get('unavailableReasons', {}).get(metadata['id']) or
                        proof.get('code') or fmt.get('reasonCode')}
                    if sys.platform != 'win32' and target == 'svm' and rejected['reason'] == 'NATIVE_FORMAT_IMPORT_ONLY':
                        rejected.update(status='platform_not_applicable',
                            requiredPlatform='Windows native UNO graphic-export acceptance')
                        evidence['platformUnsupported'].append(rejected)
                    else:
                        evidence['unplannedPairs'].append(rejected)
            if targets:
                plan.append({'fileId': metadata['id'], 'formats': targets})
        if not plan:
            evidence.update(status='INCOMPLETE', reason='No native routes admitted')
            return 2
        print('AIO_V17_NATIVE_PLAN=' + json.dumps({'tasks': sum(len(row['formats']) for row in plan),
            'unplannedPairs': evidence['unplannedPairs']}, ensure_ascii=True), flush=True)
        guard.start(identity, file_ids=[row['fileId'] for row in plan], plan=plan,
                    only_verified=True, archive_mode='on_demand')
        until = time.monotonic() + args.seconds * sum(len(row['formats']) for row in plan) + 120
        last_print = 0
        while not guard.status(identity)['finished']:
            if time.monotonic() >= until:
                guard.cancel(identity)
                raise TimeoutError('Native v17 acceptance deadline')
            if time.monotonic() - last_print >= 10:
                progress = guard.status(identity)
                print('AIO_V17_NATIVE_PROGRESS=' + json.dumps({key: progress.get(key)
                    for key in ('total', 'completed', 'succeeded', 'failed', 'currentTask')}, ensure_ascii=True), flush=True)
                last_print = time.monotonic()
            time.sleep(.1)
        report = guard.report(identity)
        state = guard.status(identity)
        evidence['counts'] = {key: state.get(key) for key in ('total', 'completed', 'succeeded', 'failed', 'cancelled')}
        print('AIO_V17_NATIVE_CONVERTED=' + json.dumps(evidence['counts']), flush=True)
        (args.output / 'conversion-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        if state.get('result'):
            saved = guard.save_folder(state['result']['ticket'], args.output, verify=True)
            folder = args.output / saved['name']
            manifest = json.loads((folder / 'AIO-results-manifest.json').read_text(encoding='utf-8'))
            outputs = {}
            for row in manifest['outputs']:
                path = folder / row['path']
                if path.stat().st_size != row['size'] or digest(path) != row['sha256']:
                    raise AssertionError('Independent save readback differs from manifest')
                outputs.setdefault((row['fileId'], row['format']), []).append(path)
            for metadata, case in zip(files, cases):
                for target in case['targets']:
                    paths = outputs.get((metadata['id'], target))
                    if not paths:
                        continue
                    work = args.output / ('check-' + metadata['id'] + '-' + target)
                    work.mkdir()
                    check = {'source': case['source'].name, 'target': target}
                    try:
                        check.update(check_case(case, target, paths, work, args.executable.resolve(), args.seconds), status='PASS')
                    except Exception as error:
                        check.update(status='FAIL', error=type(error).__name__, reason=str(error))
                    evidence['checks'].append(check)
                    print('AIO_V17_NATIVE_CHECK=' + json.dumps({key: check.get(key)
                        for key in ('source', 'target', 'status', 'reason')}, ensure_ascii=True), flush=True)
            evidence['savedByteReadback'] = 'PASS'
        evidence['originalsUnchanged'] = all(digest(Path(name)) == expected for name, expected in before.items())
        failed = (not evidence['originalsUnchanged'] or state['failed'] or state['cancelled'] or
                  any(check['status'] == 'FAIL' for check in evidence['checks']))
        expected = sum(len(case['targets']) for case in cases) - len(evidence['platformUnsupported'])
        incomplete = missing_sources or evidence['unplannedPairs'] or len(evidence['checks']) != expected
        evidence['status'] = 'FAIL' if failed else 'INCOMPLETE' if incomplete else 'PASS'
    except Exception as error:
        import traceback
        traceback.print_exc()
        evidence.update(status='FAIL', error=type(error).__name__, reason=str(error))
    finally:
        guard.close()
        (args.output / 'evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
        print('AIO_V17_NATIVE_EVIDENCE=' + json.dumps(evidence, ensure_ascii=True, separators=(',', ':')), flush=True)
    return 1 if evidence['status'] == 'FAIL' else 2 if evidence['status'] == 'INCOMPLETE' else 0


if __name__ == '__main__':
    sys.exit(main())
