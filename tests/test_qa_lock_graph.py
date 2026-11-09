"""A10 lock graph: validate before pip, never attest unverified wheel hashes."""
import hashlib
import json
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch

HELPER = Path(__file__).resolve().parents[1] / 'tools/build/verify_qa_dependency_lock.py'


class QaLockGraph(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.helper = runpy.run_path(str(HELPER))
        self.read = self.helper['read_lock_graph']

    def file(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def test_nested_and_diamond_includes_are_read_and_hashed(self):
        top = self.file('top.txt', '-r sub/first.txt\n-r second.txt\nTop==2 # reviewed\n')
        first = self.file('sub/first.txt', '-r ../common.txt\nFirst==1\n')
        second = self.file('second.txt', '-r common.txt\nSecond==3\n')
        common = self.file('common.txt', 'Core_Pkg==4\n')
        deps, hashes = self.read((top,))
        self.assertEqual(set(deps), {'top', 'first', 'second', 'core-pkg'})
        for path in (top, first, second, common):
            self.assertEqual(hashes[path.relative_to(self.root).as_posix()],
                             hashlib.sha256(path.read_bytes()).hexdigest())
        common.write_text('Core_Pkg==5\n', encoding='utf-8')
        changed, changed_hashes = self.read((top,))
        self.assertEqual(changed['core-pkg'][1], '5')
        self.assertNotEqual(changed_hashes['common.txt'], hashes['common.txt'])

    def test_canonical_name_conflict_across_entrypoints_is_rejected(self):
        runtime = self.file('runtime.txt', 'My__Pkg==1\n')
        build = self.file('build.txt', 'my.-pkg==2\n')
        with self.assertRaisesRegex(ValueError, 'Conflicting'):
            self.read((runtime, build))
        build.write_text('my.-pkg==1\n', encoding='utf-8')
        self.assertEqual(len(self.read((runtime, build))[0]), 1)

    def test_cycles_are_rejected(self):
        a = self.file('a.txt', '-r b.txt\n')
        self.file('b.txt', '-r a.txt\n')
        with self.assertRaisesRegex(ValueError, 'Cyclic'):
            self.read((a,))

    def test_missing_and_escaping_includes_fail_closed(self):
        top = self.file('safe/top.txt', '-r missing.txt\n')
        with self.assertRaises(FileNotFoundError):
            self.read((top,))
        self.file('outside.txt', 'Package==1\n')
        top.write_text('-r ../outside.txt\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'escapes'):
            self.read((top,))

    def test_unsupported_pip_syntax_and_remote_includes_are_rejected(self):
        lines = ['Package>=1', 'Package==1; python_version > "3"',
                 '-r https://example.invalid/lock.txt', '-r /tmp/lock.txt',
                 '-r C:/temp/lock.txt', '-r ${LOCK}', '--extra-index-url https://example.invalid',
                 '-c constraints.txt', '--requirement file.txt', '-e .', 'Package==1 --hash=sha256:abc']
        for line in lines:
            with self.subTest(line=line):
                top = self.file('top.txt', line + '\n')
                with self.assertRaises(ValueError):
                    self.read((top,))

    def test_graph_and_file_size_limits_are_enforced(self):
        top = self.file('top.txt', 'Package==1\n')
        globals_ = self.read.__globals__
        with patch.dict(globals_, MAX_LOCK_BYTES=3):
            with self.assertRaisesRegex(ValueError, 'too large'):
                self.read((top,))
        top.write_text('-r next.txt\n', encoding='utf-8')
        self.file('next.txt', 'Package==1\n')
        with patch.dict(globals_, MAX_LOCK_FILES=1):
            with self.assertRaisesRegex(ValueError, 'Too many'):
                self.read((top,))

    def test_check_only_does_not_read_installed_packages_or_write_manifest(self):
        runtime = self.file('runtime.txt', 'Package==1\n')
        build = self.file('build.txt', '-r runtime.txt\nBuilder==2\n')
        main = self.helper['main']
        destination = self.root / 'manifest.json'
        with patch.dict(main.__globals__, LOCK=runtime, BUILD_LOCK=build, MANIFEST=destination), \
                patch('sys.argv', ['check', '--phase', 'runtime', '--check-locks']), \
                patch.object(self.helper['metadata'], 'version', side_effect=AssertionError('not installed QA')), \
                patch('builtins.print'):
            main()
        self.assertFalse(destination.exists())
        build.write_text('Package==2\n', encoding='utf-8')
        with patch.dict(main.__globals__, LOCK=runtime, BUILD_LOCK=build), \
                patch('sys.argv', ['check', '--phase', 'runtime', '--check-locks']):
            with self.assertRaisesRegex(ValueError, 'Conflicting'):
                main()

    def test_build_manifest_keeps_version_and_hash_claims_distinct(self):
        runtime = self.file('runtime.txt', 'Package==1\n')
        build = self.file('build.txt', '-r runtime.txt\nBuilder==2\n')
        pin = self.file('pin.json', json.dumps({'version': 'test', 'sha256': 'a' * 64}))
        destination = self.root / 'manifest.json'
        main = self.helper['main']
        with patch.dict(main.__globals__, LOCK=runtime, BUILD_LOCK=build, PIN=pin, MANIFEST=destination), \
                patch('sys.argv', ['check', '--phase', 'build']), \
                patch('sys.version_info', (3, 11, 0)), \
                patch.dict('os.environ', AIO_QA_SOURCE_SHA='b' * 40), \
                patch.object(self.helper['metadata'], 'version', side_effect=lambda n: {'Package':'1', 'Builder':'2'}[n]), \
                patch('builtins.print'):
            main()
        manifest = json.loads(destination.read_text(encoding='utf-8'))
        self.assertFalse(manifest['wheelHashesVerified'])
        self.assertEqual(manifest['sourceRevision'], 'b' * 40)
        self.assertEqual(set(manifest['resolvedLockSha256']), {'runtime.txt', 'build.txt'})
        self.assertEqual(manifest['packages'], {'builder':'2', 'package':'1'})
