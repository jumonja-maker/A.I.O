"""M30 ZIP payload/readback and snapshot acknowledgement; fake conversion only."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import shutil
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch
import warnings
import zipfile

from app_backend.converter_guard import ConverterGuard, GuardError
from app_backend.converter_guard.archive_verification import verify_built_archive, CHUNK
from test_converter_guard_v02 import FakeAdapter, wait_finished


SNAPSHOT = {'snapshotId': 'a' * 32, 'snapshotRevision': 7}
META = {'AIO-conversion-report.json': b'{"qualityCompared":false}',
        'AIO-results-manifest.json': b'{"readbackVerified":false}'}


def entry(name, content):
    digest = hashlib.sha256(content).hexdigest()
    return {'path': name, 'size': len(content), 'sha256': digest,
            'exportSha256': digest, 'copySha256': digest,
            'fileId': 'f', 'format': 'pdf'}


class ArchiveReadbackPolicy(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'result.zip'
        self.payload = [('one/doc.pdf', b'first'), ('two/empty.txt', b'')]
        self.entries = [entry(name, content) for name, content in self.payload]

    def build(self, payload=None, metadata=None, compression=zipfile.ZIP_STORED):
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            with zipfile.ZipFile(self.path, 'w', compression=compression) as archive:
                for name, content in (self.payload if payload is None else payload):
                    with archive.open(name, 'w', force_zip64=True) as stream:
                        stream.write(content)
                for name, content in (META if metadata is None else metadata).items():
                    with archive.open(name, 'w', force_zip64=True) as stream:
                        stream.write(content)

    def verify(self, **kwargs):
        return verify_built_archive(self.path, self.entries, META, SNAPSHOT, **kwargs)

    def reject(self):
        with self.assertRaises(GuardError) as caught:
            self.verify()
        self.assertEqual(caught.exception.code, 'BUNDLE_VERIFY_FAILED')

    def test_exact_zip64_empty_and_metadata_match_container_digest_without_save_claim(self):
        self.build(); before = deepcopy(self.entries)
        size, digest, receipt = self.verify()
        raw = self.path.read_bytes()
        self.assertEqual((size, digest), (len(raw), hashlib.sha256(raw).hexdigest()))
        self.assertEqual(receipt['outputsVerified'], 2)
        self.assertEqual(receipt['bytesVerified'], 5)
        self.assertEqual(receipt['metadataVerified'], 2)
        self.assertFalse(receipt['externalSaveVerified'])
        self.assertFalse(receipt['qualityCompared'])
        self.assertEqual(receipt['snapshotId'], SNAPSHOT['snapshotId'])
        self.assertEqual(self.entries, before)

    def test_repaired_crc_wrong_member_bytes_are_rejected(self):
        self.build([('one/doc.pdf', b'other'), self.payload[1]])
        with zipfile.ZipFile(self.path) as archive:
            self.assertIsNone(archive.testzip())  # Valid CRC is not the export digest.
        self.reject()

    def test_crc_damage_is_rejected(self):
        self.build()
        with zipfile.ZipFile(self.path) as archive:
            offset = archive.getinfo('one/doc.pdf').header_offset
        with self.path.open('r+b') as stream:
            stream.seek(offset + 26)
            names, extra = struct.unpack('<HH', stream.read(4))
            stream.seek(offset + 30 + names + extra)
            stream.write(b'X')
        self.reject()

    def test_missing_unexpected_duplicate_members_rejected(self):
        for payload in (self.payload[:1], self.payload + [('extra.bin', b'x')],
                        self.payload + [self.payload[0]]):
            with self.subTest(payload=payload):
                self.build(payload); self.reject()

    def test_report_and_manifest_tampering_rejected(self):
        for name in META:
            with self.subTest(name=name):
                metadata = dict(META); metadata[name] = b'{}'
                self.build(metadata=metadata); self.reject()

    def test_metadata_members_must_both_exist(self):
        self.build(metadata={}); self.reject()

    def test_expected_digest_size_and_export_binding_rejected(self):
        self.build()
        for key, value in (('size', True), ('size', -1), ('size', 6),
                           ('sha256', 'bad'), ('exportSha256', '0' * 64),
                           ('copySha256', '0' * 64)):
            with self.subTest(key=key, value=value):
                original = self.entries[0][key]; self.entries[0][key] = value
                self.reject(); self.entries[0][key] = original

    def test_manifest_path_aliases_and_traversal_rejected(self):
        self.build()
        for name in ('../escape', '/absolute', 'C:/escape', 'dir\\escape',
                     'a/../b', 'a//b', 'AIO-results-manifest.json'):
            with self.subTest(name=name):
                original = self.entries[0]['path']; self.entries[0]['path'] = name
                self.reject(); self.entries[0]['path'] = original
        self.entries.append(entry('ONE/DOC.PDF', b'first')); self.reject()

    def test_file_directory_prefix_collision_rejected(self):
        self.payload = [('a', b'x'), ('a-b', b'x'), ('a/b', b'x')]
        self.entries = [entry(n, b) for n, b in self.payload]
        self.build(); self.reject()

    def test_nfd_filename_preserved_without_rewriting(self):
        self.payload[0] = ('one/cafe\u0301.pdf', b'first')
        self.entries[0] = entry(*self.payload[0]); self.build()
        self.assertEqual(self.verify()[2]['outputsVerified'], 2)
        self.entries.append(entry('one/caf\u00e9.pdf', b'first')); self.reject()

    def test_compressed_or_symlink_member_rejected(self):
        self.build(compression=zipfile.ZIP_DEFLATED); self.reject()
        self.build()
        # Duplicate/symlink entry is never interpreted as a filesystem path.
        with zipfile.ZipFile(self.path, 'a') as archive:
            info = zipfile.ZipInfo('link'); info.create_system = 3
            info.external_attr = 0o120777 << 16
            archive.writestr(info, b'outside')
        self.reject()

    def test_pre_cancelled_verification_does_not_open_archive(self):
        cancel = threading.Event(); cancel.set()
        with patch('app_backend.converter_guard.archive_verification.os.open') as opening:
            with self.assertRaises(GuardError) as caught: self.verify(cancel=cancel)
        self.assertEqual(caught.exception.code, 'BUNDLE_CANCELLED'); opening.assert_not_called()

    def test_mid_read_cancellation_does_not_claim_verified(self):
        self.payload[0] = ('one/doc.pdf', b'x' * (CHUNK * 3))
        self.entries[0] = entry(*self.payload[0]); self.build()
        cancel = threading.Event(); original = zipfile.ZipExtFile.read
        def read(stream, size=-1):
            data = original(stream, size); cancel.set(); return data
        with patch.object(zipfile.ZipExtFile, 'read', read):
            with self.assertRaises(GuardError) as caught: self.verify(cancel=cancel)
        self.assertEqual(caught.exception.code, 'BUNDLE_CANCELLED')

    def test_payload_reads_are_chunked(self):
        self.payload[0] = ('one/doc.pdf', b'x' * (CHUNK * 33 + 1))
        self.entries[0] = entry(*self.payload[0]); self.build()
        sizes = []; original = zipfile.ZipExtFile.read
        def read(stream, size=-1):
            sizes.append(size); return original(stream, size)
        with patch.object(zipfile.ZipExtFile, 'read', read): self.verify()
        self.assertGreater(len(sizes), 33)
        self.assertEqual(set(sizes), {CHUNK})

    def test_container_replacement_or_change_during_fingerprint_rejected(self):
        self.build()
        from app_backend.converter_guard import archive_verification as module
        original = module._hash
        def change(stream, cancel):
            result = original(stream, cancel)
            if isinstance(stream, io.BufferedReader):
                with self.path.open('ab') as writer: writer.write(b'x')
            return result
        with patch.object(module, '_hash', side_effect=change): self.reject()

    def test_container_link_and_read_errors_rejected(self):
        self.build()
        with patch('app_backend.converter_guard.archive_verification.path_is_link', return_value=True):
            self.reject()
        with patch.object(zipfile.ZipExtFile, 'read', side_effect=OSError('controlled')):
            self.reject()


class ArchiveSnapshotIntegration(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / 'owned').mkdir()
        self.guard = ConverterGuard(adapter=FakeAdapter(), spool_parent=self.root / 'owned')
        self.addCleanup(self.guard.close)

    def ready(self, formats=('pdf', 'html')):
        identity = self.guard.create([{'id':'f', 'name':'source.docx', 'size':4}], list(formats))['jobId']
        self.guard.upload(identity, 'f', io.BytesIO(b'keep'), 4)
        self.guard.start(identity, archive_mode='on_demand')
        wait_finished(self.guard, identity)
        return self.guard._jobs[identity]

    def bundle(self, job):
        self.guard.rebuild_bundle(job.id)
        return wait_finished(self.guard, job.id)

    def assert_fallback(self, job, reserved, code):
        state = self.guard.status(job.id)
        self.assertEqual(state['packagingError'], code)
        self.assertIsNone(job.bundle)
        self.assertEqual(job.reserved, reserved)
        self.assertFalse(list(job.root.glob('results-*.zip')))
        self.assertTrue(all(a.path.exists() and not a.saved for a in job.artifacts))
        with self.guard.open_result(state['result']['ticket']) as (stream, _, _):
            self.assertTrue(stream.read())

    def test_corrupt_valid_crc_archive_is_not_published(self):
        job = self.ready(); reserved = job.reserved
        original = zipfile.ZipFile.open
        @contextmanager
        def changed(archive, name, mode='r', pwd=None, *, force_zip64=False):
            with original(archive, name, mode, pwd, force_zip64=force_zip64) as stream:
                if mode == 'w' and isinstance(name, zipfile.ZipInfo):
                    class Writer:
                        def write(self, data):
                            return stream.write(bytes([data[0] ^ 1]) + data[1:] if data else data)
                    yield Writer()
                else:
                    yield stream
        with patch.object(zipfile.ZipFile, 'open', changed): self.bundle(job)
        self.assert_fallback(job, reserved, 'BUNDLE_VERIFY_FAILED')
        self.assertEqual(self.guard.status(job.id)['failed'], 0)  # Not a conversion failure.

    def test_saved_archive_acknowledges_its_snapshot_not_later_outputs(self):
        from app_backend.converter_guard.core import _Artifact
        job = self.ready(); self.bundle(job)
        original_count = len(job.artifacts)
        destination = self.root / 'saved.zip'
        with self.guard.open_result(job.bundle.ticket) as (stream, _, _), destination.open('xb') as output:
            shutil.copyfileobj(stream, output)
        self.guard.verify_saved_download(job.bundle.ticket, destination)
        with self.guard._lock:
            path = job.root / 'late.pdf'; path.write_bytes(b'late')
            later = _Artifact(job.id, path, 'late.pdf', 4, 'z'*43, 'f', 'pdf', 'late.pdf',
                              sha256=hashlib.sha256(b'late').hexdigest())
            job.artifacts.append(later); job.files[0].targets[0].artifacts.append(later)
            self.guard._tickets[later.ticket] = later
        result = self.guard.confirm_saved(job.id, job.bundle.ticket, compact=True)
        self.assertFalse(later.saved)
        self.assertFalse(result['savedAll'])
        self.assertEqual(result['outputs'], original_count)
        self.assertEqual(result['remainingOutputs'], 1)
        self.assertTrue(all(a.saved and a.readback_verified for a in job.artifacts[:-1]))

    def test_receipt_and_embedded_snapshots_are_explicit_not_external_save_claims(self):
        job = self.ready(('pdf','html','bad')); state = self.bundle(job)
        check = state['result']['archiveVerification']
        self.assertEqual(check['outputsVerified'], 3)
        self.assertFalse(check['externalSaveVerified']); self.assertFalse(check['qualityCompared'])
        with zipfile.ZipFile(job.bundle.path) as archive:
            report = json.loads(archive.read('AIO-conversion-report.json'))
            manifest = json.loads(archive.read('AIO-results-manifest.json'))
        self.assertEqual(report['archiveSnapshot'], manifest['archiveSnapshot'])
        self.assertEqual(report['archiveSnapshot']['snapshotId'], check['snapshotId'])
        self.assertEqual(report['archiveSnapshot']['externalSave'], 'not_observed')
        self.assertEqual(report['reportScope'], 'conversion_snapshot_at_archive_start')
        self.assertEqual(report['failed'], 1)
        self.assertEqual(report['files'][0]['targets'][-1]['state'], 'error')
        self.assertFalse(manifest['readbackVerified'])  # Embedded before entry readback.
        self.assertFalse(any(a.saved for a in job.artifacts))

    def test_cancelled_archive_readback_keeps_outputs_and_can_retry(self):
        from app_backend.converter_guard import archive_verification as module
        job = self.ready(); reserved = job.reserved
        original = module.verify_built_archive
        def cancel(*args, **kwargs):
            job.bundle_cancel.set(); return original(*args, **kwargs)
        with patch.object(module, 'verify_built_archive', side_effect=cancel): self.bundle(job)
        self.assert_fallback(job, reserved, 'BUNDLE_CANCELLED')
        self.assertIsNone(self.bundle(job)['packagingError'])
        self.assertIsNotNone(job.bundle)

    def test_folder_save_does_not_create_or_verify_zip(self):
        job = self.ready()
        with patch('app_backend.converter_guard.archive_verification.verify_built_archive') as verifying:
            result = self.guard.save_folder(job.artifacts[0].ticket, self.root)
        verifying.assert_not_called(); self.assertTrue(result['readbackVerified'])
        self.assertIsNone(job.bundle)

    def test_empty_output_and_html_resources_are_all_verified(self):
        job = self.ready(('txt','html')); state = self.bundle(job)
        self.assertEqual(state['result']['archiveVerification']['outputsVerified'], 3)
        self.assertEqual(len(job.bundle.bundle_outputs), 3)
        self.assertTrue(any(a.size == 0 for a in job.bundle.bundle_outputs))

    def test_changed_external_archive_never_enables_readback_acknowledgement(self):
        job = self.ready(); self.bundle(job)
        destination = self.root / 'changed.zip'; destination.write_bytes(job.bundle.path.read_bytes() + b'X')
        with self.assertRaises(GuardError): self.guard.verify_saved_download(job.bundle.ticket, destination)
        self.assertFalse(job.bundle.readback_verified)
        self.assertFalse(any(a.saved for a in job.artifacts))

    def test_source_change_before_packaging_still_fails_export_hash_check(self):
        job = self.ready(); reserved = job.reserved
        artifact = job.artifacts[0]; artifact.path.write_bytes(b'evil')
        self.bundle(job)
        self.assert_fallback(job, reserved, 'SAVE_CHANGED')


if __name__ == '__main__':
    unittest.main()
