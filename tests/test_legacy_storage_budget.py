"""Legacy byte-budget failure paths; real owned process, controlled document bytes."""
import http.client
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app_backend.converter_transport import ConverterTransport
from app_backend.legacy_storage_budget import LegacyRunControl, measured_bytes
from app_backend.libreoffice_engine import LibreOfficeError
from app_backend.native_process import ProcessTree


def fixture(source, target, workspace, **kwargs):
    folder = workspace / 'output'
    folder.mkdir(parents=True)
    output = folder / ('result.' + target)
    output.write_bytes(b'%PDF-1.7\n' + b'x' * 1024)
    return [output]


class LegacyByteBudget(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name) / 'legacy'

    def transport(self, maximum=1024 * 1024):
        value = ConverterTransport(recovery_parent=self.parent, max_owned_bytes=maximum)
        self.addCleanup(value.close)
        return value

    def retain(self, value, identity='a' * 32):
        request = value.begin(identity, None)
        with patch('app_backend.converter_transport.convert_path', side_effect=fixture):
            output = value.execute(request, [{'path': Path('source.txt'), 'warnings': []}],
                                   ['f'], ['pdf'])
        value.finish(request, retained=True)
        return request, output['result']['ticket']

    def test_admission_counts_inputs_profiles_journals_and_unknown_roots(self):
        value = self.transport(200)
        for name in ('input', 'native/profile', 'unknown-unverified'):
            path = self.parent / name / 'bytes'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'x' * 60)
        value.check_storage(20)
        self.assertEqual(value.storage_stats()['usedBytes'], 180)
        with self.assertRaises(LibreOfficeError) as error:
            value.begin('a' * 32, None, expected_upload_bytes=21)
        self.assertEqual(error.exception.code, 'LEGACY_STORAGE_BYTES')
        self.assertFalse(value.active)
        self.assertEqual(measured_bytes([self.parent]), 180)

    def test_restored_result_above_quota_remains_downloadable_and_not_evicted(self):
        first = self.transport()
        request, ticket = self.retain(first)
        expected = first.results[ticket].path.read_bytes()
        first.close()
        second = self.transport(1)
        recovered = second.recover(request.identity)['result']
        with self.assertRaises(LibreOfficeError):
            second.begin('b' * 32, None)
        with second.lease(recovered['ticket']) as result:
            self.assertEqual(result.path.read_bytes(), expected)
        self.assertEqual(second.inventory()['storage']['availableBytes'], 0)
        second.close()
        self.assertTrue(request.root.exists())

    def test_explicit_cleanup_returns_capacity_only_after_physical_deletion(self):
        value = self.transport()
        request, ticket = self.retain(value)
        value.check_storage()
        value.max_owned_bytes = value.used_bytes
        with patch.object(request.temporary, 'cleanup', side_effect=PermissionError('locked')), \
                patch.object(value, '_schedule_cleanup'):
            self.assertFalse(value.release(ticket))
            with self.assertRaises(LibreOfficeError):
                value.begin('b' * 32, None, expected_upload_bytes=1)
            self.assertTrue(request.root.exists())
        value.retry_cleanup()
        next_request = value.begin('b' * 32, None, expected_upload_bytes=1)
        value.finish(next_request)
        self.assertFalse(request.root.exists())

    def test_busy_other_instance_and_unverified_crash_bytes_are_counted(self):
        first = self.transport()
        request, ticket = self.retain(first)
        second = self.transport(1)
        self.assertEqual(second.inventory()['recoveryWarnings'][0]['code'], 'LEGACY_RECOVERY_BUSY')
        with self.assertRaises(LibreOfficeError):
            second.begin('b' * 32, None)
        self.assertTrue(first.results[ticket].path.exists())
        self.assertGreater(second.used_bytes, 1000)
        first.close()
        (request.root / 'legacy-result.json').write_bytes(b'corrupt')
        second.close()
        third = self.transport(1)
        with self.assertRaises(LibreOfficeError):
            third.begin('c' * 32, None)
        self.assertGreater(third.used_bytes, 1000)

    def test_zip_duplication_is_rejected_before_creating_an_archive(self):
        value = self.transport(3000)
        request = value.begin('a' * 32, None)
        with patch('app_backend.converter_transport.convert_path', side_effect=fixture):
            with self.assertRaises(LibreOfficeError) as error:
                value.execute(request, [{'path': Path('source.txt'), 'warnings': []}], ['f'], ['pdf', 'txt'])
        self.assertEqual(error.exception.code, 'LEGACY_STORAGE_BYTES')
        self.assertFalse((request.root / 'Конвертированные_файлы.zip').exists())
        value.finish(request)
        self.assertEqual(len(list(request.root.glob('native/*/*/output/*'))), 2)
        self.assertEqual(value.inventory()['total'], 0)

    def test_live_growth_stops_own_real_process_and_next_native_target(self):
        from app_backend import libreoffice_engine as engine
        value = self.transport(100_000)
        request = value.begin('a' * 32, None)
        source = Path(self.temp.name) / 'source.txt'
        source.write_bytes(b'original source bytes')
        processes = []
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'])
        self.addCleanup(lambda: unrelated.poll() is None and unrelated.kill())
        def make_tree(command, **kwargs):
            output = Path(command[command.index('--outdir') + 1]) / 'growth.pdf'
            code = ('import sys,time;from pathlib import Path;'
                    'p=Path(sys.argv[1]);f=p.open("wb",buffering=0);'
                    '[(f.write(b"x"*65536),time.sleep(.02)) for _ in range(150)]')
            tree = ProcessTree([sys.executable, '-c', code, str(output)], **kwargs)
            processes.append(tree.process)
            return tree
        def convert(source, target, workspace, **kwargs):
            with patch.object(engine, 'ProcessTree', side_effect=make_tree):
                return engine.convert_path(source, target, workspace, executable=Path(sys.executable), **kwargs)
        with patch('app_backend.converter_transport.convert_path', side_effect=convert) as native:
            with self.assertRaises(LibreOfficeError) as error:
                value.execute(request, [{'path': source, 'warnings': []}], ['f'], ['pdf', 'txt'])
        self.assertEqual(error.exception.code, 'LEGACY_STORAGE_BYTES')
        self.assertEqual(native.call_count, 1)
        self.assertEqual(error.exception.batch_report['failed'], 2)
        self.assertIsNotNone(processes[0].poll())
        self.assertIsNone(unrelated.poll())
        unrelated.terminate()
        unrelated.wait(timeout=5)
        self.assertEqual(source.read_bytes(), b'original source bytes')
        value.finish(request)
        self.assertTrue((request.root / 'native/0/0/output/growth.pdf').exists())
        self.assertFalse(value.inventory()['total'])

    def test_io_failure_is_closed_without_holding_transport_lock(self):
        value = self.transport()
        entered, unblock = threading.Event(), threading.Event()
        errors = []
        def slow(_):
            entered.set()
            if not unblock.wait(5):
                raise AssertionError('test scan not released')
            raise PermissionError('cannot inspect')
        def check():
            try:
                value.check_storage()
            except LibreOfficeError as error:
                errors.append(error)
        with patch('app_backend.converter_transport.measured_bytes', side_effect=slow):
            thread = threading.Thread(target=check, daemon=True)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(value.inventory()['total'], 0)
            finally:
                unblock.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors[0].code, 'LEGACY_STORAGE_MEASUREMENT_FAILED')
        self.assertFalse(value.storage_stats()['storageMeasurementOk'])
        self.assertIsNone(value.storage_stats()['availableBytes'])

    def test_invalid_budget_and_additional_bytes_are_not_coerced(self):
        for maximum in (0, -1, True, 1.5, '100'):
            with self.subTest(maximum=maximum), self.assertRaises(ValueError):
                self.transport(maximum)
        value = self.transport()
        for additional in (-1, True, 1.5):
            with self.subTest(additional=additional), self.assertRaises(ValueError):
                value.check_storage(additional)

    def test_link_and_reparse_point_are_never_followed_or_charged_as_zero(self):
        for info in (SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=0),
                     SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)):
            with self.subTest(info=info), patch('app_backend.legacy_storage_budget.os.lstat', return_value=info), \
                    patch('os.scandir', side_effect=AssertionError('must not follow')):
                with self.assertRaises(OSError):
                    measured_bytes([self.parent])

    def test_child_entry_links_and_reparse_points_are_not_traversed(self):
        # Check the optimized DirEntry branch, not just the root lstat path.
        from unittest.mock import MagicMock
        self.parent.mkdir()
        for info in (SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=0),
                     SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)):
            entry = MagicMock()
            entry.path = str(self.parent / 'untrusted-child')
            entry.stat.return_value = info
            listing = MagicMock()
            listing.__enter__.return_value = iter([entry])
            with self.subTest(info=info), patch('app_backend.legacy_storage_budget.os.scandir',
                                               return_value=listing) as scan:
                with self.assertRaises(OSError):
                    measured_bytes([self.parent])
                scan.assert_called_once_with(str(self.parent))
                entry.stat.assert_called_once_with(follow_symlinks=False)

    def test_inspection_entry_limit_fails_instead_of_under_counting(self):
        self.parent.mkdir()
        for number in range(4):
            (self.parent / str(number)).write_bytes(b'x')
        with patch('app_backend.legacy_storage_budget.MAX_SCAN_ENTRIES', 3):
            with self.assertRaises(OSError):
                measured_bytes([self.parent])

    def test_monitor_throttles_scans_and_prioritizes_user_cancellation(self):
        cancel = threading.Event()
        with patch('app_backend.legacy_storage_budget.time.monotonic', return_value=10):
            calls = []
            control = LegacyRunControl(cancel, lambda: calls.append(1))
            for _ in range(20):
                self.assertFalse(control.is_set())
            self.assertEqual(calls, [1])
            cancel.set()
            self.assertTrue(control.wait(.1))
            self.assertEqual(calls, [1])


class LegacyByteHttp(unittest.TestCase):
    def test_oversized_advertised_upload_gets_507_before_body_read_and_releases_gate(self):
        from app_backend.server import start_server
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {'AIO_LEGACY_STORAGE_BYTES': '100'}):
            server, token = start_server(guard_recovery_parent=Path(directory) / 'guard')
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
            thread.start()
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            try:
                with patch('app_backend.server.MultipartUpload', side_effect=AssertionError('body read')):
                    connection.putrequest('POST', '/api/converter/convert?request=' + 'a' * 32)
                    connection.putheader('X-AIO-Token', token)
                    connection.putheader('Content-Length', '101')
                    connection.endheaders()
                    response = connection.getresponse()
                    self.assertEqual(response.status, 507)
                    self.assertIn(b'LEGACY_STORAGE_BYTES', response.read())
                    self.assertEqual(response.getheader('Connection'), 'close')
                self.assertTrue(server.converter_gate.acquire(blocking=False))
                server.converter_gate.release()
                self.assertFalse(server.converter.active)
                self.assertEqual(server.converter.max_owned_bytes, 100)
            finally:
                connection.close()
                server.shutdown()
                server.server_close()
                thread.join(5)

    def test_bad_production_budget_fails_before_binding_server(self):
        from app_backend.server import LocalServer
        from http.server import BaseHTTPRequestHandler
        for value in ('0', '-1', '1.5', 'true', '9' * 19):
            with self.subTest(value=value), patch.dict(os.environ, {'AIO_LEGACY_STORAGE_BYTES': value}), \
                    patch('app_backend.server.ThreadingHTTPServer.__init__', side_effect=AssertionError('bound')):
                with self.assertRaises(ValueError):
                    LocalServer(('127.0.0.1', 0), BaseHTTPRequestHandler)
