"""Legacy ZIP must match retained native exports before publishing/recovery."""
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from app_backend import converter_transport as transport_module
from app_backend.converter_guard import archive_verification
from app_backend.converter_transport import ConverterTransport
from app_backend.libreoffice_engine import LibreOfficeError


def fixture(source, target, workspace, **kwargs):
    if target == 'bad':
        raise LibreOfficeError('Unsupported fixture', 'UNSUPPORTED_FORMAT')
    folder = workspace / 'output'
    folder.mkdir(parents=True)
    output = folder / ('result.' + target)
    output.write_bytes(b'ORIGINAL_NATIVE_BYTES_' + target.encode())
    return [output]


class LegacyArchiveReadback(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name) / 'legacy'
        self.transport = ConverterTransport(recovery_parent=self.parent)
        self.addCleanup(self.transport.close)
        self.request = self.transport.begin('a' * 32, None)

    def execute(self, formats=('pdf', 'txt')):
        with patch('app_backend.converter_transport.convert_path', side_effect=fixture):
            return self.transport.execute(self.request,
                [{'path': Path('unused-source.txt'), 'warnings': []}], ['file'], list(formats))

    def assert_retained_unpublished(self):
        self.assertFalse(self.transport.results)
        self.assertFalse(self.request.published)
        self.assertFalse((self.request.root / 'legacy-result.json').exists())
        self.transport.finish(self.request)
        self.assertTrue(self.request.root.exists())
        self.assertEqual((self.request.root / 'native/0/0/output/result.pdf').read_bytes(),
                         b'ORIGINAL_NATIVE_BYTES_pdf')
        self.transport.close()
        restarted = ConverterTransport(recovery_parent=self.parent)
        self.addCleanup(restarted.close)
        self.assertFalse(restarted.results)

    def test_valid_crc_changed_payload_is_rejected_before_ticket_or_journal(self):
        original_open = zipfile.ZipFile.open

        @contextmanager
        def change(archive, name, mode='r', pwd=None, *, force_zip64=False):
            with original_open(archive, name, mode, pwd, force_zip64=force_zip64) as stream:
                if mode == 'w' and isinstance(name, zipfile.ZipInfo):
                    write = stream.write
                    stream.write = lambda data: write(bytes([data[0] ^ 1]) + data[1:] if data else data)
                yield stream

        with patch.object(zipfile.ZipFile, 'open', change):
            with self.assertRaises(LibreOfficeError) as caught:
                self.execute()
        self.assertEqual(caught.exception.code, 'LEGACY_BUNDLE_VERIFY_FAILED')
        with zipfile.ZipFile(self.request.root / 'Конвертированные_файлы.zip') as archive:
            self.assertIsNone(archive.testzip())
        self.assert_retained_unpublished()

    def test_exact_zip_including_partial_report_is_published_and_recoverable(self):
        result = self.execute(('pdf', 'bad'))
        self.transport.finish(self.request, retained=True)
        path = self.transport.results[result['result']['ticket']].path
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(set(archive.namelist()),
                {'001_pdf/result.pdf', 'AIO-conversion-report.json'})
            self.assertEqual(archive.read('001_pdf/result.pdf'), b'ORIGINAL_NATIVE_BYTES_pdf')
            self.assertEqual(json.loads(archive.read('AIO-conversion-report.json'))['failed'], 1)
        self.transport.close()
        restarted = ConverterTransport(recovery_parent=self.parent)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.recover(self.request.identity)['state'], 'ready')

    def test_changed_partial_report_is_rejected(self):
        original_write = zipfile.ZipFile.writestr

        def change(archive, name, data, *args, **kwargs):
            if name == 'AIO-conversion-report.json':
                data = b'{}'
            return original_write(archive, name, data, *args, **kwargs)

        with patch.object(zipfile.ZipFile, 'writestr', change):
            with self.assertRaises(LibreOfficeError) as caught:
                self.execute(('pdf', 'bad'))
        self.assertEqual(caught.exception.code, 'LEGACY_BUNDLE_VERIFY_FAILED')
        self.assert_retained_unpublished()

    def test_cancel_during_readback_keeps_outputs_without_ticket(self):
        verify = transport_module.verify_archive_members

        def cancel_then_read(*args, **kwargs):
            self.assertTrue(self.request.packaging)
            self.assertTrue(self.transport.cancel(self.request.identity))
            return verify(*args, **kwargs)

        with patch.object(transport_module, 'verify_archive_members', cancel_then_read):
            with self.assertRaises(LibreOfficeError) as caught:
                self.execute()
        self.assertEqual(caught.exception.code, 'CANCELLED')
        self.assertFalse(self.request.packaging)
        self.assert_retained_unpublished()

    def test_readback_storage_error_preserves_code_and_outputs(self):
        verify = transport_module.verify_archive_members

        def quota_then_read(*args, **kwargs):
            # Force the next cooperative readback scan to observe a real
            # exhausted budget, rather than mocking the verifier's error.
            self.transport.max_owned_bytes = 1
            return verify(*args, **kwargs)

        with patch.object(transport_module, 'verify_archive_members', quota_then_read):
            with self.assertRaises(LibreOfficeError) as caught:
                self.execute()
        self.assertEqual(caught.exception.code, 'LEGACY_STORAGE_BYTES')
        self.assertFalse(self.request.packaging)
        self.assert_retained_unpublished()

    def test_quota_after_first_member_read_keeps_storage_failure_code(self):
        verify = transport_module.verify_archive_members
        read = zipfile.ZipExtFile.read
        observed = {'reads': 0, 'polls': 0}

        def exhaust_after_read(member, *args, **kwargs):
            block = read(member, *args, **kwargs)
            if block:
                observed['reads'] += 1
                self.transport.max_owned_bytes = 1
            return block

        def verify_with_scans(*args, **kwargs):
            control = kwargs['cancel']
            check = control.check

            def count_and_check():
                observed['polls'] += 1
                return check()

            with (patch.object(control, 'check', count_and_check),
                  patch('app_backend.legacy_storage_budget.time.monotonic', side_effect=range(1000)),
                  patch.object(archive_verification, 'CHUNK', 8),
                  patch.object(zipfile.ZipExtFile, 'read', exhaust_after_read)):
                return verify(*args, **kwargs)

        with patch.object(transport_module, 'verify_archive_members', verify_with_scans):
            with self.assertRaises(LibreOfficeError) as caught:
                self.execute()
        self.assertGreaterEqual(observed['polls'], 4)
        self.assertGreaterEqual(observed['reads'], 1)
        self.assertEqual(caught.exception.code, 'LEGACY_STORAGE_BYTES')
        self.assertFalse(self.request.packaging)
        self.assert_retained_unpublished()

    def test_cancel_after_first_member_read_keeps_cancel_code(self):
        verify = transport_module.verify_archive_members
        read = zipfile.ZipExtFile.read
        reads = []

        def cancel_after_read(member, *args, **kwargs):
            block = read(member, *args, **kwargs)
            if block:
                reads.append(len(block))
                self.assertTrue(self.transport.cancel(self.request.identity))
            return block

        def verify_with_cancel(*args, **kwargs):
            with (patch.object(archive_verification, 'CHUNK', 8),
                  patch.object(zipfile.ZipExtFile, 'read', cancel_after_read)):
                return verify(*args, **kwargs)

        with patch.object(transport_module, 'verify_archive_members', verify_with_cancel):
            with self.assertRaises(LibreOfficeError) as caught:
                self.execute()
        self.assertEqual(reads, [8])
        self.assertEqual(caught.exception.code, 'CANCELLED')
        self.assertFalse(self.request.packaging)
        self.assert_retained_unpublished()


if __name__ == '__main__':
    unittest.main()
