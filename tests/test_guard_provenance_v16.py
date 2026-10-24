"""Byte identity must survive retention, saving, packaging and recovery."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app_backend.converter_guard import ConverterGuard, GuardError
from test_converter_guard_v02 import FakeAdapter, wait_finished


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.guard = ConverterGuard(adapter=FakeAdapter(), recovery_parent=self.root/'owned')
        self.addCleanup(self.guard.close)

    def ready(self):
        job = self.guard.create([{'id':'f','name':'source.bin','size':4}], ['pdf','odt'])['jobId']
        self.guard.upload(job,'f',io.BytesIO(b'keep'),4)
        self.guard.start(job,archive_mode='on_demand')
        return job,wait_finished(self.guard,job)

    def test_source_and_retained_output_hashes_match_saved_independent_readback(self):
        job,state = self.ready()
        report = self.guard.report(job)
        self.assertEqual(report['files'][0]['sourceSha256'],hashlib.sha256(b'keep').hexdigest())
        expected = hashlib.sha256(b'data').hexdigest()
        self.assertEqual(report['files'][0]['targets'][0]['outputs'][0]['sha256'],expected)
        saved = self.guard.save_folder(state['result']['ticket'],self.root)
        manifest = json.loads((self.root/saved['name']/'AIO-results-manifest.json').read_text())
        self.assertTrue(manifest['readbackVerified'])
        for output in manifest['outputs']:
            self.assertEqual([output[k] for k in ('sha256','exportSha256','copySha256','readbackSha256')],
                             [expected]*4)

    def test_download_path_replaced_after_read_is_not_verified(self):
        from app_backend.converter_guard.save_verification import sha256_stream
        job,_state = self.ready()
        artifact = self.guard._jobs[job].artifacts[0]
        download = self.root/'external.pdf'
        download.write_bytes(artifact.path.read_bytes())
        replacement = self.root/'replacement'
        replacement.write_bytes(b'evil')
        def replace_after_read(stream, **kwargs):
            result = sha256_stream(stream, **kwargs)
            if isinstance(stream.name, int):
                replacement.replace(download)
            return result
        with patch('app_backend.converter_guard.save_verification.sha256_stream', side_effect=replace_after_read):
            with self.assertRaises(GuardError) as error:
                self.guard.verify_saved_download(artifact.ticket,download)
        self.assertEqual(error.exception.code,'SAVE_VERIFY_FAILED')

    def test_changed_import_does_not_repeat_siblings_or_block_next_file(self):
        for code in ('SOURCE_MODEL_MISMATCH','NATIVE_IMPORT_FILTER_CHANGED'):
            with self.subTest(code=code):
                attempts = []
                class ChangedImport(FakeAdapter):
                    def convert(self, source, target, workspace, **kwargs):
                        attempts.append((source.stem,target))
                        if source.stem == 'first':
                            raise GuardError(code,'Import changed.',stage='import')
                        return super().convert(source,target,workspace,**kwargs)
                guard = ConverterGuard(adapter=ChangedImport(),recovery_parent=self.root/code)
                self.addCleanup(guard.close)
                rows = [{'id':n,'name':n+'.bin','size':4} for n in ('first','second')]
                job = guard.create(rows,['pdf','odt'])['jobId']
                for row in rows:
                    guard.upload(job,row['id'],io.BytesIO(b'keep'),4)
                guard.start(job)
                state = wait_finished(guard,job)
                self.assertEqual((state['succeeded'],state['failed']),(2,2))
                self.assertEqual(attempts,[('first','pdf'),('second','pdf'),('second','odt')])

    def test_same_size_temp_mutation_cannot_be_acknowledged_or_archived(self):
        job,state = self.ready()
        artifact = self.guard._jobs[job].artifacts[0]
        artifact.path.write_bytes(b'evil')
        with self.assertRaises(GuardError) as error:
            self.guard.save_folder(state['result']['ticket'],self.root)
        self.assertEqual(error.exception.code,'SAVE_CHANGED')
        self.assertFalse(any(a.saved for a in self.guard._jobs[job].artifacts))
        self.guard._make_result(self.guard._jobs[job],force_archive=True)
        self.assertIsNone(self.guard._jobs[job].bundle)
        self.assertEqual(self.guard._jobs[job].packaging_error,'SAVE_CHANGED')

    def test_download_cannot_verify_two_matching_mutated_copies(self):
        job,_state = self.ready()
        artifact = self.guard._jobs[job].artifacts[0]
        artifact.path.write_bytes(b'evil')
        destination = self.root/'download.pdf'
        destination.write_bytes(b'evil')
        with self.assertRaises(GuardError) as error:
            self.guard.verify_saved_download(artifact.ticket,destination)
        self.assertEqual(error.exception.code,'SAVE_CHANGED')
        self.assertFalse(artifact.saved)

    def test_bundle_download_uses_fingerprint_from_archive_creation(self):
        job,_state = self.ready()
        self.guard._make_result(self.guard._jobs[job],force_archive=True)
        bundle = self.guard._jobs[job].bundle
        self.assertIsNotNone(bundle)
        self.assertEqual(bundle.sha256,hashlib.sha256(bundle.path.read_bytes()).hexdigest())
        # Same-length mutation of both copies must not become a new expected
        # fingerprint during readback of a previously validated result.
        changed = bytearray(bundle.path.read_bytes())
        changed[-1] ^= 1
        bundle.path.write_bytes(changed)
        destination = self.root/'download.zip'
        destination.write_bytes(changed)
        with self.assertRaises(GuardError) as error:
            self.guard.verify_saved_download(bundle.ticket,destination)
        self.assertEqual(error.exception.code,'SAVE_CHANGED')
        self.assertFalse(bundle.saved)

    def test_recovery_detects_same_size_mutation_using_retained_fingerprint(self):
        job,_state = self.ready()
        artifact = self.guard._jobs[job].artifacts[0]
        artifact.path.write_bytes(b'evil')
        self.guard.close()
        recovered = ConverterGuard(adapter=FakeAdapter(),recovery_parent=self.root/'owned')
        self.addCleanup(recovered.close)
        report = recovered.report(job)
        self.assertEqual(report['files'][0]['targets'][0]['code'],'RECOVERY_OUTPUT_MISSING')
        self.assertEqual(report['files'][0]['targets'][0]['state'],'error')
        self.assertEqual(report['files'][0]['sourceSha256'],hashlib.sha256(b'keep').hexdigest())


if __name__ == '__main__':
    unittest.main()
