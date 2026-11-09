"""Targeted fidelity checks on the unchanged current 178-source upload.

Requires --input directory, --executable installed LibreOffice, --output NEW_DIR.
Complements the primary-route survey; this is not the historic 5723-task run.
PICT's simple DirectBitsRect bitmap is decoded independently with stdlib.
Legacy PPT visible text atoms are read independently from the original CFB.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import struct
import sys
import threading
import time
import unicodedata

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def compact(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFC', text))


class HtmlContent(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.suppressed, self.parts, self.hrefs = 0, [], []

    def handle_starttag(self, tag, attrs):
        if tag.lower() in ('head', 'style', 'script'):
            self.suppressed += 1
        if tag.lower() in ('a', 'area') and dict(attrs).get('href'):
            self.hrefs.append(dict(attrs)['href'])

    def handle_endtag(self, tag):
        if tag.lower() in ('head', 'style', 'script'):
            self.suppressed = max(0, self.suppressed - 1)

    def handle_data(self, data):
        if not self.suppressed:
            self.parts.append(data)


def html_content(path):
    value = HtmlContent()
    value.feed(Path(path).read_text(encoding='utf-8-sig'))
    value.close()
    return value


def ppt_slide_atoms(raw):
    """MS-PPT record types 1006/4000/4008; exclude masters and notes."""
    if len(raw) > 64 * 1024 * 1024:
        raise ValueError('PPT oracle stream budget exceeded')
    texts, slides, records = [], 0, 0

    def walk(start, end, visible=False, depth=0):
        nonlocal slides, records
        if depth > 40:
            raise ValueError('PPT oracle nesting budget exceeded')
        position = start
        while position < end:
            records += 1
            if records > 100000 or position + 8 > end:
                raise ValueError('PPT oracle record budget/truncation')
            version, kind, size = struct.unpack_from('<HHI', raw, position)
            position += 8
            if position + size > end:
                raise ValueError('PPT oracle record exceeds container')
            if kind == 1006:
                slides += 1
            scope = visible or kind == 1006
            if kind in (4000, 4008) and scope:
                text = raw[position:position + size].decode(
                    'utf-16-le' if kind == 4000 else 'cp1252', errors='strict')
                if compact(text):
                    texts.append(text)
            elif version & 15 == 15:
                walk(position, position + size, scope, depth + 1)
            position += size

    walk(0, len(raw))
    if not slides or not texts:
        raise ValueError('PPT oracle found no slide text')
    return texts, slides


def ppt_source_text(path):
    import olefile
    with olefile.OleFileIO(path) as source:
        stream = source.openstream('PowerPoint Document')
        if stream.size > 64 * 1024 * 1024:
            raise ValueError('PPT document stream too large')
        return ppt_slide_atoms(stream.read())


def pict_rgb(path):
    """Decode this corpus's PICT v2 DirectBitsRect PackBits RGB bitmap.

    Unsupported drawing opcodes are refused, never treated as a blank bitmap.
    This decoder is only the independent QA oracle, not the product importer.
    """
    raw = Path(path).read_bytes()
    if len(raw) > 32 * 1024 * 1024 or len(raw) < 552:
        raise ValueError('PICT oracle input size')
    top, left, bottom, right = struct.unpack_from('>4h', raw, 514)
    width, height = right - left, bottom - top
    if (top, left) != (0, 0) or width <= 0 or height <= 0 or width * height > 16 * 1024 * 1024:
        raise ValueError('PICT oracle frame unsupported')
    if raw[522:528] != b'\0\x11\x02\xff\x0c\0':
        raise ValueError('PICT oracle expected v2 extended header')
    position = 552
    if raw[position:position + 2] == b'\0\x01':
        region = struct.unpack_from('>H', raw, position + 2)[0]
        if region != 10 or struct.unpack_from('>4h', raw, position + 4) != (top, left, bottom, right):
            raise ValueError('PICT oracle nonrectangular clip')
        position += 2 + region
    if raw[position:position + 2] != b'\0\x9a':
        raise ValueError('PICT oracle expected one DirectBitsRect')
    position += 2
    # baseAddr, rowBytes, bounds, PixMap fixed fields.
    position += 4
    row_bytes = struct.unpack_from('>H', raw, position)[0] & 0x3fff
    bounds = struct.unpack_from('>4h', raw, position + 2)
    pack_type = struct.unpack_from('>H', raw, position + 12)[0]
    pixel_type, pixel_size, components, component_size = struct.unpack_from('>4H', raw, position + 26)
    if (bounds != (top, left, bottom, right) or row_bytes != width * 4 or pack_type != 4
            or (pixel_type, pixel_size, components, component_size) != (16, 32, 3, 8)):
        raise ValueError('PICT oracle PixMap variant unsupported')
    position += 46
    source_rect = struct.unpack_from('>4h', raw, position)
    target_rect = struct.unpack_from('>4h', raw, position + 8)
    mode = struct.unpack_from('>H', raw, position + 16)[0]
    if source_rect != bounds or target_rect != bounds or mode not in (0, 64):
        raise ValueError('PICT oracle rectangle transfer unsupported')
    position += 18
    rgb = bytearray()
    for _ in range(height):
        size_bytes = 2 if row_bytes > 250 else 1
        if position + size_bytes > len(raw):
            raise ValueError('PICT oracle missing scanline')
        count = int.from_bytes(raw[position:position + size_bytes], 'big')
        position += size_bytes
        end = position + count
        if end > len(raw):
            raise ValueError('PICT oracle truncated scanline')
        decoded = bytearray()
        while position < end:
            control = raw[position]
            position += 1
            if control < 128:
                length = control + 1
                if position + length > end:
                    raise ValueError('PICT oracle truncated literal')
                decoded.extend(raw[position:position + length])
                position += length
            elif control > 128:
                if position >= end:
                    raise ValueError('PICT oracle truncated repeat')
                decoded.extend(bytes((raw[position],)) * (257 - control))
                position += 1
            if len(decoded) > width * 3:
                raise ValueError('PICT oracle oversized scanline')
        if len(decoded) != width * 3:
            raise ValueError('PICT oracle incomplete RGB planes')
        rgb.extend(value for x in range(width) for value in
                   (decoded[x], decoded[width + x], decoded[2 * width + x]))
    if position % 2:
        position += 1
    if raw[position:position + 2] != b'\0\xff' or any(raw[position + 2:]):
        raise ValueError('PICT oracle unexpected trailing drawing content')
    return (width, height), bytes(rgb)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--executable', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    from PIL import Image
    from PySide6.QtGui import QGuiApplication
    from app_backend.converter_guard import LibreOfficeAdapter
    from app_backend.converter_guard.errors import GuardError
    app = QGuiApplication.instance() or QGuiApplication([])
    adapter, cancel = LibreOfficeAdapter(args.executable), threading.Event()
    if not adapter.available:
        raise RuntimeError('Required native LibreOffice engine is unavailable')
    cases, originals, proofs, serial = [], {}, {}, 0
    expected_cases = 32
    completed = False

    def source(ext, level):
        path = args.input / ('NEW_0%d_LEVEL_%d.%s' % (level, level, ext))
        originals[path] = digest(path)
        return path

    def convert(path, target):
        nonlocal serial
        if path not in proofs:
            serial += 1
            proof = adapter.preflight(path, args.output/('probe-%03d' % serial), cancel=cancel, timeout=45)
            if proof.get('state') != 'detected':
                raise ValueError('Native preflight unconfirmed:' + str(proof.get('code')))
            proofs[path] = proof
        adapter.prepare_source(proofs[path])
        serial += 1
        paths = adapter.convert(path, target, args.output/('task-%03d' % serial), cancel=cancel,
                                timeout=90, on_started=lambda: None)
        output = next(p for p in paths if p.suffix.lower() == '.' + target)
        return output, {'mode':adapter.execution_mode(), 'exportInfo':adapter.export_info()}

    def case(path, target, action):
        start = time.monotonic()
        row = {'source':path.name, 'sourceSha256':originals[path], 'target':target}
        try:
            row['status'] = 'PASS'
            row.update(action())
        except Exception as error:
            row.update(status='FAIL', code=getattr(error, 'code', None), reason=str(error),
                       diagnostic=getattr(error, 'diagnostic', None),
                       mode=adapter.execution_mode(), exportInfo=adapter.export_info())
        row['elapsedSeconds'] = round(time.monotonic() - start, 3)
        cases.append(row)
        print('CURRENT_FIDELITY_CASE=' + json.dumps({k:row[k] for k in (
            'source','target','status','code','reason','elapsedSeconds') if k in row}, ensure_ascii=True), flush=True)

    def raster(path, target):
        if path.suffix == '.pct':
            size, pixels = pict_rgb(path)
        else:
            with Image.open(path) as expected:
                size, pixels = expected.size, expected.convert('RGB').tobytes()
        output, extra = convert(path, target)
        with Image.open(output) as actual:
            if actual.size != size or actual.convert('RGB').tobytes() != pixels:
                raise AssertionError('Pixel canvas or decoded RGB differs from the independent source oracle')
            if path.suffix == '.pcx' and any(abs(float(x) - 100) > .05 for x in actual.info.get('dpi', (0, 0))):
                raise AssertionError('Source 100 DPI not retained')
        if path.suffix == '.pcd' and 'MEDIA_PHOTOCD_BASE_ONLY' not in extra['exportInfo'].get('warnings', ()):
            raise AssertionError('PhotoCD Base scope warning missing')
        if path.suffix == '.psd' and 'MEDIA_PSD_MERGED_COMPOSITE' not in extra['exportInfo'].get('warnings', ()):
            raise AssertionError('PSD editing/layer limitation missing')
        return {**extra, 'size':list(size), 'rgbCompared':True, 'rgbSha256':hashlib.sha256(pixels).hexdigest(),
                'outputSha256':digest(output), 'notChecked':['original_visual_equivalence','editing_objects']}

    def html(path):
        before = html_content(path)
        output, extra = convert(path, path.suffix[1:])
        after = html_content(output)
        if compact(''.join(before.parts)) not in compact(''.join(after.parts)):
            raise AssertionError('Visible source HTML text missing/reordered')
        if before.hrefs and Counter(before.hrefs) != Counter(after.hrefs):
            raise AssertionError('Source hyperlink targets changed')
        urls = [url for part in before.parts for url in re.findall(r'https?://[^\s<>]+', part)]
        if any(compact(url) not in compact(''.join(after.parts)) for url in urls):
            raise AssertionError('Plain source URL text changed')
        return {**extra, 'textCompared':True, 'sourceHyperlinks':len(before.hrefs),
                'hyperlinkTargetsCompared':bool(before.hrefs), 'plainUrlsCompared':urls,
                'notChecked':['visible_print_clipping','historical_124_rewritten_outputs']}

    def gif(path, target):
        with Image.open(path) as before:
            count = before.n_frames
            expected = []
            for index in range(count):
                before.seek(index)
                expected.append((before.size, before.convert('RGBA').tobytes()))
        output, extra = convert(path, target)
        with Image.open(output) as after:
            if after.n_frames != len(expected):
                raise AssertionError('GIF frame/page count changed')
            for index, (size, rgba) in enumerate(expected):
                after.seek(index)
                if after.size != size or after.convert('RGBA').tobytes() != rgba:
                    raise AssertionError('GIF decoded frame pixels changed')
        return {**extra, 'framesCompared':count, 'rgbaCompared':True,
                'outputSha256':digest(output), 'notChecked':['source_visual_equivalence']}

    def ppt(path):
        texts, slides = ppt_source_text(path)
        expected = Counter(compact(text) for text in texts)
        try:
            output, extra = convert(path, 'xhtml')
        except GuardError as error:
            if error.code not in ('ENGINE_OUTPUT_CONTENT_EMPTY', 'ENGINE_OUTPUT_CONTENT_LOSS'):
                raise
            return {'status':'SAFE_REFUSAL', 'code':error.code, 'sourceTextAtoms':len(texts),
                    'slides':slides, 'outputPublished':False, 'notChecked':['successful_XHTML_conversion']}
        observed = compact(''.join(html_content(output).parts))
        missing = {text:count for text,count in expected.items() if observed.count(text) < count}
        if missing:
            raise AssertionError('PPT visible text atom occurrences missing: ' + str(missing))
        return {**extra, 'sourceTextAtoms':len(texts), 'slides':slides, 'textOccurrencesCompared':True,
                'notChecked':['slide_layout','images','animations','hyperlink_functionality']}

    try:
        with adapter.batch(args.output/'office'):
            for level in (1, 2):
                for ext in ('pcx','pcd','pct','tga','psd'):
                    path = source(ext, level)
                    for target in ('png','tiff'):
                        case(path, target, lambda p=path,t=target:raster(p,t))
                for ext in ('html','htm','xhtml'):
                    path = source(ext, level)
                    case(path, ext, lambda p=path:html(p))
                path = source('gif', level)
                for target in ('gif', 'tiff'):
                    case(path, target, lambda p=path,t=target:gif(p,t))
                path = source('ppt', level)
                case(path, 'xhtml', lambda p=path:ppt(p))
        completed = len(cases) == expected_cases
    finally:
        source_checks = [{'source':p.name,'sha256Before':sha,'sha256After':digest(p),
                          'unchanged':digest(p)==sha} for p,sha in originals.items()]
        failed = not completed or any(row['status']=='FAIL' for row in cases) or any(not row['unchanged'] for row in source_checks)
        report = {'schema':1,'status':'FAIL' if failed else 'PASS','cases':cases,'sourceIntegrity':source_checks,
            'expectedCases':expected_cases, 'completed':completed,
            'scope':'Targeted actual20261108 corpus; exact raster properties/HTML text/PPT visible text atoms',
            'historical5723Tasks':'NOT_RUN','complete178Matrix':False,'visiblePrintClipping':'NOT_CHECKED'}
        (args.output/'evidence.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print('CURRENT_FIDELITY_NATIVE=' + json.dumps({'status':report['status'],
            'counts':dict(Counter(row['status'] for row in cases))}),flush=True)
    del app
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
