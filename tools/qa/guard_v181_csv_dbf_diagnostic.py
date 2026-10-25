"""NIKITA-only, fixture-only DBF investigation for issue #50.

No writes to app input/output and no release gate changes. Uses the installed
LibreOffice through the project's existing UNO/CLI code paths.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app_backend.converter_guard.adapter import prepare_profile
from app_backend.converter_guard.csv_import import csv_import_hint
from app_backend.converter_guard.routes import registry
from app_backend.converter_guard.uno_session import UnoSession
from app_backend.libreoffice_engine import convert_path

CALC = 'com.sun.star.sheet.SpreadsheetDocument'
CSV_BYTES = b'name,amount\nalpha,123\nbeta,456\n'
FIELDS = ('name', 'amount', 'alpha', 'beta', '123', '456')


def error_record(exc):
    return {'type': type(exc).__name__, 'code': getattr(exc, 'code', None),
            'stage': getattr(exc, 'stage', None),
            'message': str(exc)[-400:]}


def dbf_check(path):
    """Structural and simple fixture-value check; NOT generic DBF validation."""
    data = path.read_bytes()
    rows = struct.unpack_from('<I', data, 4)[0] if len(data) >= 8 else -1
    head = struct.unpack_from('<H', data, 8)[0] if len(data) >= 10 else 0
    record = struct.unpack_from('<H', data, 10)[0] if len(data) >= 12 else 0
    enough = (head >= 33 and record >= 2 and rows == 2
              and len(data) >= head + rows * record)
    markers = {value: value.encode('ascii').lower() in data.lower()
               for value in FIELDS}
    return {'rows': rows, 'headerBytes': head, 'recordBytes': record,
            'markersPresent': markers,
            'validFixture': bool(enough and all(markers.values()))}


def outputs(path):
    found = []
    for item in sorted(path.iterdir()) if path.is_dir() else ():
        if item.is_file() and not item.is_symlink():
            info = {'name': item.name, 'bytes': item.stat().st_size}
            if item.suffix.lower() == '.dbf':
                info['dbfCheck'] = dbf_check(item)
            found.append(info)
    return found


def request(office, source, output, target, export_filter, export_options,
            import_filter=None, import_options=None, inspect=False):
    base = {'source': str(source), 'services': [CALC],
            'expectedSourceServices': [CALC],
            'importFilter': import_filter, 'importOptions': import_options}
    cancel = threading.Event()
    observation = None
    if inspect:
        observation = office.request(dict(base, operation='inspect'),
                                     cancel=cancel, timeout=90)
    args = dict(base, operation='export', output=str(output), format=target,
                expectedServices=[CALC], exportFilter=export_filter,
                exportOptions=export_options,
                expectedImportFilter=import_filter, importTimeout=60)
    result = office.request(args, cancel=cancel, timeout=90)
    def select(value):
        if not isinstance(value, dict):
            return None
        return {key: value.get(key) for key in
                ('ok', 'code', 'stage', 'filterName', 'typeName', 'services',
                 'interaction', 'timings', 'recycle', 'diagnostic') if key in value}
    return {'inspection': select(observation), 'export': select(result)}


def run_uno(root, label, engine, source, import_filter, import_options,
            *, export_options='76', warmup=0, inspect=False, warm_filter=None):
    folder = root / label
    folder.mkdir()
    result = {'name': label, 'mode': 'uno', 'warmupRequested': warmup,
              'exportOptions': export_options, 'sourceSuffix': source.suffix.lower()}
    start = time.monotonic()
    try:
        with UnoSession(engine, folder / 'session', prepare_profile, max_tasks=128) as office:
            if warmup:
                result['warmup'] = []
                for index in range(warmup):
                    out = folder / ('warm-%02d' % index)
                    out.mkdir()
                    sample = request(office, source, out, 'xlsx', warm_filter, None,
                                     import_filter, import_options)
                    result['warmup'].append({'ok': sample['export'].get('ok'),
                                            'code': sample['export'].get('code')})
                    if not sample['export'].get('ok'):
                        result['warmupIncomplete'] = True
                        break
            out = folder / 'out'
            out.mkdir()
            result['requests'] = request(office, source, out, 'dbf', 'dBase',
                                         export_options, import_filter,
                                         import_options, inspect=inspect)
            result['outputFiles'] = outputs(out)
            result['sessionStarts'] = office.starts
    except Exception as exc:
        result['error'] = error_record(exc)
    result['elapsedMs'] = int((time.monotonic() - start) * 1000)
    result['fixtureDbfValid'] = any(f.get('dbfCheck', {}).get('validFixture')
                                    for f in result.get('outputFiles', []))
    return result


def run_cli(root, label, engine, source, import_filter, convert_spec):
    folder = root / label
    folder.mkdir()
    result = {'name': label, 'mode': 'cli', 'convertSpec': convert_spec,
              'sourceSuffix': source.suffix.lower(),
              'explicitImportFilter': import_filter is not None}
    start = time.monotonic()
    try:
        paths = convert_path(source, 'dbf', folder / 'session',
                             executable=engine, cancel=threading.Event(),
                             convert_spec=convert_spec, import_filter=import_filter)
        result['returnedNames'] = [p.name for p in paths]
    except Exception as exc:
        result['error'] = error_record(exc)
    result['outputFiles'] = outputs(folder / 'session' / 'output')
    result['elapsedMs'] = int((time.monotonic() - start) * 1000)
    result['fixtureDbfValid'] = any(f.get('dbfCheck', {}).get('validFixture')
                                    for f in result['outputFiles'])
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--engine', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    src = args.output / 'source'
    src.mkdir()
    csv = src / 'table.csv'
    csv.write_bytes(CSV_BYTES)
    digest = hashlib.sha256(csv.read_bytes()).hexdigest()
    native = registry(args.engine)
    hint = csv_import_hint(csv, native)
    report = {'schemaVersion': 1, 'issue': 50, 'sourceLength': len(CSV_BYTES),
              'sourceSHA256': digest, 'nativeRegistryValid': native.valid,
              'csvImportHint': hint, 'cases': []}
    if hint is None:
        report['status'] = 'BLOCKED_NATIVE_IMPORT_HINT_MISSING'
    else:
        name, options = hint['filter'], hint['options']
        warm_filter = native.export_filter('xlsx', [CALC])
        report['warmupFilter'] = warm_filter
        # Fresh UNO process. First export before any inspect request.
        for label, kwargs in (
            ('uno-cold-native-76', {}),
            ('uno-cold-after-inspect', {'inspect': True}),
            ('uno-cold-no-options', {'export_options': None}),
        ):
            case = run_uno(args.output, label, args.engine, csv, name, options,
                           warm_filter=warm_filter, **kwargs)
            report['cases'].append(case)
            print('AIO_DBF_PROBE=' + json.dumps(case, ensure_ascii=True), flush=True)
        if warm_filter:
            case = run_uno(args.output, 'uno-after-nine-exports', args.engine,
                           csv, name, options, warmup=9, warm_filter=warm_filter)
            report['cases'].append(case)
            print('AIO_DBF_PROBE=' + json.dumps(case, ensure_ascii=True), flush=True)
        for label, input_spec, export_spec in (
            ('cli-csv-filtered-76', name + ':' + options, 'dbf:dBase:76'),
            ('cli-csv-auto-76', None, 'dbf:dBase:76'),
            ('cli-csv-filtered-default', name + ':' + options, 'dbf:dBase'),
        ):
            case = run_cli(args.output, label, args.engine, csv, input_spec, export_spec)
            report['cases'].append(case)
            print('AIO_DBF_PROBE=' + json.dumps(case, ensure_ascii=True), flush=True)
        # Verify whether a native LO FODS snapshot is a viable bridge. We do
        # not author, parse into or synthesize a DBF format ourselves.
        fods_folder = args.output / 'fods-snapshot'
        fods_folder.mkdir()
        try:
            fods_paths = convert_path(csv, 'fods', fods_folder, executable=args.engine,
                cancel=threading.Event(), convert_spec='fods:OpenDocument Spreadsheet Flat XML',
                import_filter=name + ':' + options)
            fods = next(p for p in fods_paths if p.suffix.lower() == '.fods')
            fods_data = fods.read_bytes()
            report['fodsSnapshot'] = {'created': True, 'bytes': len(fods_data),
                                      'markersPresent': all(x.encode() in fods_data
                                                            for x in ('alpha', 'beta', 'name', 'amount'))}
            if report['fodsSnapshot']['markersPresent']:
                case = run_uno(args.output, 'uno-fods-cold', args.engine, fods,
                               None, None, warm_filter=warm_filter, inspect=True)
                report['cases'].append(case)
                print('AIO_DBF_PROBE=' + json.dumps(case, ensure_ascii=True), flush=True)
        except Exception as exc:
            report['fodsSnapshot'] = {'created': False, 'error': error_record(exc)}
    report['originalUnchanged'] = (csv.read_bytes() == CSV_BYTES
                                   and hashlib.sha256(csv.read_bytes()).hexdigest() == digest)
    report['status'] = report.get('status', 'DIAGNOSTIC_ONLY_NOT_QA_PASS')
    (args.output / 'evidence.json').write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    summary = {'status': report['status'], 'sourceUnchanged': report['originalUnchanged'],
               'dbfCasesPass': sum(bool(x.get('fixtureDbfValid')) for x in report['cases']),
               'dbfCasesTotal': len(report['cases'])}
    print('AIO_DBF_SUMMARY=' + json.dumps(summary), flush=True)
    if not report['originalUnchanged']:
        raise SystemExit('Source fixture modified')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
