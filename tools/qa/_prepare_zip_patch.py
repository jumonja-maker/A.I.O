"""Disposable exact-base probe; never included in the product tree."""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys
import unittest
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]


def reproduce():
    names = [
        'test_archive_readback.ArchiveSnapshotIntegration.test_corrupt_valid_crc_archive_is_not_published',
        'test_archive_readback.ArchiveSnapshotIntegration.test_saved_archive_acknowledges_its_snapshot_not_later_outputs']
    suite = unittest.defaultTestLoader.loadTestsFromNames(names)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    assert result.testsRun == 2 and len(result.failures) == 2 and not result.errors, result
    print('ZIP_BASELINE=REPRODUCED_TWO_DEFECTS;CONVERSION=CONTROLLED;FILESYSTEM=REAL')


def prepare():
    p = ROOT / 'app_backend/converter_guard/core.py'
    raw = p.read_bytes().replace(b'\r\n', b'\n')
    sha = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
    assert sha == '6ac790c5dd0aeded9db16aacbd6fe6458548f367', sha
    text = raw.decode('utf-8')
    def replace(old, new):
        nonlocal text
        assert text.count(old) == 1, (old[:140], text.count(old))
        text = text.replace(old, new)
    replace('    source_sheet: dict | None = None\n',
            '    source_sheet: dict | None = None\n'
            '    bundle_outputs: tuple | None = None\n'
            '    archive_verification: dict | None = None\n')
    replace("                'downloadUrl': '/api/converter-guard/result?ticket=' + self.ticket}",
            "                'downloadUrl': '/api/converter-guard/result?ticket=' + self.ticket,\n"
            "                **({'archiveVerification':copy.deepcopy(self.archive_verification)}\n"
            "                   if self.archive_verification is not None else {})}")
    replace('            report_value = self._report(job)\n',
            "            copies = tuple(job.artifacts)\n"
            "            archive_snapshot = {'schema': 1, 'snapshotId': secrets.token_hex(16),\n"
            "                'snapshotRevision': job.revision, 'state': 'packaged',\n"
            "                'jobStateAtStart': job.state, 'externalSave': 'not_observed',\n"
            "                'saveStateScope': 'at_archive_start_not_this_download'}\n"
            "            report_value = copy.deepcopy(self._report(job))\n"
            "            report_value['reportScope'] = 'conversion_snapshot_at_archive_start'\n"
            "            report_value['archiveSnapshot'] = archive_snapshot\n")
    start = text.index('    def _make_result(')
    end = text.index('    def rebuild_bundle(', start)
    section = text[start:end]
    section = section.replace('for a in job.artifacts', 'for a in copies')
    section = section.replace('for artifact in job.artifacts:', 'for artifact in copies:')
    text = text[:start] + section + text[end:]
    replace("                with archive.open('AIO-conversion-report.json', 'w', force_zip64=True) as output:\n"
            "                    output.write(report)\n"
            "                with archive.open('AIO-results-manifest.json', 'w', force_zip64=True) as output:\n"
            "                    output.write(json.dumps(\n"
            "                    {'schema':2, 'jobId':job.id, 'readbackVerified':False,\n"
            "                     'verificationScope':'hashes_during_zip', 'outputs':manifest}, ensure_ascii=False).encode('utf-8'))\n"
            "            from .save_verification import sha256_stream\n"
            "            with bundle.open('rb') as stream:\n"
            "                actual, bundle_sha256 = sha256_stream(stream, cancel=job.bundle_cancel)\n",
            "                metadata = {'AIO-conversion-report.json': report,\n"
            "                    'AIO-results-manifest.json': json.dumps(\n"
            "                        {'schema':2, 'jobId':job.id, 'readbackVerified':False,\n"
            "                         'archiveSnapshot':archive_snapshot,\n"
            "                         'verificationScope':'hashes_during_zip', 'outputs':manifest},\n"
            "                        ensure_ascii=False).encode('utf-8')}\n"
            "                for name, content in metadata.items():\n"
            "                    with archive.open(name, 'w', force_zip64=True) as output:\n"
            "                        output.write(content)\n"
            "            from .archive_verification import verify_built_archive\n"
            "            actual, bundle_sha256, archive_check = verify_built_archive(\n"
            "                bundle, manifest, metadata, archive_snapshot, cancel=job.bundle_cancel)\n")
    replace('            with self._lock:\n                delta = actual - budget\n',
            "            with self._lock:\n"
            "                if job.bundle_cancel.is_set():\n"
            "                    raise GuardError('BUNDLE_CANCELLED', 'Упаковка отменена.', stage='bundle')\n"
            "                delta = actual - budget\n")
    replace("                                     sha256=bundle_sha256)\n",
            "                                     sha256=bundle_sha256, bundle_outputs=copies,\n"
            "                                     archive_verification=archive_check)\n")
    replace("            copies = tuple(job.artifacts) if artifact is job.bundle else (artifact,)\n",
            "            if artifact is job.bundle:\n"
            "                # Acknowledgement belongs to the immutable ZIP inventory,\n"
            "                # never to outputs that appeared after it was packaged.\n"
            "                copies = artifact.bundle_outputs\n"
            "                check = artifact.archive_verification or {}\n"
            "                if (not copies or check.get('status') != 'byte_verified' or\n"
            "                        check.get('outputsVerified') != len(copies)):\n"
            "                    raise GuardError('BUNDLE_VERIFY_FAILED',\n"
            "                        'Нет подтверждения состава архива. Результаты остаются в AIO.',\n"
            "                        stage='save')\n"
            "            else:\n"
            "                copies = (artifact,)\n")
    compile(text, str(p), 'exec')
    p.write_bytes(text.encode('utf-8'))
    print('ZIP_EXACT_BASE_PATCH=core.py')


def publish():
    for path in ('app_backend/converter_guard/core.py',
                 'app_backend/converter_guard/archive_verification.py',
                 'tests/test_archive_readback.py'):
        raw = (ROOT/path).read_bytes().replace(b'\r\n', b'\n')
        body = json.dumps({'content':raw.decode('utf-8'), 'encoding':'utf-8'}).encode()
        request = Request('https://api.github.com/repos/jumonja-maker/A.I.O/git/blobs',
            data=body, method='POST', headers={'Authorization':'Bearer '+os.environ['GH_TOKEN'],
            'Accept':'application/vnd.github+json','Content-Type':'application/json'})
        with urlopen(request, timeout=60) as response: data=json.load(response)
        sha = hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
        assert data['sha']==sha
        print('TESTED_ZIP_BLOB='+json.dumps({'path':path,'sha':sha,'bytes':len(raw)}))


if __name__ == '__main__':
    {'reproduce':reproduce,'prepare':prepare,'publish':publish}[sys.argv[1]]()
