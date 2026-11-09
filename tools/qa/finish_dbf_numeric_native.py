"""Require working signed-decimal DBF export and independent exact readback."""
import argparse
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import sys
import threading
import time
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main():
    from app_backend.converter_guard import ConverterGuard, LibreOfficeAdapter
    from app_backend.libreoffice_engine import convert_path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--require-uno', action='store_true')
    parser.add_argument('--corpus', type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    cases, failures, originals = [], [], {}
    adapter = LibreOfficeAdapter(args.executable)
    cancel = threading.Event()
    counter = 0

    def convert(source, target='dbf'):
        nonlocal counter
        counter += 1
        proof = adapter.preflight(source, args.output/('probe-%d' % counter), cancel=cancel, timeout=120)
        assert proof['state'] == 'detected', proof
        adapter.prepare_source(proof)
        output = adapter.convert(source, target, args.output/('task-%d' % counter),
            cancel=cancel, timeout=180, on_started=lambda: None)
        return output, adapter.export_info(), adapter.execution_mode()

    def run(name, function):
        try:
            result = {'case': name, 'status': 'PASS', **function()}
        except Exception as error:
            result = {'case': name, 'status': 'FAIL', 'error': repr(error),
                      'code': getattr(error, 'code', None), 'diagnostic': getattr(error, 'diagnostic', None)}
            failures.append(name)
        cases.append(result)
        print('DBF_NUMERIC_CASE='+json.dumps(result, ensure_ascii=True), flush=True)

    with adapter.batch(args.output/'office'):
        def signed():
            source = args.output/'signed.csv'
            values = ['-12.750', '0.125'] + ['0.125']*128 + ['-999.990', '0.001']
            identifiers = ['%06d' % (index+1) for index in range(len(values))]
            raw = ('ID,AMOUNT\n'+''.join('%s,%s\n' % pair for pair in zip(identifiers, values))).encode()
            source.write_bytes(raw)
            originals[source] = hashlib.sha256(raw).hexdigest()
            outputs, info, mode = convert(source)
            if args.require_uno:
                assert mode == 'native_uno', mode
            from tools.qa.fulltest_dbf_emf_native import read_dbf
            dbf = next(path for path in outputs if path.suffix == '.dbf')
            physical = read_dbf(dbf)
            assert physical['fields'][1] == {'name':'AMOUNT', 'type':'N', 'width':8, 'decimal':3}, physical
            assert [row[0] for row in physical['rows']] == identifiers
            assert [Decimal(row[1]) for row in physical['rows']] == list(map(Decimal, values))
            mapping = json.loads(next(path for path in outputs if path.name.endswith('.aio-columns.jsonl')).read_text().splitlines()[0])
            assert mapping['numericFields'] == [{'column':1, 'fieldName':'AMOUNT', 'width':8, 'scale':3}], mapping
            assert source.read_bytes() == raw
            # A second native importer, with an explicit encoding, independently
            # checks every number and every leading-zero string after export.
            imported = convert_path(dbf, 'xlsx', args.output/'signed-readback', executable=args.executable,
                                    import_filter='dBase:'+('34' if physical['ldid']==0xC9 else '76'))
            from openpyxl import load_workbook
            book = load_workbook(next(path for path in imported if path.suffix == '.xlsx'), data_only=True)
            rows = list(book.active.values)[1:]
            book.close()
            assert [row[0] for row in rows] == identifiers
            assert [Decimal(str(row[1])) for row in rows] == list(map(Decimal, values))
            return {'executionMode':mode, 'rows':len(rows), 'field':physical['fields'][1],
                    'binaryAndNativeReadback':True, 'leadingZeros':True, 'warnings':info['warnings']}
        run('negative_decimal_later_row_width_and_exact_values', signed)

        def typed():
            import datetime
            from openpyxl import Workbook
            source = args.output/'typed.xlsx'
            book = Workbook()
            sheet = book.active
            sheet.append(['ID','AMOUNT','DATE','FLAG','MIXED'])
            sheet.append(['000123',-12.75,datetime.date(2002,8,22),True,42])
            sheet.append(['000007',.125,datetime.date(2003,8,22),False,'00042'])
            book.save(source)
            book.close()
            originals[source] = hashlib.sha256(source.read_bytes()).hexdigest()
            # Mixed-column mapping belongs to the native UNO path. The existing
            # CLI source gate deliberately does not infer a replacement schema.
            if not (args.executable.parent/'python.exe').is_file():
                return {'status':'NOT_RUN', 'reason':'native_UNO_required_for_mixed_column_mapping'}
            outputs, _info, mode = convert(source)
            assert mode == 'native_uno'
            from tools.qa.fulltest_dbf_emf_native import read_dbf
            physical = read_dbf(next(path for path in outputs if path.suffix == '.dbf'))
            assert [field['type'] for field in physical['fields']] == ['C','N','D','L','M'], physical
            assert [Decimal(row[1]) for row in physical['rows']] == [Decimal('-12.75'),Decimal('.125')]
            assert [row[2] for row in physical['rows']] == ['20020822','20030822']
            assert [row[3] for row in physical['rows']] == ['T','F']
            assert [row[4] for row in physical['rows']] == ['42','00042']
            assert any(path.suffix == '.dbt' for path in outputs)
            return {'executionMode':mode, 'fieldTypes':['C','N','D','L','M'], 'valuesPreserved':True}
        run('date_logical_id_and_mixed_memo_types_preserved', typed)

        if args.corpus:
            def corpus():
                import csv
                facts = []
                for source in sorted(args.corpus.glob('*.csv')):
                    originals[source] = hashlib.sha256(source.read_bytes()).hexdigest()
                    with source.open(encoding='utf-8-sig',newline='') as stream:
                        rows = list(csv.reader(stream))
                    outputs, _info, mode = convert(source)
                    from tools.qa.fulltest_dbf_emf_native import read_dbf
                    physical = read_dbf(next(path for path in outputs if path.suffix == '.dbf'))
                    assert [row[1] for row in physical['rows']] == [row[1] for row in rows[1:]]
                    assert [Decimal(row[2]) for row in physical['rows']] == [Decimal(row[2]) for row in rows[1:]]
                    assert [row[3] for row in physical['rows']] == [row[3] for row in rows[1:]]
                    facts.append({'source':source.name, 'rows':len(rows)-1,'executionMode':mode})
                assert facts, args.corpus
                return {'files':facts}
            run('latest_original_csv_exact_numeric_id_cyrillic_values', corpus)

    def guard_case():
        guard = ConverterGuard(adapter=LibreOfficeAdapter(args.executable), recovery_parent=args.output/'owned')
        raw = b'ID,AMOUNT\n000123,-12.750\n000007,0.125\n'
        try:
            job = guard.create([{'id':'source','name':'negative-decimal.csv','size':len(raw)}], prepare_only=True)['jobId']
            guard.upload(job,'source',io.BytesIO(raw),len(raw))
            prepared = guard.preflight(job)
            assert prepared['files'][0]['preflight']['state']=='detected', prepared
            guard.start(job,file_ids=['source'],formats=['dbf'],only_verified=True)
            deadline = time.monotonic()+240
            while not (state := guard.status(job))['finished']:
                assert time.monotonic()<deadline,state
                time.sleep(.05)
            (args.output/'guard-status.json').write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf-8')
            assert state['state']=='ready',state
            from tools.qa.fulltest_dbf_emf_native import read_dbf
            with guard.open_result(state['result']['ticket']) as (stream,_name,_size):
                with ZipFile(stream) as archive:
                    name = next(name for name in archive.namelist() if name.endswith('.dbf'))
                    path = args.output/'guard-result.dbf'
                    path.write_bytes(archive.read(name))
                    physical = read_dbf(path)
                    assert physical['fields'][1]=={'name':'AMOUNT','type':'N','width':7,'decimal':3},physical
                    assert [row[0] for row in physical['rows']]==['000123','000007']
                    assert [Decimal(row[1]) for row in physical['rows']]==[Decimal('-12.750'),Decimal('.125')]
                    assert any(name.endswith('.aio-columns.jsonl') for name in archive.namelist())
            return {'jobState':'ready','nativeOutcome':'VALUES_PRESERVED','field':physical['fields'][1]}
        finally:
            guard.close()
    run('guard_negative_decimal_ready_zip_exact_readback',guard_case)
    assert all(hashlib.sha256(path.read_bytes()).hexdigest()==sha for path,sha in originals.items())
    evidence={'status':'FAIL' if failures else 'PASS','cases':cases,'sourceUnchanged':True}
    (args.output/'evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
    if failures:
        raise AssertionError(failures)


if __name__=='__main__':
    main()
