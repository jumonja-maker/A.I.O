"""Explicit local acceptance of live saving across real native exports.

All documents and directories belong to this run. A test barrier BEFORE the
second native export makes the live-save race reproducible; exports themselves
use the unchanged LibreOffice adapter. This is not a speed/visual/Windows test.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app_backend.converter_guard import ConverterGuard, GuardError, GuardLimits, LibreOfficeAdapter
from guard_v08_native_acceptance import fixtures


class BarrierAdapter(LibreOfficeAdapter):
    def __init__(self, executable=None):
        super().__init__(executable)
        self.waiting, self.resume = threading.Event(), threading.Event()
        self.native_calls = 0

    def convert(self, source, target, workspace, **kwargs):
        if source.name == 'hold.docx':
            self.waiting.set()
            deadline = time.monotonic() + 120
            while not self.resume.wait(.02):
                if kwargs['cancel'].is_set():
                    raise GuardError('CANCELLED', 'Acceptance barrier cancelled.')
                if time.monotonic() >= deadline:
                    raise GuardError('ACCEPTANCE_TIMEOUT', 'Acceptance barrier timed out.')
        self.native_calls += 1
        return super().convert(source, target, workspace, **kwargs)


def sha256(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def verify_snapshot(root, acknowledgement):
    folder = root / acknowledgement['name']
    manifest = json.loads((folder / 'AIO-results-manifest.json').read_text())
    status = json.loads((folder / 'AIO-save-status.json').read_text())
    assert status['state'] == 'completed'
    assert status['saveId'] == manifest['saveId'] == acknowledgement['saveId']
    assert status['outputsWritten'] == len(manifest['outputs']) == acknowledgement['outputs']
    assert not manifest['readbackVerified'], 'Application must not claim an independent readback'
    for item in manifest['outputs']:
        path = folder / item['path']
        assert path.stat().st_size == item['size']
        assert sha256(path) == item['sha256'], 'Independent QA readback mismatch'
    return folder, manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--executable', type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    source = fixtures(args.output / 'fixtures')['docx']
    inputs = args.output / 'inputs'
    inputs.mkdir()
    paths = [inputs / name for name in ('first.docx', 'hold.docx', 'last.docx')]
    for path in paths:
        path.write_bytes(source.read_bytes())
    hashes = {path.name: sha256(path) for path in paths}
    adapter = BarrierAdapter(args.executable)
    assert adapter.available, 'LibreOffice executable is required'
    version = subprocess.run([str(adapter.executable), '--version'], capture_output=True,
                             text=True, check=True, timeout=15).stdout.strip()
    guard = ConverterGuard(adapter=adapter, limits=GuardLimits(file_timeout=120, preflight_timeout=90),
                           spool_parent=args.output / 'spool', recovery_parent=args.output / 'owned-jobs')
    tick = time.monotonic()
    evidence = {'status': 'FAIL', 'engineVersion': version, 'nativeExports': 0,
                'scope': 'Linux native CLI; generated DOCX fixtures; controlled barrier before second export',
                'windowsQt': 'NOT_RUN', 'fullVisualQuality': 'NOT_RUN'}
    try:
        identity = guard.create([{'id': str(i), 'name': p.name, 'size': p.stat().st_size}
                                 for i, p in enumerate(paths)], prepare_only=True)['jobId']
        for i, path in enumerate(paths):
            with path.open('rb') as stream:
                guard.upload(identity, str(i), stream, path.stat().st_size)
        start = time.monotonic()
        inspection = guard.preflight(identity)
        evidence['preflightMs'] = round((time.monotonic() - start) * 1000)
        pdf = next(f for f in inspection['formats'] if f['id'] == 'pdf')
        assert set(pdf['verifiedFileIds']) == {'0', '1', '2'}
        guard.start(identity, file_ids=['0', '1', '2'],
                    plan=[{'fileId': str(i), 'formats': ['pdf']} for i in range(3)], only_verified=True)
        assert adapter.waiting.wait(90), 'Second export did not reach the acceptance barrier'
        state = guard.status(identity)
        assert not state['finished'] and state['succeeded'] == 1
        assert state['currentTask']['fileId'] == '1'
        destination = args.output / 'saved'
        destination.mkdir()
        start = time.monotonic()
        partial = guard.save_folder(state['partialResult']['ticket'], destination)
        evidence['liveSaveMs'] = round((time.monotonic() - start) * 1000)
        assert partial['saved'] and partial['snapshotComplete'] and not partial['savedAll']
        assert partial['jobRunning'] and partial['outputs'] == 1
        partial_root, partial_manifest = verify_snapshot(destination, partial)
        live = guard.status(identity)
        assert not live['finished'] and live['currentTask']['fileId'] == '1'
        adapter.resume.set()
        deadline = time.monotonic() + 120
        while not (done := guard.status(identity))['finished']:
            assert time.monotonic() < deadline, 'Native queue did not finish'
            time.sleep(.05)
        assert (done['succeeded'], done['failed'], done['cancelled']) == (3, 0, 0), guard.report(identity)
        assert sum(output['saved'] for output in done['result']['outputs']) == 1
        final = guard.save_folder(done['result']['ticket'], destination)
        assert final['savedAll'] and final['outputs'] == 3 and final['remainingOutputs'] == 0
        final_root, final_manifest = verify_snapshot(destination, final)
        assert partial_manifest['outputs'][0]['sha256'] == final_manifest['outputs'][0]['sha256']
        pdf_text = 'NOT_RUN'
        try:
            from pypdf import PdfReader
            for item in final_manifest['outputs']:
                reader = PdfReader(final_root / item['path'])
                assert len(reader.pages) == 1
                text = '\n'.join(page.extract_text() or '' for page in reader.pages)
                for marker in ('AIO_DOCUMENT_MARKER', 'AIO_TABLE_LEFT', 'AIO_TABLE_RIGHT'):
                    assert marker in text, 'Expected marker absent from real PDF'
            pdf_text = 'PASS'
        except ImportError:
            pass
        assert hashes == {p.name: sha256(p) for p in paths}
        guard.release(identity)
        assert not guard.recoverable()['jobs']
        evidence.update(status='PASS', nativeExports=adapter.native_calls,
                        liveSnapshotOutputs=partial['outputs'], finalSnapshotOutputs=final['outputs'],
                        savedDuringLiveQueue=True, lateOutputsNotAcknowledged=True,
                        independentQaReadback='PASS', pdfTextMarkers=pdf_text,
                        sourceBytesUnchanged='PASS', sourceSHA256=hashes,
                        finalSucceeded=done['succeeded'], finalFailed=done['failed'],
                        finalCancelled=done['cancelled'])
    finally:
        adapter.resume.set()
        guard.close()
        evidence['elapsedMs'] = round((time.monotonic() - tick) * 1000)
        (args.output / 'evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(evidence, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
