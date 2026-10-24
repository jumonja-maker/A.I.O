"""Matrix planning accounts for every original and every admitted catalogue target."""
import importlib.util
import contextlib
import io
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from test_guard_fulltest_v15 import Prepared

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('full_matrix_qa', ROOT / 'tools/qa/guard_v16_full_corpus_matrix.py')
qa = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(qa)


class OneRoute(Prepared):
    def capabilities(self, files, *, proofs=None):
        return {'files': [{**f, 'preflight': (proofs or {}).get(f['id'])} for f in files],
                'formats': [{'id': 'pdf', 'selectableFileIds': [f['id'] for f in files
                    if (proofs or {}).get(f['id'], {}).get('state') == 'detected']}]}


class FullMatrixPlanning(unittest.TestCase):
    def arguments(self, root, names=('source.bin',)):
        corpus = root / 'corpus'; corpus.mkdir()
        for name in names: (corpus / name).write_bytes(b'keep')
        return SimpleNamespace(corpus=corpus, output=root / 'output', executable=root / 'controlled-native',
            expected_sources=len(names), quality_check=False, task_seconds=2, preflight_seconds=2,
            max_seconds=10, progress_seconds=.01, min_free_gb=0)

    def run_control(self, args, adapter):
        with patch.object(qa, 'LibreOfficeAdapter', return_value=adapter), patch.object(
                qa, 'machine_facts', return_value={'scope': 'controlled_adapter_filesystem_smoke'}), contextlib.redirect_stdout(io.StringIO()):
            return qa.run_matrix(args)

    def test_every_admitted_target_is_selected_once_per_source(self):
        files = [{'fileId': 'a', 'relativePath': 'a.docx'}, {'fileId': 'b', 'relativePath': 'b.png'}]
        inspection = {'formats': [{'id': 'pdf', 'selectableFileIds': ['a', 'b']},
            {'id': 'apng', 'selectableFileIds': ['b']}, {'id': 'pdf', 'selectableFileIds': ['a']}],
            'files': [{'id': 'a', 'preflight': {'state': 'detected'}}, {'id': 'b', 'preflight': {'state': 'detected'}}]}
        plan, accounting, catalogue = qa.build_plan(inspection, files)
        self.assertEqual(plan, [{'fileId': 'a', 'formats': ['pdf']}, {'fileId': 'b', 'formats': ['pdf', 'apng']}])
        self.assertEqual(catalogue, ['pdf', 'apng'])
        self.assertEqual(sum(r['targetCount'] for r in accounting), 3)

    def test_negative_and_unresolved_inputs_are_never_omitted_or_called_success(self):
        files = [{'fileId': key, 'relativePath': key + '.bin'} for key in ('bad', 'missing', 'unknown')]
        inspection = {'formats': [], 'files': [
            {'id': 'bad', 'preflight': {'state': 'undetermined', 'code': 'SOURCE_CONTAINER_INCONSISTENT'}},
            {'id': 'missing', 'preflight': {'state': 'undetermined', 'code': 'HTML_LOCAL_RESOURCES_NOT_UPLOADED'}},
            {'id': 'unknown', 'preflight': {'state': 'undetermined', 'code': 'SOURCE_IMPORT_FAILED'}}]}
        plan, rows, catalogue = qa.build_plan(inspection, files)
        self.assertFalse(plan)
        self.assertEqual([r['classification'] for r in rows],
                         ['unresolved_input', 'unresolved_input', 'unresolved_input'])
        self.assertEqual([r['reportedFixtureClass'] for r in rows],
                         ['inconsistent_fixture', 'fixture_missing_resources', 'unresolved_input'])
        self.assertFalse(any(r['reviewedFixtureNegative'] for r in rows))
        self.assertTrue(all(r['targetCount'] == 0 and r['code'] for r in rows))

    def test_inventory_hashes_nested_originals_without_rewriting(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root / 'nested').mkdir()
            (root / 'a.txt').write_bytes(b'original'); (root / 'nested/b.png').write_bytes(b'bytes')
            rows = qa.source_inventory(root)
            self.assertEqual([r['relativePath'] for r in rows], ['a.txt', 'nested/b.png'])
            self.assertEqual([r['size'] for r in rows], [8, 5])
            self.assertEqual((root / 'a.txt').read_bytes(), b'original')
            self.assertEqual(rows[0]['sha256'], qa.digest(root / 'a.txt'))

    def test_controlled_adapter_runs_real_guard_save_readback_and_reports(self):
        with tempfile.TemporaryDirectory() as folder:
            args = self.arguments(Path(folder))
            evidence = self.run_control(args, OneRoute())
            self.assertTrue(evidence['acceptancePassed'], evidence)
            self.assertTrue(evidence['save']['readbackVerified'])
            self.assertEqual(evidence['save']['outputsVerified'], 1)
            self.assertTrue(evidence['sourcesUnchanged'])
            self.assertTrue(evidence['uploadsMatchInventory'])
            self.assertEqual((args.corpus / 'source.bin').read_bytes(), b'keep')
            self.assertTrue((args.output / 'conversion-report-final.json').is_file())

    def test_negative_input_keeps_its_actual_upload_hash_after_plan_selection(self):
        class Negative(OneRoute):
            def preflight(self, source, workspace, **kwargs):
                return ({'state': 'undetermined', 'code': 'SOURCE_CONTAINER_INCONSISTENT'}
                        if source.name.startswith('bad') else super().preflight(source, workspace, **kwargs))
        with tempfile.TemporaryDirectory() as folder:
            args = self.arguments(Path(folder), ('source.bin', 'bad-fixture.bin'))
            args.fixture_negatives = Path(folder) / 'reviewed-negatives.json'
            args.fixture_negatives.write_text(json.dumps({'schema': 1, 'files': [{
                'relativePath': 'bad-fixture.bin', 'sha256': qa.digest(args.corpus / 'bad-fixture.bin'),
                'code': 'SOURCE_CONTAINER_INCONSISTENT', 'reason': 'Controlled independently reviewed invalid fixture'}]}))
            evidence = self.run_control(args, Negative())
            self.assertTrue(evidence['acceptancePassed'], evidence)
            self.assertEqual(evidence['status'], 'completed_with_fixture_negatives')
            self.assertTrue(evidence['uploadsMatchInventory'])

    def test_rejecting_good_originals_as_bad_fixtures_does_not_pass_acceptance(self):
        class Regressed(OneRoute):
            def preflight(self, source, workspace, **kwargs):
                return ({'state': 'undetermined', 'code': 'SOURCE_CONTAINER_INCONSISTENT'}
                        if source.name.startswith('rejected') else super().preflight(source, workspace, **kwargs))
        with tempfile.TemporaryDirectory() as folder:
            args = self.arguments(Path(folder), ('source.bin', 'rejected1.bin', 'rejected2.bin'))
            evidence = self.run_control(args, Regressed())
            self.assertFalse(evidence['acceptancePassed'], evidence)
            self.assertEqual(evidence['status'], 'completed_with_failures')
            self.assertEqual(evidence['inputClassifications']['unresolved_input'], 2)
            self.assertEqual(evidence['tasks']['succeeded'], 1)
            self.assertEqual(evidence['reviewedFixtureNegatives'], 0)

    def test_reviewed_exclusions_require_exact_source_hash_and_explicit_reason(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); args = self.arguments(root)
            path = root / 'negatives.json'
            row = {'relativePath': 'source.bin', 'sha256': qa.digest(args.corpus / 'source.bin'),
                   'code': 'SOURCE_CONTAINER_INCONSISTENT', 'reason': 'Independent fixture inspection'}
            def write(): path.write_text(json.dumps({'schema': 1, 'files': [row]}))
            write()
            self.assertIn('source.bin', qa.load_fixture_negatives(path, qa.source_inventory(args.corpus)))
            row['sha256'] = '0' * 64; write()
            with self.assertRaises(ValueError): qa.load_fixture_negatives(path, qa.source_inventory(args.corpus))
            row['sha256'] = qa.digest(args.corpus / 'source.bin'); row['reason'] = ''; write()
            with self.assertRaises(ValueError): qa.load_fixture_negatives(path, qa.source_inventory(args.corpus))

    def test_admitted_pair_filtered_without_conversion_is_not_a_pass(self):
        class Filtered(OneRoute):
            def route_decision(self, target, proof): return 'unavailable'
        with tempfile.TemporaryDirectory() as folder:
            args = self.arguments(Path(folder)); evidence = self.run_control(args, Filtered())
            self.assertFalse(evidence['acceptancePassed'], evidence)
            self.assertFalse(evidence['plannedPairsFullySucceeded'])
            self.assertEqual(evidence['tasks']['succeeded'], 0)
            self.assertEqual(evidence['tasks']['filtered'], 1)

    def test_supervisor_deadline_cancels_save_and_retains_unsaved_guard_output(self):
        original = qa.ConverterGuard.save_folder
        def slow_save(guard, *args, **kwargs):
            time.sleep(2.8)
            return original(guard, *args, **kwargs)
        with tempfile.TemporaryDirectory() as folder:
            args = self.arguments(Path(folder)); args.max_seconds = 2.5
            with patch.object(qa.ConverterGuard, 'save_folder', slow_save):
                evidence = self.run_control(args, OneRoute())
            self.assertFalse(evidence.get('acceptancePassed', False), evidence)
            self.assertEqual(evidence['cancelReason'], 'QA_SUPERVISOR_DEADLINE')
            self.assertTrue(evidence['sourcesUnchanged'])
            self.assertTrue(list((args.output / 'owned').rglob('source.pdf')),
                            'Unacknowledged result must remain recoverable after cancelled save')


if __name__ == '__main__': unittest.main()
