"""Real bulk Guard XHTML preparation/PDF and specialized native-route controls.

The two preserved FT4 XHTML sources are repeated under distinct input names to
exercise the production >=32-file preparation path. This is not the historical
178-file corpus or installed Windows UI acceptance. Optional --samples reads the
original pinned 22-source diagnostic ZIP to survey actual ODM/OTM imports.
No import failure is converted into invented model support or a successful route.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import sys
import threading
import time
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
FIXTURES = ROOT / 'tests' / 'fixtures' / 'completion_routes'
SAMPLES_SHA256 = 'fc3fedff2160b65c0b463826e317e284e700d1c45994e809a370bbd7d134988f'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def native_proof(proof):
    return {key: proof.get(key) for key in (
        'state', 'code', 'basis', 'modelLoaded', 'documentServices',
        'diagnosticStage', 'nativeImportMs') if key in proof}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--count', default=32, type=int)
    parser.add_argument('--samples', type=Path,
                        default=ROOT / 'tools' / 'qa' / 'fixtures' / 'aio_windows_exact22_new_samples.zip')
    args = parser.parse_args()
    if not 32 <= args.count <= 510:
        parser.error('--count must be 32..510')
    args.output.mkdir(parents=True, exist_ok=False)
    from PySide6.QtGui import QGuiApplication
    from pypdf import PdfReader
    from app_backend.converter_guard import ConverterGuard, GuardLimits, LibreOfficeAdapter
    app = QGuiApplication.instance() or QGuiApplication([])
    adapter = LibreOfficeAdapter(args.engine)
    if not adapter.available or not adapter._registry().valid:
        parser.error('--engine must identify an installed native LibreOffice with its registry')
    guard = ConverterGuard(adapter=adapter, limits=GuardLimits(preflight_timeout=120, file_timeout=90),
                           recovery_parent=args.output / 'owned-jobs')
    report = {'schema': 1, 'status': 'INCOMPLETE', 'platform': platform.platform(),
              'engineName': args.engine.name, 'registryFingerprint': adapter._registry().fingerprint,
              'scope': 'real Guard bulk native preparation, exact two preserved FT4 XHTML sources',
              'historical178Corpus': 'NOT_RUN', 'installedWindowsUI': 'NOT_RUN',
              'm14Closed': False, 'realBaseReportPositive': 'NOT_TESTED',
              'qualityCompared': False, 'count': args.count, 'cases': [], 'controls': {}}
    started = time.monotonic()
    checks = report['controls']
    try:
        provenance = json.loads((FIXTURES / 'provenance.json').read_text(encoding='utf-8'))
        report['sourceProvenance'] = provenance
        originals = []
        for row in provenance['sources']:
            source = FIXTURES / row['name']
            if source.stat().st_size != row['bytes'] or sha(source) != row['sha256']:
                raise ValueError('Preserved XHTML source changed')
            originals.append(source)
        entries = [{'id': 'f%04d' % i, 'name': '%04d_%s' % (i, originals[i % 2].name),
                    'size': originals[i % 2].stat().st_size} for i in range(args.count)]
        job = guard.create(entries, prepare_only=True)['jobId']
        for i, row in enumerate(entries):
            with originals[i % 2].open('rb') as stream:
                guard.upload(job, row['id'], stream, row['size'])
        phase_start = time.monotonic()
        prepared = guard.preflight(job)
        status = guard.status(job)
        report['preflightSeconds'] = round(time.monotonic() - phase_start, 3)
        report['preflightBudgetSeconds'] = status.get('preflightTimeoutSeconds')
        report['preflightCompleted'] = status.get('preflightCompleted')
        pdf = next(row for row in prepared['formats'] if row['id'] == 'pdf')
        report['pdfVerifiedIds'] = pdf['verifiedFileIds']
        checks['fullConfiguredImportBudget'] = status.get('preflightTimeoutSeconds') == 120
        checks['allBulkSourcesInspected'] = status.get('preflightCompleted') == args.count
        checks['allBulkSourcesHavePdfRoute'] = set(pdf['verifiedFileIds']) == {row['id'] for row in entries}
        checks['writerDoesNotInventMasterOrReportSupport'] = all(
            not row['verifiedFileIds'] for row in prepared['formats'] if row['id'] in ('odm', 'otm', 'orp'))
        report['sources'] = [{'id': row['id'], 'name': row['name'],
                              'preflight': native_proof(row.get('preflight') or {})}
                             for row in prepared['files']]
        if checks['allBulkSourcesHavePdfRoute']:
            selected = [row['id'] for row in entries[:2]]
            guard.start(job, file_ids=selected, only_verified=True,
                        plan=[{'fileId': identity, 'formats': ['pdf']} for identity in selected])
            until = time.monotonic() + 210
            while not guard.status(job).get('finished'):
                if time.monotonic() >= until:
                    guard.cancel(job)
                    raise TimeoutError('Native PDF conversion deadline')
                time.sleep(.1)
            status = guard.status(job)
            report['job'] = {key: status.get(key) for key in ('state', 'total', 'succeeded', 'failed', 'filtered')}
            checks['twoSelectedSourcesConverted'] = status.get('succeeded') == 2 and status.get('failed') == 0
            if checks['twoSelectedSourcesConverted']:
                saved_root = args.output / 'saved'
                saved_root.mkdir()
                saved = guard.save_folder(status['result']['ticket'], saved_root)
                for index, source in enumerate(originals):
                    identity = selected[index]
                    files = [row for row in status['result']['outputs']
                             if row['fileId'] == identity and row['format'] == 'pdf']
                    if len(files) != 1:
                        raise ValueError('Missing saved PDF')
                    output = saved_root / saved['name'] / files[0]['archiveName']
                    pages = PdfReader(output).pages
                    text = '\n'.join(page.extract_text() or '' for page in pages)
                    required = [f'AIO-NEW-20261108-L{index + 1}', '00123', '12345678901234567', 'Москва']
                    report['cases'].append({'source': source.name, 'sourceSha256': sha(source),
                        'pdfPages': len(pages), 'pdfBytes': output.stat().st_size,
                        'pdfSha256': sha(output), 'requiredTextPresent': all(marker in text for marker in required),
                        'textMarkers': {marker: marker in text for marker in required}})
                checks['pdfTextRetained'] = len(report['cases']) == 2 and all(
                    row['requiredTextPresent'] for row in report['cases'])
        if args.samples:
            if sha(args.samples) != SAMPLES_SHA256:
                raise ValueError('Original diagnostic ZIP SHA mismatch')
            report['nativeModelSurvey'] = []
            source_root = args.output / 'original-model-sources'
            source_root.mkdir()
            with ZipFile(args.samples) as archive:
                if archive.testzip() is not None:
                    raise ValueError('Original diagnostic ZIP CRC mismatch')
                for name in archive.namelist():
                    if Path(name).suffix.lower() not in ('.odm', '.otm'):
                        continue
                    source = source_root / Path(name).name
                    source.write_bytes(archive.read(name))
                    before = sha(source)
                    proof = adapter.preflight(source, args.output / ('inspect-' + name),
                                              cancel=threading.Event(), timeout=45)
                    decisions = {target: adapter.route_decision(target, proof)
                                 for target in ('odm', 'otm', 'orp', 'pdf')}
                    services = proof.get('documentServices', [])
                    loaded_master = (proof.get('basis') == 'native_preflight_uno_document'
                                     and proof.get('modelLoaded') is True
                                     and 'com.sun.star.text.GlobalDocument' in services)
                    report['nativeModelSurvey'].append({'source': source.name,
                        'sourceSha256': before, 'sourceUnchanged': sha(source) == before,
                        'preflight': native_proof(proof), 'routes': decisions,
                        'actualMasterServiceProved': loaded_master})
                    checks['modelSourceBytesPreserved_' + name] = sha(source) == before
                    if not loaded_master:
                        checks['unprovedModelNeverEnablesMasterExport_' + name] = all(
                            decisions[target] != 'registered' for target in ('odm', 'otm'))
        checks['sourceBytesPreserved'] = all(sha(FIXTURES / row['name']) == row['sha256']
                                             for row in provenance['sources'])
        report['status'] = 'PASS' if checks and all(checks.values()) else 'FAIL'
    except BaseException as error:
        report['status'] = 'FAIL'
        report['error'] = {'type': type(error).__name__, 'message': str(error)[-300:]}
    finally:
        guard.close()
        report['totalSeconds'] = round(time.monotonic() - started, 3)
        (args.output / 'evidence.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n',
                                                   encoding='utf-8')
        print('AIO_COMPLETION_ROUTES=' + json.dumps({key: report.get(key) for key in
              ('status', 'count', 'preflightBudgetSeconds', 'preflightCompleted',
               'controls', 'cases', 'error', 'totalSeconds')}, ensure_ascii=True), flush=True)
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
