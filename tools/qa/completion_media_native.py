"""Native FT4 media controls with independent pixels and explicit blocked cases.

Run with the application's Python and a real LibreOffice installation:
  python tools/qa/completion_media_native.py --executable PATH --output NEW_DIR

Generated PhotoCD tests its uncompressed base raster only, not every PhotoCD
resolution. The minimal PICT is an independently authored QuickDraw v1 drawing.
Optional --fixture-dir adds original .pcd/.pct/.pict files without changing them.
This bounded test is not the historical FULLTEST or installed desktop GUI QA.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import struct
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while block := stream.read(65536):
            value.update(block)
    return value.hexdigest()


def pcd_fixture(path):
    """PhotoCD base image: independent, interleaved Y/Y/Cb/Cr scanline bytes."""
    header = bytearray(96 * 2048)
    header[2048:2055] = b'PCD_IPI'
    header[2048 + 1538] = 0  # Unrotated landscape base raster.
    with path.open('xb') as stream:
        stream.write(header)
        for y in range(0, 512, 2):
            for row in (y, y + 1):
                stream.write(bytes(32 + (x // 8 + row // 8) % 160 for x in range(768)))
            stream.write(bytes(112 + (x // 12 + y // 16) % 32 for x in range(384)))
            stream.write(bytes(124 + (x // 16 + y // 12) % 32 for x in range(384)))


def pict_fixture(path):
    """QuickDraw v1 picture with a 37x19 clip/frame and a black rectangle."""
    frame = (0, 0, 19, 37)  # top, left, bottom, right, in 72-dpi picture units.
    picture = (struct.pack('>4h', *frame) + b'\x11\x01'  # VersionOp, version 1.
               + b'\x01' + struct.pack('>H4h', 10, *frame)  # Clip region.
               + b'\x31' + struct.pack('>4h', *frame)  # PaintRect, default black.
               + b'\xff')  # EndPic.
    path.write_bytes(bytes(512) + struct.pack('>H', len(picture) + 2) + picture)


def pict_dimensions(path):
    """Read declared frame, independently from the application PICT probe."""
    with path.open('rb') as stream:
        prefix = stream.read(560)
    for offset in (512, 0):
        if len(prefix) < offset + 12:
            continue
        top, left, bottom, right = struct.unpack_from('>4h', prefix, offset + 2)
        version = prefix[offset + 10:offset + 14]
        if (bottom > top and right > left and
                (version.startswith(b'\x11\x01') or version == b'\x00\x11\x02\xff')):
            return right - left, bottom - top
    raise ValueError('PICT frame/version not independently understood')


def drawing_fixture(path, *, rich):
    """Author native Draw shapes, with independently specified source colours."""
    columns, rows = (32, 16) if rich else (3, 1)
    styles, shapes = [], []
    for y in range(rows):
        for x in range(columns):
            index = y * columns + x
            colour = ((x * 7, y * 15, (x * 3 + y * 11) % 256) if rich
                      else ((255, 0, 0), (0, 160, 0), (0, 0, 255))[x])
            styles.append('<style:style style:name="C%d" style:family="graphic">'
                          '<style:graphic-properties draw:fill="solid" '
                          'draw:fill-color="#%02x%02x%02x" draw:stroke="none"/>'
                          '</style:style>' % (index, *colour))
            shapes.append('<draw:rect draw:style-name="C%d" svg:x="%.8fmm" '
                          'svg:y="%.8fmm" svg:width="%.8fmm" svg:height="%.8fmm"/>'
                          % (index, x * 50.8 / columns, y * 25.4 / rows,
                             50.8 / columns, 25.4 / rows))
    content = ('''<?xml version="1.0" encoding="UTF-8"?>
<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
 xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0"
 xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0"
 xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0"
 office:mimetype="application/vnd.oasis.opendocument.graphics" office:version="1.2">
<office:automatic-styles><style:page-layout style:name="PageLayout">
<style:page-layout-properties fo:page-width="50.8mm" fo:page-height="25.4mm"
 fo:margin-top="0mm" fo:margin-bottom="0mm" fo:margin-left="0mm" fo:margin-right="0mm"/>
</style:page-layout>''' + ''.join(styles) + '''</office:automatic-styles>
<office:master-styles><style:master-page style:name="Default" style:page-layout-name="PageLayout"/>
</office:master-styles><office:body><office:drawing>
<draw:page draw:name="Colours" draw:master-page-name="Default">'''
               + ''.join(shapes)
               + '</draw:page></office:drawing></office:body></office:document>')
    path.write_text(content, encoding='utf-8')


class Blocked(RuntimeError):
    pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--fixture-dir', type=Path)
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    if sys.platform.startswith('linux'):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

    cases, originals = [], {}
    report = {'schema': 1, 'status': 'FAIL', 'cases': cases,
              'scope': 'FT4 PCX DPI, selected raster/PICT canvas and consistent strict GIF encoding',
              'historicalFULLTEST': 'NOT_RUN', 'desktopGUI': 'NOT_RUN',
              'platform': platform.platform(), 'python': sys.version.split()[0],
              'executable': str(args.executable), 'generatedFixtureFormat': 1}
    started = time.monotonic()
    counter = 0
    proofs = {}

    def remember(path):
        originals[path] = digest(path)
        return path

    def run_case(name, action):
        begin = time.monotonic()
        row = {'case': name}
        try:
            details = action()
            row.update({'status': 'PASS', **(details or {})})
        except Blocked as error:
            row.update(status='BLOCKED', reason=str(error))
        except Exception as error:
            row.update(status='FAIL', error=type(error).__name__, reason=str(error),
                       code=getattr(error, 'code', None),
                       diagnostic=getattr(error, 'diagnostic', None))
        row['elapsedSeconds'] = round(time.monotonic() - begin, 3)
        cases.append(row)
        summary = {key: row[key] for key in ('case', 'status', 'source', 'executionMode',
                                             'size', 'dpi', 'code', 'reason', 'elapsedSeconds')
                   if key in row}
        print('COMPLETION_MEDIA_CASE=' + json.dumps(summary, ensure_ascii=True), flush=True)

    try:
        from PIL import Image
        from PySide6.QtGui import QGuiApplication
        from app_backend.converter_guard import LibreOfficeAdapter
        from app_backend.converter_guard.errors import GuardError
        app = QGuiApplication.instance() or QGuiApplication([])
        cancel = threading.Event()
        adapter = LibreOfficeAdapter(args.executable)

        fixtures = args.output / 'sources'
        fixtures.mkdir()
        rgb = bytes(channel for y in range(19) for x in range(37)
                    for channel in ((x * 7 + y * 3) % 256, (y * 13) % 256, (x * 11) % 256))
        source_image = Image.frombytes('RGB', (37, 19), rgb)
        pcx = fixtures / 'pcx-100dpi.pcx'
        source_image.save(pcx, format='PCX', dpi=(100, 100))
        # The binary PCX declaration is the independent 100-DPI oracle.
        pcx_data = bytearray(pcx.read_bytes())
        struct.pack_into('<HH', pcx_data, 12, 100, 100)
        pcx.write_bytes(pcx_data)
        tga = fixtures / 'pattern-37x19.tga'
        source_image.save(tga, format='TGA')
        pcd = fixtures / 'base-photo-cd.pcd'
        pcd_fixture(pcd)
        pct = fixtures / 'quickdraw-37x19.pct'
        pict_fixture(pct)
        sources = [(pcx, 'pcx'), (tga, 'tga'), (pcd, 'pcd'), (pct, 'pct')]
        if args.fixture_dir:
            for index, external in enumerate(sorted(args.fixture_dir.iterdir())):
                kind = external.suffix.lower().lstrip('.')
                if external.is_file() and kind in ('pcd', 'pct', 'pict'):
                    remember(external)
                    copied = fixtures / ('external-%03d-' % index + external.name)
                    shutil.copyfile(external, copied)
                    sources.append((copied, 'pcd' if kind == 'pcd' else 'pct'))
        for path, _ in sources:
            remember(path)
        rich = fixtures / 'native-rich.fodg'
        limited = fixtures / 'native-limited.fodg'
        drawing_fixture(rich, rich=True)
        drawing_fixture(limited, rich=False)
        remember(rich)
        remember(limited)
        qt_rich = fixtures / 'qt-rich.png'
        source_image.save(qt_rich)
        remember(qt_rich)
        qt_limited = fixtures / 'qt-limited.png'
        Image.frombytes('RGB', (37, 19), bytes(value for y in range(19) for x in range(37)
                                             for value in ((255, 0, 0) if x < 12 else
                                                           (0, 160, 0) if x < 24 else (0, 0, 255)))).save(qt_limited)
        remember(qt_limited)

        def proof(path):
            nonlocal counter
            if path in proofs:
                return proofs[path]
            counter += 1
            value = adapter.preflight(path, args.output / ('probe-%03d' % counter),
                                      cancel=cancel, timeout=90)
            if value.get('state') != 'detected':
                raise Blocked('preflight:' + json.dumps(value, ensure_ascii=True))
            proofs[path] = value
            return value

        def convert(path, kind, evidence):
            nonlocal counter
            counter += 1
            adapter.prepare_source(evidence)
            try:
                files = adapter.convert(path, kind, args.output / ('task-%03d-%s' % (counter, kind)),
                                        cancel=cancel, timeout=120, on_started=lambda: None)
            except GuardError as error:
                if error.code in ('ENGINE_OUTPUT_VALIDATION_INCOMPLETE', 'ENGINE_NOT_FOUND',
                                  'UNO_SESSION_UNAVAILABLE', 'PREFLIGHT_UNSUPPORTED',
                                  'ENGINE_ROUTE_UNAVAILABLE'):
                    raise Blocked(error.code + ':' + str(getattr(error, 'diagnostic', '') or str(error))) from error
                raise
            images = [Path(path) for path in files if Path(path).suffix.lower() == '.' + kind]
            if len(images) != 1:
                raise AssertionError('Expected one primary image, observed ' + repr([str(p) for p in files]))
            return images[0], {'executionMode': adapter.execution_mode(), 'exportInfo': adapter.export_info()}

        def raster_check(path, kind, target):
            evidence = proof(path)
            if kind == 'pct':
                expected_size = pict_dimensions(path)
                # The generated QuickDraw PaintRect fills the entire clip
                # black; original external PICTs have only a bounds oracle.
                expected_pixels = (bytes(expected_size[0] * expected_size[1] * 3)
                                   if path == pct else None)
            else:
                with Image.open(path) as source:
                    source.load()
                    expected_size = source.size
                    expected_pixels = source.convert('RGB').tobytes()
            output, metadata = convert(path, target, evidence)
            with Image.open(output) as result:
                result.load()
                if result.size != expected_size:
                    raise AssertionError('Canvas differs: expected=%r observed=%r' % (expected_size, result.size))
                pixels = result.convert('RGB').tobytes()
                if expected_pixels is not None and pixels != expected_pixels:
                    raise AssertionError('Decoded RGB pixels differ from independent source oracle')
                if kind == 'pcx':
                    dpi = result.info.get('dpi')
                    if not dpi or any(abs(float(value) - 100) > 0.05 for value in dpi[:2]):
                        raise AssertionError('100-DPI PCX resolution differs: ' + repr(dpi))
                    metadata['dpi'] = [float(value) for value in dpi]
                metadata.update(size=list(result.size), decodedRgbSha256=hashlib.sha256(pixels).hexdigest(),
                                pixelsCompared=expected_pixels is not None,
                                frames=getattr(result, 'n_frames', 1))
            metadata['source'] = path.name
            metadata['outputSha256'] = digest(output)
            return metadata

        def gif_check(path, *, expect_rejection, native):
            evidence = proof(path)
            metadata = {'source': path.name, 'strictPolicy': 'exact_palette_binary_alpha'}
            if native:
                reference, reference_metadata = convert(path, 'png', evidence)
                with Image.open(reference) as image:
                    reference_pixels = image.convert('RGB').tobytes()
                    reference_size = image.size
                    colours = len(set(image.convert('RGB').getdata()))
                metadata.update(nativePngColours=colours, nativePngReference=reference_metadata,
                                nativePngSize=list(reference_size))
                if (colours > 256) != expect_rejection:
                    raise AssertionError('Native PNG did not establish expected palette boundary: ' + str(colours))
            else:
                with Image.open(path) as image:
                    reference_pixels = image.convert('RGB').tobytes()
                    reference_size = image.size
            try:
                output, converted = convert(path, 'gif', evidence)
            except GuardError as error:
                if expect_rejection and error.code == 'MEDIA_GIF_PALETTE_UNREPRESENTABLE':
                    return {**metadata, 'status': 'PASS_SAFE_REFUSAL', 'code': error.code,
                            'published': False, 'executionMode': adapter.execution_mode()}
                raise
            if expect_rejection:
                raise AssertionError('Rich GIF succeeded despite confirmed >256 source colours')
            with Image.open(output) as image:
                if image.size != reference_size or image.convert('RGB').tobytes() != reference_pixels:
                    raise AssertionError('Limited GIF differs from the independent/reference RGB raster')
            return {**metadata, **converted, 'size': list(reference_size), 'pixelsCompared': True,
                    'outputSha256': digest(output)}

        with adapter.batch(args.output / 'office'):
            for path, kind in sources:
                # APNG is a Qt frame route. Native PICT export currently
                # supports PNG/TIFF only; do not turn an unoffered route into
                # a misleading required failure or an unsupported success.
                for target in (('png', 'tiff') if kind == 'pct' else ('png', 'apng', 'tiff')):
                    run_case(path.name + '->' + target,
                             lambda path=path, kind=kind, target=target: raster_check(path, kind, target))
            for path, native, rich_source in ((qt_rich, False, True), (qt_limited, False, False),
                                               (rich, True, True), (limited, True, False)):
                run_case(path.name + '->gif',
                         lambda path=path, native=native, rich_source=rich_source:
                             gif_check(path, expect_rejection=rich_source, native=native))
        del app
    except Exception as error:
        cases.append({'case': 'driver_setup', 'status': 'FAIL', 'error': type(error).__name__,
                      'reason': str(error), 'code': getattr(error, 'code', None)})
    finally:
        source_checks = []
        for path, original in originals.items():
            exists = path.is_file()
            try:
                observed = digest(path) if exists else None
            except OSError:
                observed = None
            source_checks.append({'file': str(path), 'sha256Before': original, 'sha256After': observed,
                                  'unchanged': original == observed})
        report['sourceIntegrity'] = source_checks
        if any(not item['unchanged'] for item in source_checks):
            cases.append({'case': 'source_integrity', 'status': 'FAIL', 'reason': 'Source SHA256 changed'})
        counts = {status: sum(item['status'] == status for item in cases)
                  for status in ('PASS', 'PASS_SAFE_REFUSAL', 'BLOCKED', 'FAIL')}
        report['counts'] = counts
        report['status'] = 'FAIL' if counts['FAIL'] else 'BLOCKED' if counts['BLOCKED'] else 'PASS'
        report['elapsedSeconds'] = round(time.monotonic() - started, 3)
        (args.output / 'completion-media-report.json').write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print('COMPLETION_MEDIA_NATIVE=' + json.dumps({
            'status': report['status'], 'counts': counts,
            'elapsedSeconds': report['elapsedSeconds'],
            'report': str(args.output / 'completion-media-report.json')}, ensure_ascii=True), flush=True)
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
