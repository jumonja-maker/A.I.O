"""Explicit native text-export QA on generated, owned fixtures.

Checks exact CSV records, not visual equivalence or a user's full corpus.
Windows execution is owner-only; this tool does not dispatch any runner.
"""
import argparse
import csv
import hashlib
import io
import json
from decimal import Decimal, InvalidOperation
import re
from pathlib import Path
import subprocess
import sys
import threading
import time
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app_backend.converter_guard import ConverterGuard, GuardLimits, LibreOfficeAdapter
from app_backend.converter_guard.adapter import prepare_profile
from app_backend.libreoffice_engine import convert_path
from guard_v08_native_acceptance import fixtures

CSV_FILTER = 'Text - txt - csv (StarCalc)'


class Deadline:
    """Bound this explicit QA run; not a new application policy."""
    def __init__(self, seconds=90):
        self.until = time.monotonic() + seconds
        self.event = threading.Event()

    def is_set(self):
        return time.monotonic() >= self.until

    def wait(self, seconds):
        self.event.wait(max(0, min(seconds, self.until - time.monotonic())))
        return self.is_set()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def csv_bytes(rows, delimiter):
    text = io.StringIO(newline='')
    csv.writer(text, delimiter=delimiter, lineterminator='\n').writerows(rows)
    return text.getvalue().encode('utf-8')


def records(path, delimiter):
    with path.open(encoding='utf-8-sig', newline='') as stream:
        return list(csv.reader(stream, delimiter=delimiter, strict=True))


def verify_cells(key, target, actual, expected):
    """Exact text, exact numeric *value* for two generated numeric fixture cells.

    Text/delimiter/encoding must match literally. An OS/Calc decimal comma
    differs from the literal representation, not the underlying numeric value.
    A rounding, extra row, or a changed text cell must still fail QA.
    """
    if len(actual) != len(expected):
        raise AssertionError('Row count mismatch: ' + key + ' -> ' + target)
    variants = []
    for row_index, (left, right) in enumerate(zip(actual, expected)):
        if len(left) != len(right):
            raise AssertionError('Column count mismatch: ' + key + ' -> ' + target)
        for column, (value, reference) in enumerate(zip(left, right)):
            if value == reference:
                continue
            if (key != 'boundary' and row_index in (0, 1) and column == 1 and
                    re.fullmatch(r'-?[0-9]+(?:[.,][0-9]+)?', value) and
                    re.fullmatch(r'-?[0-9]+(?:[.,][0-9]+)?', reference)):
                if Decimal(value.replace(',', '.')) == Decimal(reference.replace(',', '.')):
                    variants.append({'source': key, 'target': target, 'row': row_index + 1,
                                     'column': column + 1, 'kind': 'locale_decimal_notation'})
                    continue
            raise AssertionError('Cell mismatch: ' + key + ' -> ' + target +
                                 ' row=' + str(row_index + 1) +
                                 ' col=' + str(column + 1))
    return variants


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--executable', type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    sheet = fixtures(args.output / 'fixtures')['xlsx']
    with zipfile.ZipFile(sheet) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    rows = ('<row r="3"><c r="A3" t="inlineStr"><is><t>comma, text</t></is></c>'
            '<c r="B3" t="inlineStr"><is><t>tab\tvalue</t></is></c></row>'
            '<row r="4"><c r="A4" t="inlineStr"><is><t>line\nbreak</t></is></c>'
            '<c r="B4" t="inlineStr"><is><t>Мир🙂</t></is></c></row>')
    parts['xl/worksheets/sheet1.xml'] = parts['xl/worksheets/sheet1.xml'].replace(
        b'</sheetData>', rows.encode('utf-8') + b'</sheetData>')
    with zipfile.ZipFile(sheet, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    expected = [['AIO_SHEET_MARKER', '1234.5'], ['Проверка', '1244.5'],
                ['comma, text', 'tab\tvalue'], ['line\nbreak', 'Мир🙂']]
    adapter = LibreOfficeAdapter(args.executable)
    assert adapter.available, 'LibreOffice executable is required'
    version = subprocess.run([str(adapter.executable), '--version'], capture_output=True,
                             text=True, check=True, timeout=15).stdout.strip()
    preparation = args.output / 'make-ods'
    prepare_profile(preparation / 'profile')
    print('Preparing the ODS fixture with native LibreOffice', flush=True)
    paths = convert_path(sheet, 'ods', preparation, executable=adapter.executable, cancel=Deadline())
    ods = next(path for path in paths if path.suffix == '.ods')
    semicolon = args.output / 'fixtures' / 'semicolon.csv'
    semicolon.write_bytes(csv_bytes(expected, ';'))
    tsv = args.output / 'fixtures' / 'quoted.tsv'
    tsv.write_bytes(csv_bytes(expected, '\t'))
    boundary = args.output / 'fixtures' / 'boundary.csv'
    prefix, filler = b'name;amount\nalpha;1\nbeta;2\n', b'row;1\n'
    count = (65535 - len(prefix)) // len(filler)
    padding = b'x' * (65535 - len(prefix) - count * len(filler))
    boundary.write_bytes(prefix + filler * count + padding + 'я'.encode('utf-8') + b';3\n')
    assert boundary.read_bytes()[65535:65537] == 'я'.encode('utf-8')
    sources = {'xlsx': sheet, 'ods': ods, 'semicolon': semicolon, 'tsv': tsv, 'boundary': boundary}
    expected_by_source = {key: expected for key in sources}
    expected_by_source['boundary'] = records(boundary, ';')
    hashes = {key: digest(path) for key, path in sources.items()}
    guard = ConverterGuard(adapter=adapter, limits=GuardLimits(file_timeout=90, preflight_timeout=90),
                           spool_parent=args.output / 'spool', recovery_parent=args.output / 'owned-jobs')
    evidence = {'status': 'FAIL', 'engineVersion': version,
                'scope': 'Linux native CLI; generated text/spreadsheet fixtures',
                'windowsUnoQt': 'NOT_RUN', 'fullVisualQuality': 'NOT_RUN'}
    try:
        identity = guard.create([{'id': key, 'name': path.name, 'size': path.stat().st_size}
                                 for key, path in sources.items()], prepare_only=True)['jobId']
        for key, path in sources.items():
            with path.open('rb') as stream:
                guard.upload(identity, key, stream, path.stat().st_size)
        tick = time.monotonic()
        inspection = guard.preflight(identity)
        evidence['preflightMs'] = round((time.monotonic() - tick) * 1000)
        eligible = {item['id']: set(item['verifiedFileIds']) for item in inspection['formats']}
        for target in ('csv', 'tsv', 'tab'):
            assert set(sources) <= eligible.get(target, set()), 'Unconfirmed route: ' + target
        print('Native preflight confirmed all 15 text routes', flush=True)
        plan = [{'fileId': key, 'formats': ['csv', 'tsv', 'tab'] + (['pdf'] if key == 'xlsx' else [])}
                for key in sources]
        tick = time.monotonic()
        guard.start(identity, file_ids=list(sources), plan=plan, only_verified=True)
        revision = -1
        deadline = time.monotonic() + 240
        while True:
            state = guard.status(identity)
            if state['revision'] != revision:
                revision = state['revision']
                print(json.dumps({'completed': state['completed'], 'total': state['total'],
                                  'current': state['currentTask']}, ensure_ascii=True), flush=True)
            if state['finished']:
                break
            assert time.monotonic() < deadline, 'Native queue did not finish'
            time.sleep(.1)
        evidence.update(conversionMs=round((time.monotonic() - tick) * 1000),
                        nativeExportsSucceeded=state['succeeded'], nativeExportsFailed=state['failed'],
                        nativeExportsCancelled=state['cancelled'],
                        failedTargets=guard.report(identity).get('failedTargets', []))
        assert (state['succeeded'], state['failed'], state['cancelled']) == (16, 0, 0), guard.report(identity)
        saved = guard.save_folder(state['result']['ticket'], args.output)
        assert saved['savedAll']
        saved_root = args.output / saved['name']
        outputs = {(item['fileId'], item['format']): saved_root / item['archiveName']
                   for item in state['result']['outputs']}
        checks, locale_variants = [], []
        for key in sources:
            for target in ('csv', 'tsv', 'tab'):
                actual = records(outputs[key, target], ',' if target == 'csv' else '\t')
                differences = verify_cells(key, target, actual, expected_by_source[key])
                locale_variants.extend(differences)
                if differences:
                    print(json.dumps({'qaLocaleNumericVariants': differences},
                                     ensure_ascii=True), flush=True)
                checks.append({'source': key, 'target': target, 'rows': len(actual), 'status': 'PASS'})
        assert outputs['xlsx', 'pdf'].read_bytes().startswith(b'%PDF-')
        controls = []
        for key, target, separator, import_filter in (
                ('xlsx', 'tsv', 9, None), ('semicolon', 'csv', 44, CSV_FILTER + ':59,34,76,1')):
            work = args.output / ('direct-' + key)
            prepare_profile(work / 'profile')
            paths = convert_path(sources[key], target, work, executable=adapter.executable,
                                 cancel=Deadline(), import_filter=import_filter,
                                 convert_spec=f'{target}:{CSV_FILTER}:{separator},34,76,1,')
            actual = next(path for path in paths if path.suffix == '.' + target)
            assert actual.read_bytes() == outputs[key, target].read_bytes(), 'Native control mismatch'
            controls.append({'source': key, 'target': target, 'byteIdentical': True})
        assert hashes == {key: digest(path) for key, path in sources.items()}
        guard.release(identity)
        evidence.update(status='PASS', sources=len(sources), fixturePreparationExports=1,
                        localeNumericNotationVariants=locale_variants,
                        cellQualityContract='exact text, decimal-equivalent for two fixture numeric cells',
                        directNativeControls=controls, outputChecks=checks, savedOutputs=saved['outputs'],
                        boundaryRows=len(expected_by_source['boundary']), sourceBytesUnchanged='PASS',
                        sourceSHA256=hashes, quotedLfUnicodeAndFormulaValue='PASS', pdfHeader='PASS')
    finally:
        guard.close()
        evidence['elapsedMs'] = round((time.monotonic() - started) * 1000)
        (args.output / 'evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2),
                                                encoding='utf-8')
        print(json.dumps(evidence, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
