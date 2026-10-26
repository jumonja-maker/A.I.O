"""Bounded private failed-output evidence must never become ready or read sources."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from app_backend.converter_guard.diagnostic_quarantine import capture_failed_output


class DiagnosticQuarantineV20(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'job'
        self.work = self.root / 'work' / '1'
        self.output = self.work / 'output'
        self.output.mkdir(parents=True)
        self.original = self.root / 'input' / 'original.docx'
        self.original.parent.mkdir()
        self.original.write_bytes(b'ORIGINAL NEVER CAPTURE')
        self.source_hash = hashlib.sha256(self.original.read_bytes()).hexdigest()

    def capture(self, **kwargs):
        arguments = {'task_index': 1, 'error_code': 'OUTPUT_CONTENT_LOST',
                     'source_sha256': self.source_hash, 'max_bytes': 8192}
        arguments.update(kwargs)
        return capture_failed_output(self.root, self.work, **arguments)

    def samples(self):
        return self.root / 'diagnostics' / 'failed-outputs'

    def disk_size(self):
        return sum(p.stat().st_size for p in self.samples().rglob('*') if p.is_file())

    def test_default_disabled_does_not_touch_any_path_or_copy_any_source(self):
        with patch.object(Path, 'lstat', side_effect=AssertionError('filesystem inspected while disabled')):
            result = capture_failed_output(self.root, self.work, task_index=1, error_code='OUTPUT_BAD')
        self.assertEqual(result['status'], 'disabled')
        self.assertFalse(result['ready'])
        self.assertFalse(self.samples().exists())

    def test_small_output_is_copied_and_hashed_with_private_relative_receipt(self):
        data = b'failed native bytes\x00\xff'
        source = self.output / 'broken.xhtml'; source.write_bytes(data)
        result = self.capture(diagnostic=f'Native output failed: "{source}" and C:\\private\\test.xlsx')
        self.assertEqual(result['status'], 'captured')
        self.assertEqual(result['capturedBytes'], len(data))
        self.assertFalse(result['ready']); self.assertFalse(result['qualityCompared'])
        self.assertEqual(result['sourceSha256'], self.source_hash)
        entry = result['files'][0]
        self.assertEqual((self.root / entry['relativePath']).read_bytes(), data)
        self.assertEqual(entry['sha256'], hashlib.sha256(data).hexdigest())
        self.assertFalse(Path(entry['relativePath']).is_absolute())
        self.assertNotIn(str(self.root), result['diagnostic'])
        self.assertNotIn('C:\\private', result['diagnostic'])
        self.assertEqual(self.original.read_bytes(), b'ORIGINAL NEVER CAPTURE')
        self.assertEqual(source.read_bytes(), data)
        stored = self.root / entry['relativePath']; receipt = stored.parent / 'receipt.json'
        self.assertEqual(json.loads(receipt.read_text()), result)
        self.assertLessEqual(self.disk_size(), 8192)

    def test_oversize_result_records_size_without_opening_or_hashing_it(self):
        path = self.output / 'large.pdf'; path.write_bytes(b'x' * 20000)
        original_open = Path.open
        def guarded_open(candidate, *args, **kwargs):
            if candidate == path:
                self.fail('large output read despite quarantine byte budget')
            return original_open(candidate, *args, **kwargs)
        with patch.object(Path, 'open', guarded_open):
            result = self.capture()
        self.assertEqual(result['status'], 'metadata_only')
        entry = result['files'][0]
        self.assertEqual(entry['size'], 20000)
        self.assertIsNone(entry['sha256'])
        self.assertEqual(entry['reason'], 'byte_budget')
        self.assertNotIn('relativePath', entry)

    def test_total_job_budget_and_task_count_apply_across_calls(self):
        (self.output / 'small.bin').write_bytes(b'x' * 1000)
        results = [self.capture(max_bytes=4096, max_tasks=2) for _ in range(3)]
        self.assertEqual(results[0]['status'], 'captured')
        self.assertIn(results[1]['status'], ('captured', 'metadata_only'))
        self.assertEqual(results[2]['status'], 'task_limit')
        self.assertEqual(len(list(self.samples().iterdir())), 2)
        self.assertLessEqual(self.disk_size(), 4096)

    def test_low_budget_creates_no_task_and_never_exceeds_budget(self):
        (self.output / 'small.bin').write_bytes(b'x' * 1000)
        result = self.capture(max_bytes=16)
        self.assertEqual(result['status'], 'byte_limit')
        self.assertEqual(self.disk_size(), 0)

    def test_output_scan_and_receipt_are_entry_bounded(self):
        for index in range(100):
            (self.output / ('%03d.bin' % index)).write_bytes(b'x')
        result = self.capture(max_entries=3)
        self.assertEqual(len(result['files']), 3)
        self.assertEqual(len(list(self.samples().rglob('*.capture'))), 3)
        self.assertLessEqual(len(json.dumps(result)), 4096)

    def test_wrong_workspace_original_tree_and_escaping_parent_are_rejected(self):
        for workspace in [self.original.parent, self.root / 'work' / '2', self.work / '..' / '1']:
            with self.subTest(workspace=workspace):
                result = capture_failed_output(self.root, workspace, task_index=1,
                    error_code='BAD', max_bytes=8192)
                self.assertEqual(result['status'], 'unavailable')
        self.assertFalse(self.samples().exists())

    @unittest.skipUnless(hasattr(os, 'symlink'), 'symbolic links unavailable')
    def test_symlink_output_and_linked_diagnostic_destination_are_not_followed(self):
        linked = self.output / 'link.docx'
        try:
            linked.symlink_to(self.original)
        except OSError as error:
            self.skipTest(str(error))
        result = self.capture()
        self.assertEqual(result['files'][0]['reason'], 'link_or_reparse')
        self.assertEqual(result['capturedBytes'], 0)
        outside = self.root.parent / 'foreign'; outside.mkdir()
        # Remove this invocation's owned evidence before replacing the parent.
        import shutil
        shutil.rmtree(self.root / 'diagnostics')
        (self.root / 'diagnostics').symlink_to(outside, target_is_directory=True)
        result = self.capture()
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(list(outside.iterdir()), [])

    @unittest.skipUnless(hasattr(os, 'link'), 'hardlinks unavailable')
    def test_hardlinked_input_alias_is_metadata_only_and_never_read(self):
        try:
            os.link(self.original, self.output / 'alias.docx')
        except OSError as error:
            self.skipTest(str(error))
        result = self.capture()
        self.assertEqual(result['capturedBytes'], 0)
        self.assertEqual(result['files'][0]['reason'], 'non_regular_or_hardlinked')

    def test_reparse_attribute_is_rejected_without_windows_only_path_api(self):
        from app_backend.converter_guard import diagnostic_quarantine as module
        from types import SimpleNamespace
        self.assertTrue(module._linked(SimpleNamespace(st_mode=0o100600, st_file_attributes=0x400)))
        self.assertFalse(module._linked(SimpleNamespace(st_mode=0o100600, st_file_attributes=0)))

    def test_cancel_during_copy_removes_all_new_samples_and_partials(self):
        (self.output / 'medium.bin').write_bytes(b'x' * 200000)
        class CancelDuringCopy:
            checks = 0
            def is_set(self):
                self.checks += 1
                return self.checks >= 4
        result = self.capture(max_bytes=1024 * 1024, cancel=CancelDuringCopy())
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(result['files'], [])
        self.assertEqual(list(self.samples().iterdir()), [])
        self.assertEqual(self.original.read_bytes(), b'ORIGINAL NEVER CAPTURE')

    def test_copy_error_never_replaces_original_error_and_removes_own_partial(self):
        (self.output / 'small.bin').write_bytes(b'x')
        with patch('app_backend.converter_guard.diagnostic_quarantine.os.link', side_effect=OSError('disk failure')):
            result = self.capture()
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['errorCode'], 'OUTPUT_CONTENT_LOST')
        self.assertEqual(list(self.samples().iterdir()), [])

    def test_age_cleanup_only_removes_verified_own_samples(self):
        (self.output / 'small.bin').write_bytes(b'x')
        with patch('app_backend.converter_guard.diagnostic_quarantine.time.time', return_value=1000):
            first = self.capture(max_tasks=1)
        first_path = self.root / first['files'][0]['relativePath']
        with patch('app_backend.converter_guard.diagnostic_quarantine.time.time', return_value=2000):
            second = self.capture(max_tasks=1, max_age_seconds=10)
        self.assertEqual(second['status'], 'captured')
        self.assertFalse(first_path.exists())
        self.assertEqual(len(list(self.samples().iterdir())), 1)
        foreign = self.samples() / 'unowned'; foreign.mkdir()
        (foreign / 'keep.txt').write_text('do not delete')
        result = self.capture(max_age_seconds=1)
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual((foreign / 'keep.txt').read_text(), 'do not delete')

    def test_changed_output_is_not_published_as_a_hashed_sample(self):
        path = self.output / 'changing.bin'; path.write_bytes(b'x' * 200000)
        class ChangeDuringCopy:
            checks = 0
            def is_set(self):
                self.checks += 1
                if self.checks == 4:
                    path.write_bytes(b'y' * 200000)
                return False
        result = self.capture(max_bytes=1024 * 1024, cancel=ChangeDuringCopy())
        self.assertEqual(result['status'], 'metadata_only')
        self.assertEqual(result['files'][0]['reason'], 'changed_during_capture')
        self.assertIsNone(result['files'][0]['sha256'])
        self.assertEqual(list(self.samples().rglob('*.capture')), [])

    def test_failed_age_cleanup_retains_owner_for_a_later_retry(self):
        (self.output / 'small.bin').write_bytes(b'x')
        with patch('app_backend.converter_guard.diagnostic_quarantine.time.time', return_value=1000):
            first = self.capture(max_tasks=1)
        path = self.root / first['files'][0]['relativePath']
        original_unlink = Path.unlink
        def locked(candidate, *args, **kwargs):
            if candidate == path:
                raise OSError('file temporarily locked')
            return original_unlink(candidate, *args, **kwargs)
        with patch('app_backend.converter_guard.diagnostic_quarantine.time.time', return_value=2000), \
                patch.object(Path, 'unlink', locked):
            result = self.capture(max_tasks=1, max_age_seconds=10)
        self.assertEqual(result['status'], 'unavailable')
        self.assertTrue((path.parent / 'owner.json').exists())
        with patch('app_backend.converter_guard.diagnostic_quarantine.time.time', return_value=3000):
            recovered = self.capture(max_tasks=1, max_age_seconds=10)
        self.assertEqual(recovered['status'], 'captured')
        self.assertFalse(path.exists())

    def test_missing_native_output_is_an_unavailable_diagnostic_not_conversion_failure(self):
        self.output.rmdir()
        self.assertEqual(self.capture()['status'], 'unavailable')

    def test_invalid_policy_and_huge_requested_limits_are_bounded(self):
        self.assertEqual(self.capture(max_tasks=0)['status'], 'invalid_policy')
        self.assertEqual(self.capture(max_age_seconds=float('nan'))['status'], 'invalid_policy')
        (self.output / 'small.bin').write_bytes(b'x')
        result = self.capture(max_bytes=10**100, max_entries=10**100, max_tasks=10**100)
        self.assertEqual(result['status'], 'captured')
        self.assertEqual(result['entryLimit'], 16)
        self.assertLessEqual(self.disk_size(), 8 * 1024 * 1024)


if __name__ == '__main__':
    unittest.main()
