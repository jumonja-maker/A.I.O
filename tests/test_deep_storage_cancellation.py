"""Cancellation interrupts quota traversal without accepting partial totals."""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from app_backend import legacy_storage_budget as budget
from app_backend.converter_transport import ConverterTransport
from app_backend.libreoffice_engine import LibreOfficeError


class ScanCancellation(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name) / 'legacy'
        self.parent.mkdir()
        self.cancel = threading.Event()
        self.stats = []

    def listing_that_cancels_on_first_stat(self):
        original = budget.os.scandir
        cancel, stats = self.cancel, self.stats
        class Entry:
            def __init__(self, item):
                self.item, self.path = item, item.path
            def stat(self, **kwargs):
                stats.append(self.path)
                cancel.set()
                return self.item.stat(**kwargs)
        class Listing:
            def __init__(self, path):
                self.inner = original(path)
            def __enter__(self):
                return (Entry(item) for item in self.inner)
            def __exit__(self, *args):
                self.inner.close()
        return Listing

    def test_monitor_rechecks_cancel_after_a_storage_callback(self):
        calls = []
        def check():
            calls.append(True)
            self.cancel.set()
        control = budget.LegacyRunControl(self.cancel, check)
        self.assertTrue(control.is_set())
        self.assertEqual(calls, [True])

    def test_cancelled_measurement_does_not_even_open_a_directory(self):
        self.cancel.set()
        with patch.object(budget.os, 'scandir', side_effect=AssertionError('cancel before I/O')):
            with self.assertRaises(LibreOfficeError) as caught:
                budget.measured_bytes([self.parent], cancel=self.cancel)
        self.assertEqual(caught.exception.code, 'CANCELLED')

    def test_cancellation_during_scanning_stops_before_all_entries_are_stat_ed(self):
        for index in range(30):
            (self.parent / (str(index) + '.bin')).write_bytes(b'x')
        with patch.object(budget.os, 'scandir', self.listing_that_cancels_on_first_stat()):
            with self.assertRaises(LibreOfficeError) as caught:
                budget.measured_bytes([self.parent], cancel=self.cancel)
        self.assertEqual(caught.exception.code, 'CANCELLED')
        self.assertLessEqual(len(self.stats), 2)

    def test_incomplete_scan_never_replaces_a_last_complete_byte_count(self):
        payload = self.parent / 'payload.bin'
        payload.write_bytes(b'first')
        transport = ConverterTransport(recovery_parent=self.parent, max_owned_bytes=1000)
        self.addCleanup(transport.close)
        transport.check_storage()
        self.assertEqual(transport.used_bytes, 5)
        payload.write_bytes(b'expanded')
        with patch.object(budget.os, 'scandir', self.listing_that_cancels_on_first_stat()):
            with self.assertRaises(LibreOfficeError) as caught:
                transport.check_storage(cancel=self.cancel)
        self.assertEqual(caught.exception.code, 'CANCELLED')
        self.assertEqual(transport.used_bytes, 5)
        self.assertFalse(transport.storage_measurement_ok)
        self.assertIsNone(transport.storage_stats()['availableBytes'])
        transport.check_storage()
        self.assertEqual(transport.used_bytes, 8)
        self.assertTrue(transport.storage_measurement_ok)

    def test_native_request_control_passes_cancellation_into_the_scan(self):
        transport = ConverterTransport(recovery_parent=self.parent, max_owned_bytes=100000)
        self.addCleanup(transport.close)
        request = transport.begin('a' * 32, None)
        self.cancel = request.cancel
        with patch.object(budget.os, 'scandir', self.listing_that_cancels_on_first_stat()):
            with self.assertRaises(LibreOfficeError) as caught:
                transport.run_control(request).is_set()
        self.assertEqual(caught.exception.code, 'CANCELLED')
        self.assertFalse(transport.storage_measurement_ok)
        transport.finish(request)


if __name__ == '__main__':
    unittest.main()
