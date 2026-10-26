"""An opt-in analysis memory budget must not constrain native conversions."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from app_backend.native_process import ProcessTree

MIB = 1024 * 1024


class ComparisonMemoryBudget(unittest.TestCase):
    def test_invalid_or_unrepresentable_limits_fail_before_spawning(self):
        for value in (True, False, 0, -1, 16 * MIB - 1, '768MiB', 96.0 * MIB, [], sys.maxsize + 1):
            with self.subTest(value=value), patch('app_backend.native_process.subprocess.Popen') as spawn:
                with self.assertRaises(ValueError):
                    ProcessTree([sys.executable, '-c', 'pass'], cwd=Path.cwd(), memory_limit_bytes=value)
                spawn.assert_not_called()

    def test_real_child_allocation_is_rejected_by_opt_in_budget(self):
        limit = 96 * MIB
        # Windows enforces the Job Object's commit limit. POSIX deliberately
        # installs RLIMIT_DATA inside the disposable child, never in preexec_fn
        # or in the multithreaded host. The gate avoids allocation racing with
        # Windows AssignProcessToJobObject.
        prefix = ('' if os.name == 'nt' else
                  'import resource\nresource.setrlimit(resource.RLIMIT_DATA, (' + str(limit) + ', ' + str(limit) + '))\n')
        code = (prefix + 'import sys\nsys.stdin.buffer.read(1)\n'
                'try:\n payload = bytearray(256 * 1024 * 1024)\n'
                'except MemoryError:\n print("BUDGET_ENFORCED", flush=True)\n'
                'else:\n print("BUDGET_NOT_ENFORCED", flush=True)\n sys.exit(1)\n')
        previous = None
        if os.name != 'nt':
            import resource
            previous = resource.getrlimit(resource.RLIMIT_DATA)
        with tempfile.TemporaryDirectory() as folder:
            with ProcessTree([sys.executable, '-c', code], cwd=Path(folder), memory_limit_bytes=limit,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as tree:
                output, errors = tree.process.communicate(input=b'G', timeout=10)
                self.assertEqual(tree.process.returncode, 0, errors.decode(errors='replace'))
                self.assertEqual(output.strip(), b'BUDGET_ENFORCED')
        if previous is not None:
            self.assertEqual(resource.getrlimit(resource.RLIMIT_DATA), previous,
                             'The host must retain its original memory policy')

    def test_real_default_child_can_allocate_more_than_the_analysis_test_budget(self):
        code = ('payload = bytearray(128 * 1024 * 1024)\n'
                'payload[0] = 1\npayload[-1] = 2\n'
                'print(len(payload), payload[0] + payload[-1], flush=True)\n')
        with tempfile.TemporaryDirectory() as folder:
            with ProcessTree([sys.executable, '-c', code], cwd=Path(folder),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE) as tree:
                output, errors = tree.process.communicate(timeout=10)
                self.assertIsNone(tree.memory_limit_bytes)
                self.assertEqual(tree.process.returncode, 0, errors.decode(errors='replace'))
                self.assertEqual(output.strip(), b'134217728 3')
