"""M25/M31/FT4-04 native DBF regression, with independent consumer readback.

Use --require-uno on Windows acceptance jobs. Linux can exercise the real CLI
path, encodings, DBT and save transactions; complex literal-heading mapping
requires the native UNO worker and is explicitly reported when unavailable.
The small binary DBF/DBT reader is independent of production code. Optional
GDAL/OGR is a third, external consumer that applies LDID/CPG automatically.
"""
import argparse
import datetime
from decimal import Decimal
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.qa.fulltest_dbf_emf_native import read_dbf


def main():
    from openpyxl import Workbook, load_workbook
    from app_backend.converter_guard import ConverterGuard, LibreOfficeAdapter
    from app_backend.converter_guard.dbf_codepage import attach_native_codepages, declare_native_cp1251
    from app_backend.libreoffice_engine import convert_path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--ogrinfo', type=Path)
    parser.add_argument('--require-uno', action='store_true')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    cancel = threading.Event()
    adapter = LibreOfficeAdapter(args.executable)
    ogr = args.ogrinfo or (Path(shutil.which('ogrinfo')) if shutil.which('ogrinfo') else None)
    cases, failures, originals, counter = [], [], {}, 0
    untested = []

    def remember(path):
        originals[path] = hashlib.sha256(path.read_bytes()).hexdigest()
        return path

    def native(source, target, *, spec=None, import_filter=None):
        nonlocal counter
        counter += 1
        return convert_path(source, target, args.output/('native-%03d' % counter),
                            executable=args.executable, convert_spec=spec, import_filter=import_filter)

    def read_xlsx(path):
        book = load_workbook(path, data_only=True)
        try:
            return list(book.active.values)
        finally:
            book.close()

    def lo_rows(path, encoding=None):
        outputs = native(path, 'xlsx', import_filter=('dBase:'+encoding) if encoding else None)
        return read_xlsx(next(p for p in outputs if p.suffix == '.xlsx'))[1:]

    def external(path):
        if ogr is None:
            return {'status': 'NOT_RUN', 'reason': 'ogrinfo_not_installed'}
        result = subprocess.run([str(ogr), '-ro', '-al', '-json', '-features', str(path)],
                                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=60, encoding='utf-8')
        raw = json.loads(result.stdout)
        layer = raw['layers'][0]
        return {'status': 'PASS', 'driver': raw['driverShortName'],
                'encoding': layer.get('metadata', {}).get('SHAPEFILE', {}),
                'rows': [feature['properties'] for feature in layer['features']]}

    def convert(source, target='dbf'):
        nonlocal counter
        counter += 1
        proof = adapter.preflight(source, args.output/('probe-%03d' % counter), cancel=cancel, timeout=120)
        assert proof.get('state') == 'detected', proof
        adapter.prepare_source(proof)
        paths = adapter.convert(source, target, args.output/('task-%03d' % counter),
                                cancel=cancel, timeout=180, on_started=lambda: None)
        return paths, adapter.export_info(), adapter.execution_mode()

    def run(name, action):
        try:
            detail = action()
            result = {'case': name, 'status': 'PASS', **detail}
        except Exception as error:
            failures.append(name)
            result = {'case': name, 'status': 'FAIL', 'error': repr(error),
                      'code': getattr(error, 'code', None), 'diagnostic': getattr(error, 'diagnostic', None)}
        cases.append(result)
        print('DBF_COMPLETION_CASE='+json.dumps(result, ensure_ascii=True), flush=True)

    base = args.output/'cyrillic.xlsx'
    book = Workbook()
    names = ['Семён Ёлкин', 'Алёна Фёдорова']
    identifiers = ['000123', '000007']
    for index, (title, values) in enumerate((('Cyrillic', names), ('Unicode', ['漢字 😀', 'é € Ω']))):
        sheet = book.active if index == 0 else book.create_sheet()
        sheet.title = title
        sheet.append(['ID', 'NAME', 'AMOUNT', 'DATE', 'ACTIVE'])
        for i, (identifier, name) in enumerate(zip(identifiers, values)):
            sheet.append([identifier, name, [-12.75, 0.125][i], datetime.date(2002+i, 8, 22), bool(i)])
            sheet.cell(i+2, 3).number_format = '0.000'
    book.save(base)
    book.close()
    remember(base)

    single = args.output/'single-cyrillic.xlsx'
    book = Workbook()
    book.active.append(['ID', 'NAME'])
    for identifier, name in zip(identifiers, names):
        book.active.append([identifier, name])
    book.save(single)
    book.close()
    remember(single)

    with adapter.batch(args.output/'office'):
        def baseline():
            path = next(p for p in native(single, 'dbf', spec='dbf:dBase:76') if p.suffix == '.dbf')
            path.with_suffix('.cpg').write_text('UTF-8\n', encoding='ascii')
            explicit, automatic = lo_rows(path, '76'), lo_rows(path)
            assert [r[1] for r in explicit] == names, explicit
            physical = read_dbf(path)
            consumer = external(path)
            if consumer['status'] == 'PASS':
                assert [r['NAME'] for r in consumer['rows']] == names, consumer
            return {'nativeLdid': physical['ldid'], 'explicitUtf8Correct': True,
                    'automaticUtf8Correct': [r[1] for r in automatic] == names,
                    'defaultLibreOfficeNames': [r[1] for r in automatic], 'ogr': consumer}
        run('native_utf8_cpg_default_import_baseline', baseline)

        def metadata():
            path = next(p for p in native(single, 'dbf', spec='dbf:dBase:34') if p.suffix == '.dbf')
            before = path.read_bytes()
            declare_native_cp1251(path)
            after = path.read_bytes()
            assert before[:29] == after[:29] and before[30:] == after[30:]
            assert after[29] == 0xC9
            attach_native_codepages([path], filter_name='dBase')
            physical = read_dbf(path, encoding='cp1251')
            assert [r[1] for r in physical['rows']] == names, physical
            automatic = lo_rows(path)
            assert [r[1] for r in lo_rows(path, '34')] == names
            with_cpg_auto = [r[1] for r in automatic] == names
            with_cpg = external(path)
            if with_cpg['status'] == 'PASS':
                assert [r['NAME'] for r in with_cpg['rows']] == names, with_cpg
            path.with_suffix('.cpg').unlink()
            automatic = lo_rows(path)
            assert [r[1] for r in lo_rows(path, '34')] == names
            no_cpg = external(path)
            if no_cpg['status'] == 'PASS':
                assert [r['NAME'] for r in no_cpg['rows']] == names, no_cpg
            return {'onlyMetadataByte29Changed': True, 'ldid': after[29],
                    'explicitLibreOfficeReadbackWithAndWithoutCpg': True,
                    'defaultLibreOfficeCorrectWithCpg': with_cpg_auto,
                    'defaultLibreOfficeCorrectWithoutCpg': [r[1] for r in automatic] == names,
                    'ogrWithCpg': with_cpg, 'ogrWithoutCpg': no_cpg}
        run('native_cp1251_metadata_and_automatic_external_consumers', metadata)

        def workbook_case(source):
            outputs, info, mode = convert(source)
            dbfs = sorted(p for p in outputs if p.suffix == '.dbf')
            assert len(dbfs) == 2, outputs
            facts = []
            for index, path in enumerate(dbfs):
                codec, option, expected = ('cp1251', '34', names) if index == 0 else ('utf-8', '76', ['漢字 😀', 'é € Ω'])
                physical = read_dbf(path, encoding=codec)
                assert [r[0] for r in physical['rows']] == identifiers, physical
                assert [r[1] for r in physical['rows']] == expected, physical
                assert [Decimal(r[2]) for r in physical['rows']] == [Decimal('-12.75'), Decimal('0.125')]
                assert [r[3] for r in physical['rows']] == ['20020822', '20030822']
                assert [r[4] for r in physical['rows']] == ['F', 'T']
                assert physical['ldid'] == (0xC9 if index == 0 else 0), physical
                assert path.with_suffix('.cpg') in outputs
                assert path.with_suffix('.cpg').read_text().strip() == ('1251' if index == 0 else 'UTF-8')
                automatic = lo_rows(path, option)
                assert [r[0] for r in automatic] == identifiers, automatic
                assert [r[1] for r in automatic] == expected, automatic
                consumer = external(path)
                if consumer['status'] == 'PASS':
                    assert [r['NAME'] for r in consumer['rows']] == expected, consumer
                facts.append({'encoding': codec, 'fields': physical['fields'], 'ldid': physical['ldid'], 'ogr': consumer})
            assert 'NATIVE_DBF_CP1251_CPG' in info['warnings'], info
            assert 'NATIVE_DBF_UTF8_REQUIRES_ENCODING_SELECTION' in info['warnings'], info
            return {'executionMode': mode, 'outputs': facts, 'warnings': info['warnings']}
        run('xlsx_per_sheet_cyrillic_unicode_values_and_schema', lambda: workbook_case(base))
        def ods_case():
            ods = next(p for p in native(base, 'ods') if p.suffix == '.ods')
            remember(ods)
            return workbook_case(ods)
        run('ods_per_sheet_cyrillic_unicode_values_and_schema', ods_case)

        def csv_case():
            csv = args.output/'cyrillic.csv'
            csv.write_text('ID,NAME,AMOUNT\n000123,Семён Ёлкин,42\n000007,Алёна Фёдорова,7\n', encoding='utf-8-sig')
            remember(csv)
            outputs, info, mode = convert(csv)
            path = next(p for p in outputs if p.suffix == '.dbf')
            physical = read_dbf(path, encoding='cp1251')
            assert [r[0] for r in physical['rows']] == identifiers, physical
            assert [r[1] for r in physical['rows']] == names, physical
            assert [r[0] for r in lo_rows(path, '34')] == identifiers
            return {'executionMode': mode, 'ldid': physical['ldid'], 'leadingZerosAndCyrillic': True}
        run('csv_native_import_and_explicit_cyrillic_readback', csv_case)

        def aio_roundtrip():
            outputs, _info, _mode = convert(single)
            dbf = next(p for p in outputs if p.suffix == '.dbf')
            imported, info, mode = convert(dbf, 'xlsx')
            rows = read_xlsx(next(p for p in imported if p.suffix == '.xlsx'))[1:]
            assert [r[0] for r in rows] == identifiers, rows
            assert [r[1] for r in rows] == names, rows
            return {'executionMode': mode, 'ownAioReadback': True, 'valuesPreserved': True}
        run('aio_own_cp1251_dbf_roundtrip', aio_roundtrip)

        def memo_case():
            source = args.output/'memo.xlsx'
            book = Workbook()
            book.active.append(['ID', 'NOTE'])
            expected = ['Ёжик ёлка ' + 'длинная строка ' * 30, 'второй текст']
            for identifier, value in zip(identifiers, expected):
                book.active.append([identifier, value])
            book.save(source)
            book.close()
            remember(source)
            outputs, info, mode = convert(source)
            path = next(p for p in outputs if p.suffix == '.dbf')
            physical = read_dbf(path, encoding='utf-8')
            assert physical['ldid'] == 0, physical
            assert path.with_suffix('.cpg').read_text().strip() == 'UTF-8'
            assert physical['fields'][1]['type'] == 'M', physical
            assert path.with_suffix('.dbt') in outputs, outputs
            assert [r[1] for r in physical['rows']] == expected, physical
            rows = lo_rows(path, '76')
            assert [r[1] for r in rows] == expected, rows
            imported, _info, _mode = convert(path, 'xlsx')
            own_rows = read_xlsx(next(p for p in imported if p.suffix == '.xlsx'))[1:]
            assert [r[1] for r in own_rows] == expected, own_rows
            return {'executionMode': mode, 'memoCharacters': len(expected[0]),
                    'binaryDbtAndExplicitLibreOfficeReadback': True, 'ownAioReadback': True,
                    'encoding': 'utf-8', 'cp1251MemoImport': 'unsupported_kept_utf8'}
        run('dbt_long_cyrillic_memo_binary_and_native_readback', memo_case)

        def literal_case():
            source = args.output/'literal-headers.xlsx'
            headings = ['ID,C,8', 'ID', 'Повтор', 'Повтор', 'Long field name', 'X,N,2']
            book = Workbook()
            sheet = book.active
            sheet.append(headings)
            sheet.append(['000123', 'A', 'Ёлка', 'ёж', 'long', 42])
            sheet.append(['000007', 'B', 'Юлия', 'Иванов', 'text', '00042'])
            book.save(source)
            book.close()
            remember(source)
            outputs, info, mode = convert(source)
            path = next(p for p in outputs if p.suffix == '.dbf')
            physical = read_dbf(path, encoding='utf-8')
            mapping = json.loads(next(p for p in outputs if p.name.endswith('.aio-columns.jsonl')).read_text().splitlines()[0])
            assert mapping['originalHeaders'] == headings, mapping
            assert mapping['fieldNames'] == [f['name'] for f in physical['fields']], (mapping, physical)
            assert [r[0] for r in physical['rows']] == identifiers
            assert [r[5] for r in physical['rows']] == ['42', '00042']
            return {'executionMode': mode, 'schemaMapping': mapping, 'valuesPreserved': True}
        # The CLI route deliberately rejects unrepresentable literal headings;
        # claiming native mapping success on Linux would hide this limitation.
        if (args.executable.parent/'python.exe').is_file():
            run('literal_duplicate_long_cyrillic_field_mapping_and_mixed_memo', literal_case)
        else:
            detail = {'case': 'literal_duplicate_long_cyrillic_field_mapping_and_mixed_memo',
                      'status': 'NOT_RUN', 'reason': 'native_UNO_python_exe_unavailable'}
            cases.append(detail)
            untested.append(detail)
            if args.require_uno:
                failures.append(detail['case'])

    def save_case():
        guard = ConverterGuard(adapter=LibreOfficeAdapter(args.executable), recovery_parent=args.output/'owned')
        try:
            raw = single.read_bytes()
            job = guard.create([{'id': 'source', 'name': single.name, 'size': len(raw)}], prepare_only=True)['jobId']
            guard.upload(job, 'source', io.BytesIO(raw), len(raw))
            assert guard.preflight(job)['files'][0]['preflight']['state'] == 'detected'
            guard.start(job, file_ids=['source'], formats=['dbf'], only_verified=True)
            deadline = time.monotonic() + 240
            while not (state := guard.status(job))['finished']:
                assert time.monotonic() < deadline, state
                time.sleep(.05)
            assert state['state'] == 'ready', state
            ticket = state['result']['ticket']
            destination = args.output/'saved'
            destination.mkdir()
            saved = guard.save_folder(ticket, destination)
            folder = destination/saved['name']
            manifest = json.loads((folder/'AIO-results-manifest.json').read_text(encoding='utf-8'))
            outputs = {row['path']: row for row in manifest['outputs']}
            dbf = next(name for name in outputs if name.endswith('.dbf'))
            assert str(Path(dbf).with_suffix('.cpg')).replace('\\', '/') in outputs
            assert [r[1] for r in read_dbf(folder/dbf, encoding='cp1251')['rows']] == names
            for name, row in outputs.items():
                assert hashlib.sha256((folder/name).read_bytes()).hexdigest() == row['sha256'], row
            with guard.open_result(ticket) as (stream, _name, _size):
                with ZipFile(stream) as archive:
                    assert archive.testzip() is None
                    zipped = json.loads(archive.read('AIO-results-manifest.json'))
                    assert {row['path'] for row in zipped['outputs']} == set(outputs)
                    for row in zipped['outputs']:
                        assert hashlib.sha256(archive.read(row['path'])).hexdigest() == row['sha256'], row
                    report = json.loads(archive.read('AIO-conversion-report.json'))
                    assert report['files'][0]['targets'][0]['warnings'], report
            return {'manifestReadback': bool(manifest['readbackVerified']),
                    'folderAndZipHashes': True, 'companionsAndReports': True}
        finally:
            guard.close()
    run('guard_folder_zip_manifests_companions_and_reports', save_case)

    def negative_decimal_case():
        # Native width inference used to fail on -12.750 in a six-byte field.
        # This case now requires a working, exactly verified conversion.
        source = args.output/'negative-decimal.csv'
        raw = b'ID,AMOUNT\n000123,-12.750\n000007,0.125\n'
        source.write_bytes(raw)
        remember(source)
        guard = ConverterGuard(adapter=LibreOfficeAdapter(args.executable),
                               recovery_parent=args.output/'negative-owned')
        try:
            job = guard.create([{'id': 'negative', 'name': source.name, 'size': len(raw)}], prepare_only=True)['jobId']
            guard.upload(job, 'negative', io.BytesIO(raw), len(raw))
            prepared = guard.preflight(job)
            assert prepared['files'][0]['preflight']['state'] == 'detected', prepared
            guard.start(job, file_ids=['negative'], formats=['dbf'], only_verified=True)
            deadline = time.monotonic()+240
            while not (state := guard.status(job))['finished']:
                assert time.monotonic() < deadline, state
                time.sleep(.05)
            (args.output/'negative-decimal-status.json').write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
            assert state['state'] == 'ready', state
            ticket = state['result']['ticket']
            with guard.open_result(ticket) as (stream, _name, _size):
                with ZipFile(stream) as archive:
                    name = next(name for name in archive.namelist() if name.endswith('.dbf'))
                    path = args.output/'negative-decimal-native.dbf'
                    path.write_bytes(archive.read(name))
                    physical = read_dbf(path)
                    assert physical['fields'][1] == {'name': 'AMOUNT', 'type': 'N', 'width': 7, 'decimal': 3}, physical
                    rows = physical['rows']
                    assert [r[0] for r in rows] == ['000123', '000007'], rows
                    assert [Decimal(r[1]) for r in rows] == [Decimal('-12.750'), Decimal('0.125')], rows
                    assert any(name.endswith('.aio-columns.jsonl') for name in archive.namelist())
            return {'nativeOutcome': 'VALUES_PRESERVED', 'sourceUnchanged': source.read_bytes() == raw,
                    'numericField': physical['fields'][1], 'leadingZerosPreserved': True}
        finally:
            guard.close()
    run('negative_decimal_native_width_exact_conversion', negative_decimal_case)

    assert all(hashlib.sha256(path.read_bytes()).hexdigest() == sha for path, sha in originals.items())
    result = {'status': 'FAIL' if failures else 'PASS', 'cases': cases,
              'sourceUnchanged': True, 'notRun': untested, 'historicalFULLTEST': 'NOT_RUN',
              'macroPreservation': 'NOT_RUN_actual_macro_fixture_required',
              'externalConsumerScope': 'LibreOffice default + optional GDAL OGR, not all DBF consumers'}
    (args.output/'evidence.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    if failures:
        raise AssertionError(failures)


if __name__ == '__main__':
    main()
