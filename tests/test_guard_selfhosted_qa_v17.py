"""Acceptance must distinguish incomplete coverage from an executed PASS."""
import hashlib
import importlib.util
import json
import os
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

    def test_source_only_linux_marks_unavailable_native_checks_without_claiming_execution(self):
        test = SimpleNamespace(id=lambda: 'native.example')
        result = SimpleNamespace(testsRun=2, failures=[], errors=[], unexpectedSuccesses=[],
            skipped=[(test, 'LibreOffice not installed'), (test, 'Application Qt dependency required')])
        linux = qa.suite_summary(result, 'Linux', source_only=True)
        self.assertEqual(linux['status'], 'PASS')
        self.assertEqual(linux['passed'], 0)
        self.assertTrue(all(row['status'] == 'platform_not_applicable' for row in linux['skipped']))
        self.assertEqual(qa.suite_summary(result, 'Windows', source_only=True)['status'], 'INCOMPLETE')
        self.assertEqual(qa.suite_summary(result, 'Linux')['status'], 'INCOMPLETE')

    def test_source_only_does_not_hide_missing_dom_tests(self):
        test = SimpleNamespace(id=lambda: 'ui.example')
        result = SimpleNamespace(testsRun=1, failures=[], errors=[], unexpectedSuccesses=[],
            skipped=[(test, 'Set AIO_DOM_MODULE')])
        self.assertEqual(qa.suite_summary(result, 'Linux', source_only=True)['status'], 'INCOMPLETE')

    def test_source_only_linux_records_new_qt_checks_as_unexecuted(self):
        skipped = []
        for test_id, reason in (
            ('test_comparison_pdf_preview_v19.PagePreview.test_pdf_pages_are_bounded_png_not_a_sandboxed_browser_plugin',
             'Existing app Qt dependency required'),
            ('test_guard_ui_audit_v18.NativeDownloadLifetime.test_terminal_requests_and_callbacks_leave_profile_ownership',
             'Qt bindings required for QObject lifetime proof'),
        ):
            skipped.append((SimpleNamespace(id=lambda value=test_id: value), reason))
        result = SimpleNamespace(testsRun=2, failures=[], errors=[], unexpectedSuccesses=[], skipped=skipped)
        summary = qa.suite_summary(result, 'Linux', source_only=True)
        self.assertEqual(summary['status'], 'PASS')
        self.assertEqual(summary['passed'], 0)
        self.assertEqual(summary['discovered'], 2)
        self.assertEqual([(row['test'], row['reason'], row['status']) for row in summary['skipped']],
                         [(test.id(), reason, 'platform_not_applicable') for test, reason in skipped])
        for system, source_only in (('Windows', True), ('Windows', False), ('Linux', False), ('Darwin', True)):
            with self.subTest(system=system, source_only=source_only):
                unaccepted = qa.suite_summary(result, system, source_only=source_only)
                self.assertEqual(unaccepted['status'], 'INCOMPLETE')
                self.assertEqual(unaccepted['passed'], 0)
                self.assertTrue(all(row['status'] == 'INCOMPLETE' for row in unaccepted['skipped']))

    def test_source_only_qt_exceptions_require_the_corresponding_test_class_and_exact_reason(self):
        for test_id, reason in (
            ('unrelated.PagePreview.test_case', 'Existing app Qt dependency required'),
            ('test_comparison_pdf_preview_v19.PagePreviewExtra.test_case', 'Existing app Qt dependency required'),
            ('test_comparison_pdf_preview_v19.PagePreview.test_case', 'Qt bindings required for QObject lifetime proof'),
            ('test_guard_ui_audit_v18.NativeDownloadLifetime.test_case', 'Existing app Qt dependency required'),
            ('test_guard_ui_audit_v18.NativeDownloadLifetimeExtra.test_case', 'Qt bindings required for QObject lifetime proof'),
            ('test_guard_ui_audit_v18.NativeDownloadLifetime.test_case', 'Qt bindings required for QObject lifetime proof (unexpected)'),
            ('test_comparison_pdf_preview_v19.PagePreview.test_case', 'Set AIO_DOM_MODULE'),
        ):
            with self.subTest(test=test_id, reason=reason):
                test = SimpleNamespace(id=lambda: test_id)
                result = SimpleNamespace(testsRun=1, failures=[], errors=[], unexpectedSuccesses=[],
                                         skipped=[(test, reason)])
                summary = qa.suite_summary(result, 'Linux', source_only=True)
                self.assertEqual(summary['status'], 'INCOMPLETE')
                self.assertEqual(summary['skipped'][0]['status'], 'INCOMPLETE')

    def test_source_only_keeps_pypdf_openpyxl_and_dom_mandatory(self):
        deps = {'PySide6': False, 'openpyxl': True, 'pypdf': False, 'node': True, 'DOM': False}
        summary = qa.dependency_summary(deps, system='Linux', source_only=True)
        self.assertEqual(summary['status'], 'INCOMPLETE')
        self.assertEqual(summary['missing'], ['pypdf', 'DOM'])
        self.assertEqual(summary['platformNotApplicable'], ['PySide6'])
        self.assertIn('PySide6', qa.dependency_summary(deps, system='Windows', source_only=True)['missing'])

    def test_source_only_command_runs_no_native_stages_and_reports_not_run(self):
        def fake_run(name, command, output):
            self.assertEqual(name, 'unittest')
            qa.write_json(output / 'unittest-evidence.json', {'status': 'PASS', 'discovered': 1, 'passed': 1})
            return {'status': 'PASS', 'returncode': 0}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dom = root / 'dom.mjs'; dom.write_text('existing dependency')
            with patch.dict(os.environ, {'AIO_DOM_MODULE': str(dom)}), \
                 patch.object(qa.platform, 'system', return_value='Linux'), \
                 patch.object(qa.importlib.util, 'find_spec', side_effect=lambda name: None if name == 'PySide6' else object()), \
                 patch.object(qa.shutil, 'which', return_value='/existing/node'), \
                 patch.object(qa, 'corpus_candidates', return_value=[]), \
                 patch.object(qa, 'bundled_corpus', return_value=None), \
                 patch.object(qa, 'run_command', side_effect=fake_run) as run, \
                 patch.object(qa, 'emit_summary') as emit:
                self.assertEqual(qa.main(['--source-only', '--output', str(root / 'out')]), 0)
                self.assertEqual(run.call_count, 1)
                evidence = emit.call_args.args[0]
                self.assertEqual(evidence['scope'], 'source_only')
                self.assertEqual(evidence['native']['status'], 'NOT_RUN')
                self.assertFalse(any(name.startswith('native') for name in evidence['stages']))

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
        for args in ({'name': '../escape.odt'}, {'name': 'C:escape.odt'}, {'extra': True}):
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
