"""M14 native capability survey: observed LO facts, never a suffix allowlist.

Synthetic ODF/PSD cases exercise real imported services and actual Guard routes.
A genuine Base ReportDefinition is not synthesized; ORP has a negative only.
The script intentionally reports INCOMPLETE coverage, even when every control
finishes, and cannot authorize enabling any unsupported export route.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys
import threading
import time
from zipfile import ZipFile, ZIP_DEFLATED, ZIP_STORED

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

OFFICE = 'urn:oasis:names:tc:opendocument:xmlns:office:1.0'
TEXT = 'urn:oasis:names:tc:opendocument:xmlns:text:1.0'
CHART = 'urn:oasis:names:tc:opendocument:xmlns:chart:1.0'
FAMILIES = {
    'odc': ('chart', 'chart', 'com.sun.star.chart2.ChartDocument'),
    'odm': ('text-master', 'text', 'com.sun.star.text.GlobalDocument'),
    'otm': ('text-master-template', 'text', 'com.sun.star.text.GlobalDocument'),
}
TARGETS = ('odc', 'odm', 'otm', 'orp', 'psd', 'pdf', 'png', 'svg')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_odf(path, mime_kind, body_kind):
    """Self-contained *minimal* declarations, not a fidelity reference corpus."""
    if body_kind == 'chart':
        inner = ('<office:chart><chart:chart chart:class="chart:bar">'
                 '<chart:plot-area/></chart:chart></office:chart>')
    elif body_kind == 'text':
        inner = ('<office:text><text:p>AIO_M14_REAL_NATIVE_IMPORT_00123'
                 '</text:p></office:text>')
    else:
        raise ValueError('unknown fixture model')
    mime = 'application/vnd.oasis.opendocument.' + mime_kind
    content = (f'<?xml version="1.0" encoding="UTF-8"?>'
               f'<office:document-content xmlns:office="{OFFICE}" '
               f'xmlns:text="{TEXT}" xmlns:chart="{CHART}" '
               'office:version="1.2"><office:body>'
               + inner + '</office:body></office:document-content>')
    manifest = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" '
        'manifest:version="1.2">'
        f'<manifest:file-entry manifest:full-path="/" manifest:media-type="{mime}"/>'
        '<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>'
        '</manifest:manifest>'
    )
    with ZipFile(path, 'x') as archive:
        archive.writestr('mimetype', mime, compress_type=ZIP_STORED)
        archive.writestr('content.xml', content, compress_type=ZIP_DEFLATED)
        archive.writestr('META-INF/manifest.xml', manifest, compress_type=ZIP_DEFLATED)
    return path


def write_psd(path):
    """Real decodable 2x2, 8-bit RGB Photoshop PSD (no external converter)."""
    header = (b'8BPS' + struct.pack('>H', 1) + bytes(6)
              + struct.pack('>HIIHH', 3, 2, 2, 8, 3))
    sections = struct.pack('>IIIH', 0, 0, 0, 0)
    planes = bytes((11, 20, 30, 40, 51, 60, 70, 80, 91, 100, 110, 120))
    path.write_bytes(header + sections + planes)
    return path


def make_samples(output):
    output = Path(output)
    sources = {}
    for ext, (mime, body, _) in FAMILIES.items():
        sources[ext] = write_odf(output / ('synthetic.' + ext), mime, body)
    sources['psd'] = write_psd(output / 'synthetic.psd')
    # Negative-only: never label an arbitrary file as a real Base ReportDefinition.
    invalid_orp = output / 'not-a-base-report.orp'
    invalid_orp.write_bytes(b'AIO M14 invalid Base ReportDefinition negative only')
    return sources, invalid_orp


def registry_row(native, extension):
    matches = [{'filter': name, 'service': service, 'flags': sorted(flags)}
               for name, (service, extensions, flags) in sorted(native.filters.items())
               if extension in extensions]
    return {'extension': extension,
            'importServices': sorted(native.imports.get(extension, ())),
            'exportServices': sorted(native.exports.get(extension, ())),
            'filterCount': len(matches), 'filters': matches,
            'uncertain': extension in native.uncertain_extensions}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    from PySide6.QtGui import QGuiApplication
    from PIL import Image
    from app_backend.converter_guard import LibreOfficeAdapter
    from app_backend.converter_guard.errors import GuardError
    from app_backend.converter_guard.source_contract import inspect_source_contract
    gui = QGuiApplication.instance() or QGuiApplication([])
    sources, false_report = make_samples(args.output)
    with Image.open(sources['psd']) as image:
        assert image.format == 'PSD' and image.size == (2, 2)
        assert image.convert('RGB').getpixel((0, 0)) == (11, 51, 91)
    original = {ext: digest(p) for ext, p in sources.items()}
    original['orp_negative'] = digest(false_report)
    cancel = threading.Event()
    adapter = LibreOfficeAdapter(args.executable)
    rows, attempt_count = [], 0
    report = {'status': 'INCOMPLETE', 'scope': 'synthetic native capability survey; no real Base ORP or historical FULLTEST',
              'm14Closed': False, 'sourceUnchanged': False, 'realBaseReportPositive': 'NOT_TESTED'}
    try:
        with adapter.batch(args.output / 'office'):
            native = adapter._registry()
            assert adapter.available and native.valid, 'installed registry unavailable'
            registry = {ext: registry_row(native, ext)
                        for ext in ('odc', 'odm', 'orp', 'otm', 'psd')}
            for ext, source in sources.items():
                contract = inspect_source_contract(source, cancel=cancel)
                if ext in FAMILIES:
                    expected_service = FAMILIES[ext][2]
                    assert contract['state'] == 'recognized' and contract['expectedServices'] == [expected_service], contract
                else:
                    assert contract['state'] == 'not_applicable', contract
                started = time.monotonic()
                attempt_count += 1
                try:
                    proof = adapter.preflight(source, args.output / ('probe-' + ext),
                                              cancel=cancel, timeout=45)
                except GuardError as err:
                    proof = {'state': 'undetermined', 'code': err.code}
                decisions = {target: adapter.route_decision(target, proof)
                             for target in TARGETS}
                # A stale or forged registry fingerprint cannot be used to
                # promote a filename-only decision to a registered route.
                corrupted = {**proof, 'registryFingerprint': 'incorrect-native-installation'}
                assert all(adapter.route_decision(target, corrupted) != 'registered'
                           for target in TARGETS), ('stale proof admitted', ext)
                if proof.get('state') != 'detected':
                    assert 'registered' not in decisions.values(), ('undetermined proof admitted', ext)
                assert decisions['orp'] != 'registered', ('unproven report export', ext)
                row = {'source': ext, 'sha256': original[ext],
                       'contentContract': contract['state'],
                       'expectedServices': contract.get('expectedServices', ()),
                       'preflight': {'state': proof.get('state'),
                                     'basis': proof.get('basis'),
                                     'code': proof.get('code'),
                                     'documentServices': proof.get('documentServices', ())},
                       'decisions': decisions, 'reasonOrp': native.route_reason('orp', proof),
                       'elapsedMs': round((time.monotonic()-started)*1000)}
                # A registered native route is not fidelity proof. Try one safe
                # route through production Guard; preserve both successes and
                # explicit rejections as observations, not as universal support.
                preferred = {'odc': 'odc', 'odm': 'pdf', 'otm': 'pdf', 'psd': 'png'}[ext]
                if decisions.get(preferred) == 'registered':
                    attempt_count += 1
                    adapter.prepare_source(proof)
                    try:
                        files = adapter.convert(source, preferred,
                                                args.output / ('conversion-' + ext),
                                                cancel=cancel, timeout=90,
                                                on_started=lambda: None)
                        primary = [p for p in files if p.suffix.lower() == '.' + preferred]
                        assert primary and all(p.is_file() and p.stat().st_size > 0
                                               for p in primary)
                        row['nativeAttempt'] = {'status': 'PASS_SYNTHETIC',
                                                'target': preferred,
                                                'primaryCount': len(primary)}
                    except GuardError as err:
                        row['nativeAttempt'] = {'status': 'SAFE_REJECTION',
                                                'target': preferred, 'code': err.code}
                else:
                    row['nativeAttempt'] = {'status': 'UNVERIFIED_NO_ATTEMPT',
                                            'target': preferred}
                assert digest(source) == original[ext], ('source changed', ext)
                rows.append(row)
                print('M14_NATIVE_CASE=' + json.dumps(row, ensure_ascii=True), flush=True)
            # ORP positive requires a genuine LibreOffice Base report, never
            # a manufactured text file with a report-like extension.
            try:
                fake = adapter.preflight(false_report, args.output / 'probe-orp-negative',
                                         cancel=cancel, timeout=35)
            except GuardError as err:
                fake = {'state': 'undetermined', 'code': err.code}
            assert adapter.route_decision('orp', fake) != 'registered', 'fake ORP accepted'
            rows.append({'source': 'orp', 'fixture': 'invalid_negative_only',
                         'realPositive': 'NOT_TESTED', 'preflightCode': fake.get('code'),
                         'orpDecision': adapter.route_decision('orp', fake)})
        assert all(digest(path) == original[ext] for ext, path in sources.items())
        assert digest(false_report) == original['orp_negative']
        report = {'status': 'SURVEY_COMPLETED_WITH_GAPS', 'm14Closed': False,
                  'scope': 'Windows native minimal synthetic fixtures; not user originals',
                  'engine': str(args.executable.name), 'registryFingerprint': native.fingerprint,
                  'registry': registry, 'cases': rows, 'attemptCount': attempt_count,
                  'realBaseReportPositive': 'NOT_TESTED', 'historicalFulltest': 'NOT_RUN',
                  'sourceUnchanged': True, 'qualityCompared': False}
    except Exception as error:
        report = {'status': 'FAIL', 'm14Closed': False,
                  'error': type(error).__name__, 'reason': str(error),
                  'cases': rows, 'realBaseReportPositive': 'NOT_TESTED'}
        raise
    finally:
        (args.output / 'evidence.json').write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print('M14_NATIVE_MATRIX=' + json.dumps(report, ensure_ascii=True), flush=True)


if __name__ == '__main__':
    main()
