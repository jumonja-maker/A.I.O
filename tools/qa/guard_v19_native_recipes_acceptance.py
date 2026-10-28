"""Actual installed-LibreOffice proof for both finite before/after recipes.

Uses only existing openpyxl/pypdf and the standard library. The RGB PNG and its
OOXML drawing are generated without Pillow, reportlab, user files or network.
This proves one image's PDF pixels and two synthetic CSV sheets, not general
document fidelity, visual layout, fonts or arbitrary localized CSV dialects.
On Windows, --require-mode native_uno prevents a CLI fallback being mistaken
for evidence of the production UNO path. The lossy PDF control explicitly uses
the same installation's CLI; it is not a product default or an automatic recipe.
"""
import argparse
import csv
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import random
import struct
import sys
import threading
import time
import xml.etree.ElementTree as ET
from zipfile import ZipFile
import zlib

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

MAIN = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
PACKAGE_REL = 'http://schemas.openxmlformats.org/package/2006/relationships'
CONTENT_TYPES = 'http://schemas.openxmlformats.org/package/2006/content-types'


def sha256(blob):
    return hashlib.sha256(blob).hexdigest()


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def rgb_png(width=1024, height=512):
    """A deterministic RGB image with enough entropy to expose lossy changes."""
    pixels = random.Random(190025).randbytes(width * height * 3)

    def chunk(kind, data):
        return (struct.pack('!I', len(data)) + kind + data +
                struct.pack('!I', zlib.crc32(kind + data) & 0xffffffff))

    stride = width * 3
    rows = b''.join(b'\0' + pixels[row * stride:(row + 1) * stride]
                    for row in range(height))
    png = (b'\x89PNG\r\n\x1a\n' +
           chunk(b'IHDR', struct.pack('!IIBBBBB', width, height, 8, 2, 0, 0, 0)) +
           chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))
    return png, pixels


def synthetic_workbook(path, png):
    """Attach a native OOXML drawing to an openpyxl-generated two-sheet book."""
    from openpyxl import Workbook
    book = Workbook()
    precision = book.active
    precision.title = 'Precision'
    precision.append(['code', 'amount', 'note'])
    precision.append(['00123', 1.23456789012345, 'Привет'])
    # Keep this synthetic numeric control independent from the owner's locale.
    # CSV token 6 is an import option and cannot pin export decimal syntax.
    precision['B2'].number_format = '[$-409]0.00'
    second = book.create_sheet('Second')
    second.append(['OTHER_SHEET', 'code'])
    second.append(['Сохранён второй лист', '00500'])
    book.save(path)
    book.close()
    with ZipFile(path) as archive:
        entries = {info.filename: (info, archive.read(info)) for info in archive.infolist()}
    sheet = ET.fromstring(entries['xl/worksheets/sheet1.xml'][1])
    sheet.append(ET.Element('{' + MAIN + '}drawing', {'{' + REL + '}id': 'rIdAioImage'}))
    types = ET.fromstring(entries['[Content_Types].xml'][1])
    ET.SubElement(types, '{' + CONTENT_TYPES + '}Default',
                  {'Extension': 'png', 'ContentType': 'image/png'})
    ET.SubElement(types, '{' + CONTENT_TYPES + '}Override',
                  {'PartName': '/xl/drawings/drawing1.xml',
                   'ContentType': 'application/vnd.openxmlformats-officedocument.drawing+xml'})
    drawing = '''<?xml version="1.0" encoding="UTF-8"?>
<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
 <xdr:oneCellAnchor><xdr:from><xdr:col>0</xdr:col><xdr:colOff>0</xdr:colOff>
 <xdr:row>4</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>
 <xdr:ext cx="1828800" cy="914400"/><xdr:pic><xdr:nvPicPr>
 <xdr:cNvPr id="1" name="AIO synthetic RGB control"/><xdr:cNvPicPr/>
 </xdr:nvPicPr><xdr:blipFill><a:blip r:embed="rId1"/>
 <a:stretch><a:fillRect/></a:stretch></xdr:blipFill><xdr:spPr>
 <a:prstGeom prst="rect"><a:avLst/></a:prstGeom></xdr:spPr>
 </xdr:pic><xdr:clientData/></xdr:oneCellAnchor></xdr:wsDr>'''
    def relationships(identity, target, kind):
        root = ET.Element('{' + PACKAGE_REL + '}Relationships')
        ET.SubElement(root, '{' + PACKAGE_REL + '}Relationship',
                      {'Id': identity, 'Target': target, 'Type': REL + '/' + kind})
        return ET.tostring(root, encoding='utf-8', xml_declaration=True)

    replacements = {
        'xl/worksheets/sheet1.xml': ET.tostring(sheet, encoding='utf-8', xml_declaration=True),
        '[Content_Types].xml': ET.tostring(types, encoding='utf-8', xml_declaration=True),
        'xl/worksheets/_rels/sheet1.xml.rels': relationships(
            'rIdAioImage', '../drawings/drawing1.xml', 'drawing'),
        'xl/drawings/drawing1.xml': drawing.encode(),
        'xl/drawings/_rels/drawing1.xml.rels': relationships('rId1', '../media/image1.png', 'image'),
        'xl/media/image1.png': png,
    }
    with ZipFile(path, 'w') as archive:
        for name, (info, contents) in entries.items():
            archive.writestr(info, replacements.pop(name, contents))
        for name, contents in replacements.items():
            archive.writestr(name, contents)


def pdf_images(path):
    """Inspect image streams directly; do not rely on Pillow-backed .images."""
    from pypdf import PdfReader
    observed = []
    reader = PdfReader(path)
    seen = set()

    def resources(dictionary):
        if hasattr(dictionary, 'get_object'):
            dictionary = dictionary.get_object()
        xobjects = dictionary.get('/XObject', {})
        if hasattr(xobjects, 'get_object'):
            xobjects = xobjects.get_object()
        for identity, reference in xobjects.items():
            key = (getattr(reference, 'idnum', id(reference)),
                   getattr(reference, 'generation', 0))
            if key in seen:
                continue
            seen.add(key)
            item = reference.get_object()
            if item.get('/Subtype') == '/Form':
                resources(item.get('/Resources', {}))
                continue
            if item.get('/Subtype') != '/Image':
                continue
            filters = item.get('/Filter', [])
            if not isinstance(filters, list):
                filters = [filters]
            decoded = item.get_data()
            width, height = int(item['/Width']), int(item['/Height'])
            colour, bits = str(item.get('/ColorSpace')), int(item.get('/BitsPerComponent', 0))
            decoded_rgb = (colour == '/DeviceRGB' and bits == 8 and
                           len(decoded) == width * height * 3 and
                           all(str(kind) in ('/FlateDecode', '/LZWDecode', '/ASCII85Decode')
                               for kind in filters))
            observed.append({'name': str(identity), 'width': width, 'height': height,
                'colourSpace': colour, 'bitsPerComponent': bits,
                'filters': [str(kind) for kind in filters], 'decodedBytes': len(decoded),
                'decodedRGB': decoded_rgb,
                'decodedRGBSha256': sha256(decoded) if decoded_rgb else None})
    for page in reader.pages:
        resources(page.get('/Resources', {}))
    return observed


class Deadline:
    def __init__(self, seconds):
        self.until = time.monotonic() + seconds
        self.event = threading.Event()

    def remaining(self):
        value = self.until - time.monotonic()
        if value <= 0:
            raise TimeoutError('Native recipe acceptance exceeded its own deadline')
        return value

    def is_set(self):
        self.remaining()
        return self.event.is_set()

    def wait(self, seconds):
        self.event.wait(min(seconds, self.remaining()))
        return self.is_set()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--executable', type=Path, required=True)
    parser.add_argument('--seconds', type=float, default=240)
    parser.add_argument('--require-mode', choices=('native_uno', 'native_cli'))
    args = parser.parse_args(argv)
    if not args.executable.is_file() or args.seconds <= 0:
        parser.error('Existing LibreOffice executable and positive seconds required')
    args.output.mkdir(parents=True, exist_ok=False)
    evidence = {'schema': 1, 'status': 'FAIL', 'platform': sys.platform,
        'scope': 'actual installed LO: one synthetic RGB image and two CSV sheets',
        'fullFormatMatrix': 'NOT_RUN', 'windowsGUI': 'NOT_RUN',
        'arbitraryLocalizedCSV': 'NOT_PROVED', 'executions': {}}
    started = time.monotonic()
    try:
        from app_backend.converter_guard.adapter import LibreOfficeAdapter, prepare_profile
        from app_backend.converter_guard.export_recipe import native_export_recipe
        from app_backend.libreoffice_engine import convert_path
        control = Deadline(args.seconds)
        png, pixels = rgb_png()
        (args.output / 'synthetic-rgb.png').write_bytes(png)
        source = args.output / 'Пример книги с рисунком.xlsx'
        synthetic_workbook(source, png)
        original_hash = sha256(source.read_bytes())
        evidence['source'] = {'name': source.name, 'sha256': original_hash,
            'imageWidth': 1024, 'imageHeight': 512, 'imageDisplayPixels': [192, 96],
            'rgbSha256': sha256(pixels), 'sheets': ['Precision', 'Second'],
            'numericControlLocale': 'en-US', 'numberFormat': '[$-409]0.00'}
        adapter = LibreOfficeAdapter(args.executable.resolve())
        expected_mode = args.require_mode or ('native_uno' if os.name == 'nt' and
            (args.executable.parent / 'python.exe').is_file() else 'native_cli')
        evidence['requiredProductionMode'] = expected_mode
        with adapter.batch(args.output / 'native-session'):
            proof = adapter.preflight(source, args.output / 'preflight', cancel=control,
                                      timeout=control.remaining())
            evidence['proof'] = proof
            require(proof.get('state') == 'detected', 'Native model preflight did not confirm Calc')
            adapter.prepare_source(proof)
            baseline_recipe = native_export_recipe(adapter._registry(), 'pdf', proof)
            require(baseline_recipe is not None, 'Installed native PDF filter is ambiguous')
            baseline_spec = baseline_recipe.cli_spec('pdf')
            evidence['ordinaryPDFSpecBefore'] = baseline_spec

            def export(label, target, recipe_id):
                adapter.prepare_source(proof)
                adapter.prepare_retry_recipe(recipe_id)
                try:
                    paths = adapter.convert(source, target, args.output / label, cancel=control,
                                            timeout=control.remaining(), on_started=lambda: None)
                    evidence['executions'][label] = {
                        'mode': adapter.execution_mode(), 'export': adapter.export_info(),
                        'timings': adapter.native_timings(),
                        'files': [{'name': path.name, 'sha256': sha256(path.read_bytes())}
                                  for path in paths]}
                    require(adapter.execution_mode() == expected_mode,
                            label + ': unexpected production execution mode')
                    if recipe_id:
                        require(adapter.export_info().get('automaticRecipe') == recipe_id,
                                label + ': automatic recipe provenance is missing')
                    else:
                        require(not adapter.export_info().get('automaticRecipe'),
                                label + ': automatic recipe leaked into ordinary conversion')
                    return paths
                finally:
                    adapter.prepare_retry_recipe(None)

            before = export('ordinary-before', 'pdf', None)[0]
            before_hash = sha256(before.read_bytes())
            evidence['ordinaryImagesBefore'] = pdf_images(before)
            adapter.validate_retry_recipe('pdf_preserve_images', 'pdf', proof)
            candidate = export('pdf-retry', 'pdf', 'pdf_preserve_images')[0]
            evidence['candidateImages'] = pdf_images(candidate)
            after = export('ordinary-after', 'pdf', None)[0]
            evidence['ordinaryImagesAfter'] = pdf_images(after)
            evidence['ordinaryPDFSpecAfter'] = native_export_recipe(
                adapter._registry(), 'pdf', proof).cli_spec('pdf')

            # Deliberately downsample only this fresh task-owned CLI profile.
            # It demonstrates a lower-resolution control, not a default defect.
            lossy = args.output / 'lossy-control'
            prepare_profile(lossy / 'profile')
            params = {'UseLosslessCompression': {'type': 'boolean', 'value': False},
                      'ReduceImageResolution': {'type': 'boolean', 'value': True},
                      'MaxImageResolution': {'type': 'long', 'value': 75},
                      'Quality': {'type': 'long', 'value': 35}}
            spec = 'pdf:' + baseline_recipe.filter_name + ':' + json.dumps(params, separators=(',', ':'))
            lossy_paths = convert_path(source, 'pdf', lossy, executable=args.executable.resolve(),
                                       cancel=control, convert_spec=spec)
            evidence['lossyControl'] = {'mode': 'native_cli', 'filter': baseline_recipe.filter_name,
                'explicitControlParameters': params, 'images': pdf_images(lossy_paths[0])}

            adapter.validate_retry_recipe('csv_preserve_values', 'csv', proof)
            csv_paths = [path for path in export('csv-retry', 'csv', 'csv_preserve_values')
                         if path.suffix.lower() == '.csv']
            rows = {}
            for path in csv_paths:
                with path.open(encoding='utf-8-sig', newline='') as stream:
                    rows[path.name] = list(csv.reader(stream))
            evidence['csvSheets'] = rows
            evidence['sourceUnchanged'] = sha256(source.read_bytes()) == original_hash
            evidence['firstPDFUnchanged'] = sha256(before.read_bytes()) == before_hash

            # Judge only after retaining all native observations for failures.
            source_image = [image for image in evidence['candidateImages']
                            if (image['width'], image['height']) == (1024, 512)]
            require(len(source_image) == 1 and source_image[0]['decodedRGB'] and
                    source_image[0]['decodedRGBSha256'] == sha256(pixels),
                    'Lossless PDF does not contain the exact generated RGB pixels')
            require(any((image['width'], image['height']) == (150, 75)
                        for image in evidence['lossyControl']['images']),
                    'Explicit 75dpi control did not produce the expected 150x75 image')
            require(evidence['ordinaryPDFSpecBefore'] == evidence['ordinaryPDFSpecAfter'] and
                    evidence['ordinaryImagesBefore'] == evidence['ordinaryImagesAfter'],
                    'Automatic recipe changed the following ordinary PDF export')
            require(len(rows) == 2, 'Automatic CSV export did not preserve both sheets')
            grids = list(rows.values())
            precision = next((grid for grid in grids if grid and grid[0] == ['code', 'amount', 'note']), None)
            second = next((grid for grid in grids if grid and grid[0] == ['OTHER_SHEET', 'code']), None)
            require(precision and precision[1][0] == '00123' and precision[1][2] == 'Привет' and
                    Decimal(precision[1][1]) == Decimal('1.23456789012345'),
                    'CSV precision, leading zeros or UTF-8 text changed')
            require(second and second[1] == ['Сохранён второй лист', '00500'],
                    'Second CSV sheet, text or leading zeros changed')
            require(evidence['sourceUnchanged'] and evidence['firstPDFUnchanged'],
                    'Source or initial PDF was modified during retry')
        evidence['status'] = 'PASS'
        return 0
    except Exception as error:
        evidence.update(error=type(error).__name__, reason=str(error),
                        code=getattr(error, 'code', None), stage=getattr(error, 'stage', None),
                        diagnostic=str(getattr(error, 'diagnostic', ''))[-4096:])
        return 1
    finally:
        evidence['elapsedSeconds'] = round(time.monotonic() - started, 3)
        (args.output / 'evidence.json').write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print('AIO_V19_NATIVE_RECIPES_ACCEPTANCE=' + json.dumps(evidence, ensure_ascii=True), flush=True)


if __name__ == '__main__':
    raise SystemExit(main())
