"""Ordinary A.I.O keeps report-only history, not old legacy result files.

Legacy persistence is explicit opt-in for isolated compatibility/migration.
Historical user files are never deleted by this change.
"""
from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest

from app_backend.server import start_server


class HistoryOnlyProductDefault(unittest.TestCase):
    @contextmanager
    def _start(self, root):
        server, _token = start_server(
            guard_recovery_parent=root / 'guard-jobs',
            guard_history_parent=root / 'report-history')
        try:
            yield server
        finally:
            server.converter.close()
            server.guard.close()
            server.server_close()

    def test_normal_desktop_does_not_create_or_scan_legacy_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self._start(root) as server:
                self.assertIsNone(server.converter.recovery_parent)
                self.assertEqual(server.converter.inventory()['scope'], 'current_process')
                self.assertEqual(server.converter.inventory()['items'], [])
                self.assertFalse((root / 'legacy-jobs').exists())
                self.assertIsNotNone(server.guard._history)
                self.assertEqual(server.guard.history_list()['items'], [])

    def test_old_legacy_files_survive_but_are_not_resurrected_or_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / 'legacy-jobs' / ('aio-legacy-' + 'f' * 32)
            legacy.mkdir(parents=True)
            marker = legacy / 'old-unsaved-result.pdf'
            marker.write_bytes(b'private-old-bytes')
            with self._start(root) as server:
                self.assertIsNone(server.converter.recovery_parent)
                self.assertEqual(server.converter.inventory()['total'], 0)
                self.assertEqual(server.converter.storage_stats()['retainedResults'], 0)
                self.assertEqual(marker.read_bytes(), b'private-old-bytes')
            self.assertEqual(marker.read_bytes(), b'private-old-bytes')

    def test_production_ui_exposes_only_report_history_not_legacy_results(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / 'index.html').read_text(encoding='utf-8')
        queue = (root / 'src/guard-queue.js').read_text(encoding='utf-8')
        self.assertIn('src/conversion-history.js', html)
        self.assertIn('AIOConversionHistory?.mount', queue)
        self.assertNotIn('src/legacy-recovery.js', html)
        self.assertNotIn('AIOLegacyRecovery', queue)
        # Guard M02 is a separate, explicitly manual operation and its
        # recovered rows never enter the displayed live unsaved count.
        self.assertIn("jobs.filter(job=>!job.recovered)", queue)


if __name__ == '__main__':
    unittest.main()
