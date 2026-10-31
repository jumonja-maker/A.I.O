"""Real disk/restart/crash/ZIP boundaries; synthetic conversion, no installer claim."""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

from app_backend.converter_guard import ConverterGuard, GuardError
from app_backend.converter_guard.result_store import ResultStore
from test_converter_guard_v02 import FakeAdapter, wait_finished


class RecoverySaveRegression(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()

    def guard(self):
        guard = ConverterGuard(adapter=FakeAdapter(), recovery_parent=self.root/'owned')
        self.addCleanup(guard.close)
        return guard

    def ready(self, formats=('pdf', 'html', 'bad')):
        guard = self.guard()
        identity = guard.create([{'id':'f','name':'source.docx','size':4}], list(formats))['jobId']
        guard.upload(identity, 'f', io.BytesIO(b'keep'), 4)
        guard.start(identity, archive_mode='on_demand'); wait_finished(guard, identity)
        return guard, guard._jobs[identity]

    def scan(self, guard):
        guard.start_recovery_scan()
        guard._recovery_executor.submit(lambda:None).result(timeout=5)
        return guard.recoverable(scan=False)

    def bundle(self, guard, job):
        guard.rebuild_bundle(job.id); wait_finished(guard, job.id)
        self.assertIsNotNone(job.bundle)
        path = self.root/'external.zip'
        with guard.open_result(job.bundle.ticket) as (source, _, _), path.open('wb') as target:
            shutil.copyfileobj(source, target)
        return path

    def test_busy_root_is_visible_retryable_and_does_not_hide_other_results(self):
        first, a = self.ready(); first.close()
        second, b = self.ready(); second.close()
        owner = ResultStore(a.root); self.assertTrue(owner.acquire()); self.addCleanup(owner.close)
        consumer = self.guard()
        found = self.scan(consumer)
        self.assertEqual([j['jobId'] for j in found['jobs']], [b.id])
        self.assertEqual(found['recoveryScan']['state'], 'retry_available')
        self.assertEqual(found['recoveryScan']['retryFolders'], 1)
        self.assertIn('RECOVERY_BUSY', [w['code'] for w in found['warnings']])
        with patch.object(ResultStore, 'acquire', side_effect=AssertionError('snapshot scanned disk')):
            consumer.recoverable(scan=False)
        owner.close()
        found = self.scan(consumer)
        self.assertEqual({j['jobId'] for j in found['jobs']}, {a.id, b.id})
        self.assertEqual(found['recoveryScan']['state'], 'completed')
        self.assertFalse(any(w.get('retryable') for w in found['warnings']))

    def test_permission_failure_is_not_corrupt_metadata_and_retry_keeps_identity(self):
        producer, a = self.ready(); producer.close()
        producer, b = self.ready(); producer.close()
        consumer = self.guard(); original = ResultStore.read
        def blocked(store, **kwargs):
            if store.root == a.root: raise PermissionError(13, 'private path')
            return original(store, **kwargs)
        with patch.object(ResultStore, 'read', blocked):
            found = self.scan(consumer)
        self.assertEqual([j['jobId'] for j in found['jobs']], [b.id])
        self.assertEqual([w['code'] for w in found['warnings']], ['RECOVERY_ACCESS_DENIED'])
        self.assertNotIn('private path', repr(found))
        self.assertTrue(all(x.path.exists() for x in a.artifacts))
        self.assertEqual({j['jobId'] for j in self.scan(consumer)['jobs']}, {a.id, b.id})

    def test_salvage_is_not_conversion_success_and_accounts_all_owned_bytes(self):
        producer, old = self.ready(); producer.close()
        (old.root/'job.json').write_text('invalid JSON')
        (old.root/'leftover-input.bin').write_bytes(b'x'*8192)
        consumer = self.guard(); self.scan(consumer)
        job = next(iter(consumer._jobs.values()))
        self.assertTrue(job.salvage)
        state = consumer.status(job.id)
        self.assertEqual(state['succeeded'], 0)
        self.assertGreater(state['failed'], 0)
        self.assertEqual(job.reserved, consumer._recovery_bytes(job.root))
        result = consumer.save_folder(job.artifacts[0].ticket, self.root)
        self.assertTrue(result['saved'])
        self.assertFalse(result['savedAll'])
        self.assertTrue(job.root.exists())
        self.assertEqual(consumer.status(job.id)['succeeded'], 0)

    def test_legacy_unverified_save_record_cannot_reap_the_only_result_copy(self):
        producer, old = self.ready(('pdf',))
        old.store.saved(old.artifacts, readback_verified=False)
        producer.close()
        consumer = self.guard(); found = self.scan(consumer)
        self.assertIn('RECOVERY_SAVE_UNVERIFIED', [w['code'] for w in found['warnings']])
        self.assertEqual(consumer.status(old.id)['files'][0]['targets'][0]['state'], 'ready')
        for _ in range(3): consumer.recoverable(scan=False)
        self.assertTrue(old.artifacts[0].path.exists())
        self.assertTrue(consumer.save_folder(consumer._jobs[old.id].artifacts[0].ticket,self.root)['savedAll'])

    def test_abrupt_process_exit_recovers_partial_success_without_auto_scan(self):
        # os._exit bypasses close/finally: this is an actual process crash
        # simulation on each hosted OS, not a fabricated in-memory restart.
        script = '''import io,os,sys
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'tests'))
from test_converter_guard_v02 import FakeAdapter,wait_finished
from app_backend.converter_guard import ConverterGuard
g=ConverterGuard(adapter=FakeAdapter(),recovery_parent=sys.argv[1])
j=g.create([{'id':'f','name':'source.docx','size':4}],['pdf','html','bad'])['jobId']
g.upload(j,'f',io.BytesIO(b'keep'),4);g.start(j,archive_mode='on_demand')
wait_finished(g,j)
print(j,flush=True)
os._exit(23)
'''
        child = subprocess.run([sys.executable, '-c', script, str(self.root/'owned')],
                               capture_output=True, text=True, timeout=20)
        self.assertEqual(child.returncode, 23, child.stderr)
        identity = child.stdout.strip()
        consumer = self.guard()
        self.assertEqual(consumer.recoverable(scan=False)['jobs'], [])
        self.assertIn(identity, [j['jobId'] for j in self.scan(consumer)['jobs']])
        state = consumer.status(identity)
        self.assertEqual((state['succeeded'], state['failed']), (2, 1))
        result = consumer.save_folder(state['result']['ticket'], self.root)
        self.assertTrue(result['readbackVerified'])
        self.assertEqual(result['outputs'], 3)  # PDF + HTML + image resource.
        report = json.loads((self.root/result['name']/'AIO-conversion-report.json').read_text())
        self.assertEqual(report['files'][0]['targets'][-1]['state'], 'error')

    def test_spool_archive_or_download_alone_cannot_acknowledge_save(self):
        guard, job = self.ready(); path = self.bundle(guard, job)
        with self.assertRaises(GuardError) as caught:
            guard.confirm_saved(job.id, job.bundle.ticket)
        self.assertEqual(caught.exception.code, 'SAVE_VERIFY_MISSING')
        self.assertTrue(path.exists())
        self.assertTrue(all(not a.saved and a.path.exists() for a in job.artifacts))

    def test_explicit_zip_for_one_successful_output_is_an_archive(self):
        guard, job = self.ready(('pdf',))
        self.assertEqual(job.result['name'], 'source.pdf')
        path = self.bundle(guard, job)
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(len(archive.namelist()), 4)  # One output and three metadata.
        receipt = guard.verify_saved_download(job.bundle.ticket, path)
        self.assertEqual(receipt['outputsVerified'], 1)
        self.assertTrue(guard.confirm_saved(job.id, job.bundle.ticket, compact=True)['savedAll'])

    def test_external_zip_readback_receipt_binds_inventory_metadata_and_operation(self):
        guard, job = self.ready(); path = self.bundle(guard, job)
        from app_backend.converter_guard.archive_verification import verify_built_archive
        with patch('app_backend.converter_guard.archive_verification.verify_built_archive', wraps=verify_built_archive) as check:
            receipt = guard.verify_saved_download(job.bundle.ticket, path)
        self.assertEqual(Path(check.call_args.args[0]), path)
        self.assertEqual(receipt['verificationScope'], 'final_destination_zip_members')
        self.assertEqual(receipt['metadataVerified'], 3)
        self.assertEqual(receipt['outputsVerified'], 3)
        self.assertEqual(receipt['destinationSha256'], hashlib.sha256(path.read_bytes()).hexdigest())
        disk = json.loads(path.with_name(path.name+'.AIO-save-status.json').read_text())
        self.assertEqual(disk, receipt)
        with zipfile.ZipFile(path) as archive:
            self.assertIsNone(archive.testzip())
            report = json.loads(archive.read('AIO-conversion-report.json'))
            manifest = json.loads(archive.read('AIO-results-manifest.json'))
            status = json.loads(archive.read('AIO-save-status.json'))
        self.assertEqual(report['archiveSnapshot'], manifest['archiveSnapshot'])
        self.assertEqual(receipt['archiveSnapshot'], report['archiveSnapshot'])
        self.assertEqual(status['snapshotId'], receipt['snapshotId'])
        self.assertEqual(status['state'], 'packaged')  # Never an external-save claim.
        self.assertEqual(report['files'][0]['targets'][-1]['state'], 'error')
        with self.assertRaises(GuardError):
            guard.confirm_saved(job.id, job.bundle.ticket, save_id='stale-save')
        outcome = guard.confirm_saved(job.id, job.bundle.ticket, save_id=receipt['saveId'], compact=True)
        self.assertTrue(outcome['savedAll'])
        self.assertEqual(guard.status(job.id)['failed'], 1)

    def test_failed_or_cancelled_external_read_never_reuses_an_earlier_receipt(self):
        guard, job = self.ready(); path = self.bundle(guard, job)
        first_receipt = guard.verify_saved_download(job.bundle.ticket, path)
        first_path = path.parent / first_receipt['receiptFile']
        first_bytes = first_path.read_bytes()
        path.write_bytes(b'broken zip')
        with self.assertRaises(GuardError): guard.verify_saved_download(job.bundle.ticket, path)
        self.assertIsNone(job.bundle.save_receipt)
        with self.assertRaises(GuardError): guard.confirm_saved(job.id, job.bundle.ticket)
        self.assertEqual(first_path.read_bytes(), first_bytes)
        attempts = [json.loads(p.read_text()) for p in path.parent.glob(path.name+'.AIO-save-status-*.json')]
        status = next(item for item in attempts if item['state'] == 'failed')
        self.assertNotEqual(status['saveId'], first_receipt['saveId'])
        self.assertEqual(status['state'], 'failed')
        self.assertFalse(status['readbackVerified'])
        path.write_bytes(job.bundle.path.read_bytes())
        cancel = threading.Event(); cancel.set()
        with self.assertRaises(GuardError) as caught:
            guard.verify_saved_download(job.bundle.ticket, path, cancel=cancel)
        self.assertEqual(caught.exception.code, 'SAVE_CANCELLED')
        self.assertTrue(all(not a.saved and a.path.exists() for a in job.artifacts))

    def test_receipt_disk_full_or_windows_style_lock_keeps_originals(self):
        for error in (OSError(28, 'disk full'), PermissionError(13, 'locked')):
            with self.subTest(error=type(error).__name__):
                guard, job = self.ready(); path = self.bundle(guard, job)
                with patch('app_backend.converter_guard.result_store.atomic_json', side_effect=error):
                    with self.assertRaises(GuardError) as caught:
                        guard.verify_saved_download(job.bundle.ticket, path)
                self.assertEqual(caught.exception.code, 'SAVE_STORAGE_FAILED')
                self.assertIsNone(job.bundle.save_receipt)
                self.assertFalse(job.saving)
                self.assertTrue(all(not a.saved and a.path.exists() for a in job.artifacts))
                guard.close()

    @unittest.skipUnless(os.name == 'nt', 'Windows exclusive file sharing contract')
    def test_real_windows_exclusive_destination_lock_blocks_acknowledgement(self):
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        guard, job = self.ready(); path = self.bundle(guard, job)
        handle = kernel.CreateFileW(str(path), 0x80000000, 0, None, 3, 0, None)
        self.assertNotEqual(handle, wintypes.HANDLE(-1).value, ctypes.get_last_error())
        try:
            with self.assertRaises(GuardError) as caught:
                guard.verify_saved_download(job.bundle.ticket, path)
            self.assertEqual(caught.exception.code, 'SAVE_VERIFY_FAILED')
            with self.assertRaises(GuardError): guard.confirm_saved(job.id, job.bundle.ticket)
            self.assertTrue(all(not a.saved and a.path.exists() for a in job.artifacts))
        finally:
            self.assertTrue(kernel.CloseHandle(handle))
        self.assertTrue(guard.verify_saved_download(job.bundle.ticket, path)['readbackVerified'])


if __name__ == '__main__': unittest.main()
