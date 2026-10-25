"""Frozen acceptance must require process output, not only a successful exit."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

from tools.qa.guard_v19_frozen_acceptance import read_outcomes, run_owned, worker_command


class FrozenWorkerAcceptance(unittest.TestCase):
    def test_command_uses_existing_frozen_dispatch_with_literal_path_arguments(self):
        executable, request = Path('private folder/A.I.O.exe'), Path('request folder/request.json')
        self.assertEqual(worker_command(executable, request),
                         [str(executable), '--aio-guard-comparison', str(request)])
        self.assertEqual(worker_command(executable, request, source_worker=True),
                         [str(executable), '-m', 'app_backend.converter_guard.comparison_worker', str(request)])

    def test_real_zero_exit_without_response_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(AssertionError, 'did not create expected bounded response'):
                run_owned([sys.executable, '-c', 'pass'], Path(directory), 'outcomes.jsonl', 5)

    def test_real_hung_child_is_stopped_by_owned_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            started = time.monotonic()
            with self.assertRaisesRegex(TimeoutError, 'exceeded its acceptance deadline'):
                run_owned([sys.executable, '-c', 'import time; time.sleep(30)'],
                          Path(directory), 'outcomes.jsonl', .15)
            self.assertLess(time.monotonic() - started, 8)

    def test_partial_duplicate_or_foreign_jsonl_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'outcomes.jsonl'
            row = {'taskId': 'expected', 'outcome': {'status': 'checked'}}
            path.write_text(json.dumps(row), encoding='utf-8')
            with self.assertRaisesRegex(AssertionError, 'incomplete final record'):
                read_outcomes(path, ['expected'])
            path.write_text((json.dumps(row) + '\n') * 2, encoding='utf-8')
            with self.assertRaisesRegex(AssertionError, 'task count'):
                read_outcomes(path, ['expected'])
            path.write_text(json.dumps({**row, 'taskId': 'foreign'}) + '\n', encoding='utf-8')
            with self.assertRaisesRegex(AssertionError, 'missing, duplicate or foreign'):
                read_outcomes(path, ['expected'])
