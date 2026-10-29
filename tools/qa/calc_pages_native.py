"""Calc -> all printed PNG/JPEG pages through the actual AIO adapter.

Synthetic input with explicit print breaks, landscape sheet, hidden sheet,
print area, 50% scaling and fit-to-page. Not historical FULLTEST acceptance.
"""
import hashlib
import json
from pathlib import Path
import threading
import time


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _fixture(path, mode):
    from openpyxl import Workbook
    from openpyxl.styles import PatternFill
    from openpyxl.worksheet.pagebreak import Break
    from openpyxl.worksheet.properties import PageSetupProperties
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = 'Portrait'
    second = workbook.create_sheet('Landscape')
    hidden = workbook.create_sheet('Hidden control')
    hidden.sheet_state = 'hidden'
    hidden['A1'] = 'HIDDEN_IS_NOT_A_PRINT_PAGE'
    for current, count, colour in ((sheet, 60, 'FF8888'), (second, 10, '88FF88')):
        current.column_dimensions['A'].width = 24
        current.column_dimensions['B'].width = 15
        for row in range(1, count + 1):
            current.cell(row, 1, '%s_ROW_%03d' % (current.title, row))
            current.cell(row, 2, row)
            current.row_dimensions[row].height = 16
            for cell in current[row][:2]:
                cell.fill = PatternFill('solid', fgColor=colour)
        current.print_area = 'A1:B%d' % count
        current['A200'] = 'OUTSIDE_PRINT_AREA_NOT_A_PRINT_PAGE'
        current.page_setup.paperSize = current.PAPERSIZE_A4
        current.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=False)
        current.page_setup.scale = 100
    second.page_setup.orientation = 'landscape'
    if mode == 'breaks':
        sheet.row_breaks.append(Break(id=20))
        sheet.row_breaks.append(Break(id=40))
    elif mode == 'scale':
        sheet.page_setup.scale = 50
    elif mode == 'fit':
        sheet.sheet_properties.pageSetUpPr.fitToPage = True
        sheet.page_setup.scale = None
        sheet.page_setup.fitToWidth = 1
        sheet.page_setup.fitToHeight = 1
    else:
        raise ValueError(mode)
    workbook.save(path)
    workbook.close()


def run(executable, root):
    from PIL import Image
    from app_backend.converter_guard import LibreOfficeAdapter
    root = Path(root)
    root.mkdir(parents=True, exist_ok=False)
    cancel = threading.Event()
    adapter = LibreOfficeAdapter(executable)
    results = []
    started = time.monotonic()
    for mode, expected in (('breaks', 4), ('scale', 2), ('fit', 2)):
        source = root / ('calc-' + mode + '.xlsx')
        _fixture(source, mode)
        original = _digest(source)
        with adapter.batch(root / ('office-' + mode)):
            proof = adapter.preflight(source, root / ('probe-' + mode), cancel=cancel, timeout=90)
            assert proof.get('state') == 'detected', proof
            targets = ('png','jpg','jpeg','jfif','jif','jpe') if mode == 'breaks' else ('png','jpg')
            for target in targets:
                adapter.prepare_source(proof)
                tick = time.monotonic()
                paths = adapter.convert(source, target, root / (mode + '-' + target),
                    cancel=cancel, timeout=120, on_started=lambda: None)
                assert adapter.execution_mode() == 'native_uno', adapter.execution_mode()
                info = adapter.export_info()
                receipt = next(check for check in info.get('resultChecks', [])
                               if check.get('policyId') == 'native_all_calc_print_pages_v1')
                assert receipt['expectedUnits'] == receipt['observedOutputs'] == expected, receipt
                assert receipt['qualityCompared'] is False
                assert 'NATIVE_CALC_RASTER_PAGES' in info.get('warnings', ()), info
                manifest = json.loads(next(p for p in paths if p.name == 'AIO-pages.json').read_text(encoding='utf-8'))
                assert manifest['pageCount'] == len(manifest['pages']) == expected, manifest
                assert manifest['scope'] == 'loaded_calc_print_layout'
                assert manifest['respectsPrintSettings'] is True
                assert [p['page'] for p in manifest['pages']] == list(range(1, expected+1))
                grouped = {0: set(), 1: set()}
                hashes = []
                for row in manifest['pages']:
                    path = next(p for p in paths if p.name == row['file'])
                    index = row['sheetIndex']
                    assert index in grouped, row
                    first_col, first_row, last_col, last_row = row['sourceRange']
                    assert first_col == 0 and last_col == 1, row
                    assert 0 <= first_row <= last_row < (60 if index == 0 else 10), row
                    grouped[index].update(range(first_row, last_row+1))
                    with Image.open(path) as image:
                        assert list(image.size) == row['pixelSize'], (image.size,row)
                        width,height=image.size
                        assert (width < height) if index == 0 else (width > height), row
                        rgb=image.convert('RGB')
                        hashes.append(hashlib.sha256(rgb.tobytes()).hexdigest())
                        # Detect blank pages/repetition of the first sheet;
                        # this is not OCR or full visual equivalence.
                        rgb.thumbnail((100,100))
                        colours = list(rgb.getdata())
                        hits = sum((r > g+25 and r > b+25) if index==0 else
                                   (g > r+25 and g > b+25) for r,g,b in colours)
                        assert hits >= 10, (mode,target,row,'page colour missing',hits)
                assert grouped[0] == set(range(60)) and grouped[1] == set(range(10)), grouped
                if mode == 'breaks':
                    assert len(set(hashes)) == expected, (mode,target,'repeated page content')
                assert _digest(source) == original, 'source changed'
                results.append({'mode':mode,'target':target,'status':'PASS','pages':expected,
                                'sourceUnchanged':True,'ms':round((time.monotonic()-tick)*1000)})
    report={'status':'PASS','scope':'synthetic Calc print-layout controls, not original FULLTEST',
            'cases':results,'elapsedSeconds':round(time.monotonic()-started,3)}
    (root/'evidence.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print('CALC_PAGES_NATIVE='+json.dumps(report,ensure_ascii=True),flush=True)
    return report
