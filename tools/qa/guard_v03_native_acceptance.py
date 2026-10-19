"""Opt-in real LibreOffice/HTTP acceptance for individual plans and retries.

Use synthetic fixtures produced by guard_v02_native_acceptance.py. No UI mount,
user corpus, Windows machine or workflow dispatch is performed by this script.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from app_backend.converter_guard import ConverterGuard, LibreOfficeAdapter
from test_converter_guard_http_v02 import start_test_server


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    from pypdf import PdfReader

    parser = argparse.ArgumentParser()
    parser.add_argument('--fixtures', required=True, type=Path)
    parser.add_argument('--work', required=True, type=Path)
    args = parser.parse_args()
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=False)
    sources = [args.fixtures / name for name in ('writer.docx', 'image.bmp', 'calc.xlsx')]
    hashes = {p.name: digest(p) for p in sources}

    class ObservedAdapter(LibreOfficeAdapter):
        def __init__(self):
            super().__init__()
            self.calls = []

        def convert(self, source, target, *args, **kwargs):
            assert digest(source) == hashes[source.name], 'Staged source bytes changed'
            self.calls.append((source.name, target))
            return super().convert(source, target, *args, **kwargs)

    adapter = ObservedAdapter()
    assert adapter.available, 'NOT_RUN: installed LibreOffice required'
    spool = work / 'spool'
    spool.mkdir()
    guard = ConverterGuard(adapter=adapter, spool_parent=spool)
    server, thread, origin, token = start_test_server(guard)
    evidence = {'scope': 'Linux/source, synthetic, real LibreOffice + loopback HTTP; Windows NOT_RUN',
                'engine': subprocess.run([str(adapter.executable), '--version'],
                    capture_output=True, text=True, check=True, timeout=15).stdout.strip(),
                'fullPolls': 0, 'unchangedPolls': 0}

    def request(path, body=None, headers=None):
        hdr = {'X-AIO-Token': token, **(headers or {})}
        if isinstance(body, dict):
            body = json.dumps(body).encode('utf-8')
            hdr['Content-Type'] = 'application/json'
        return urlopen(Request(origin + '/api/converter-guard/' + path,
                              data=body, headers=hdr), timeout=15)

    def metadata(*args, **kwargs):
        with request(*args, **kwargs) as response:
            return json.load(response)

    def conditional(identity, etag=None):
        try:
            with request('jobs?job=' + identity,
                         headers={'If-None-Match': etag} if etag else {}) as response:
                evidence['fullPolls'] += 1
                return json.load(response), response.headers['ETag']
        except HTTPError as error:
            with error:
                assert error.code == 304, error
                assert error.read() == b''
                assert error.headers['ETag'] == etag
                evidence['unchangedPolls'] += 1
                return None, etag

    def submit(paths, plan):
        rows = [{'id': str(i), 'name': p.name, 'size': p.stat().st_size}
                for i, p in enumerate(paths)]
        identity = metadata('jobs', {'files': rows, 'plan': plan})['jobId']
        before, tag = conditional(identity)
        assert conditional(identity, tag)[0] is None
        for i, path in enumerate(paths):
            with path.open('rb') as stream, request(
                    'input?job=' + identity + '&file=' + str(i), stream,
                    {'Content-Type': 'application/octet-stream',
                     'Content-Length': str(path.stat().st_size)}) as response:
                assert json.load(response)['ok']
        after, tag = conditional(identity, tag)
        assert after['revision'] > before['revision']
        metadata('start', {'jobId': identity})
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            state, tag = conditional(identity, tag)
            if state and state['finished']:
                assert conditional(identity, tag)[0] is None
                return state
            time.sleep(.04)
        raise AssertionError('Native QA deadline reached')

    def download(output, name):
        destination = work / name
        with urlopen(origin + output['downloadUrl'], timeout=15) as response:
            with destination.open('wb') as stream:
                shutil.copyfileobj(response, stream, 64 * 1024)
        return destination

    identities = []
    started = time.monotonic()
    try:
        plan = [{'fileId': '0', 'formats': ['pdf', 'odt']},
                {'fileId': '1', 'formats': ['png']},
                {'fileId': '2', 'formats': ['xlsx', 'csv']}]
        first = submit(sources, plan)
        identities.append(first['jobId'])
        assert first['state'] == 'ready' and first['total'] == 5, first
        expected = [('writer.docx', 'pdf'), ('writer.docx', 'odt'), ('image.bmp', 'png'),
                    ('calc.xlsx', 'xlsx'), ('calc.xlsx', 'csv')]
        assert adapter.calls == expected, adapter.calls
        archive = download(first['result'], 'individual-plan.zip')
        with ZipFile(archive) as bundle:
            assert bundle.namelist()[:-1] == [o['archiveName'] for o in first['result']['outputs']]
            assert len(bundle.namelist()) == 6  # five outputs plus conversion report
        pdf_output = next(o for o in first['result']['outputs'] if o['format'] == 'pdf')
        pdf = download(pdf_output, 'writer.pdf')
        text = ''.join(p.extract_text() or '' for p in PdfReader(pdf).pages)
        assert all(m in text for m in ('GUARD_BEGIN_20261019', 'GUARD_END_20261019', 'GUARD_TABLE_A'))
        saved_pdf_hash = digest(pdf)
        evidence['individualPlan'] = {'requestedPairs': 5, 'nativeCalls': len(adapter.calls),
            'allOutputsReady': True, 'archiveOutputs': 5, 'writerPDFMarkersPreserved': True}

        failed = submit([sources[0]], [{'fileId': '0', 'formats': ['pdf', 'aio_unsupported_qa']}])
        identities.append(failed['jobId'])
        assert failed['state'] == 'partial', failed
        assert failed['files'][0]['targets'][0]['state'] == 'ready'
        assert failed['files'][0]['targets'][1]['state'] == 'error'
        assert failed['files'][0]['targets'][1]['libreOfficeStarted']
        partial_pdf = next(o for o in failed['result']['outputs'] if o['format'] == 'pdf')
        partial_hash = digest(download(partial_pdf, 'partial-success.pdf'))
        before = len(adapter.calls)
        retry = metadata('retry-plan?job=' + failed['jobId'])
        assert retry['plan'] == [{'fileId': '0', 'formats': ['aio_unsupported_qa']}], retry
        assert retry['retainedOutputs'] == 1 and len(retry['files']) == 1
        assert retry['requiresUpload'] and len(adapter.calls) == before
        # Simulate an explicit user correction of the unsupported target to TXT.
        fixed = submit([sources[0]], [{'fileId': '0', 'formats': ['txt']}])
        identities.append(fixed['jobId'])
        assert fixed['state'] == 'ready'
        assert adapter.calls[before:] == [('writer.docx', 'txt')]
        assert digest(download(partial_pdf, 'retained-partial.pdf')) == partial_hash
        assert digest(download(pdf_output, 'retained-original.pdf')) == saved_pdf_hash
        assert len(adapter.calls) == 8, adapter.calls
        evidence['retry'] = {'nativeFailureTarget': 'aio_unsupported_qa',
            'retryPlanContainsOnlyFailedPair': True, 'retryPlanStartsNoWork': True,
            'explicitCorrectedTarget': 'txt', 'correctionNativeCalls': 1,
            'previousPDFsStillAvailableAndUnchanged': True,
            'automaticReconversionOfSuccessfulPairs': 0}

        for state in (first, failed, fixed):
            key = state['result']['downloadUrl'].split('ticket=')[1]
            metadata('saved', {'jobId': state['jobId'], 'ticket': key})
            metadata('release', {'jobId': state['jobId']})
        assert guard.storage_stats()['jobs'] == guard.storage_stats()['reservedBytes'] == 0
        assert all(digest(p) == hashes[p.name] for p in sources)
        evidence.update(status='PASS', nativeCalls=adapter.calls,
            sourceBytesUnchangedAtEveryNativeLaunch=True, ownedJobsAfterRelease=0,
            reservedBytesAfterRelease=0, seconds=round(time.monotonic() - started, 3))
        (work / 'evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(evidence, ensure_ascii=False), flush=True)
    finally:
        server.api.close()
        guard.close()
        server.shutdown(); server.server_close(); thread.join(3)


if __name__ == '__main__':
    main()
