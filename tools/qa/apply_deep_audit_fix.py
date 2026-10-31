"""One-shot guarded fixture correction for the already committed audit 101 fix.

This file is a temporary source workbench, never imported by the application.
All original assertions remain. Real owned files replace obsolete fake paths.
"""
import ast
import os
from pathlib import Path
import subprocess

if os.environ.get('GITHUB_REF') != 'refs/heads/fix/deep-audit-d01-d09-20261031':
    raise RuntimeError('Refusing to edit outside the isolated audit branch')

expected = {
    'tests/test_code_audit_regressions.py': '64ac2dbaa7637df4535ecaf93483c39733987a7b',
    'tests/test_guard_complete_patch_v14.py': '34f16318b14a621f29e4b12b25113cb1f0e4ca72',
    'tests/test_legacy_durable_recovery.py': 'a8e973cc6957e4d202aa05d4978688d165f9ab8e',
    'tests/test_legacy_storage_budget.py': 'cb2edbd8f61e754c523135e871d2f46c24857e7a',
}
changes = {}
for path, wanted in expected.items():
    actual = subprocess.check_output(['git', 'hash-object', path], text=True).strip()
    if actual != wanted:
        raise RuntimeError('Fixture changed concurrently: ' + path)
    changes[path] = Path(path).read_text(encoding='utf-8')


def edit(path, before, after):
    value = changes[path]
    if value.count(before) != 1:
        raise RuntimeError('Fixture anchor mismatch: ' + path + ': ' + repr(before[:100]))
    changes[path] = value.replace(before, after)


p = 'tests/test_code_audit_regressions.py'
edit(p, "        temp = FailingCleanup()\n        transport.results['ticket'] = Result(temp, Path('unused'), 'output.pdf',\n                                             'application/pdf')", """        backing = tempfile.TemporaryDirectory()
        self.addCleanup(backing.cleanup)
        self.addCleanup(transport.close)
        temp = FailingCleanup()
        temp.name = str(Path(backing.name).resolve())
        output = Path(temp.name) / 'output.pdf'
        output.write_bytes(b'owned result')
        transport.results['ticket'] = Result(temp, output, 'output.pdf',
                                             'application/pdf', size=output.stat().st_size)""")

p = 'tests/test_guard_complete_patch_v14.py'
edit(p, 'class DirectCleanupConcurrency(unittest.TestCase):\n', """class DirectCleanupConcurrency(unittest.TestCase):
    def owned_result(self, owner):
        # The cleanup behavior is controlled, but the retained file identity
        # is real: production must never accept a made-up path as saveable.
        backing = tempfile.TemporaryDirectory()
        self.addCleanup(backing.cleanup)
        owner.name = str(Path(backing.name).resolve())
        path = Path(owner.name) / 'file.pdf'
        path.write_bytes(b'owned result')
        return Result(owner, path, path.name, 'application/pdf', size=path.stat().st_size)

""")
edit(p, "Result(Busy(),Path('unused'),'file.pdf','application/pdf')", 'self.owned_result(Busy())')
edit(p, "Result(temporary,Path('unused'),'file.pdf','application/pdf')", 'self.owned_result(temporary)')
edit(p, "Result(temporary,Path('unused'),'f.pdf','application/pdf')", 'self.owned_result(temporary)')

p = 'tests/test_legacy_durable_recovery.py'
edit(p, """def retained(transport, identity='a' * 32):
    request = transport.begin(identity, None)
    with patch('app_backend.converter_transport.convert_path', side_effect=convert_fixture):
        value = transport.execute(request, [{'path': Path('source.txt'), 'warnings': []}], ['file'], ['pdf'])
""", """def retained(transport, identity='a' * 32, *, payload=None, output_format='pdf'):
    request = transport.begin(identity, None)
    def convert(source, target, workspace, **kwargs):
        outputs = convert_fixture(source, target, workspace, **kwargs)
        if payload is not None:
            outputs[0].write_bytes(payload)
        return outputs
    # The fixture bytes are final BEFORE publication and identity capture.
    with patch('app_backend.converter_transport.convert_path', side_effect=convert):
        value = transport.execute(request, [{'path': Path('source.txt'), 'warnings': []}], ['file'], [output_format])
""")
edit(p, """        _, ticket = retained(transport)
        result = transport.results[ticket]
        result.path.write_bytes(b'a' * (64 * 1024 + 17))
""", """        _, ticket = retained(transport, payload=b'a' * (64 * 1024 + 17), output_format='txt')
        result = transport.results[ticket]
""")
edit(p, """        _, ticket = retained(transport)
        result = transport.results[ticket]
        result.path.write_bytes(b'')
""", """        _, ticket = retained(transport, payload=b'', output_format='txt')
        result = transport.results[ticket]
""")

p = 'tests/test_legacy_storage_budget.py'
edit(p, "patch.object(Path, 'lstat', return_value=info)", "patch('app_backend.legacy_storage_budget.os.lstat', return_value=info)")
edit(p, '    def test_inspection_entry_limit_fails_instead_of_under_counting(self):', """    def test_child_entry_links_and_reparse_points_are_not_traversed(self):
        # Check the optimized DirEntry branch, not just the root lstat path.
        from unittest.mock import MagicMock
        self.parent.mkdir()
        for info in (SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=0),
                     SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)):
            entry = MagicMock()
            entry.path = str(self.parent / 'untrusted-child')
            entry.stat.return_value = info
            listing = MagicMock()
            listing.__enter__.return_value = iter([entry])
            with self.subTest(info=info), patch('app_backend.legacy_storage_budget.os.scandir',
                                               return_value=listing) as scan:
                with self.assertRaises(OSError):
                    measured_bytes([self.parent])
                scan.assert_called_once_with(str(self.parent))
                entry.stat.assert_called_once_with(follow_symlinks=False)

    def test_inspection_entry_limit_fails_instead_of_under_counting(self):""")

# Validate the entire edited files before changing any of them.
for path, value in changes.items():
    ast.parse(value, filename=path)
for path, value in changes.items():
    Path(path).write_text(value, encoding='utf-8', newline='\n')
    print('AUDIT101_FIXTURE_UPDATED=' + path)
print('PRODUCT_IDENTITY_GUARDS_UNCHANGED=1;TESTS_SKIPPED=0;ASSERTIONS_REMOVED=0')
