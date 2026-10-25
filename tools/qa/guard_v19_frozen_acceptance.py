"""Exercise an existing A.I.O executable's disposable comparison-worker entry.

No installer or new production flag is introduced. Synthetic text/PDF/PNG
fixtures, real owned child processes, strict response validation and bundled
asset hashes prove the packaged path. This is not desktop GUI or LO testing.
--source-worker is a non-Windows development surrogate and never frozen proof.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import time
import zlib

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def worker_command(executable, request, *, source_worker=False):
    if source_worker:
        return [str(executable), '-m', 'app_backend.converter_guard.comparison_worker', str(request)]
    return [str(executable), '--aio-guard-comparison', str(request)]


def run_owned(command, folder, response, seconds, *, cwd=None):
    """A zero process exit alone is insufficient: its response must exist."""
    from app_backend.native_process import ProcessTree
    folder = Path(folder)
    until = time.monotonic() + seconds
    environment = dict(os.environ)
    if os.name != 'nt' and not environment.get('DISPLAY'):
        environment.setdefault('QT_QPA_PLATFORM', 'offscreen')
    # A frozen proof must not borrow modules from the source checkout. The
    # explicit development surrogate alone gets the checkout as its cwd.
    environment.pop('PYTHONPATH', None)
    environment.pop('PYTHONHOME', None)
    log = folder / 'worker.log'
    with log.open('wb') as stream:
        with ProcessTree(command, cwd=folder if cwd is None else cwd, env=environment,
                         stdout=stream, stderr=stream) as tree:
            while tree.process.poll() is None:
                if time.monotonic() >= until:
                    raise TimeoutError('Owned comparison worker exceeded its acceptance deadline')
                require(log.stat().st_size <= 1024 * 1024, 'Worker diagnostic budget exceeded')
                try:
                    tree.process.wait(timeout=min(.1, max(.001, until - time.monotonic())))
                except subprocess.TimeoutExpired:
                    pass
            code = tree.process.returncode
    diagnostic = log.read_bytes()[-8192:].decode('utf-8', errors='replace')
    require(code == 0, 'Worker exit ' + str(code) + ': ' + diagnostic)
    path = folder / response
    require(path.is_file() and 0 < path.stat().st_size <= 4 * 1024 * 1024,
            'Worker did not create expected bounded response: ' + response)
    return {'exitCode': code, 'response': response, 'diagnostic': diagnostic}


def read_outcomes(path, expected_ids):
    data = path.read_bytes()
    require(data.endswith(b'\n'), 'Worker JSONL contains an incomplete final record')
    rows = [json.loads(line) for line in data.decode('utf-8').splitlines()]
    require(len(rows) == len(expected_ids), 'Worker JSONL task count differs from request')
    require({row.get('taskId') for row in rows} == set(expected_ids),
            'Worker JSONL contains missing, duplicate or foreign task identifiers')
    require(all(isinstance(row.get('outcome'), dict) and
                row['outcome'].get('status') in
                ('checked', 'confirmed_loss', 'format_limitation', 'uncertain', 'not_checked')
                for row in rows), 'Worker JSONL outcome schema is invalid')
    return {row['taskId']: row['outcome'] for row in rows}


def png_info(path):
    """Check complete PNG chunks/CRC/deflate, independently of host Qt imports."""
    blob = path.read_bytes()
    require(blob.startswith(b'\x89PNG\r\n\x1a\n'), 'Preview does not contain PNG signature')
    offset, compressed, dimensions, ended = 8, bytearray(), None, False
    while offset + 12 <= len(blob):
        length = struct.unpack('!I', blob[offset:offset + 4])[0]
        end = offset + 12 + length
        require(end <= len(blob), 'PNG chunk is truncated')
        name, data = blob[offset + 4:offset + 8], blob[offset + 8:end - 4]
        crc = struct.unpack('!I', blob[end - 4:end])[0]
        require((zlib.crc32(name + data) & 0xffffffff) == crc, 'PNG chunk CRC mismatch')
        if name == b'IHDR':
            require(length == 13, 'PNG IHDR length is invalid')
            width, height, bits, colour, compression, filtering, interlace = struct.unpack('!IIBBBBB', data)
            dimensions = {'width': width, 'height': height, 'bits': bits, 'colourType': colour}
            require(0 < width <= 1400 and 0 < height <= 1800,
                    'Preview exceeds the production image bounds')
            require(compression == filtering == interlace == 0, 'Unexpected preview PNG encoding')
        elif name == b'IDAT':
            compressed.extend(data)
        elif name == b'IEND':
            require(length == 0 and end == len(blob), 'PNG ending is invalid')
            ended = True
            break
        offset = end
    require(dimensions and ended and compressed, 'PNG lacks required chunks')
    raw = zlib.decompress(compressed)
    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(dimensions['colourType'])
    require(dimensions['bits'] == 8 and channels is not None,
            'Unexpected preview PNG bit depth or colour type')
    stride = dimensions['width'] * channels + 1
    require(len(raw) == stride * dimensions['height'], 'PNG decoded scanline length is invalid')
    require(all(raw[row * stride] <= 4 for row in range(dimensions['height'])),
            'PNG contains an invalid scanline filter')
    return {**dimensions, 'sha256': hashlib.sha256(blob).hexdigest(), 'decodedBytes': len(raw)}


def synthetic_png(path):
    def chunk(kind, data):
        return (struct.pack('!I', len(data)) + kind + data +
                struct.pack('!I', zlib.crc32(kind + data) & 0xffffffff))
    width, height = 2400, 1200
    rows = (b'\0' + bytes((220, 40, 80, 128)) * width) * height
    path.write_bytes(b'\x89PNG\r\n\x1a\n' +
        chunk(b'IHDR', struct.pack('!IIBBBBB', width, height, 8, 6, 0, 0, 0)) +
        chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))


def synthetic_pdf(path):
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
    writer = PdfWriter()
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
        NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
    for dimensions, label, colour in [((400, 300), 'AIO FROZEN PAGE ONE', '0.1 0.3 0.8'),
                                       ((300, 400), 'AIO FROZEN PAGE TWO', '0.8 0.2 0.1')]:
        page = writer.add_blank_page(width=dimensions[0], height=dimensions[1])
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):
            DictionaryObject({NameObject('/F1'): font})})
        stream = DecodedStreamObject()
        stream.set_data(('BT /F1 14 Tf 20 250 Td (' + label + ') Tj ET\nq ' +
            colour + ' rg 20 20 180 120 re f Q\n').encode('ascii'))
        page.replace_contents(stream)
    writer.write(path)
    writer.close()


def packaged_assets(executable):
    relatives = ('index.html', 'src/guard-comparison.js', 'src/guard-comparison.css')
    for root in (executable.parent / '_internal', executable.parent):
        if all((root / name).is_file() for name in relatives):
            hashes = {name: digest(root / name) for name in relatives}
            require(all(hashes[name] == digest(ROOT / name) for name in relatives),
                    'Packaged before/after assets differ from this tested checkout')
            index = (root / 'index.html').read_text(encoding='utf-8')
            require(all(name in index for name in relatives[1:]),
                    'Packaged index does not load the new comparison JS and CSS')
            return {'status': 'PASS', 'scope': 'packaged asset bytes, not GUI behavior', 'sha256': hashes}
    raise AssertionError('Existing EXE bundle lacks the current before/after JS, CSS or index')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seconds', type=float, default=90)
    parser.add_argument('--source-worker', action='store_true',
                        help='Non-Windows development surrogate; never frozen acceptance')
    args = parser.parse_args(argv)
    if not args.executable.is_file() or args.seconds <= 0:
        parser.error('An existing executable and a positive deadline are required')
    if args.source_worker and os.name == 'nt':
        parser.error('Windows acceptance requires the existing frozen A.I.O executable')
    args.output.mkdir(parents=True, exist_ok=False)
    evidence = {'schema': 1, 'status': 'FAIL', 'platform': sys.platform,
        'scope': 'source_python_worker_surrogate' if args.source_worker else 'existing_frozen_executable_worker',
        'frozenVerified': False, 'desktopGUI': 'NOT_RUN', 'LibreOffice': 'NOT_RUN', 'operations': {}}
    started, deadline = time.monotonic(), time.monotonic() + args.seconds
    try:
        evidence['assets'] = ({'status': 'NOT_RUN', 'reason': 'source worker surrogate'}
                              if args.source_worker else packaged_assets(args.executable.resolve()))
        fixtures = args.output / 'fixtures'
        fixtures.mkdir()
        text = fixtures / 'source.txt'
        text.write_text('AIO frozen worker: original text. Кириллица сохранена.\n', encoding='utf-8')
        same, changed = fixtures / 'same.txt', fixtures / 'changed.txt'
        same.write_bytes(text.read_bytes())
        changed.write_text('A deliberately changed result.', encoding='utf-8')
        pdf, image = fixtures / 'pages.pdf', fixtures / 'large-alpha.png'
        synthetic_pdf(pdf)
        synthetic_png(image)
        originals = {str(path): digest(path) for path in (text, same, changed, pdf, image)}

        def invoke(label, request, response):
            work = args.output / label
            work.mkdir()
            manifest = work / 'request.json'
            manifest.write_text(json.dumps(request, ensure_ascii=False), encoding='utf-8')
            # Resolving a development venv's python symlink would escape its
            # installed dependencies. Frozen EXEs have no such interpreter
            # alias, while the surrogate keeps the caller's executable path.
            executable = args.executable.absolute() if args.source_worker else args.executable.resolve()
            command = worker_command(executable, manifest.resolve(), source_worker=args.source_worker)
            remaining = deadline - time.monotonic()
            require(remaining > 0, 'Frozen acceptance exhausted its total deadline')
            info = run_owned(command, work, response, remaining,
                             cwd=ROOT if args.source_worker else work)
            evidence['operations'][label] = info
            return work

        pairs = [{'taskId': 'same-text', 'target': 'txt', 'paths': [str(same)], 'sha256': {str(same): digest(same)}},
                 {'taskId': 'changed-output-digest', 'target': 'txt', 'paths': [str(changed)],
                  'sha256': {str(changed): digest(same)}}]
        work = invoke('text-analysis', {'source': str(text), 'sourceSha256': digest(text), 'pairs': pairs}, 'outcomes.jsonl')
        rows = read_outcomes(work / 'outcomes.jsonl', ['same-text', 'changed-output-digest'])
        evidence['textAnalysis'] = rows
        require(rows['same-text']['status'] == 'checked' and
                'identical_file_bytes' in rows['same-text']['checkedFeatures'], 'Frozen text comparison did not inspect the original pair')
        require(rows['changed-output-digest']['status'] == 'not_checked' and
                any(item['code'] == 'RESULT_CHANGED' for item in rows['changed-output-digest']['findings']),
                'Frozen worker did not protect the output SHA contract')

        work = invoke('changed-source-digest', {'source': str(text), 'sourceSha256': '0' * 64,
                      'pairs': [pairs[0]]}, 'outcomes.jsonl')
        rows = read_outcomes(work / 'outcomes.jsonl', ['same-text'])
        evidence['sourceDigestGuard'] = rows
        require(rows['same-text']['status'] == 'not_checked' and
                any(item['code'] == 'SOURCE_CHANGED' for item in rows['same-text']['findings']),
                'Frozen worker did not protect the original SHA contract')

        pair = {'taskId': 'pdf-module-proof', 'target': 'pdf', 'paths': [str(pdf)], 'sha256': {str(pdf): digest(pdf)}}
        work = invoke('pdf-analysis', {'source': str(pdf), 'sourceSha256': digest(pdf), 'pairs': [pair]}, 'outcomes.jsonl')
        rows = read_outcomes(work / 'outcomes.jsonl', ['pdf-module-proof'])
        evidence['pdfAnalysis'] = rows
        summary = rows['pdf-module-proof']['before']
        require(summary.get('kind') == 'pdf' and summary.get('complete') and summary['counts'].get('pages') == 2,
                'Bundled pypdf did not inspect both synthetic PDF pages')

        pages = []
        for index in (0, 1, 2):
            work = invoke('pdf-page-' + str(index), {'mode': 'pdf_preview', 'source': str(pdf), 'page': index}, 'preview.json')
            metadata = json.loads((work / 'preview.json').read_text(encoding='utf-8'))
            observation = {'metadata': metadata}
            if index < 2:
                observation['png'] = png_info(work / 'preview.png')
                pages.append(observation['png'])
                require(metadata.get('kind') == 'image' and metadata.get('pages') == 2 and metadata.get('page') == index,
                        'Bundled QtPdf returned a wrong PDF page index')
            else:
                require(metadata.get('kind') == 'unavailable' and not (work / 'preview.png').exists(),
                        'Out-of-range PDF page was reported as a valid image')
            evidence['operations']['pdf-page-' + str(index)].update(observation)
        require((pages[0]['width'], pages[0]['height']) != (pages[1]['width'], pages[1]['height']) and
                pages[0]['sha256'] != pages[1]['sha256'], 'PDF navigation returned the same image for both pages')

        work = invoke('png-preview', {'mode': 'image_preview', 'source': str(image)}, 'preview.json')
        metadata = json.loads((work / 'preview.json').read_text(encoding='utf-8'))
        preview = png_info(work / 'preview.png')
        evidence['operations']['png-preview'].update(metadata=metadata, png=preview)
        require(metadata.get('kind') == 'image' and (preview['width'], preview['height']) == (1400, 700)
                and preview['colourType'] == 6,
                'Frozen image preview did not produce the bounded scaled PNG')
        evidence['fixturesUnchanged'] = all(Path(path).is_file() and digest(path) == value for path, value in originals.items())
        require(evidence['fixturesUnchanged'], 'A worker modified an original fixture or retained result')
        evidence.update(status='PASS', frozenVerified=not args.source_worker)
        return 0
    except Exception as error:
        evidence.update(error=type(error).__name__, reason=str(error))
        return 1
    finally:
        evidence['elapsedSeconds'] = round(time.monotonic() - started, 3)
        (args.output / 'evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print('AIO_V19_FROZEN_ACCEPTANCE=' + json.dumps(evidence, ensure_ascii=True), flush=True)


if __name__ == '__main__':
    raise SystemExit(main())
