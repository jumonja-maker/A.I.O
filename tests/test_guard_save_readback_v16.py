"""Save-stage provenance and independent disk readback, without a Windows claim."""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import tracemalloc
import unittest
from unittest.mock import patch

from app_backend.converter_guard import ConverterGuard, GuardError
from app_backend.converter_guard.save_verification import verify_folder, sha256_stream, readback_fingerprint
from test_converter_guard_v02 import FakeAdapter, wait_finished


class SaveReadbackV16(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def guard_ready(self):
        guard = ConverterGuard(adapter=FakeAdapter(), recovery_parent=self.root/'owned')
        self.addCleanup(guard.close)
        job = guard.create([{'id':'source','name':'source.docx','size':4}],['pdf','odt'])['jobId']
        guard.upload(job,'source',io.BytesIO(b'keep'),4)
        guard.start(job)
        return guard, job, wait_finished(guard,job)

    def entry(self,path,data):
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(data)
        return {'path':path.relative_to(self.root).as_posix(),'size':len(data),
                'sha256':hashlib.sha256(data).hexdigest()}

    def test_readback_records_actual_hash_and_byte_count_without_changing_html(self):
        data = b'<!doctype html>\r\n<body> A &amp; B </body>\r\n'
        entry = self.entry(self.root/'saved/page.html',data)
        proof = verify_folder(self.root,[entry])
        self.assertEqual(proof['verificationMethod'],'independent_read_sha256')
        self.assertEqual(proof['bytesVerified'],len(data))
        self.assertEqual(proof['outputsVerified'],1)
        self.assertEqual(entry['readbackSha256'],entry['sha256'])
        self.assertTrue(entry['readbackVerified'])
        self.assertEqual((self.root/entry['path']).read_bytes(),data)

    def test_same_size_and_plus_eight_byte_mutations_are_detected_with_evidence(self):
        for suffix in (b'',b'12345678'):
            with self.subTest(suffix=suffix):
                entry = self.entry(self.root/'page.html',b'<body>A</body>')
                observed = b'<body>B</body>'+suffix
                (self.root/'page.html').write_bytes(observed)
                with self.assertRaises(GuardError) as err:
                    verify_folder(self.root,[entry])
                self.assertEqual(err.exception.code,'SAVE_VERIFY_FAILED')
                self.assertEqual(entry['readbackSize'],len(observed))
                self.assertEqual(entry['readbackSha256'],hashlib.sha256(observed).hexdigest())
                self.assertFalse(entry['readbackVerified'])

    def test_file_replaced_during_readback_is_not_verified(self):
        path=self.root/'page.html'
        entry=self.entry(path,b'<body>good</body>')
        outcomes=[]
        def replace_after_read(stream,**kwargs):
            actual=sha256_stream(stream,**kwargs)
            replacement=self.root/'replacement'
            replacement.write_bytes(b'<body>evil</body>')
            try:
                replacement.replace(path)
                outcomes.append('replaced')
            except PermissionError:
                outcomes.append('blocked')
                raise
            return actual
        with patch('app_backend.converter_guard.save_verification.sha256_stream',replace_after_read),self.assertRaises(GuardError) as err:
            verify_folder(self.root,[entry])
        self.assertEqual(err.exception.code,'SAVE_VERIFY_FAILED')
        self.assertIn(outcomes,[['replaced'],['blocked']])
        self.assertFalse(entry['readbackVerified'])
        if outcomes==['blocked']:
            self.assertIsInstance(err.exception.__cause__,PermissionError)
            self.assertEqual(path.read_bytes(),b'<body>good</body>')
        else:
            self.assertEqual(path.read_bytes(),b'<body>evil</body>')
        path.unlink()  # The read descriptor is closed even on blocked replacement.

    def test_blocked_replacement_is_failed_readback_and_preserves_manifest(self):
        guard,job,state=self.guard_ready()
        def blocked_after_read(stream,**kwargs):
            result=sha256_stream(stream,**kwargs)
            if isinstance(stream.name,int):
                raise PermissionError(13,'Replacement blocked while file is open')
            return result
        with patch('app_backend.converter_guard.save_verification.sha256_stream',blocked_after_read),self.assertRaises(GuardError) as err:
            guard.save_folder(state['result']['ticket'],self.root)
        self.assertEqual(err.exception.code,'SAVE_VERIFY_FAILED')
        folder=next(self.root.glob('AIO_Results_*'))
        status=json.loads((folder/'AIO-save-status.json').read_text())
        manifest=json.loads((folder/'AIO-results-manifest.json').read_text())
        self.assertEqual((status['state'],status['code']),('failed','SAVE_VERIFY_FAILED'))
        self.assertFalse(manifest['readbackVerified'])
        self.assertTrue(any(e.get('readbackVerified') is False for e in manifest['outputs']))
        self.assertTrue(all(not a.saved and a.path.exists() for a in guard._jobs[job].artifacts))
        next(folder.rglob('*.pdf')).unlink()

    def test_open_and_stat_errors_are_failed_verification_with_closed_descriptor(self):
        path=self.root/'page.html'
        entry=self.entry(path,b'<body>good</body>')
        with patch('app_backend.converter_guard.save_verification.os.open',side_effect=PermissionError(13,'blocked')):
            with self.assertRaises(GuardError) as err:
                verify_folder(self.root,[entry])
        self.assertEqual(err.exception.code,'SAVE_VERIFY_FAILED')
        self.assertFalse(entry['readbackVerified'])
        opened=[]
        real_open=os.open
        def record_descriptor(*args,**kwargs):
            descriptor=real_open(*args,**kwargs);opened.append(descriptor);return descriptor
        with patch('app_backend.converter_guard.save_verification.os.open',side_effect=record_descriptor), \
                patch('app_backend.converter_guard.save_verification.os.fdopen',side_effect=PermissionError(13,'stream open blocked')):
            with self.assertRaises(GuardError) as err:
                readback_fingerprint(path)
        self.assertEqual(err.exception.code,'SAVE_VERIFY_FAILED')
        with self.assertRaises(OSError):os.fstat(opened[0])
        with patch('app_backend.converter_guard.save_verification.os.fstat',side_effect=OSError('stat failed')):
            with self.assertRaises(GuardError) as err:
                readback_fingerprint(path)
        self.assertEqual(err.exception.code,'SAVE_VERIFY_FAILED')
        actual_stat=Path.stat
        path_stats=[]
        def fail_final_path_stat(candidate,*args,**kwargs):
            if candidate==path:
                path_stats.append(True)
                if len(path_stats)==2:
                    raise PermissionError(13,'Final path stat blocked')
            return actual_stat(candidate,*args,**kwargs)
        with patch.object(Path,'stat',fail_final_path_stat):
            with self.assertRaises(GuardError) as err:
                readback_fingerprint(path)
        self.assertEqual(err.exception.code,'SAVE_VERIFY_FAILED')
        path.unlink()

    def test_cancel_before_readback_retains_originals_and_does_not_acknowledge(self):
        guard,job,state=self.guard_ready()
        cancel=threading.Event()
        with self.assertRaises(GuardError) as err:
            guard.save_folder(state['result']['ticket'],self.root,cancel=cancel,on_verifying=cancel.set)
        self.assertEqual(err.exception.code,'SAVE_CANCELLED')
        self.assertTrue(all(not a.saved and a.path.exists() for a in guard._jobs[job].artifacts))

    def test_default_and_explicit_false_cannot_bypass_independent_readback(self):
        for options in ({},{'verify':False}):
            with self.subTest(options=options):
                guard,job,state=self.guard_ready()
                saved=guard.save_folder(state['result']['ticket'],self.root,**options)
                self.assertTrue(saved['readbackVerified'])
                manifest=json.loads((self.root/saved['name']/'AIO-results-manifest.json').read_text())
                self.assertEqual(manifest['outputsVerified'],2)
                self.assertTrue(all(e['readbackVerified'] and e['readbackSha256']==e['sha256'] for e in manifest['outputs']))
                guard.close()

    def test_corruption_before_verify_has_failed_status_and_keeps_guard_outputs(self):
        guard,job,state=self.guard_ready()
        def corrupt():
            folder=next(self.root.glob('AIO_Results_*'))
            next(folder.rglob('*.pdf')).write_bytes(b'evil')
        with self.assertRaises(GuardError) as err:
            guard.save_folder(state['result']['ticket'],self.root,on_verifying=corrupt)
        self.assertEqual(err.exception.code,'SAVE_VERIFY_FAILED')
        folder=next(self.root.glob('AIO_Results_*'))
        status=json.loads((folder/'AIO-save-status.json').read_text())
        self.assertEqual((status['state'],status['code']),('failed','SAVE_VERIFY_FAILED'))
        manifest=json.loads((folder/'AIO-results-manifest.json').read_text())
        self.assertTrue(any(e.get('readbackVerified') is False for e in manifest['outputs']))
        self.assertTrue(all(not a.saved and a.path.exists() for a in guard._jobs[job].artifacts))

    def test_streaming_readback_memory_is_independent_of_file_size(self):
        path=self.root/'large.bin'
        digest=hashlib.sha256()
        with path.open('wb') as stream:
            block=b'x'*(64*1024)
            for _ in range(256):
                stream.write(block);digest.update(block)
        entry={'path':'large.bin','size':path.stat().st_size,'sha256':digest.hexdigest()}
        tracemalloc.start()
        try:
            result=verify_folder(self.root,[entry])
            _,peak=tracemalloc.get_traced_memory()
        finally:tracemalloc.stop()
        self.assertEqual(result['bytesVerified'],16*1024*1024)
        self.assertLess(peak,512*1024)

    def test_invalid_metadata_and_duplicate_paths_are_rejected(self):
        entry=self.entry(self.root/'one.bin',b'1')
        with self.assertRaises(GuardError):verify_folder(self.root,[entry,dict(entry)])
        for field,value in (('path','../outside'),('size',True),('sha256','bad')):
            with self.subTest(field=field),self.assertRaises(GuardError) as err:
                verify_folder(self.root,[dict(entry,**{field:value})])
            self.assertEqual(err.exception.code,'SAVE_VERIFY_FAILED')

    @unittest.skipUnless(shutil.which('node'),'Node is required for modal event regressions')
    def test_state_modal_uses_bounds_and_safe_close_events(self):
        run=subprocess.run(['node',str(Path(__file__).with_name('guard_state_modal_v16.mjs'))],
                           capture_output=True,text=True,timeout=10)
        self.assertEqual(run.returncode,0,run.stdout+'\n'+run.stderr)
