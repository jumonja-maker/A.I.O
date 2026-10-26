"""Offline native regression for the integrated v20 patch.

Synthetic files only; tests the installed office and the real Guard adapter.
This is a targeted safety gate, not the FULLTEST corpus or visual certification.
No packages, network access, personal office profile or global process kills.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app_backend.converter_guard import LibreOfficeAdapter
from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard.media_fidelity import inspect_media
from app_backend.converter_guard.source_contract import inspect_source_contract
from guard_v08_native_acceptance import fixtures
from guard_v16_bugbag_acceptance import presentation_seed


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--executable', type=Path)
    parser.add_argument('--require-mode', choices=['native_uno', 'native_cli'])
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    sources = fixtures(args.output / 'fixtures')
    dif = args.output / 'fixtures' / 'numbers.dif'
    dif.write_text('TABLE\n0,1\n"Numbers"\nVECTORS\n0,2\n""\nTUPLES\n0,3\n""\nDATA\n0,0\n""\n'
        '-1,0\nBOT\n1,0\n"code"\n1,0\n"amount"\n-1,0\nBOT\n1,0\n"00123"\n0,1.43\nV\n'
        '-1,0\nBOT\n1,0\n"00500"\n0,-2.86\nV\n-1,0\nEOD\n', encoding='utf-8')
    sources['dif'] = dif
    default_xlsx = args.output / 'fixtures' / 'default.xlsx'
    with zipfile.ZipFile(sources['xlsx']) as archive, zipfile.ZipFile(default_xlsx, 'x') as out:
        for entry in archive.infolist():
            data = archive.read(entry)
            if entry.filename == '[Content_Types].xml':
                data = data.replace(b'<Default Extension="xml" ContentType="application/xml"/>',
                    b'<Default Extension="xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>')
                data = data.replace(b'<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>', b'')
            out.writestr(entry, data)
    sources['default_xlsx'] = default_xlsx
    math = args.output / 'fixtures' / 'formula.odf'
    with zipfile.ZipFile(math, 'x') as out:
        out.writestr('mimetype', 'application/vnd.oasis.opendocument.formula')
        out.writestr('content.xml', '<math xmlns="http://www.w3.org/1998/Math/MathML" display="block"><semantics><mrow><mn>2</mn><mo>+</mo><mn>2</mn><mo>=</mo><mn>4</mn></mrow><annotation encoding="StarMath 5.0">2+2=4</annotation></semantics></math>')
        out.writestr('META-INF/manifest.xml', '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" manifest:version="1.3"><manifest:file-entry manifest:full-path="/" manifest:media-type="application/vnd.oasis.opendocument.formula"/><manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/></manifest:manifest>')
    sources['math'] = math
    presentation = args.output / 'fixtures' / 'slides.fodp'
    presentation.write_text(presentation_seed(), encoding='utf-8')
    sources['presentation'] = presentation
    before = {key: digest(path) for key, path in sources.items()}
    adapter = LibreOfficeAdapter(args.executable)
    if not adapter.available:
        raise RuntimeError('Required installed LibreOffice unavailable')
    started = time.monotonic()
    cases = []
    expected = {'docx': ['docm', 'dotx', 'pdf'], 'default_xlsx': ['xlsx'],
        'math': ['pdf'], 'dif': ['fods', 'xlsx', 'pdf'],
        'bmp': ['png', 'jfif', 'jif', 'jpe'], 'presentation': ['bmp', 'htm']}
    cancel = threading.Event()
    with adapter.batch(args.output / 'office'):
        for key, targets in expected.items():
            source = sources[key]
            proof = adapter.preflight(source, args.output / ('inspect-' + key), cancel=cancel, timeout=60)
            if proof.get('state') != 'detected':
                raise AssertionError((key, proof))
            adapter.prepare_source(proof)
            if key in ('docx', 'presentation'):
                assert adapter.route_decision('apng', proof) == 'unavailable', 'False native APNG offer'
            if key in ('default_xlsx', 'math'):
                assert inspect_source_contract(source)['state'] == 'recognized'
            for target in targets:
                if adapter.route_decision(target, proof) != 'registered':
                    raise AssertionError(('required_route_unavailable', key, target, proof))
                tick = time.monotonic()
                try:
                    paths = adapter.convert(source, target, args.output / ('export-' + key + '-' + target),
                        cancel=cancel, timeout=90, on_started=lambda: None)
                except GuardError as error:
                    raise AssertionError((key, target, error.code, error.diagnostic)) from error
                mode, info = adapter.execution_mode(), adapter.export_info()
                # The mode requirement concerns native Office exports, not
                # the already installed application's actual Qt raster codec.
                if args.require_mode and key != 'bmp' and mode != args.require_mode:
                    raise AssertionError(('wrong_native_mode', key, target, mode))
                primaries = [p for p in paths if p.suffix.lower() == '.' + target]
                assert primaries, (key, target, paths)
                if key == 'bmp':
                    media = inspect_media(primaries[0], validate_container=True)
                    assert (media['width'], media['height']) == (2, 2), media
                if key == 'presentation' and target == 'bmp':
                    inspect_media(primaries[0], validate_container=True)
                if target in ('docm', 'dotx'):
                    with zipfile.ZipFile(primaries[0]) as archive:
                        content_types = archive.read('[Content_Types].xml').decode()
                        expected_type = ('application/vnd.ms-word.document.macroEnabled.main+xml'
                            if target == 'docm' else 'application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml')
                        assert expected_type in content_types, content_types
                        assert b'AIO_DOCUMENT_MARKER' in archive.read('word/document.xml')
                cases.append({'source': key, 'target': target, 'status': 'PASS', 'mode': mode,
                    'elapsedMs': round((time.monotonic() - tick) * 1000),
                    'filter': info.get('filter'), 'checks': info.get('resultChecks', []),
                    'outputs': [{'name': p.name, 'size': p.stat().st_size, 'sha256': digest(p)} for p in paths]})
    assert before == {key: digest(path) for key, path in sources.items()}, 'Source bytes changed'
    result = {'status': 'PASS', 'platform': sys.platform,
        'scope': 'synthetic native safety controls; not all-format or visual certification',
        'requireMode': args.require_mode, 'cases': cases, 'sourceSHA256': before,
        'sourceBytesUnchanged': True, 'elapsedMs': round((time.monotonic() - started) * 1000)}
    (args.output / 'evidence.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print('AIO_V20_NATIVE=' + json.dumps(result, ensure_ascii=True), flush=True)


if __name__ == '__main__':
    main()
