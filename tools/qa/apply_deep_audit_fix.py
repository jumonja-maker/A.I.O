"""Guarded one-shot completion of explicit UI availability reconciliation."""
import ast
import os
from pathlib import Path
import subprocess

if os.environ.get('GITHUB_REF') != 'refs/heads/fix/deep-audit-d01-d09-20261031':
    raise RuntimeError('Isolated audit branch required')
expected = {
    'app_backend/converter_guard/core.py': '4102e7b83c71311fef83ab57941fdf84d6d5d003',
    'tests/test_audit_101_fixes.py': 'ac388a8d54c548c8491748e9af2eb868080b652b',
}
texts = {}
for path, wanted in expected.items():
    actual = subprocess.check_output(['git', 'hash-object', path], text=True).strip()
    if actual != wanted:
        raise RuntimeError('Concurrent source change: ' + path)
    texts[path] = Path(path).read_text(encoding='utf-8')

def replace(path, before, after):
    if texts[path].count(before) != 1:
        raise RuntimeError('Source anchor mismatch: ' + path)
    texts[path] = texts[path].replace(before, after)

path = 'app_backend/converter_guard/core.py'
replace(path, "        if recover and since_revision is None:\n            self._reconcile_results(job_id)",
    "        # lookup=current suppresses old-job discovery, not verification of\n        # a known job on an explicit full refresh. ETag/delta polling stays cheap.\n        if since_revision is None:\n            self._reconcile_results(job_id)")
replace(path, """            if self._recovery and self._recovery_scan_state != 'scanning':
                if not self._recovery_loaded or self._recovery_pending:
                    self._recovery_scan_state = 'scanning'
                    self._recovery_scan_error = None
                    self._recovery_scan_id += 1
                    # Claim and submit under one lock: shutdown cannot overtake
                    # admission, and repeated clicks cannot queue extra scans.
                    try:
                        self._recovery_future = self._recovery_executor.submit(self._run_recovery_scan)
                    except RuntimeError as error:
                        self._recovery_scan_state = 'failed'
                        self._recovery_scan_error = 'RECOVERY_SCAN_FAILED'
                        raise GuardError('RECOVERY_SCAN_FAILED', 'Не удалось начать поиск результатов.', status=503) from error
""", """            if (self._recovery or self._jobs) and self._recovery_scan_state != 'scanning':
                # Every explicit request also reconciles known output files;
                # loaded old-job discovery alone cannot refresh their availability.
                self._recovery_scan_state = 'scanning'
                self._recovery_scan_error = None
                self._recovery_scan_id += 1
                # Claim and submit under one lock: duplicate clicks while the
                # worker runs still share one request and never perform inline I/O.
                try:
                    self._recovery_future = self._recovery_executor.submit(self._run_recovery_scan)
                except RuntimeError as error:
                    self._recovery_scan_state = 'failed'
                    self._recovery_scan_error = 'RECOVERY_SCAN_FAILED'
                    raise GuardError('RECOVERY_SCAN_FAILED', 'Не удалось начать поиск результатов.', status=503) from error
""")
replace(path, """    def _run_recovery_scan(self):
        failed = False
        try:
            self._load_recovery()
""", """    def _run_recovery_scan(self):
        failed = False
        try:
            self.recoverable(scan=True)
""")

path = 'tests/test_audit_101_fixes.py'
addition = '''class ExplicitAvailabilityIntegration(Disposable):
    def test_current_lookup_refresh_reconciles_without_loading_old_jobs(self):
        guard, job = self.ready(('pdf',))
        job.artifacts[0].path.unlink()
        with patch.object(guard, '_load_recovery', side_effect=AssertionError('Old jobs must not auto-load')):
            state = guard.status(job.id, recover=False)
        self.assertEqual(state['state'], 'failed')
        self.assertEqual(state['conversionState'], 'ready')
        self.assertEqual(state['unavailableOutputs'], 1)
        self.assertEqual(state['unsavedDocuments'], 0)
        self.assertIsNone(state['result'])

    def test_explicit_manual_scan_reconciles_even_when_history_already_loaded(self):
        guard, job = self.ready(('pdf',))
        guard._recovery_loaded = True
        job.artifacts[0].path.unlink()
        with patch.object(guard, '_recover_results', side_effect=AssertionError('Already loaded, no old-root rescan')):
            guard.start_recovery_scan()
            guard._recovery_future.result(timeout=5)
        snapshot = guard.recoverable(scan=False)
        self.assertEqual(snapshot['recoveryScan']['state'], 'completed')
        row = next(item for item in snapshot['jobs'] if item['jobId'] == job.id)
        self.assertEqual(row['unsavedOutputs'], 0)
        self.assertEqual(row['unavailableOutputs'], 1)
        self.assertEqual(row['state'], 'failed')

    def test_current_lookup_delta_poll_never_reads_file_metadata(self):
        guard, job = self.ready(('pdf',))
        revision = job.revision
        job.artifacts[0].path.unlink()
        with patch('app_backend.file_identity.file_identity', side_effect=AssertionError('No disk poll')):
            state = guard.status(job.id, recover=False, since_revision=revision, delta=True)
        self.assertTrue(state['unchanged'])
        # The next explicit refresh, not the poll, reconciles the missing output.
        self.assertEqual(guard.status(job.id, recover=False)['unavailableOutputs'], 1)


'''
replace(path, "if __name__ == '__main__': unittest.main()", addition + "if __name__ == '__main__': unittest.main()")
for path, text in texts.items():
    ast.parse(text, filename=path)
for path, text in texts.items():
    Path(path).write_text(text, encoding='utf-8', newline='\n')
    print('AUDIT101_EXPLICIT_UI_UPDATED=' + path)
print('OLD_JOB_AUTOLOAD_UNCHANGED=1;DELTA_POLL_DISK_IO=0;NEW_REGRESSIONS=3')
