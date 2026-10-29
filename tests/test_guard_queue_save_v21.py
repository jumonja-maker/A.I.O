"""V21: loss-aware recovery, literal filenames and nonblocking selection cleanup."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from app_backend.converter_guard import ConverterGuard, GuardError
from app_backend.converter_guard.result_store import ResultStore
from app_backend.converter_guard.save_verification import verify_folder
from test_converter_guard_v02 import FakeAdapter, wait_finished


class QueueSaveAuditV21(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.guards = []

    def tearDown(self):
        for guard in self.guards:
            guard.close()
        self.temp.cleanup()

    def guard(self):
        guard = ConverterGuard(adapter=FakeAdapter(), recovery_parent=self.root / 'owned')
        self.guards.append(guard)
        return guard

    def ready(self, *, name='source.docx', formats=('pdf', 'html'), retain_sources=False):
        guard = self.guard()
        job = guard.create([{'id': 'f', 'name': name, 'size': 4}], list(formats),
                           retain_sources=retain_sources)['jobId']
        guard.upload(job, 'f', io.BytesIO(b'keep'), 4)
        guard.start(job, archive_mode='on_demand')
        return guard, job, wait_finished(guard, job)

    def test_readback_uses_literal_unicode_filename_without_renaming_it(self):
        name = 're\u0301sultat.txt'
        path = self.root / name
        path.write_bytes(b'unchanged')
        entry = {'path': name, 'size': 9, 'sha256': hashlib.sha256(b'unchanged').hexdigest()}
        proof = verify_folder(self.root, [entry])
        self.assertTrue(proof['readbackVerified'])
        self.assertEqual(entry['path'], name)
        self.assertEqual(path.read_bytes(), b'unchanged')

    def test_decomposed_unicode_native_output_can_be_saved_and_acknowledged(self):
        guard, job, state = self.ready(name='re\u0301sultat.docx', formats=('pdf',))
        saved = guard.save_folder(state['result']['ticket'], self.root)
        self.assertTrue(saved['readbackVerified'])
        self.assertTrue(saved['savedAll'])
        output = guard._jobs[job].artifacts[0]
        self.assertEqual((self.root / saved['name'] / output.archive_name).read_bytes(), b'data')
        manifest = json.loads((self.root / saved['name'] / 'AIO-results-manifest.json').read_text())
        self.assertEqual(manifest['outputs'][0]['path'], output.archive_name)

    def test_canonical_equivalent_duplicate_manifest_paths_still_fail(self):
        entries = []
        for name in ('re\u0301sultat.txt', 'r\u00e9sultat.txt'):
            (self.root / name).write_bytes(b'a')
            entries.append({'path': name, 'size': 1, 'sha256': hashlib.sha256(b'a').hexdigest()})
        with self.assertRaises(GuardError) as error:
            verify_folder(self.root, entries)
        self.assertEqual(error.exception.code, 'SAVE_VERIFY_FAILED')

    def test_slow_removed_source_cleanup_does_not_block_other_guard_operations(self):
        guard = self.guard()
        rows = [{'id': str(i), 'name': str(i) + '.docx', 'size': 1} for i in range(2)]
        job = guard.create(rows, prepare_only=True)['jobId']
        for row in rows:
            guard.upload(job, row['id'], io.BytesIO(b'x'), 1)
        with patch.object(guard.adapter, 'preflight', return_value={'state': 'detected'}, create=True), \
                patch.object(guard.adapter, 'capabilities', return_value={'files': [], 'formats': []}):
            guard.preflight(job)
        removed = guard._jobs[job].files[0].source.parent
        entered, release, responsive = threading.Event(), threading.Event(), threading.Event()
        outcomes = []
        original = __import__('shutil').rmtree

        def delayed_delete(path, *args, **kwargs):
            if Path(path) == removed:
                entered.set()
                if not release.wait(3):
                    raise RuntimeError('Test cleanup gate timed out')
            return original(path, *args, **kwargs)

        def update():
            try:
                outcomes.append(guard.update_prepared(job, [rows[1]]))
            except Exception as error:
                outcomes.append(error)

        def inspect():
            guard.status(job)
            responsive.set()

        with patch('app_backend.converter_guard.core.shutil.rmtree', delayed_delete):
            updater = threading.Thread(target=update)
            updater.start()
            self.assertTrue(entered.wait(2))
            reader = threading.Thread(target=inspect)
            reader.start()
            try:
                self.assertTrue(responsive.wait(.5), 'Disk cleanup holds the global Guard state lock')
            finally:
                release.set()
                updater.join(3)
                reader.join(3)
        self.assertFalse(updater.is_alive())
        self.assertEqual(outcomes[0]['files'][0]['fileId'], '1')
        self.assertFalse(removed.exists())
        self.assertEqual(guard.storage_stats()['reservedBytes'], 1)

    def test_shutdown_timeout_keeps_unsaved_recovery_lock_until_read_lease_drains(self):
        guard, job, state = self.ready(formats=('pdf',), retain_sources=True)
        owned = guard._jobs[job]
        root = owned.root
        reserved = guard.storage_stats()['reservedBytes']
        with guard.open_result(state['result']['ticket']) as (stream, _name, _size):
            # Model a slow HTTP client / comparison reader beyond the existing
            # shutdown deadline without making the regression sleep five sec.
            with patch.object(guard._io_drained, 'wait_for', return_value=False):
                guard.close()
            self.assertIn(job, guard._jobs, 'In-flight I/O still owns recovery storage')
            self.assertEqual(guard.storage_stats()['reservedBytes'], reserved)
            contender = ResultStore(root)
            try:
                self.assertFalse(contender.acquire(), 'A second AIO must not adopt a live tree')
            finally:
                contender.close()
            self.assertEqual(stream.read(), b'data')
            with self.assertRaises(GuardError) as error:
                with guard.retained_inputs(job):
                    self.fail('A closed Guard cannot admit another lease')
            self.assertEqual(error.exception.code, 'GUARD_CLOSED')
        self.assertNotIn(job, guard._jobs)
        self.assertEqual(guard.storage_stats()['reservedBytes'], 0)
        self.assertTrue(root.exists(), 'Closing a reader must retain unsaved outputs')
        contender = ResultStore(root)
        try:
            self.assertTrue(contender.acquire())
        finally:
            contender.close()
        recovered = self.guard()
        self.assertEqual(len(recovered.recoverable()['jobs']), 1)
        self.assertEqual(recovered.status(job)['succeeded'], 1)

    def test_shutdown_timeout_defers_saved_tree_deletion_without_losing_accounting(self):
        guard, job, state = self.ready(formats=('pdf',))
        root = guard._jobs[job].root
        with guard.open_result(state['result']['ticket']):
            guard.confirm_saved(job, state['result']['ticket'])
            with patch.object(guard._io_drained, 'wait_for', return_value=False):
                guard.close()
            self.assertIn(job, guard._jobs)
            self.assertTrue(root.exists())
            self.assertGreater(guard.storage_stats()['reservedBytes'], 0)
        self.assertFalse(root.exists())
        self.assertNotIn(job, guard._jobs)
        self.assertEqual(guard.storage_stats()['reservedBytes'], 0)

    def test_retained_source_survives_missing_unsaved_output_after_other_output_saved(self):
        guard, job, state = self.ready(formats=('pdf', 'odt'), retain_sources=True)
        owned = guard._jobs[job]
        root, original = owned.root, owned.files[0].source
        guard.confirm_saved(job, owned.artifacts[0].ticket)
        missing = owned.artifacts[1].path
        guard.close()
        missing.unlink()
        recovered = self.guard()
        recovered.recoverable()
        state = recovered.status(job)
        self.assertEqual(state['files'][0]['targets'][1]['code'], 'RECOVERY_OUTPUT_MISSING')
        self.assertEqual(state['recoveryError'], 'RECOVERY_OUTPUT_MISSING')
        # A later lookup/create invokes saved-result reaping. The missing
        # unsaved task must not be reduced to "all surviving outputs saved".
        recovered.recoverable()
        recovered.create([{'id': 'new', 'name': 'new.docx', 'size': 0}], ['pdf'])
        self.assertTrue(root.exists())
        self.assertEqual(original.read_bytes(), b'keep')

    def test_failed_task_without_artifacts_remains_a_valid_journal_transaction(self):
        guard, job, _ = self.ready(name='bad.docx', formats=('pdf',))
        root = guard._jobs[job].root
        header, records, saved, integrity = ResultStore(root).read()
        self.assertEqual(integrity, 'complete')
        self.assertEqual(records[0]['outputCount'], 0)
        self.assertEqual(records[0]['state'], 'error')
        self.assertEqual(records[0]['outputs'], [])

    def test_release_during_selection_cleanup_waits_for_its_io_lease(self):
        guard = self.guard()
        rows = [{'id': str(i), 'name': str(i) + '.docx', 'size': 1} for i in range(2)]
        job = guard.create(rows, prepare_only=True)['jobId']
        for row in rows:
            guard.upload(job, row['id'], io.BytesIO(b'x'), 1)
        with patch.object(guard.adapter, 'preflight', return_value={'state': 'detected'}, create=True), \
                patch.object(guard.adapter, 'capabilities', return_value={'files': [], 'formats': []}):
            guard.preflight(job)
        root = guard._jobs[job].root
        removed = guard._jobs[job].files[0].source.parent
        entered, proceed = threading.Event(), threading.Event()
        errors = []
        original = __import__('shutil').rmtree
        def delayed(path, *args, **kwargs):
            if Path(path) == removed:
                entered.set()
                if not proceed.wait(3):
                    raise RuntimeError('Test cleanup gate timed out')
            return original(path, *args, **kwargs)
        def update():
            try:
                guard.update_prepared(job, [rows[1]])
            except GuardError as error:
                errors.append(error.code)
        with patch('app_backend.converter_guard.core.shutil.rmtree', delayed):
            worker = threading.Thread(target=update)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                guard.release(job, discard=True)
                self.assertTrue(root.exists())
                self.assertEqual(guard._jobs[job].io, 1)
            finally:
                proceed.set()
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, ['JOB_NOT_FOUND'])
        self.assertFalse(root.exists())
        self.assertEqual(guard.storage_stats()['reservedBytes'], 0)

    def test_partial_tree_deletion_is_never_adopted_by_a_second_instance(self):
        guard, job, _ = self.ready(formats=('pdf',), retain_sources=True)
        root = guard._jobs[job].root
        marker = guard._jobs[job].store.retirement_path
        entered, proceed = threading.Event(), threading.Event()
        original = __import__('shutil').rmtree
        deleting = None
        def delayed(path, *args, **kwargs):
            if Path(path) == root:
                if threading.current_thread() is deleting:
                    # Simulate arbitrary rmtree order: old in-tree lock/header
                    # are already gone, while unsaved output bytes remain.
                    (root / 'owner.lock').unlink(missing_ok=True)
                    (root / 'job.json').unlink(missing_ok=True)
                    entered.set()
                    if not proceed.wait(3):
                        raise RuntimeError('Test retirement gate timed out')
                else:
                    raise PermissionError('Other instance observes an in-progress deletion')
            return original(path, *args, **kwargs)
        with patch('app_backend.converter_guard.core.shutil.rmtree', delayed):
            deleting = threading.Thread(target=lambda: guard.release(job, discard=True))
            deleting.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertTrue(marker.exists())
                recovered = self.guard()
                report = recovered.recoverable()
                self.assertEqual(report['jobs'], [], 'Do not rescue a tree explicitly being discarded')
                self.assertTrue(any(w['code'] == 'RECOVERY_CLEANUP_RETRY' for w in report['warnings']))
                self.assertTrue(root.exists())
            finally:
                proceed.set()
                deleting.join(3)
        self.assertFalse(deleting.is_alive())
        self.assertFalse(root.exists())
        self.assertFalse(marker.exists())

    def test_failed_retirement_cleanup_retries_on_next_startup_without_recovery(self):
        guard, job, _ = self.ready(formats=('pdf',), retain_sources=True)
        root = guard._jobs[job].root
        marker = guard._jobs[job].store.retirement_path
        original = __import__('shutil').rmtree
        def denied(path, *args, **kwargs):
            if Path(path) == root:
                raise PermissionError('Fixture antivirus lock')
            return original(path, *args, **kwargs)
        with patch('app_backend.converter_guard.core.shutil.rmtree', denied):
            guard.release(job, discard=True)
            guard.close()
        self.assertTrue(root.exists())
        self.assertTrue(marker.exists())
        recovered = self.guard()
        self.assertEqual(recovered.recoverable()['jobs'], [])
        self.assertFalse(root.exists())
        self.assertFalse(marker.exists())
        guard.close()
        self.assertEqual(guard.storage_stats()['reservedBytes'], 0)

    def test_unwritten_retirement_marker_never_releases_the_original_owner_lock(self):
        guard, job, _ = self.ready(formats=('pdf',), retain_sources=True)
        owned = guard._jobs[job]
        root = owned.root
        with patch.object(owned.store, 'retire', side_effect=OSError('Disk full before durable marker')):
            guard.release(job, discard=True)
        contender = ResultStore(root)
        try:
            self.assertFalse(contender.acquire())
        finally:
            contender.close()
        self.assertTrue(root.exists())
        self.assertFalse(owned.store.retirement_path.exists())
        guard._cleanup(owned)
        self.assertFalse(root.exists())

    def test_invalid_or_foreign_retirement_marker_never_deletes_unsaved_bytes(self):
        guard, job, _ = self.ready(formats=('pdf',), retain_sources=True)
        owned = guard._jobs[job]
        root, output = owned.root, owned.artifacts[0].path
        marker = owned.store.retirement_path
        guard.close()
        info = root.stat()
        value = {'schema': 1, 'kind': 'aio_guard_retirement', 'rootName': root.name,
                 'device': info.st_dev, 'inode': info.st_ino + 1}
        value['checksum'] = ResultStore._digest(value)
        marker.write_text(json.dumps(value))
        recovered = self.guard()
        report = recovered.recoverable()
        self.assertEqual(report['jobs'], [])
        self.assertTrue(any(w['code'] == 'RECOVERY_RETIREMENT_INVALID' for w in report['warnings']))
        self.assertEqual(output.read_bytes(), b'data')
        self.assertTrue(root.exists())
        self.assertTrue(marker.exists())
        marker.unlink()
        resumed = self.guard()
        self.assertEqual(len(resumed.recoverable()['jobs']), 1)
        self.assertEqual(resumed.status(job)['succeeded'], 1)

    def test_stale_valid_retirement_marker_is_reaped_only_after_root_is_gone(self):
        guard, job, _ = self.ready(formats=('pdf',), retain_sources=True)
        owned = guard._jobs[job]
        root = owned.root
        owned.store.retire()
        marker = owned.store.retirement_path
        owned.store.close()
        # Model process death after rmtree, before removal of the side barrier.
        __import__('shutil').rmtree(root)
        self.assertTrue(marker.exists())
        recovered = self.guard()
        self.assertEqual(recovered.recoverable()['jobs'], [])
        self.assertFalse(marker.exists())

    def test_marker_parser_recursion_failure_does_not_hide_healthy_recovery_jobs(self):
        bad, bad_job, _ = self.ready(formats=('pdf',), retain_sources=True)
        bad_owned = bad._jobs[bad_job]
        bad_root = bad_owned.root
        marker = bad_owned.store.retirement_path
        bad.close()
        healthy, healthy_job, _ = self.ready(formats=('pdf',), retain_sources=True)
        healthy.close()
        marker.write_text('fixture-deep-marker')
        loads = json.loads
        def bounded_parser(value, *args, **kwargs):
            # JSON nesting failure differs across supported Python versions;
            # explicitly exercise this parser error without relying on depth.
            if value == 'fixture-deep-marker':
                raise RecursionError('Fixture parser nesting limit')
            return loads(value, *args, **kwargs)
        with patch('app_backend.converter_guard.result_store.json.loads', bounded_parser):
            recovered = self.guard()
            report = recovered.recoverable()
        self.assertEqual([item['jobId'] for item in report['jobs']], [healthy_job])
        self.assertTrue(any(w['code'] == 'RECOVERY_RETIREMENT_INVALID' for w in report['warnings']))
        self.assertTrue(bad_root.exists())

    def test_completed_job_ignores_a_late_cancel_from_an_old_ui_request(self):
        guard, job, state = self.ready(formats=('pdf',), retain_sources=True)
        owned = guard._jobs[job]
        before = guard.status(job)
        after = guard.cancel(job)
        self.assertEqual(after['state'], before['state'])
        self.assertEqual(after['revision'], before['revision'])
        self.assertFalse(owned.cancel.is_set())
        with guard.open_result(state['result']['ticket']) as (stream, _name, _size):
            self.assertEqual(stream.read(), b'data')

    def test_explicit_discard_is_not_reversed_by_shutdown_with_a_live_lease(self):
        guard, job, state = self.ready(formats=('pdf',), retain_sources=True)
        root = guard._jobs[job].root
        with guard.open_result(state['result']['ticket']):
            guard.release(job, discard=True)
            with patch.object(guard._io_drained, 'wait_for', return_value=False):
                guard.close()
            self.assertTrue(root.exists())
            self.assertTrue(guard._jobs[job].released)
        self.assertFalse(root.exists())
        self.assertEqual(guard.storage_stats()['reservedBytes'], 0)

    def test_duplicate_output_record_cannot_replace_missing_bundle_member(self):
        guard, job, _ = self.ready()
        root = guard._jobs[job].root
        guard.close()
        ledger = root / 'results.jsonl'
        lines = ledger.read_text().splitlines()
        main = next(line for line in lines if json.loads(line)['record'].get('kind') == 'output'
                    and json.loads(line)['record']['output']['path'].endswith('.html'))
        damaged = [main if json.loads(line)['record'].get('kind') == 'output' and
                   json.loads(line)['record']['output']['path'].endswith('plot.png') else line
                   for line in lines]
        ledger.write_text('\n'.join(damaged) + '\n')
        _header, records, _saved, integrity = ResultStore(root).read()
        self.assertEqual(integrity, 'incomplete_transaction')
        self.assertNotIn(1, records)

    def test_new_journal_detects_a_missing_whole_output_line(self):
        guard, job, _ = self.ready()
        root = guard._jobs[job].root
        guard.close()
        ledger = root / 'results.jsonl'
        lines = ledger.read_text().splitlines()
        removed = False
        retained = []
        for line in lines:
            record = json.loads(line)['record']
            if record.get('kind') == 'output' and record['output']['path'].endswith('plot.png'):
                removed = True
                continue
            retained.append(line)
        self.assertTrue(removed)
        ledger.write_text('\n'.join(retained) + '\n')
        _header, records, _saved, integrity = ResultStore(root).read()
        self.assertEqual(integrity, 'incomplete_transaction')
        self.assertNotIn(1, records, 'An incomplete HTML bundle must not regain a native-success claim')
        recovered = self.guard()
        recovered.recoverable()
        current = recovered.status(job)
        self.assertEqual(len(current['result']['outputs']), 3, 'Rescue all surviving bytes')
        self.assertTrue(current['salvaged'])
        self.assertTrue(current['recoveryError'])

    def test_empty_legacy_success_is_not_counted_ready_when_another_task_survives(self):
        guard, job, _ = self.ready(retain_sources=True)
        root = guard._jobs[job].root
        guard.close()
        ledger = root / 'results.jsonl'
        lines = []
        for line in ledger.read_text().splitlines():
            envelope = json.loads(line)
            record = envelope['record']
            if record.get('kind') == 'task_begin':
                record.pop('outputCount', None)  # A legacy transaction has no count.
                envelope['checksum'] = ResultStore._digest(record)
            if record.get('kind') == 'output' and '/0/output/' in record['output']['path']:
                (root / record['output']['path']).unlink()
                continue
            lines.append(json.dumps(envelope))
        ledger.write_text('\n'.join(lines) + '\n')
        recovered = self.guard()
        recovered.recoverable()
        state = recovered.status(job)
        task = state['files'][0]['targets'][0]
        self.assertEqual(task['state'], 'error')
        self.assertEqual(task['code'], 'RECOVERY_OUTPUT_MISSING')
        self.assertTrue(state['recoveryError'])
        self.assertEqual(state['succeeded'], 1)
        self.assertEqual(len(state['result']['outputs']), 2)
        saved = recovered.save_folder(state['result']['ticket'], self.root)
        self.assertFalse(saved['savedAll'])
        recovered.release(job)
        self.assertTrue(root.exists(), 'Missing legacy outputs must not silently reap retained originals')


if __name__ == '__main__':
    unittest.main()
