"""Retained original ownership and independent, explicitly selected retries."""
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from app_backend.converter_guard import ConverterGuard, GuardError
from test_converter_guard_v02 import wait_finished


class NativeComparisonAdapter:
    available = True

    def __init__(self):
        self.calls, self.preflights, self.validations = [], [], []
        self.recipe = None
        self.block_preflight = False
        self.preflight_entered = threading.Event()

    def preflight(self, source, workspace, *, cancel, timeout):
        self.preflight_entered.set()
        while self.block_preflight:
            if cancel.wait(.01):
                raise GuardError('CANCELLED', 'cancelled', stage='preflight')
        self.preflights.append(source.read_bytes())
        return {'state':'detected', 'documentServices':['com.sun.star.text.TextDocument']}

    def capabilities(self, files, proofs=None):
        return {'files':files, 'formats':[]}

    def route_decision(self, target, proof):
        return 'registered' if proof and proof.get('state') == 'detected' else 'undetermined'

    def validate_retry_recipe(self, recipe, target, proof):
        self.validations.append((recipe, target))
        if proof is not None and proof.get('state') != 'detected':
            raise GuardError('AUTO_RETRY_UNAVAILABLE', 'unknown model')
        return {'id':recipe, 'target':target}

    def prepare_retry_recipe(self, recipe):
        self.recipe = recipe

    def export_info(self):
        return {'filter':'writer_pdf_Export', 'automaticRecipe':self.recipe,
                'automaticParameters':{'UseLosslessCompression':True} if self.recipe else {}}

    def convert(self, source, target, workspace, *, cancel, timeout, on_started,
                output_limit, on_document_service=None):
        if cancel.is_set():
            raise GuardError('CANCELLED', 'cancelled')
        on_started()
        if on_document_service:
            on_document_service({'state':'detected', 'documentServices':['com.sun.star.text.TextDocument']})
        self.calls.append((source.name, target, self.recipe))
        output = workspace/'output'; output.mkdir(parents=True)
        path = output/(source.stem+'.'+target)
        path.write_bytes((b'candidate:' if self.recipe else b'first:')+source.read_bytes())
        return [path]


class ComparisonOwnership(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.adapter = NativeComparisonAdapter()
        self.guard = ConverterGuard(adapter=self.adapter, spool_parent=self.root)
        self.addCleanup(self.guard.close)

    def converted(self, *, retain=True, count=2, formats=None, seconds=15):
        rows = [{'id':str(i), 'name':str(i)+'.docx', 'size':4} for i in range(count)]
        identity = self.guard.create(rows, formats or ['pdf'], retain_sources=retain)['jobId']
        for row in rows:
            self.guard.upload(identity, row['id'], io.BytesIO(b'DATA'), 4)
        self.guard.start(identity, archive_mode='on_demand')
        # This is an ownership fixture, not a conversion-speed benchmark. In
        # particular, a Windows runner may need longer to journal 1000 files.
        # Delta polling avoids repeatedly rebuilding every output in that test.
        deadline = time.monotonic() + seconds
        revision = None
        summary = {'jobId': identity}
        while time.monotonic() < deadline:
            state = self.guard.status(identity, since_revision=revision,
                                      delta=True, result_delta=True)
            revision = state['revision']
            if not state.get('unchanged'):
                summary = {key: state.get(key) for key in
                           ('jobId', 'state', 'total', 'completed', 'succeeded',
                            'failed', 'cancelled', 'pending')}
                if state['finished']:
                    return identity
            time.sleep(.05)
        self.fail('Guard fixture did not finish within %ss: %r' % (seconds, summary))

    def test_source_retention_is_explicit_and_does_no_extra_inspection(self):
        ordinary = self.converted(retain=False, count=1)
        retained = self.converted(count=1)
        self.assertTrue(self.guard._jobs[ordinary].files[0].source_released)
        self.assertFalse(self.guard._jobs[retained].files[0].source_released)
        self.assertFalse(self.guard.status(ordinary)['comparisonAvailable'])
        self.assertTrue(self.guard.status(retained)['comparisonAvailable'])
        self.assertEqual(self.adapter.preflights, [])
        self.assertEqual(len(self.adapter.calls), 2)
        self.assertIsNone(self.adapter.recipe)
        with self.assertRaises(GuardError):
            self.guard.create([{'id':'x','name':'x','size':0}], retain_sources=1)

    def test_comparison_lease_defers_discard_and_returns_independent_metadata(self):
        identity = self.converted()
        root = self.guard._jobs[identity].root
        with self.guard.comparison_inputs(identity) as records:
            self.assertEqual(len(records), 2)
            source = records[0]['source']
            with self.assertRaises(TypeError):
                records[0]['name'] = 'changed'
            self.guard.release(identity, discard=True)
            self.assertTrue(source.is_file())
            self.assertTrue(records[0]['paths'][0].is_file())
        self.assertFalse(root.exists())

    def test_foreign_output_is_unavailable_but_metadata_and_parent_are_preserved(self):
        identity = self.converted(count=1)
        artifact = self.guard._jobs[identity].artifacts[0]
        owned_path = artifact.path
        foreign = self.root / 'outside-this-job.pdf'
        expected = owned_path.read_bytes()
        foreign.write_bytes(expected)  # Even matching bytes do not establish ownership.
        artifact.path = foreign
        try:
            with self.guard.comparison_inputs(identity) as records:
                self.assertEqual(records[0]['paths'], ())
                self.assertTrue(records[0]['sourceAvailable'])
                self.assertEqual(records[0]['tickets'], (artifact.ticket,))
                self.assertEqual(len(records[0]['outputs']), 1)
            self.assertEqual(self.guard.status(identity)['succeeded'], 1)
            self.assertEqual(foreign.read_bytes(), expected)
        finally:
            artifact.path = owned_path

    def test_reparse_output_ancestor_is_checked_again_after_conversion(self):
        identity = self.converted(count=1)
        artifact = self.guard._jobs[identity].artifacts[0]
        # Python 3.11 Windows junctions need not satisfy Path.is_symlink().
        # Emulate the shared lstat/reparse predicate on every platform; the
        # service regression also creates a real junction on Windows.
        with patch('app_backend.converter_guard.core.path_is_link',
                   side_effect=lambda candidate: candidate == artifact.path.parent) as checked:
            with self.guard.comparison_inputs(identity) as records:
                self.assertEqual(records[0]['paths'], ())
                self.assertTrue(records[0]['sourceAvailable'])
            self.assertIn(artifact.path.parent, [call.args[0] for call in checked.call_args_list])
        with self.guard.comparison_inputs(identity) as records:
            self.assertEqual(records[0]['paths'], (artifact.path,))

    def _assert_invalid_artifact_cannot_be_downloaded_saved_or_zipped(self, identity, artifact):
        reads = []
        original_open = Path.open
        def tracked_open(path, mode='r', *args, **kwargs):
            if path == artifact.path and 'r' in mode:
                reads.append(str(path))
            return original_open(path, mode, *args, **kwargs)
        with tempfile.TemporaryDirectory() as destination:
            with patch.object(Path, 'open', tracked_open):
                with self.assertRaises(GuardError) as download:
                    with self.guard.open_result(artifact.ticket):
                        self.fail('Foreign artifact became downloadable')
                self.assertEqual(download.exception.code, 'SAVE_CHANGED')
                with self.assertRaises(GuardError) as saved:
                    self.guard.save_folder(artifact.ticket, destination)
                self.assertEqual(saved.exception.code, 'SAVE_CHANGED')
                self.guard.rebuild_bundle(identity)
                state = wait_finished(self.guard, identity)
                self.assertEqual(state['packagingError'], 'SAVE_CHANGED')
            self.assertEqual(list(Path(destination).iterdir()), [])
        self.assertEqual(reads, [], 'Ownership must be checked before reading foreign bytes')
        job = self.guard._jobs[identity]
        self.assertFalse(list(job.root.glob('*.zip')))
        self.assertTrue(all(not item.saved for item in job.artifacts))
        self.assertEqual(job.io, 0)
        self.assertFalse(job.saving)

    def test_same_byte_foreign_output_is_rejected_at_all_artifact_read_boundaries(self):
        identity = self.converted(count=2)
        artifact = self.guard._jobs[identity].artifacts[0]
        owned_path = artifact.path
        foreign = self.root / 'foreign-identical-output.pdf'
        expected = owned_path.read_bytes()
        foreign.write_bytes(expected)
        artifact.path = foreign
        try:
            self._assert_invalid_artifact_cannot_be_downloaded_saved_or_zipped(identity, artifact)
            self.assertEqual(foreign.read_bytes(), expected)
        finally:
            artifact.path = owned_path

    def test_redirected_output_parent_is_rejected_at_all_artifact_read_boundaries(self):
        identity = self.converted(count=2)
        artifact = self.guard._jobs[identity].artifacts[0]
        original = artifact.path.parent
        backup = original.with_name(original.name + '-original')
        foreign = self.root / 'foreign-output-directory'
        foreign.mkdir()
        expected = artifact.path.read_bytes()
        (foreign / artifact.path.name).write_bytes(expected)
        original.rename(backup)
        try:
            try:
                original.symlink_to(foreign, target_is_directory=True)
            except (NotImplementedError, OSError) as error:
                if os.name != 'nt':
                    self.fail('Cannot create controlled directory link: %s' % error)
                junction = subprocess.run(
                    ['cmd.exe', '/d', '/c', 'mklink', '/J', str(original), str(foreign)],
                    capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
                self.assertEqual(junction.returncode, 0,
                    'Cannot create no-admin junction: ' + (junction.stdout + junction.stderr)[-400:])
            self._assert_invalid_artifact_cannot_be_downloaded_saved_or_zipped(identity, artifact)
            self.assertEqual((foreign / artifact.path.name).read_bytes(), expected)
        finally:
            if original.is_symlink():
                original.unlink()
            elif original.exists():
                os.rmdir(original)  # Removes the junction, never its target.
            backup.rename(original)

    def test_legacy_or_missing_source_is_an_unavailable_record_not_lost_outputs(self):
        identity = self.converted(retain=False)
        with self.guard.comparison_inputs(identity) as records:
            self.assertTrue(all(not r['sourceAvailable'] and r['source'] is None for r in records))
            self.assertTrue(all(r['paths'][0].is_file() for r in records))
        self.assertEqual(self.guard.status(identity)['succeeded'], 2)
        with self.assertRaises(GuardError) as caught:
            self.guard.retry_comparison(identity, [identity+':0'], 'pdf_preserve_images')
        self.assertEqual(caught.exception.code, 'COMPARISON_SOURCE_UNAVAILABLE')

    def test_retry_creates_only_selected_candidate_and_preserves_parent_tickets(self):
        identity = self.converted()
        before = self.guard.status(identity)
        old = {a.ticket:(a.path, a.path.read_bytes()) for a in self.guard._jobs[identity].artifacts}
        created = []
        child = self.guard.retry_comparison(identity, [identity+':1'], 'pdf_preserve_images',
                                           on_created=created.append)
        after = wait_finished(self.guard, child['jobId'])
        self.assertEqual(created, [child['jobId']])
        self.assertEqual((after['total'], after['succeeded']), (1, 1))
        self.assertEqual(after['comparisonParentJobId'], identity)
        self.assertEqual(after['comparisonRecipe'], 'pdf_preserve_images')
        task = self.guard.report(child['jobId'])['files'][0]['targets'][0]
        self.assertEqual(task['automaticRecipe'], 'pdf_preserve_images')
        self.assertEqual(task['automaticParameters'], {'UseLosslessCompression':True})
        self.assertEqual(self.adapter.preflights, [b'DATA'])
        self.assertEqual(self.adapter.calls[-1], ('1.docx', 'pdf', 'pdf_preserve_images'))
        self.assertEqual(self.guard.status(identity)['result']['outputs'], before['result']['outputs'])
        self.assertTrue(all(path.read_bytes() == data and ticket in self.guard._tickets
                            for ticket,(path,data) in old.items()))
        self.assertIsNone(self.adapter.recipe)

    def test_invalid_selections_and_incompatible_recipe_have_no_side_effects(self):
        identity = self.converted()
        original = set(self.guard._jobs)
        cases = [([], 'pdf_preserve_images'), ([identity+':0']*2, 'pdf_preserve_images'),
                 (['f'*32+':0'], 'pdf_preserve_images'), ([identity+':0'], 'not_a_recipe'),
                 ([identity+':0'], 'csv_preserve_values')]
        for tasks, recipe in cases:
            with self.subTest(tasks=tasks, recipe=recipe), self.assertRaises(GuardError):
                self.guard.retry_comparison(identity, tasks, recipe)
            self.assertEqual(set(self.guard._jobs), original)
        self.assertEqual(len(self.adapter.calls), 2)

    def test_completed_candidate_is_not_cancelled_by_a_later_retry_event(self):
        identity = self.converted()
        caller = threading.Event()
        child = self.guard.retry_comparison(identity, [identity+':0'], 'pdf_preserve_images',
                                           cancel=caller)['jobId']
        completed = wait_finished(self.guard, child)
        self.assertFalse(completed['files'][0]['cancellationRequested'])
        caller.set()  # Cancellation of a subsequent operation using that event.
        after = self.guard.status(child)
        self.assertEqual(after['state'], 'ready')
        self.assertFalse(after['files'][0]['cancellationRequested'])
        self.assertEqual(after['result']['outputs'], completed['result']['outputs'])

    def test_source_mutation_rejects_candidate_before_native_start(self):
        identity = self.converted()
        self.guard._jobs[identity].files[0].source.write_bytes(b'EDIT')
        with self.assertRaises(GuardError) as caught:
            self.guard.retry_comparison(identity, [identity+':0'], 'pdf_preserve_images')
        self.assertEqual(caught.exception.code, 'COMPARISON_SOURCE_CHANGED')
        self.assertEqual(len(self.guard._jobs), 1)
        self.assertEqual(len(self.adapter.calls), 2)

    def test_cancellation_before_and_during_preparation_leaves_parent_unchanged(self):
        identity = self.converted()
        cancel = threading.Event(); cancel.set()
        with self.assertRaises(GuardError):
            self.guard.retry_comparison(identity, [identity+':0'], 'pdf_preserve_images', cancel=cancel)
        cancel.clear()
        def cancelled_after_creation(child_id):
            cancel.set()
        with self.assertRaises(GuardError):
            self.guard.retry_comparison(identity, [identity+':0'], 'pdf_preserve_images',
                                       cancel=cancel, on_created=cancelled_after_creation)
        self.assertEqual(len(self.guard._jobs), 1)
        self.assertEqual(self.guard.status(identity)['succeeded'], 2)

    def test_external_cancel_interrupts_native_preflight_without_watcher(self):
        identity = self.converted()
        cancel = threading.Event(); errors = []
        self.adapter.block_preflight = True
        def work():
            try:
                self.guard.retry_comparison(identity, [identity+':0'], 'pdf_preserve_images', cancel=cancel)
            except GuardError as error:
                errors.append(error.code)
        thread = threading.Thread(target=work)
        thread.start(); self.assertTrue(self.adapter.preflight_entered.wait(2))
        cancel.set(); thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, ['CANCELLED'])
        self.assertEqual(len(self.guard._jobs), 1)
        self.assertEqual(len(self.adapter.calls), 2)

    def test_thousand_retained_sources_stay_disk_owned_and_have_no_automatic_exports(self):
        identity = self.converted(count=1000, seconds=60)
        with self.guard.comparison_inputs(identity) as records:
            self.assertEqual(len(records), 1000)
            self.assertTrue(all(r['source'].is_file() and r['sourceAvailable'] for r in records))
        self.assertEqual(len(self.adapter.calls), 1000)
        self.assertEqual(self.adapter.preflights, [])

    def test_active_job_cannot_be_inspected(self):
        identity = self.guard.create([{'id':'x','name':'x.docx','size':4}], ['pdf'],
                                     retain_sources=True)['jobId']
        with self.assertRaises(GuardError) as caught:
            with self.guard.comparison_inputs(identity):
                pass
        self.assertEqual(caught.exception.code, 'COMPARISON_BUSY')

    def test_html_retry_preserves_private_companions_and_their_checksums(self):
        content = b'<img src="images/plot.png">'
        identity = self.guard.create([{'id':'html', 'name':'index.html', 'size':len(content)}],
                                     prepare_only=True, retain_sources=True)['jobId']
        self.guard.upload(identity, 'html', io.BytesIO(content), len(content))
        self.guard.configure_resources(identity, 'html', [{'path':'images/plot.png','size':5}])
        self.guard.upload_resource(identity, 'html', 'images/plot.png', io.BytesIO(b'IMAGE'), 5)
        self.guard.preflight(identity)
        self.guard.start(identity, file_ids=['html'], formats=['pdf'], only_verified=True,
                         archive_mode='on_demand')
        wait_finished(self.guard, identity)
        child = self.guard.retry_comparison(identity, [identity+':0'], 'pdf_preserve_images')
        wait_finished(self.guard, child['jobId'])
        with self.guard.comparison_inputs(child['jobId']) as records:
            companion = records[0]['resources'][0]
            self.assertEqual(companion['path'], 'images/plot.png')
            self.assertEqual(companion['source'].read_bytes(), b'IMAGE')
        self.assertEqual(self.guard._jobs[identity].files[0].resources['images/plot.png']['digest'],
                         self.guard._jobs[child['jobId']].files[0].resources['images/plot.png']['digest'])


class ComparisonRecovery(unittest.TestCase):
    def test_retry_recipe_and_exact_applied_parameters_survive_recovery(self):
        with tempfile.TemporaryDirectory() as folder:
            guard = ConverterGuard(adapter=NativeComparisonAdapter(), recovery_parent=folder)
            identity = guard.create([{'id':'x','name':'x.docx','size':4}], ['pdf'],
                                    retain_sources=True)['jobId']
            guard.upload(identity, 'x', io.BytesIO(b'DATA'), 4)
            guard.start(identity, archive_mode='on_demand'); wait_finished(guard, identity)
            child = guard.retry_comparison(identity, [identity+':0'], 'pdf_preserve_images')['jobId']
            wait_finished(guard, child)
            guard.close()
            restored = ConverterGuard(adapter=NativeComparisonAdapter(), recovery_parent=folder)
            try:
                report = restored.report(child)
                self.assertEqual(report['comparisonParentJobId'], identity)
                self.assertEqual(report['comparisonParentTasks'], [identity+':0'])
                self.assertEqual(report['comparisonRecipe'], 'pdf_preserve_images')
                target = report['files'][0]['targets'][0]
                self.assertEqual(target['automaticRecipe'], 'pdf_preserve_images')
                self.assertEqual(target['automaticParameters'], {'UseLosslessCompression':True})
                with restored.comparison_inputs(child) as records:
                    self.assertEqual(records[0]['source'].read_bytes(), b'DATA')
                restored.release(child, discard=True)
                restored.release(identity, discard=True)
            finally:
                restored.close()

    def test_retained_source_roundtrip_is_hash_checked_and_missing_source_is_honest(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            guard = ConverterGuard(adapter=NativeComparisonAdapter(), recovery_parent=root)
            identity = guard.create([{'id':'x','name':'x.docx','size':4}], ['pdf'],
                                    retain_sources=True)['jobId']
            guard.upload(identity, 'x', io.BytesIO(b'DATA'), 4)
            guard.start(identity, archive_mode='on_demand')
            wait_finished(guard, identity)
            source = guard._jobs[identity].files[0].source
            job_root = guard._jobs[identity].root
            guard.close()
            header = json.loads((job_root/'job.json').read_text())
            self.assertTrue(header['retainSources'])
            self.assertTrue(header['files'][0]['sourcePath'].startswith('input/'))
            self.assertNotIn(str(root), json.dumps(header))
            restored = ConverterGuard(adapter=NativeComparisonAdapter(), recovery_parent=root)
            try:
                with restored.comparison_inputs(identity) as records:
                    self.assertTrue(records[0]['sourceAvailable'])
                    self.assertEqual(records[0]['source'].read_bytes(), b'DATA')
            finally:
                restored.close()
            source.write_bytes(b'EDIT')
            changed = ConverterGuard(adapter=NativeComparisonAdapter(), recovery_parent=root)
            try:
                with changed.comparison_inputs(identity) as records:
                    self.assertFalse(records[0]['sourceAvailable'])
                    self.assertTrue(records[0]['paths'][0].is_file())
                changed.release(identity, discard=True)
            finally:
                changed.close()
