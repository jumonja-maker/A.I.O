"""Regression gates for F01/F02/F07/F10/F11 and recovery orphans (audit 2026-10-22)."""
import io
import tempfile
from pathlib import Path
from unittest.mock import patch
import unittest

from app_backend.libreoffice_engine import LibreOfficeError, validated_outputs
from app_backend.converter_guard import ConverterGuard, GuardError
from app_backend.converter_guard.core import _html_resource_warning, _missing_html_resources
from app_backend.converter_guard.result_store import ResultStore
from test_converter_guard_v02 import FakeAdapter, wait_finished


class HtmlPreflightAuditTests(unittest.TestCase):
    def check_html(self, markup, expected):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'index.html'
            source.write_text(markup, encoding='utf-8')
            self.assertEqual(_html_resource_warning(source), expected)

    def test_local_css_and_root_assets_must_be_uploaded(self):
        for markup in (
            '<style>.tile{background:url("../photo.png")}</style>',
            '<style>@import "styles/theme.css";</style>',
            '<div style="background-image: url(/img/icon.svg)"></div>',
            '<img src="/assets/logo.png">',
            '<style>.a{mask: url(#local)} .b{background:url(images/f.png)}</style>',
        ):
            with self.subTest(markup=markup):
                self.check_html(markup, 'HTML_LOCAL_RESOURCES_NOT_UPLOADED')

    def test_remote_data_and_fragment_assets_do_not_block(self):
        self.check_html('<style>.a{background:url("https://example.test/a.png")}'
                        '.b{filter:url(#id)}.c{background:url(data:image/png;base64,AAAA)}</style>'
                        '<img src="https://example.test/a.png">', None)

    def test_malformed_html_does_not_accumulate_unbounded_parser_rawdata(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'broken.html'
            with source.open('wb') as output:
                output.write(b'<!--')
                for _ in range(160):
                    output.write(b'x' * (128 * 1024))
            self.assertEqual(_html_resource_warning(source), 'HTML_PREFLIGHT_UNDETERMINED')
            self.assertTrue(_missing_html_resources(source))


class NativeOutputAuditTests(unittest.TestCase):
    def test_html_sidecar_only_never_claims_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'output').mkdir()
            (root / 'output' / 'pic.png').write_bytes(b'\x89PNG\r\n\x1a\npayload')
            with self.assertRaises(LibreOfficeError) as raised:
                validated_outputs(root / 'source.docx', 'html', root / 'output')
            self.assertEqual(raised.exception.code, 'ENGINE_OUTPUT_MISSING')

    def test_zero_byte_and_wrong_pdf_signature_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'out').mkdir()
            primary = root / 'out' / 'source.pdf'
            primary.write_bytes(b'')
            with self.assertRaises(LibreOfficeError) as raised:
                validated_outputs(root / 'source.odt', 'pdf', root / 'out')
            self.assertEqual(raised.exception.code, 'ENGINE_OUTPUT_EMPTY')
            primary.write_bytes(b'not-a-real-pdf')
            with self.assertRaises(LibreOfficeError) as raised:
                validated_outputs(root / 'source.odt', 'pdf', root / 'out')
            self.assertEqual(raised.exception.code, 'ENGINE_OUTPUT_INVALID')
            primary.write_bytes(b'%PDF-1.7\nvalid-header')
            self.assertEqual(validated_outputs(root / 'source.odt', 'pdf', root / 'out'), [primary])


class GuardRaceAndRecoveryAuditTests(unittest.TestCase):
    def test_stale_skip_identity_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = FakeAdapter()
            guard = ConverterGuard(adapter=adapter, spool_parent=directory)
            try:
                job = guard.create([{'id':'one','name':'hold.odt','size':1},
                                    {'id':'two','name':'other.odt','size':1}], ['pdf'])['jobId']
                for file_id in ('one', 'two'):
                    guard.upload(job, file_id, io.BytesIO(b'x'), 1)
                guard.start(job)
                self.assertTrue(adapter.waiting.wait(3))
                current = guard.status(job)['currentTask']
                self.assertEqual(current['fileId'], 'one')
                with self.assertRaises(GuardError) as raised:
                    guard.cancel_file(job, 'one', expected_task_id=job + ':stale')
                self.assertEqual(raised.exception.code, 'STALE_CURRENT_TASK')
                self.assertFalse(guard.status(job)['files'][0]['cancellationRequested'])
                guard.cancel_file(job, 'one', expected_task_id=current['taskId'])
                result = wait_finished(guard, job)
                self.assertEqual(result['files'][0]['targets'][0]['state'], 'cancelled')
                self.assertEqual(result['files'][1]['targets'][0]['state'], 'ready')
            finally:
                adapter.resume.set()
                guard.close()

    def test_recovery_write_error_cannot_claim_durable_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            guard = ConverterGuard(adapter=FakeAdapter(), recovery_parent=Path(directory))
            try:
                job = guard.create([{'id':'one','name':'source.odt','size':1}], ['pdf'])['jobId']
                guard.upload(job, 'one', io.BytesIO(b'x'), 1)
                with patch.object(ResultStore, 'task', side_effect=OSError('simulated disk failure')):
                    guard.start(job)
                    state = wait_finished(guard, job)
                self.assertEqual(state['state'], 'ready')
                self.assertEqual(state['recoveryError'], 'RECOVERY_WRITE_FAILED')
                self.assertFalse(state['recoveryDurable'])
                self.assertEqual(state['resultAvailability'], 'ready_volatile')
                self.assertFalse(state['qualityCompared'])
                stats = guard.storage_stats()
                self.assertEqual(stats['unsavedJobs'], 1)
                self.assertGreater(stats['unsavedBytes'], 0)
            finally:
                guard.close()

    def test_incomplete_manifest_surfaces_without_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            orphan = Path(directory) / 'aio-guard-missingmanifest'
            orphan.mkdir()
            (orphan / 'output.bin').write_bytes(b'preserve')
            guard = ConverterGuard(adapter=FakeAdapter(), recovery_parent=directory)
            try:
                warnings = guard.recoverable()['warnings']
                self.assertTrue(any(w['code'] == 'RECOVERY_MANIFEST_MISSING' and
                                    w['folder'] == orphan.name for w in warnings))
                self.assertTrue((orphan / 'output.bin').exists())
            finally:
                guard.close()


class DirectTransportAuditTests(unittest.TestCase):
    def test_explicit_release_retries_cleanup_but_unsaved_tickets_remain(self):
        from app_backend.converter_transport import ConverterTransport, Result
        class FailingCleanup:
            def __init__(self):
                self.calls = 0
            def cleanup(self):
                self.calls += 1
                if self.calls == 1:
                    raise OSError('simulated Windows sharing violation')
        transport = ConverterTransport()
        backing = tempfile.TemporaryDirectory()
        self.addCleanup(backing.cleanup)
        self.addCleanup(transport.close)
        temp = FailingCleanup()
        temp.name = str(Path(backing.name).resolve())
        output = Path(temp.name) / 'output.pdf'
        output.write_bytes(b'owned result')
        transport.results['ticket'] = Result(temp, output, 'output.pdf',
                                             'application/pdf', size=output.stat().st_size)
        transport.retry_cleanup()
        self.assertEqual(temp.calls, 0, 'unsaved result must never be reaped')
        self.assertEqual(transport.storage_stats()['retainedResults'], 1)
        self.assertFalse(transport.release('ticket'))
        self.assertEqual(transport.storage_stats()['cleanupPending'], 1)
        transport.retry_cleanup()
        self.assertNotIn('ticket', transport.results)
        self.assertEqual(temp.calls, 2)


if __name__ == '__main__':
    unittest.main()
