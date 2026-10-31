"""Owner contract: M04 is metadata-only, A06 is manually accessible and never counted as Guard current work."""
from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest

from app_backend.server import start_server


class ReportHistoryAndManualLegacy(unittest.TestCase):
    @contextmanager
    def _start(self, root):
        server, _token = start_server(
            guard_recovery_parent=root / 'guard-jobs',
            guard_history_parent=root / 'report-history')
        try:
            yield server
        finally:
            server.server_close()

    def test_desktop_legacy_storage_is_durable_but_lazy_and_history_is_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self._start(root) as server:
                self.assertEqual(server.converter.recovery_parent, root / 'legacy-jobs')
                self.assertFalse(server.converter._recovery_loaded)
                self.assertFalse((root / 'legacy-jobs').exists())
                self.assertIsNotNone(server.guard._history)
                self.assertEqual(server.guard.history_list()['items'], [])
                self.assertEqual(server.guard.recoverable(scan=False)['jobs'], [])

    def test_old_unverified_files_do_not_appear_as_success_or_guard_unsaved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / 'legacy-jobs' / ('aio-legacy-' + 'f' * 32)
            old.mkdir(parents=True)
            marker = old / 'old-unsaved-result.pdf'
            marker.write_bytes(b'private-old-bytes')
            with self._start(root) as server:
                self.assertFalse(server.converter._recovery_loaded)
                self.assertEqual(server.guard.recoverable(scan=False)['jobs'], [])
                inventory = server.converter.inventory()
                self.assertEqual(inventory['total'], 0)
                self.assertEqual(inventory['recoveryWarnings'][0]['code'],
                                 'LEGACY_RECOVERY_UNVERIFIED')
                self.assertEqual(server.guard.recoverable(scan=False)['jobs'], [])
                self.assertEqual(marker.read_bytes(), b'private-old-bytes')
            self.assertEqual(marker.read_bytes(), b'private-old-bytes')

    def test_ui_has_both_manual_legacy_recovery_and_report_only_history(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / 'index.html').read_text(encoding='utf-8')
        queue = (root / 'src/guard-queue.js').read_text(encoding='utf-8')
        history = (root / 'app_backend/converter_guard/conversion_history.py').read_text(encoding='utf-8')
        self.assertIn('src/legacy-recovery.js', html)
        self.assertIn('AIOLegacyRecovery?.mount', queue)
        self.assertIn('src/conversion-history.js', html)
        self.assertIn('AIOConversionHistory?.mount', queue)
        self.assertIn('jobs.filter(job=>!job.recovered)', queue)
        self.assertIn("'containsResultFiles': False", history)


if __name__ == '__main__':
    unittest.main()
