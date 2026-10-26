"""Current-session isolation, corrupt catalogue recovery and export provenance."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from app_backend.converter_guard import GuardError
from app_backend.converter_guard import comparison_analysis as analysis
from app_backend.converter_guard import comparison_worker as worker
import test_comparison_sessions_v19 as session_fixtures


class ComparisonRecoverySafety(unittest.TestCase):
    setUp = session_fixtures.ComparisonSessions.setUp
    cleanup = session_fixtures.ComparisonSessions.cleanup
    converted = session_fixtures.ComparisonSessions.converted
    checked = session_fixtures.ComparisonSessions.checked
    analyze = session_fixtures.ComparisonSessions.analyze
    recreate = session_fixtures.ComparisonSessions.recreate

    def history_run(self, identity):
        item = self.service.jobs[identity]
        run = {'runId': 'a' * 32, 'analysisId': item.identity, 'state': 'completed',
               'children': [], 'jobIds': [], 'jobId': None, 'selectedTaskIds': [],
               'selectedCount': 0, 'verification': {'assessment': 'not_comparable'}}
        item.retry = run
        self.service._retry_event(item, strict=True)
        return run

    def test_prior_retry_is_history_only_after_new_analysis_and_process_recovery(self):
        identity = self.converted()
        first = self.analyze(identity)
        old_run = self.history_run(identity)
        with patch.object(self.service, '_worker', side_effect=self.checked):
            self.service.start(identity, restart=True, expected_analysis_id=first['analysisId'])
            self.service.jobs[identity].future.result(5)
        current = self.service.snapshot(identity)
        self.assertIsNone(current['retry'])
        self.assertEqual(current['retryHistory'][0]['runId'], old_run['runId'])
        self.assertFalse(current['retryHistory'][0]['isCurrentAnalysis'])
        self.assertNotEqual(current['analysisId'], old_run['analysisId'])
        self.recreate()
        recovered = self.service.snapshot(identity)
        self.assertIsNone(recovered['retry'])
        self.assertEqual(recovered['historyTotal'], 1)
        self.assertEqual(recovered['retryHistory'][0]['analysisId'], old_run['analysisId'])
        self.assertFalse(recovered['retryHistory'][0]['isCurrentAnalysis'])
        self.assertEqual(recovered['historyRetention'], 'owned_job_lifetime')

    def test_retry_payload_cannot_claim_a_different_analysis_than_its_index(self):
        identity = self.converted()
        self.analyze(identity)
        self.history_run(identity)
        item = self.service.jobs[identity]
        with closing(sqlite3.connect(item.catalog)) as db, db:
            row = db.execute('SELECT seq,payload FROM retry_events').fetchone()
            value = json.loads(row[1])
            value['analysisId'] = 'b' * 32
            db.execute('UPDATE retry_events SET payload=? WHERE seq=?', (json.dumps(value), row[0]))
        with self.assertRaises(GuardError) as caught:
            self.service.retry_history(identity)
        self.assertEqual(caught.exception.code, 'COMPARISON_REPORT_UNAVAILABLE')
        self.assertEqual(self.guard.status(identity)['succeeded'], 1)

    def test_history_decorates_each_validated_run_only_once(self):
        identity = self.converted()
        self.analyze(identity)
        self.history_run(identity)
        with patch.object(self.service, '_decorate_retry', wraps=self.service._decorate_retry) as decorated:
            history = self.service.retry_history(identity)
        self.assertEqual(history['total'], 1)
        self.assertEqual(decorated.call_count, 1)

    def test_corrupt_catalogue_requires_explicit_restart_and_preserves_evidence(self):
        identity = self.converted()
        first = self.analyze(identity)
        item = self.service.jobs[identity]
        old_report = item.db
        artifact = self.guard._jobs[identity].artifacts[0]
        output_bytes = artifact.path.read_bytes()
        report_bytes = old_report.read_bytes()
        catalog = item.catalog
        corruption = b'controlled invalid sqlite catalogue'
        self.recreate()
        catalog.write_bytes(corruption)
        with self.assertRaises(GuardError) as caught:
            self.service.start(identity)
        self.assertEqual(caught.exception.code, 'COMPARISON_REPORT_UNAVAILABLE')
        self.assertEqual(catalog.read_bytes(), corruption)
        self.assertEqual(list(catalog.parent.glob('sessions-corrupt-*.sqlite')), [])
        # A durable identity cannot be proven from a corrupt catalogue after a
        # new process: stale commands must not acquire deletion authority.
        with self.assertRaises(GuardError) as caught:
            self.service.start(identity, restart=True, expected_analysis_id=first['analysisId'])
        self.assertEqual(caught.exception.code, 'COMPARISON_STALE')
        with patch.object(self.service, '_worker', side_effect=self.checked):
            restarted = self.service.start(identity, restart=True)
            self.service.jobs[identity].future.result(5)
        self.assertNotEqual(restarted['analysisId'], first['analysisId'])
        quarantines = list(catalog.parent.glob('sessions-corrupt-*.sqlite'))
        self.assertEqual(len(quarantines), 1)
        self.assertEqual(quarantines[0].read_bytes(), corruption)
        self.assertEqual(old_report.read_bytes(), report_bytes)
        self.assertEqual(artifact.path.read_bytes(), output_bytes)
        self.assertEqual(self.service.snapshot(identity)['state'], 'completed')
        self.assertEqual(self.guard._jobs[identity].io, 0)

    def test_cached_session_can_explicitly_restart_corrupt_catalogue_with_matching_token(self):
        identity = self.converted()
        first = self.analyze(identity)
        item = self.service.jobs[identity]
        item.catalog.write_bytes(b'not a SQLite database')
        with patch.object(self.service, '_worker', side_effect=self.checked):
            self.service.start(identity, restart=True, expected_analysis_id=first['analysisId'])
            self.service.jobs[identity].future.result(5)
        self.assertNotEqual(self.service.snapshot(identity)['analysisId'], first['analysisId'])
        self.assertEqual(self.guard.status(identity)['succeeded'], 1)

    def test_matching_cached_token_can_restart_corrupt_catalogue_schema(self):
        identity = self.converted()
        first = self.analyze(identity)
        item = self.service.jobs[identity]
        artifact = self.guard._jobs[identity].artifacts[0]
        original_output = artifact.path.read_bytes()
        original_report = item.db.read_bytes()
        with closing(sqlite3.connect(item.catalog)) as db, db:
            db.execute('ALTER TABLE current RENAME COLUMN id TO broken_id')
            self.assertEqual(db.execute('PRAGMA quick_check').fetchone(), ('ok',))
        broken_catalogue = item.catalog.read_bytes()
        with patch.object(self.service, '_worker', side_effect=self.checked):
            self.service.start(identity, restart=True, expected_analysis_id=first['analysisId'])
            self.service.jobs[identity].future.result(5)
        new = self.service.snapshot(identity)
        self.assertNotEqual(new['analysisId'], first['analysisId'])
        self.assertEqual(new['state'], 'completed')
        quarantined = list(item.catalog.parent.glob('sessions-corrupt-*.sqlite'))
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].read_bytes(), broken_catalogue)
        self.assertEqual(item.db.read_bytes(), original_report)
        self.assertEqual(artifact.path.read_bytes(), original_output)
        self.assertEqual(self.guard._jobs[identity].io, 0)

    def test_parallel_explicit_restarts_have_one_published_owner(self):
        identity = self.converted()
        first = self.analyze(identity)
        old = self.service.jobs[identity]
        old.catalog.write_bytes(b'broken parallel catalogue')
        barrier = threading.Barrier(2)
        outcomes, errors = [], []
        def restart():
            try:
                barrier.wait(3)
                outcomes.append(self.service.start(identity, restart=True,
                    expected_analysis_id=first['analysisId']))
            except GuardError as error:
                errors.append(error.code)
        with patch.object(self.service, '_worker', side_effect=self.checked):
            threads = [threading.Thread(target=restart) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.service.jobs[identity].future.result(5)
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(len(errors), 1)
        self.assertIn(errors[0], ('COMPARISON_STALE', 'COMPARISON_BUSY'))
        self.assertEqual(len(list(old.catalog.parent.glob('sessions-corrupt-*.sqlite'))), 1)
        self.assertEqual(self.service.snapshot(identity)['analysisId'], outcomes[0]['analysisId'])
        self.assertEqual(self.guard._jobs[identity].io, 0)

    def test_sidecar_move_failure_restores_original_catalogue_and_sidecar(self):
        identity = self.converted()
        self.analyze(identity)
        item = self.service.jobs[identity]
        corruption = b'broken sidecar catalogue'
        item.catalog.write_bytes(corruption)
        sidecar = Path(str(item.catalog) + '-shm')
        sidecar.write_bytes(b'controlled sidecar')
        rename = Path.rename
        def blocked(path, target):
            if path == sidecar:
                raise OSError('controlled sidecar rename failure')
            return rename(path, target)
        with patch.object(Path, 'rename', blocked):
            with self.assertRaises(GuardError) as caught:
                self.service.start(identity, restart=True)
        self.assertEqual(caught.exception.code, 'COMPARISON_PERSISTENCE_FAILED')
        self.assertEqual(item.catalog.read_bytes(), corruption)
        self.assertEqual(sidecar.read_bytes(), b'controlled sidecar')
        self.assertEqual(list(item.catalog.parent.glob('sessions-corrupt-*.sqlite')), [])
        self.assertEqual(self.guard.status(identity)['succeeded'], 1)

    def test_catalogue_create_failure_rolls_back_only_its_own_replacement(self):
        identity = self.converted()
        self.analyze(identity)
        item = self.service.jobs[identity]
        corruption = b'broken create catalogue'
        item.catalog.write_bytes(corruption)
        original_catalog = self.service._catalog
        def unavailable(job, *, create=False):
            if create:
                raise OSError('controlled schema creation failure')
            return original_catalog(job, create=create)
        with patch.object(self.service, '_catalog', side_effect=unavailable):
            with self.assertRaises(GuardError) as caught:
                self.service.start(identity, restart=True)
        self.assertEqual(caught.exception.code, 'COMPARISON_PERSISTENCE_FAILED')
        self.assertEqual(item.catalog.read_bytes(), corruption)
        self.assertEqual(list(item.catalog.parent.glob('sessions-corrupt-*.sqlite')), [])
        self.assertEqual(self.guard.status(identity)['succeeded'], 1)

    def test_failed_rebuild_does_not_unlink_an_index_replaced_by_another_owner(self):
        identity = self.converted()
        self.analyze(identity)
        item = self.service.jobs[identity]
        corruption = b'broken original catalogue'
        item.catalog.write_bytes(corruption)
        replacement = item.catalog.parent / 'controlled-other-index.sqlite'
        replacement.write_bytes(b'other owner index bytes')
        original_catalog = self.service._catalog
        def replaced(job, *, create=False):
            if create:
                replacement.replace(item.catalog)
                raise OSError('controlled replacement during rebuild')
            return original_catalog(job, create=create)
        with patch.object(self.service, '_catalog', side_effect=replaced):
            with self.assertRaises(GuardError) as caught:
                self.service.start(identity, restart=True)
        self.assertEqual(caught.exception.code, 'COMPARISON_PERSISTENCE_FAILED')
        self.assertEqual(item.catalog.read_bytes(), b'other owner index bytes')
        quarantine = list(item.catalog.parent.glob('sessions-corrupt-*.sqlite'))
        self.assertEqual(len(quarantine), 1)
        self.assertEqual(quarantine[0].read_bytes(), corruption)
        self.assertEqual(self.guard.status(identity)['succeeded'], 1)

    def test_closed_service_cannot_quarantine_or_recreate_catalogue(self):
        identity = self.converted()
        self.analyze(identity)
        item = self.service.jobs[identity]
        item.catalog.write_bytes(b'broken while closed')
        self.service.close()
        with self.assertRaises(GuardError) as caught:
            self.service.start(identity, restart=True)
        self.assertEqual(caught.exception.code, 'GUARD_CLOSED')
        self.assertEqual(item.catalog.read_bytes(), b'broken while closed')
        self.assertEqual(list(item.catalog.parent.glob('sessions-corrupt-*.sqlite')), [])

    def test_catalogue_lock_is_not_classified_as_corruption(self):
        identity = self.converted()
        self.analyze(identity)
        catalog = self.service.jobs[identity].catalog
        before = catalog.read_bytes()
        connect = sqlite3.connect
        def locked(path, *args, **kwargs):
            if 'sessions.sqlite' in str(path):
                error = sqlite3.OperationalError('controlled database is locked')
                error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                raise error
            return connect(path, *args, **kwargs)
        with patch('app_backend.converter_guard.comparison_service.sqlite3.connect', side_effect=locked):
            with self.assertRaises(GuardError):
                self.service.start(identity, restart=True)
        self.assertEqual(catalog.read_bytes(), before)
        self.assertEqual(list(catalog.parent.glob('sessions-corrupt-*.sqlite')), [])
        self.assertEqual(self.guard._jobs[identity].io, 0)

    def test_catalogue_quarantine_has_a_finite_job_local_budget(self):
        identity = self.converted()
        self.analyze(identity)
        item = self.service.jobs[identity]
        for index in range(4):
            item.catalog.with_name('sessions-corrupt-%024x.sqlite' % index).write_bytes(b'old')
        item.catalog.write_bytes(b'broken fifth catalogue')
        with self.assertRaises(GuardError) as caught:
            self.service.start(identity, restart=True)
        self.assertEqual(caught.exception.code, 'COMPARISON_RECOVERY_BUDGET')
        self.assertEqual(item.catalog.read_bytes(), b'broken fifth catalogue')
        self.assertEqual(self.guard.status(identity)['succeeded'], 1)

    def test_dangling_catalogue_link_cannot_create_an_external_database(self):
        identity = self.converted()
        job = self.guard._jobs[identity]
        base = job.root / 'comparison'
        base.mkdir()
        outside = self.root / 'must-not-be-created.sqlite'
        catalog = base / 'sessions.sqlite'
        try:
            catalog.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest('Host cannot create the controlled symlink')
        with self.assertRaises(GuardError) as caught:
            self.service.start(identity, restart=True)
        self.assertEqual(caught.exception.code, 'COMPARISON_STORAGE_UNSAFE')
        self.assertFalse(outside.exists())
        self.assertTrue(catalog.is_symlink())
        self.assertEqual(self.guard.status(identity)['succeeded'], 1)

    def test_history_metadata_does_not_survive_explicit_owned_job_discard(self):
        identity = self.converted()
        self.analyze(identity)
        root = self.guard._jobs[identity].root
        self.assertEqual(self.service.snapshot(identity)['historyRetention'], 'owned_job_lifetime')
        self.guard.release(identity, discard=True)
        self.assertFalse(root.exists())
        with self.assertRaises(GuardError):
            self.service.snapshot(identity)


class ComparisonExportProvenance(unittest.TestCase):
    def invoke(self, directory, *, expected_result, mutate=False, include_missing=False):
        root = Path(directory)
        source = root / 'source.csv'
        target = root / 'result.csv'
        source.write_bytes(b'name,value\nA,42\n')
        target.write_bytes(b'name,value\nA,0\n')
        pairs = [{'taskId': 'loss', 'target': 'csv', 'paths': [str(target)],
                  'sha256': {str(target): expected_result}}]
        if include_missing:
            pairs.insert(0, {'taskId': 'missing', 'target': 'txt',
                             'paths': [str(root / 'missing.txt')], 'sha256': {}})
        request = root / 'request.json'
        request.write_text(json.dumps({'source': str(source),
            'sourceSha256': hashlib.sha256(source.read_bytes()).hexdigest(), 'pairs': pairs}))
        compare = analysis.analyze_pair
        def changed(*args, **kwargs):
            outcome = compare(*args, **kwargs)
            if mutate:
                target.write_bytes(b'name,value\nA,1\n')
            return outcome
        with patch.object(worker, '_apply_memory_budget'), patch.object(analysis, 'analyze_pair', side_effect=changed):
            worker.main(request)
        return [json.loads(line) for line in (root / 'outcomes.jsonl').read_text().splitlines()]

    def test_missing_historical_sha_reports_current_difference_without_blame(self):
        with tempfile.TemporaryDirectory() as directory:
            row = self.invoke(directory, expected_result=None)[0]['outcome']
        self.assertEqual(row['status'], 'uncertain')
        self.assertEqual(row['observedStatus'], 'confirmed_loss')
        self.assertEqual(row['provenance']['comparisonScope'], 'current_bytes')
        self.assertEqual(row['provenance']['originalExportIdentity'], 'unavailable')
        self.assertIn('original_export_identity', row['unknown'])
        self.assertEqual(row['recipeCandidates'], [])
        differences = [f for f in row['findings'] if f.get('observedCategory') == 'confirmed_loss']
        self.assertTrue(differences)
        self.assertTrue(all(f['category'] == 'uncertain' for f in differences))
        self.assertTrue(all('приписать' in f['message'] for f in differences))

    def test_known_sha_retains_attributable_checked_subset_loss(self):
        digest = hashlib.sha256(b'name,value\nA,0\n').hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            row = self.invoke(directory, expected_result=digest)[0]['outcome']
        self.assertEqual(row['status'], 'confirmed_loss')
        self.assertEqual(row['provenance']['comparisonScope'], 'retained_export_bytes')
        self.assertEqual(row['provenance']['originalExportIdentity'], 'verified')

    def test_unknown_historical_sha_does_not_ignore_mutation_during_analysis(self):
        with tempfile.TemporaryDirectory() as directory:
            row = self.invoke(directory, expected_result=None, mutate=True)[0]['outcome']
        self.assertEqual(row['status'], 'not_checked')
        self.assertEqual(row['findings'][0]['code'], 'RESULT_CHANGED')
        self.assertEqual(row['checkedFeatures'], [])

    def test_current_byte_preview_without_historical_sha_still_checks_decoder_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'legacy.txt'
            source.write_bytes(b'OLD LEGACY RESULT')
            request = root / 'preview-request.json'
            request.write_text(json.dumps({'source': str(source), 'mode': 'preview', 'previewSide': 'result'}))
            def mutate(path):
                path.write_bytes(b'NEW LEGACY RESULT')
                return {'kind': 'text', 'text': 'OLD LEGACY RESULT'}
            with patch.object(worker, '_apply_memory_budget'), patch.object(analysis, 'preview_text', side_effect=mutate):
                worker.main(request)
            result = json.loads((root / 'preview.json').read_text())
        self.assertEqual(result['kind'], 'unavailable')
        self.assertEqual(result['code'], 'RESULT_CHANGED')
        self.assertNotIn('OLD LEGACY', json.dumps(result))

    def test_missing_result_does_not_abort_remaining_pairs_from_same_source(self):
        digest = hashlib.sha256(b'name,value\nA,0\n').hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            rows = self.invoke(directory, expected_result=digest, include_missing=True)
        self.assertEqual([row['taskId'] for row in rows], ['missing', 'loss'])
        self.assertEqual(rows[0]['outcome']['status'], 'not_checked')
        self.assertEqual(rows[1]['outcome']['status'], 'confirmed_loss')
