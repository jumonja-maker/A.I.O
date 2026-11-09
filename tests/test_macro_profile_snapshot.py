import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.qa.completion_macro_native import read_macro_profile_snapshot


class OwnedMacroProfileSnapshot(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name)/'registrymodifications.xcu'
        self.raw = (b'<oor:items xmlns:oor="http://openoffice.org/2001/registry">'
                    b'<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
                    b'<prop oor:name="DisableMacrosExecution"><value>true</value></prop>'
                    b'</item></oor:items>')
        self.path.write_bytes(self.raw)

    def test_transient_native_sharing_lock_is_retried_until_verified(self):
        original = Path.read_bytes
        calls = []

        def read(path):
            calls.append(path)
            if len(calls) == 1:
                raise PermissionError(13, 'simulated native sharing lock')
            return original(path)

        with patch.object(Path, 'read_bytes', autospec=True, side_effect=read):
            actual = read_macro_profile_snapshot(self.path, interval=0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(hashlib.sha256(actual).digest(), hashlib.sha256(self.raw).digest())

    def test_persistent_unreadable_profile_is_not_security_pass(self):
        with patch.object(Path, 'read_bytes', side_effect=PermissionError(13, 'persistent lock')) as read:
            with self.assertRaisesRegex(AssertionError, 'native_profile_snapshot_unavailable'):
                read_macro_profile_snapshot(self.path, interval=0, max_attempts=3)
        self.assertEqual(read.call_count, 3)

    def test_missing_profile_is_not_accepted_as_missing_optional_metadata(self):
        self.path.unlink()
        with self.assertRaisesRegex(AssertionError, 'native_profile_snapshot_unavailable'):
            read_macro_profile_snapshot(self.path, timeout=0)

    def test_stable_disabled_false_fails_without_waiting_for_a_true_snapshot(self):
        self.path.write_bytes(self.raw.replace(b'>true<', b'>false<'))
        with patch.object(Path, 'read_bytes', autospec=True, wraps=Path.read_bytes) as read:
            # Reading an actual disabled=false file must fail, rather than
            # treating a successful read as sufficient profile evidence.
            read.side_effect = lambda path: self.raw.replace(b'>true<', b'>false<')
            with self.assertRaisesRegex(AssertionError, 'native_profile_macros_not_disabled'):
                read_macro_profile_snapshot(self.path, interval=0)
        self.assertEqual(read.call_count, 1)

    def test_atomic_native_replacement_does_not_return_the_old_snapshot(self):
        original = Path.read_bytes
        changed = self.raw.replace(b'</item>', b'<prop name="extra"/></item>')
        calls = []

        def read(path):
            calls.append(path)
            old = original(path)
            if len(calls) == 1:
                temporary = path.with_suffix('.new')
                temporary.write_bytes(changed)
                temporary.replace(path)
            return old

        with patch.object(Path, 'read_bytes', autospec=True, side_effect=read):
            actual = read_macro_profile_snapshot(self.path, interval=0)
        self.assertEqual(actual, changed)
        self.assertEqual(len(calls), 2)

    def test_temporary_partial_xml_is_not_counted_as_a_valid_snapshot(self):
        original = Path.read_bytes
        calls = []

        def read(path):
            calls.append(path)
            return b'<' * len(self.raw) if len(calls) == 1 else original(path)

        with patch.object(Path, 'read_bytes', autospec=True, side_effect=read):
            actual = read_macro_profile_snapshot(self.path, interval=0)
        self.assertEqual(actual, self.raw)
        self.assertEqual(len(calls), 2)


if __name__ == '__main__':
    unittest.main()
