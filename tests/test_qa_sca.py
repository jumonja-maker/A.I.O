"""A10: unavailable, truncated, stale and vulnerable scans cannot pass."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.build import qa_sca as qa


class PythonSca(unittest.TestCase):
    def setUp(self):
        self.queries = [{'package': {'name': 'pillow', 'ecosystem': 'PyPI'}, 'version': '13.0.0'}]
        self.hashes = {'pins.txt': 'b' * 64}
        self.patcher = patch.object(qa, 'queries_for', return_value=(self.queries, self.hashes))
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def audit(self, response):
        return qa.audit('build', 'a' * 40, fetch=lambda queries: response)

    def test_complete_empty_response_passes_only_declared_scope(self):
        value = self.audit({'results': [{}]})
        self.assertEqual(value['status'], 'PASS')
        self.assertEqual(value['matchingRecords'], 0)
        self.assertIn('vendored/native/system components not audited', value['scope'])

    def test_any_matching_advisory_blocks_and_record_count_is_not_distinct_count(self):
        value = self.audit({'results': [{'vulns': [{'id': 'GHSA-test-1'}, {'id': 'GHSA-test-1'}]}]})
        self.assertEqual(value['status'], 'FINDINGS')
        self.assertEqual(value['matchingRecords'], 2)
        self.assertEqual(value['advisoryIds'], ['GHSA-test-1'])

    def test_network_failure_is_incomplete(self):
        def failed(queries):
            raise TimeoutError('provider unavailable')
        value = qa.audit('build', 'a' * 40, fetch=failed)
        self.assertEqual(value['status'], 'INCOMPLETE')
        self.assertIn('TimeoutError', value['error'])

    def test_missing_invalid_and_paginated_responses_cannot_pass(self):
        responses = [{}, {'results': []}, {'results': [None]}, {'results': [{'error': 'failure'}]},
                     {'results': [{'next_page_token': 'more'}]}, {'results': [{'vulns': None}]},
                     {'results': [{'vulns': [{'id': '../unsafe'}]}]}]
        for response in responses:
            with self.subTest(response=response):
                self.assertEqual(self.audit(response)['status'], 'INCOMPLETE')

    def test_report_requires_current_source_and_exact_dependencies(self):
        value = self.audit({'results': [{}]})
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'audit.json'
            path.write_text(json.dumps(value))
            self.assertEqual(qa.verify_report(path, 'build', 'a' * 40), value)
            with self.assertRaises(ValueError):
                qa.verify_report(path, 'build', 'c' * 40)
            changed = deepcopy(value)
            changed['queries'][0]['version'] = '12.3.0'
            path.write_text(json.dumps(changed))
            with self.assertRaises(ValueError):
                qa.verify_report(path, 'build', 'a' * 40)

    def test_changing_status_cannot_hide_vulnerabilities(self):
        value = self.audit({'results': [{'vulns': [{'id': 'GHSA-test-1'}]}]})
        value.update(status='PASS', matchingRecords=0, advisoryIds=[], records=[])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'audit.json'
            path.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                qa.verify_report(path, 'build', 'a' * 40)

    def test_exact_revision_is_required_before_network(self):
        with self.assertRaisesRegex(ValueError, 'exact source SHA'):
            qa.audit('build', 'main', fetch=lambda queries: self.fail('no network'))


if __name__ == '__main__':
    unittest.main()
