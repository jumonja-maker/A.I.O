"""Opt-in, synthetic native QA; never runs at app startup or from unit tests."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app_backend.converter_guard import GuardError, LibreOfficeAdapter
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
    shutil.copyfile(args.fixtures / 'image.bmp', sources / 'picture.bmp')
    shutil.copyfile(args.fixtures / 'writer.docx', sources / 'renamed.bmp')
    (sources / 'plain.unlisted').write_text('GUARD_V05_UNKNOWN_NATIVE\n', encoding='utf-8')
    paths = [sources / name for name in ('picture.bmp', 'renamed.bmp', 'plain.unlisted')]
    hashes = {path.name: digest(path) for path in paths}

    class ObservedAdapter(LibreOfficeAdapter):
        def __init__(self):
            super().__init__()
            self.calls = []

        def convert(self, source, target, *positional, **kwargs):
            assert digest(source) == hashes[source.name], 'Input changed before native launch'
            self.calls.append((source.name, target))
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
        execution = subprocess.run(['node', str(Path(__file__).with_name('guard_v05_native_bridge.mjs'))],
            env=dict(os.environ, AIO_TEST_SERVER='http://127.0.0.1:' + str(server.server_port),
                     AIO_TEST_TOKEN=token, AIO_TEST_SOURCES=json.dumps([str(p) for p in paths]),
                     AIO_TEST_ARCHIVE=str(work / 'results.zip')),
            capture_output=True, text=True, timeout=180)
        assert execution.returncode == 0, execution.stdout + '\n' + execution.stderr
        evidence = json.loads(execution.stdout)
        assert len(adapter.calls) == evidence['attempted']
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
    # Independent unfiltered controls for every actual exclusion, rather than
    # assuming a fixed catalog (e.g. recent Writer can export PNG).
    controls = []
    for file_index, row in enumerate(report['files']):
        for target in row['targets']:
            if target['state'] != 'skipped':
                continue
            control = work / 'negative-controls' / (str(file_index) + '-' + target['format'])
            try:
                LibreOfficeAdapter(adapter.executable).convert(paths[file_index], target['format'],
                    control, cancel=threading.Event(), timeout=15, on_started=lambda: None)
            except GuardError as error:
                assert error.code in ('ENGINE_OUTPUT_MISSING', 'ENGINE_FAILED'), error.code
                controls.append({'source': paths[file_index].name, 'format': target['format'],
                                 'nativeError': error.code})
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
    assert hashes == {path.name: digest(path) for path in paths}
    assert not [t for t in threading.enumerate() if t.name.startswith(('aio-guard', 'aio-native-observation'))]
    evidence.update(engine=engine, windows='NOT_RUN', qtNativeSave='NOT_RUN',
        sourceBytesUnchanged=True, observerThreadsAfterClose=0,
        directNegativeControls={'checked': len(controls), 'falseDenials': 0, 'results': controls},
        pdfBaseline={'pages':2, 'textEqual':True, 'encodedImagesEqual':True},
        qualityScannerAdded=False)
    (work / 'evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n',
                                       encoding='utf-8')
    print(json.dumps(evidence, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
