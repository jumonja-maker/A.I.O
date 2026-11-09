"""Source/provenance and uninstall controls; no installer execution is claimed."""
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.build import build_completion_installer as builder
from tools.build.qa_sca import audit
from tools.build.verify_qa_dependency_lock import profile_lock

ROOT = Path(__file__).resolve().parents[1]
SHA = 'a' * 40


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding='utf-8')
    return hashlib.sha256(path.read_bytes()).hexdigest()


class InstallerPayloadControls(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.payload = self.root / 'payload'
        self.payload.mkdir()
        for relative in ('A.I.O.exe', '_internal/libreoffice/program/soffice.exe',
                         '_internal/libreoffice/program/soffice.com',
                         '_internal/libreoffice/program/soffice.bin',
                         '_internal/libreoffice/program/python.exe',
                         '_internal/libreoffice/share/registry/main.xcd',
                         '_internal/libreoffice/LICENSE.txt'):
            path = self.payload / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'static fake fixture; never executed\n')
        self.info = self.payload / '_internal/build-info'
        self.info.mkdir()
        (self.info / 'aio-source-revision.txt').write_text(SHA + '\n', encoding='ascii')
        pin_path = ROOT / 'tools/build/libreoffice-release.json'
        (self.info / pin_path.name).write_bytes(pin_path.read_bytes())
        pin = json.loads(pin_path.read_text())
        self.sbom = self.info / 'aio-qa-sbom-build.cdx.json'
        self.sca = self.info / 'aio-qa-sca-build.json'
        sbom_hash = write_json(self.sbom, {'metadata': {'component': {'version': SHA}}})
        sca_hash = write_json(self.sca, audit('build', SHA,
            fetch=lambda queries: {'results': [{} for _ in queries]}))
        expected, hashes = profile_lock('build')
        self.manifest = {'schema': 1, 'phase': 'build', 'platform': 'win32', 'sourceRevision': SHA,
            'wheelHashesVerified': True, 'scaStatus': 'pass_python_distribution_scope',
            'packages': {name: version for name, (_, version) in expected.items()},
            'resolvedLockSha256': hashes,
            'wheelLockSha256': builder.digest(ROOT / 'tools/build/qa-wheel-hashes.json'),
            'libreOfficeVersion': pin['version'], 'libreOfficeMsiSha256': pin['sha256'],
            'sbomFile': self.sbom.name, 'sbomSha256': sbom_hash,
            'scaReportFile': self.sca.name, 'scaReportSha256': sca_hash}
        self.save_manifest()

    def save_manifest(self):
        write_json(self.info / 'aio-qa-dependencies.json', self.manifest)

    def test_static_fake_payload_has_complete_hashed_inventory(self):
        result = builder.validate_payload(self.payload, SHA)
        expected = {p.relative_to(self.payload).as_posix() for p in self.payload.rglob('*') if p.is_file()}
        self.assertEqual({r['path'] for r in result['files']}, expected)
        self.assertEqual(result['fileCount'], len(expected))
        for row in result['files']:
            path = self.payload / row['path']
            self.assertEqual(row['sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(row['size'], path.stat().st_size)

    def test_wrong_source_unverified_wheels_or_stale_engine_refused(self):
        mutations = [('sourceRevision', 'b' * 40), ('wheelHashesVerified', False),
                     ('libreOfficeVersion', '0.0'), ('libreOfficeMsiSha256', '0' * 64),
                     ('platform', 'linux'), ('phase', 'runtime')]
        for key, value in mutations:
            with self.subTest(key=key):
                before = self.manifest[key]
                self.manifest[key] = value
                self.save_manifest()
                with self.assertRaises(ValueError):
                    builder.validate_payload(self.payload, SHA)
                self.manifest[key] = before
        self.save_manifest()
        (self.info / 'aio-source-revision.txt').write_text('b' * 40)
        with self.assertRaisesRegex(ValueError, 'Embedded source revision'):
            builder.validate_payload(self.payload, SHA)

    def test_changed_lock_packages_or_manifest_paths_refused(self):
        for key, value in [('packages', {}), ('resolvedLockSha256', {}),
                           ('wheelLockSha256', '0' * 64), ('sbomFile', '../escape.json')]:
            with self.subTest(key=key):
                before = self.manifest[key]
                self.manifest[key] = value
                self.save_manifest()
                with self.assertRaises(ValueError):
                    builder.validate_payload(self.payload, SHA)
                self.manifest[key] = before

    def test_changed_sbom_bytes_and_rehashed_wrong_source_refused(self):
        original = self.sbom.read_bytes()
        self.sbom.write_bytes(original + b' ')
        with self.assertRaisesRegex(ValueError, 'SBOM/SCA digest'):
            builder.validate_payload(self.payload, SHA)
        self.manifest['sbomSha256'] = write_json(self.sbom,
            {'metadata': {'component': {'version': 'b' * 40}}})
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, 'SBOM is not bound'):
            builder.validate_payload(self.payload, SHA)

    def test_rehashed_sca_for_other_source_or_with_findings_refused(self):
        for report in (audit('build', 'b' * 40, fetch=lambda q: {'results': [{} for _ in q]}),
                       audit('build', SHA, fetch=lambda q: {'results': [
                           {'vulns': [{'id': 'TEST-ADVISORY-1'}]} for _ in q]})):
            with self.subTest(source=report['sourceRevision'], status=report['status']):
                self.manifest['scaReportSha256'] = write_json(self.sca, report)
                self.save_manifest()
                with self.assertRaises(ValueError):
                    builder.validate_payload(self.payload, SHA)

    def test_incomplete_libreoffice_refused(self):
        path = self.payload / '_internal/libreoffice/program/python.exe'
        path.unlink()
        with self.assertRaisesRegex(ValueError, 'Complete executable/LibreOffice'):
            builder.validate_payload(self.payload, SHA)

    def test_payload_symlink_and_reparse_points_refused(self):
        external = self.root / 'external.txt'
        external.write_text('outside payload')
        alias = self.payload / 'alias.txt'
        if os.name == 'nt':
            # This tests the refusal branch without requiring Developer Mode
            # or a symlink privilege. Real Windows junction handling has a
            # separate native quota test; do not label this a native link run.
            with patch.object(Path, 'lstat', return_value=SimpleNamespace(
                    st_mode=stat.S_IFLNK | 0o777, st_file_attributes=0)):
                with self.assertRaisesRegex(ValueError, 'Symlink/reparse'):
                    builder.inventory(self.payload)
        else:
            alias.symlink_to(external)
            with self.assertRaisesRegex(ValueError, 'Symlink/reparse'):
                builder.inventory(self.payload)
            alias.unlink()
        with patch.object(Path, 'lstat', return_value=SimpleNamespace(
                st_mode=stat.S_IFDIR | 0o755, st_file_attributes=0x400)):
            with self.assertRaisesRegex(ValueError, 'Symlink/reparse'):
                builder.checked_path(self.payload)

    def test_reserved_receipt_name_and_unsafe_path_refused(self):
        (self.payload / builder.INSTALLED_MANIFEST).write_text('{}')
        with self.assertRaisesRegex(ValueError, 'reserved installed manifest'):
            builder.inventory(self.payload)
        with self.assertRaisesRegex(ValueError, 'represented safely'):
            builder.checked_path(self.root / 'unsafe"definition', must_exist=False)

    def test_validate_only_never_compiles_or_claims_installer(self):
        compiler = self.root / 'fake-ISCC.exe'
        compiler.write_bytes(b'not executed')
        output = self.root / 'validation'
        identity = {'sourceRevision': SHA, 'sourceTree': 'b' * 40, 'trackedTreeClean': True}
        with patch.object(builder, 'source_identity', return_value=identity), \
             patch.object(builder, 'compiler_identity', return_value={
                 'path': str(compiler), 'sha256': builder.digest(compiler), 'pinStatus': 'test fixture'}), \
             patch.object(builder.subprocess, 'run') as execute:
            result = builder.build(iscc=compiler, payload=self.payload, output=output,
                                   source_sha=SHA, validate_only=True)
            execute.assert_not_called()
        self.assertEqual(result['status'], 'VALIDATED_NOT_COMPILED')
        self.assertIsNone(result['installer'])
        self.assertEqual(json.loads((output / 'installer-build.json').read_text()), result)
        installed = json.loads((output / builder.INSTALLED_MANIFEST).read_text())
        self.assertEqual(installed['payload']['files'], result['payload']['files'])
        self.assertEqual(installed['sourceRevision'], SHA)
        self.assertNotIn('installer', installed)
        self.assertFalse(list(output.glob('*.exe')))

    def test_post_compile_guards_reject_mutated_receipt_compiler_and_payload(self):
        # A controlled compiler boundary writes inert bytes, not a real EXE.
        # These cases verify that a successful child exit cannot hide changes
        # to the exact receipt or inputs consumed by the external compiler.
        compiler = self.root / 'fake-ISCC.exe'
        compiler.write_bytes(b'inert compiler fixture')
        program = self.payload / 'A.I.O.exe'
        original_compiler, original_program = compiler.read_bytes(), program.read_bytes()
        identity = {'sourceRevision': SHA, 'sourceTree': 'b' * 40, 'trackedTreeClean': True}
        for mutation in ('receipt', 'compiler', 'payload'):
            with self.subTest(mutation=mutation):
                output = self.root / ('compile-' + mutation)
                def compile_fixture(command, **kwargs):
                    self.assertEqual(command[0], str(compiler))
                    self.assertIn('/DAIOSourceSHA=' + SHA, command)
                    (output / ('AIO-Installed-Acceptance-' + SHA[:12] + '.exe')).write_bytes(b'MZ inert fixture')
                    changed = {'receipt': output / builder.INSTALLED_MANIFEST,
                               'compiler': compiler, 'payload': program}[mutation]
                    changed.write_bytes(changed.read_bytes() + b'changed after child success')
                with patch.object(builder, 'source_identity', return_value=identity), \
                     patch.object(builder, 'compiler_identity', return_value={
                         'path': str(compiler), 'sha256': builder.digest(compiler), 'pinStatus': 'test fixture'}), \
                     patch.object(builder.sys, 'platform', 'win32'), \
                     patch.object(builder.subprocess, 'run', side_effect=compile_fixture):
                    with self.assertRaisesRegex(ValueError, 'changed during compilation'):
                        builder.build(iscc=compiler, payload=self.payload, output=output,
                                      source_sha=SHA)
                result = json.loads((output / 'installer-build.json').read_text())
                self.assertEqual(result['status'], 'FAIL')
                self.assertIsNone(result['installer'])
                compiler.write_bytes(original_compiler)
                program.write_bytes(original_program)


class InstallerSourceAndRemovalControls(unittest.TestCase):
    def test_real_git_head_and_dirty_tracked_tree_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args):
                return subprocess.run(['git', *args], cwd=root, check=True,
                    capture_output=True, text=True).stdout.strip()
            git('init', '-q')
            (root / 'source.txt').write_text('reviewed')
            git('add', 'source.txt')
            git('-c', 'user.name=Disposable test', '-c', 'user.email=test@example.invalid',
                'commit', '-qm', 'owned fixture')
            sha = git('rev-parse', 'HEAD')
            self.assertEqual(builder.source_identity(sha, root=root)['sourceRevision'], sha)
            (root / 'generated-build-output.json').write_text('{}')
            self.assertTrue(builder.source_identity(sha, root=root)['trackedTreeClean'])
            with self.assertRaisesRegex(ValueError, 'HEAD differs'):
                builder.source_identity('a' * 40, root=root)
            (root / 'source.txt').write_text('changed')
            with self.assertRaisesRegex(ValueError, 'tree is dirty'):
                builder.source_identity(sha, root=root)

    def test_acceptance_installer_uses_unique_id_and_logged_cleanup_only(self):
        text = builder.ISS.read_text(encoding='utf-8-sig')
        self.assertIn('AppId={' + builder.APP_ID, text)
        self.assertIn('PrivilegesRequired=lowest', text)
        self.assertIn('UsePreviousAppDir=no', text)
        self.assertIn('SolidCompression=no', text)
        self.assertNotIn('[UninstallDelete]', text)
        self.assertNotIn('[UninstallRun]', text)
        self.assertNotIn('[Run]', text)
        self.assertIn('DestName: "AIO-install-source.json"', text)
        for path in (ROOT / 'installer/windows').glob('*.iss'):
            if path != builder.ISS:
                self.assertNotIn(builder.APP_ID.strip('{}'), path.read_text(encoding='utf-8-sig'))


if __name__ == '__main__':
    unittest.main()
