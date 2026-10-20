"""Opt-in native preflight acceptance. Never executes at application startup."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app_backend.converter_guard import ConverterGuard, GuardError, GuardLimits, LibreOfficeAdapter
from app_backend.server import start_server


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--fixtures', type=Path, required=True)
    args = parser.parse_args()
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=False)
    sources = work / 'sources'
    sources.mkdir()
    for fixture, name in [('image.bmp', 'picture.bmp'), ('writer.docx', 'renamed.bmp'),
                          ('calc.xlsx', 'table.xlsx')]:
        shutil.copyfile(args.fixtures / fixture, sources / name)
    paths = [sources / name for name in ('picture.bmp', 'renamed.bmp', 'table.xlsx')]
    hashes = {path.name: digest(path) for path in paths}

    class ObservedAdapter(LibreOfficeAdapter):
        def __init__(self):
            super().__init__()
            self.imports, self.exports = [], []

        def preflight(self, source, *positional, **kwargs):
            assert digest(source) == hashes[source.name], 'Input changed before inspection'
            assert not self.exports, 'Preflight must precede the first requested export'
            started = time.monotonic()
            result = super().preflight(source, *positional, **kwargs)
            self.imports.append({'source': source.name, 'seconds': round(time.monotonic()-started, 3)})
            return result

        def convert(self, source, target, *positional, **kwargs):
            assert digest(source) == hashes[source.name], 'Input changed before export'
            assert len(self.imports) == 3, 'Every selected original must be inspected before export'
            self.exports.append({'source': source.name, 'format': target})
            return super().convert(source, target, *positional, **kwargs)

    adapter = ObservedAdapter()
    assert adapter.available and adapter.capabilities([])['registryAvailable'], (
        'NOT_RUN: set AIO_LIBREOFFICE to the real installation with its registry')
    engine = subprocess.run([str(adapter.executable), '--version'], capture_output=True,
                            text=True, check=True, timeout=15).stdout.strip()
    server, token = start_server()
    server.guard.adapter = adapter
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        execution = subprocess.run(['node', str(Path(__file__).with_name('guard_v06_native_bridge.mjs'))],
            env=dict(os.environ, AIO_TEST_SERVER='http://127.0.0.1:' + str(server.server_port),
                     AIO_TEST_TOKEN=token, AIO_TEST_SOURCES=json.dumps([str(p) for p in paths]),
                     AIO_TEST_ARCHIVE=str(work / 'results.zip')),
            capture_output=True, text=True, timeout=180)
        assert execution.returncode == 0, execution.stdout + '\n' + execution.stderr
        evidence = json.loads(execution.stdout)
        assert len(adapter.exports) == evidence['attempted']
        assert len(adapter.imports) == evidence['preflightChecks'] == 3
        assert server.guard.storage_stats()['jobs'] == 0
    finally:
        server.shutdown()
        server.server_close()
        worker.join(5)

    from pypdf import PdfReader
    with ZipFile(work / 'results.zip') as archive:
        report = json.loads(archive.read('AIO-conversion-report.json'))
        assert report['total'] == report['filtered'] + report['succeeded'] == 15
        assert 'ticket' not in json.dumps(report), 'Download tokens must not enter the archive report'
        selected = next(o for o in evidence['outputs'] if o['fileId'] == 'f1' and o['format'] == 'pdf')
        guarded_pdf = work / 'guarded.pdf'
        guarded_pdf.write_bytes(archive.read(selected['archiveName']))
    # Independent unfiltered native controls for every actual preflight exclusion.
    controls = []
    for index, row in enumerate(report['files']):
        for target in row['targets']:
            if target['state'] != 'skipped':
                continue
            assert target['stage'] == 'preflight' and not target['libreOfficeStarted']
            control = work / 'negative-controls' / (str(index) + '-' + target['format'])
            try:
                LibreOfficeAdapter(adapter.executable).convert(paths[index], target['format'],
                    control, cancel=threading.Event(), timeout=15, on_started=lambda: None)
            except GuardError as error:
                assert error.code in ('ENGINE_OUTPUT_MISSING', 'ENGINE_FAILED', 'ENGINE_TIMEOUT'), error.code
                controls.append({'source': paths[index].name, 'format': target['format'],
                                 'nativeError': error.code,
                                 'conclusive': error.code != 'ENGINE_TIMEOUT'})
            else:
                raise AssertionError('False native exclusion: ' + str(control))
            finally:
                shutil.rmtree(control, ignore_errors=True)
    direct = LibreOfficeAdapter(adapter.executable).convert(sources / 'renamed.bmp', 'pdf',
        work / 'direct', cancel=threading.Event(), timeout=None, on_started=lambda: None)[0]
    guarded, control = PdfReader(guarded_pdf), PdfReader(direct)
    assert len(guarded.pages) == len(control.pages) == 2
    assert [p.extract_text() for p in guarded.pages] == [p.extract_text() for p in control.pages]
    images = lambda pdf: [[hashlib.sha256(img.data).hexdigest() for img in p.images] for p in pdf.pages]
    assert images(guarded) == images(control)
    assert any(images(guarded)), 'Fixture must contain an actual embedded image'
    # A short inspection deadline is NOT an export timeout or an unsupported verdict.
    fallback = ConverterGuard(adapter=LibreOfficeAdapter(adapter.executable),
        limits=GuardLimits(preflight_timeout=.001), spool_parent=work)
    try:
        source = sources / 'picture.bmp'
        job = fallback.create([{'id': 'x', 'name': source.name, 'size': source.stat().st_size}],
                              prepare_only=True)
        identity = job['jobId']
        with source.open('rb') as stream:
            fallback.upload(identity, 'x', stream, source.stat().st_size)
        inspection = fallback.preflight(identity)
        assert inspection['files'][0]['preflight']['state'] == 'undetermined'
        assert inspection['files'][0]['preflight']['code'] == 'PREFLIGHT_TIMEOUT'
        fallback.start(identity, file_ids=['x'], formats=['pdf'], only_verified=False)
        deadline = time.monotonic()+30
        while not (state := fallback.status(identity))['finished']:
            assert time.monotonic() < deadline
            time.sleep(.02)
        assert state['succeeded'] == state['attempted'] == 1
        timeout_fallback = {'code': 'PREFLIGHT_TIMEOUT', 'manualExportSucceeded': True}
    finally:
        fallback.close()
    cancelled = threading.Event()
    timer = threading.Timer(.05, cancelled.set)
    timer.start()
    cancelled_work = work / 'cancelled-preflight'
    try:
        try:
            LibreOfficeAdapter(adapter.executable).preflight(sources / 'renamed.bmp',
                cancelled_work, cancel=cancelled, timeout=15)
        except GuardError as error:
            assert error.code == 'CANCELLED'
        else:
            raise AssertionError('Native inspection cancellation was ignored')
    finally:
        timer.join(2)
        shutil.rmtree(cancelled_work, ignore_errors=True)
    assert hashes == {path.name: digest(path) for path in paths}
    assert not [t for t in threading.enumerate() if t.name.startswith(('aio-guard', 'aio-native-observation'))]
    evidence.update(engine=engine, windows='NOT_RUN', qtNativeSave='NOT_RUN',
        sourceBytesUnchanged=True, observerThreadsAfterClose=0, nativeImports=adapter.imports,
        exportCalls=adapter.exports, sourceSha256=hashes,
        directNegativeControls={'checked': len(controls), 'falseDenialsDetected': 0,
                                'inconclusive': sum(not r['conclusive'] for r in controls),
                                'results': controls},
        nativeTimeoutFallback=timeout_fallback, nativeInspectionCancellation=True,
        pdfBaseline={'pages': 2, 'textEqual': True, 'encodedImagesEqual': True},
        qualityScannerAdded=False)
    (work / 'evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n',
                                       encoding='utf-8')
    print(json.dumps(evidence, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
