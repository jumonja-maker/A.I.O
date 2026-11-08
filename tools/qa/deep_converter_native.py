"""Generated native controls for deep-audit fixes, not universal fidelity QA.

No network, original owner corpus, personal profile, installer or publication.
Known engine observations remain explicitly separate from required safeguards.
"""
from __future__ import annotations
import argparse
from io import BytesIO
import hashlib
import json
from pathlib import Path
import random
import sys
import threading
import time
from xml.etree import ElementTree as ET
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def pdf_image(path, expected_size):
    from pypdf import PdfReader
    reader = PdfReader(path)
    try:
        candidates = [image.image.convert('RGB') for page in reader.pages for image in page.images
                      if image.image is not None and image.image.size == expected_size]
        if len(candidates) != 1:
            raise AssertionError('Expected one independent full source image: ' + str(len(candidates)))
        return candidates[0].tobytes(), len(reader.pages)
    finally:
        reader.close()


def precision_fixture(path):
    from openpyxl import Workbook
    book = Workbook(); sheet = book.active
    sheet.append(['Marker', 'Number'])
    sheet.append(['EXACT_EVEN_INTEGER', 42]); sheet.append(['SEVENTEEN_DIGITS', 43])
    output = BytesIO(); book.save(output); book.close()
    values = {'B2': '10000000000000002', 'B3': '12345678901234567'}
    with ZipFile(BytesIO(output.getvalue())) as source, ZipFile(path, 'x', ZIP_DEFLATED) as target:
        for name in source.namelist():
            content = source.read(name)
            if name == 'xl/worksheets/sheet1.xml':
                tree = ET.fromstring(content)
                for cell in tree.findall('.//{*}c'):
                    if cell.get('r') in values:
                        cell.find('{*}v').text = values[cell.get('r')]
                content = ET.tostring(tree)
            target.writestr(name, content)
    return {coordinate: int(value) for coordinate, value in values.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(); args.output.mkdir(parents=True, exist_ok=False)
    from PySide6.QtGui import QGuiApplication
    from PIL import Image
    from openpyxl import Workbook, load_workbook
    from app_backend.converter_guard import LibreOfficeAdapter
    from app_backend.converter_guard.errors import GuardError
    from app_backend.converter_transport import ConverterTransport
    from app_backend.libreoffice_engine import LibreOfficeError, convert_path
    app = QGuiApplication.instance() or QGuiApplication([])
    cancel = threading.Event(); cases = []; originals = {}
    started = time.monotonic()
    report = {'schema': 1, 'status': 'FAIL', 'cases': cases,
              'scope': 'generated native deep-audit safeguards and distinct engine observation; not full visual QA',
              'historicalFULLTEST': 'NOT_RUN', 'desktopGUI': 'NOT_RUN'}

    def remember(path):
        originals[path] = digest(path); return path

    def prepare(adapter, source, name):
        proof = adapter.preflight(source, args.output/(name+'-preflight'), cancel=cancel, timeout=90)
        assert proof.get('state') == 'detected', proof
        adapter.prepare_source(proof)
        return proof

    try:
        randomizer = random.Random(81273)
        pixels = bytes(randomizer.randrange(256) for _ in range(64*64*3))
        source = args.output/'noise.png'
        Image.frombytes('RGB', (64,64), pixels).save(source); remember(source)
        begin = time.monotonic()
        raw = convert_path(source, 'pdf', args.output/'legacy-default-pdf', executable=args.executable)
        actual, pages = pdf_image(next(p for p in raw if p.suffix == '.pdf'), (64,64))
        assert actual == pixels, 'Direct PDF export added image loss'
        cases.append({'case': 'legacy_default_pdf_source_pixels', 'status': 'PASS', 'pages': pages,
                      'pixelSha256': hashlib.sha256(actual).hexdigest(),
                      'elapsedSeconds': round(time.monotonic()-begin,3)})

        adapter = LibreOfficeAdapter(args.executable)
        with adapter.batch(args.output/'office'):
            prepare(adapter, source, 'pdf')
            begin = time.monotonic()
            outputs = adapter.convert(source, 'pdf', args.output/'guard-default-pdf', cancel=cancel,
                                      timeout=120, on_started=lambda: None)
            actual, pages = pdf_image(next(p for p in outputs if p.suffix == '.pdf'), (64,64))
            assert actual == pixels, 'Ordinary Guard PDF export added image loss'
            cases.append({'case': 'guard_default_pdf_source_pixels', 'status': 'PASS', 'pages': pages,
                          'pixelSha256': hashlib.sha256(actual).hexdigest(),
                          'executionMode': adapter.execution_mode(),
                          'elapsedSeconds': round(time.monotonic()-begin,3)})

            csv_source = args.output/'precise-literals.csv'
            csv_source.write_text('CustomerID,Literal,Count\n12345678901234567,=1+1,2\n'
                                  '99999999999999999,normal,3\n', encoding='utf-8')
            remember(csv_source); prepare(adapter, csv_source, 'csv')
            begin = time.monotonic()
            outputs = adapter.convert(csv_source, 'xlsx', args.output/'csv-to-xlsx', cancel=cancel,
                                      timeout=120, on_started=lambda: None)
            book = load_workbook(next(p for p in outputs if p.suffix == '.xlsx'), read_only=True)
            try:
                values = [(book.active[cell].value, book.active[cell].data_type)
                          for cell in ('A2','A3','B2','C2')]
            finally:
                book.close()
            assert values[:3] == [('12345678901234567','s'), ('99999999999999999','s'), ('=1+1','s')], values
            assert values[3] == (2,'n'), values
            cases.append({'case': 'csv_precision_formula_literals_and_unaffected_numeric',
                          'status': 'PASS', 'values': values, 'exportInfo': adapter.export_info(),
                          'elapsedSeconds': round(time.monotonic()-begin,3)})

        # A plain native flat export cannot silently stand for a whole workbook.
        multi = args.output/'two-sheets.xlsx'; book = Workbook()
        for index, sheet in enumerate((book.active, book.create_sheet('Second')),1):
            sheet.append(['Marker','Value']); sheet.append(['SHEET_'+str(index),index])
        book.save(multi); book.close(); remember(multi)
        transport = ConverterTransport(recovery_parent=args.output/'legacy-owned')
        request = transport.begin('d'*32, None)
        try:
            with patch_engine(args.executable):
                try:
                    transport.execute(request, [{'path': multi, 'warnings': []}], ['f'], ['csv'])
                except LibreOfficeError:
                    row = request.files[0]['targets'][0]
                    assert row['code'] == 'LEGACY_OUTPUT_CONTENT_LOSS', row
                    assert not transport.results and not request.published
                    cases.append({'case': 'legacy_multi_sheet_flat_export_refused', 'status': 'PASS_SAFE_REFUSAL',
                                  'code': row['code'], 'sourceSheets': 2, 'published': False})
                else:
                    raise AssertionError('Legacy returned an unchecked first-sheet-only result')
        finally:
            transport.finish(request); transport.close()

        # This separate observation is not certification of native numeric
        # roundtrips. AIO's source-CSV protection does not prove this path.
        precise = args.output/'native-precision.xlsx'
        expected = precision_fixture(precise); remember(precise)
        outputs = convert_path(precise, 'xlsx', args.output/'native-xlsx-roundtrip', executable=args.executable)
        book = load_workbook(next(p for p in outputs if p.suffix == '.xlsx'), read_only=True)
        try: observed = {coordinate: book.active[coordinate].value for coordinate in expected}
        finally: book.close()
        losses = [coordinate for coordinate in expected if observed[coordinate] != expected[coordinate]]
        report['nativeNumericObservation'] = {
            'status': 'OBSERVED_NATIVE_VALUE_CHANGE' if losses else 'CONTROL_VALUES_PRESERVED',
            'sourceValues': expected, 'outputValues': observed, 'differentCoordinates': losses,
            'scope': 'two exact source numbers, not a whole workbook value contract',
            'aIOGeneralXlsxNumericProtection': 'NOT_CERTIFIED_BY_THIS_CONTROL'}
        # Do not turn the thin engine observation into an AIO ready result.
        # Use fresh identical copies: the import-only LOK probe can leave a
        # lock sidecar beside its private input after its worker exits.
        guarded = args.output/'guard-precision.xlsx'; guarded.write_bytes(precise.read_bytes()); remember(guarded)
        adapter = LibreOfficeAdapter(args.executable)
        with adapter.batch(args.output/'numeric-office'):
            prepare(adapter, guarded, 'numeric')
            try:
                outputs = adapter.convert(guarded, 'xlsx', args.output/'guard-numeric', cancel=cancel,
                                          timeout=120, on_started=lambda: None)
            except GuardError as error:
                assert error.code == 'ENGINE_OUTPUT_CONTENT_LOSS', error.as_dict()
                cases.append({'case': 'guard_high_precision_xlsx', 'status': 'PASS_SAFE_REFUSAL',
                              'code': error.code})
            else:
                book = load_workbook(next(p for p in outputs if p.suffix == '.xlsx'), read_only=True)
                try: values = {coordinate: book.active[coordinate].value for coordinate in expected}
                finally: book.close()
                assert values == expected, values
                cases.append({'case': 'guard_high_precision_xlsx', 'status': 'PASS_VALUES_PRESERVED'})

        legacy_precise = args.output/'legacy-precision.xlsx'
        legacy_precise.write_bytes(precise.read_bytes()); remember(legacy_precise)
        transport = ConverterTransport(recovery_parent=args.output/'numeric-legacy-owned')
        request = transport.begin('e'*32, None)
        try:
            with patch_engine(args.executable):
                try:
                    result = transport.execute(request, [{'path': legacy_precise, 'warnings': []}], ['f'], ['xlsx'])
                except LibreOfficeError:
                    row = request.files[0]['targets'][0]
                    assert row['code'] == 'LEGACY_OUTPUT_CONTENT_LOSS', row
                    assert not transport.results and not request.published
                    cases.append({'case': 'legacy_high_precision_xlsx', 'status': 'PASS_SAFE_REFUSAL',
                                  'code': row['code'], 'published': False})
                else:
                    saved = transport.results[result['result']['ticket']]
                    book = load_workbook(saved.path, read_only=True)
                    try: values = {coordinate: book.active[coordinate].value for coordinate in expected}
                    finally: book.close()
                    assert values == expected, values
                    cases.append({'case': 'legacy_high_precision_xlsx', 'status': 'PASS_VALUES_PRESERVED'})
        finally:
            transport.finish(request, retained=request.published); transport.close()
        assert all(digest(path) == checksum for path, checksum in originals.items())
        report.update(status='PASS', sourceUnchanged=True, sourceHashes={p.name:v for p,v in originals.items()},
                      elapsedSeconds=round(time.monotonic()-started,3))
    except Exception as error:
        report.update(error=type(error).__name__, reason=str(error), code=getattr(error,'code',None))
        raise
    finally:
        (args.output/'evidence.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print('DEEP_CONVERTER_NATIVE='+json.dumps(report,ensure_ascii=True),flush=True)


def patch_engine(executable):
    """Select the explicit trusted test engine without changing a user's profile."""
    import os
    from unittest.mock import patch
    return patch.dict(os.environ, {'AIO_LIBREOFFICE': str(executable)})


if __name__ == '__main__': main()
