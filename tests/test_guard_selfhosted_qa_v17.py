"""Acceptance must distinguish incomplete coverage from an executed PASS."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

FILE = Path(__file__).resolve().parents[1] / 'tools/qa/guard_v17_selfhosted_acceptance.py'
SPEC = importlib.util.spec_from_file_location('guard_v17_selfhosted', FILE)
qa = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(qa)


class AcceptanceEvidenceV17(unittest.TestCase):
    def native(self):
        return {'generatedFixtures': [{'status': 'pass'}], 'negativeControls': [],
                'guardAcceptance': {'status': 'pass', 'report': {'succeeded': 3}}}

    def test_partial_native_coverage_never_becomes_pass(self):
        evidence = self.native()
        evidence['guardAcceptance']['status'] = 'partial_native_coverage'
        evidence['guardAcceptance']['unplannedPairs'] = [{'target': 'apng'}]
        result = qa.native_summary(evidence)
        self.assertEqual(result['status'], 'INCOMPLETE')
        self.assertEqual(result['unplannedPairs'], [{'target': 'apng'}])

    def test_missing_native_results_do_not_count_as_success(self):
        for evidence in ({}, {'generatedFixtures': [], 'guardAcceptance': {'status': 'pass'}}):
            with self.subTest(evidence=evidence):
                self.assertEqual(qa.native_summary(evidence)['status'], 'INCOMPLETE')

    def test_absent_exporter_is_reported_separately_from_executed_native_cases(self):
        evidence = self.native()
        evidence['generatedFixtures'].append({'target': 'sxw', 'status': 'not_run',
            'reason': 'installed_native_export_filter_unavailable'})
        result = qa.native_summary(evidence)
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['unsupportedFixtureGeneration'][0]['target'], 'sxw')
        self.assertEqual(result['generated'], {'pass': 1, 'not_run': 1})

    def test_native_failure_takes_precedence_over_unavailable_generation(self):
        evidence = self.native()
        evidence['negativeControls'] = [{'status': 'fail', 'reason': 'bad_container_became_ready'}]
        self.assertEqual(qa.native_summary(evidence)['status'], 'FAIL')
        self.assertEqual(qa.completion_status({'native': {'status': 'FAIL'}, 'DOM': {'status': 'INCOMPLETE'}}), 'FAIL')

    def test_passing_report_cannot_hide_a_crashed_or_timed_out_process(self):
        stage = {'status': 'FAIL', 'returncode': 1}
        qa.attach_evidence(stage, {'status': 'PASS', 'succeeded': 20})
        self.assertEqual(stage['status'], 'FAIL')
        self.assertEqual(stage['succeeded'], 20)

    def test_only_specific_posix_skips_are_inapplicable_on_windows(self):
        test = SimpleNamespace(id=lambda: 'example')
        result = SimpleNamespace(testsRun=2, failures=[], errors=[], unexpectedSuccesses=[],
            skipped=[(test, 'POSIX test fixture; real Windows Job Object needs Windows acceptance'),
                     (test, 'Qt is missing')])
        windows = qa.suite_summary(result, 'Windows')
        self.assertEqual([row['status'] for row in windows['skipped']], ['not_applicable', 'INCOMPLETE'])
        self.assertEqual(windows['status'], 'INCOMPLETE')
        linux = qa.suite_summary(result, 'Linux')
        self.assertEqual([row['status'] for row in linux['skipped']], ['INCOMPLETE', 'INCOMPLETE'])

    def make_bundle(self, root, *, name='source.odt', digest=None, extra=False):
        path = root / 'tests/data/guard_v17_fulltest_subset.zip'
        path.parent.mkdir(parents=True)
        data = b'original fixture'
        manifest = {'scope': 'targeted_original_FULLTEST_subset', 'sourceCorpusFiles': 510,
                    'files': [{'name': name, 'bytes': len(data),
                               'sha256': digest or hashlib.sha256(data).hexdigest()}]}
        with zipfile.ZipFile(path, 'x') as archive:
            archive.writestr('__manifest__.json', json.dumps(manifest))
            archive.writestr(name, data)
            if extra:
                archive.writestr('unlisted.txt', b'not in manifest')
        return data

    def test_bundled_fixture_is_hash_checked_before_extracting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = self.make_bundle(root)
            out = root / 'out'; out.mkdir()
            with patch.object(qa, 'ROOT', root):
                corpus = qa.bundled_corpus(out)
            self.assertEqual((corpus / 'source.odt').read_bytes(), expected)
            self.assertEqual(qa.corpus_inventory(corpus)['count'], 1)

    def test_bundle_hash_mismatch_does_not_publish_corpus(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_bundle(root, digest='0' * 64)
            out = root / 'out'; out.mkdir()
            with patch.object(qa, 'ROOT', root), self.assertRaisesRegex(ValueError, 'bytes differ'):
                qa.bundled_corpus(out)
            self.assertFalse((out / 'targeted-corpus').exists())

    def test_bundle_traversal_and_unlisted_entries_are_rejected(self):
        for args in ({'name': '../escape.odt'}, {'extra': True}):
            with self.subTest(args=args), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.make_bundle(root, **args)
                out = root / 'out'; out.mkdir()
                with patch.object(qa, 'ROOT', root), self.assertRaises(ValueError):
                    qa.bundled_corpus(out)
                self.assertFalse((out / 'targeted-corpus').exists())

    def test_inventory_detects_same_size_changes_without_modifying_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'fixture.odt'; source.write_bytes(b'before')
            before = qa.corpus_inventory(root)
            self.assertEqual(source.read_bytes(), b'before')
            source.write_bytes(b'after!')
            after = qa.corpus_inventory(root)
            self.assertEqual(before['bytes'], after['bytes'])
            self.assertNotEqual(before['inventorySha256'], after['inventorySha256'])


if __name__ == '__main__':
    unittest.main()
