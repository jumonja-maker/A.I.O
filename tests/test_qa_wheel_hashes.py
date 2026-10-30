"""A10: byte provenance, consumed pip inputs and SBOM must agree."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
from shutil import copyfile
import subprocess
import sys
import unittest
from unittest.mock import patch
import zipfile

from tools.build import qa_wheels as qa


class WheelEvidence(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / 'wheels'
        self.folder.mkdir()
        self.wheel = self.folder / 'example_pkg-1-py3-none-any.whl'
        self.make_wheel()
        self.selected = {'example-pkg': {'name': 'Example_Pkg', 'version': '1',
                         'file': self.wheel.name, 'sha256': qa.sha256(self.wheel)}}

    def make_wheel(self, name='Example_Pkg', version='1'):
        with zipfile.ZipFile(self.wheel, 'w') as archive:
            archive.writestr('example_pkg-1.dist-info/METADATA',
                             'Metadata-Version: 2.1\nName: ' + name + '\nVersion: ' + version + '\n')

    def report(self, entries=None):
        entry = {'metadata': {'name': 'Example_Pkg', 'version': '1'},
                 'download_info': {'url': self.wheel.resolve().as_uri(),
                    'archive_info': {'hashes': {'sha256': qa.sha256(self.wheel)}}}}
        path = self.root / 'pip.json'
        path.write_text(json.dumps({'version': '1', 'install': entries if entries is not None else [entry]}))
        return path, entry

    def test_real_wheel_bytes_and_metadata_produce_exact_evidence(self):
        evidence = qa.verify_wheelhouse(self.folder, self.selected)
        self.assertEqual(evidence, [{'package': 'example-pkg', 'version': '1',
            'file': self.wheel.name, 'sha256': qa.sha256(self.wheel), 'bytes': self.wheel.stat().st_size}])

    def test_one_byte_corruption_is_detected_before_installation(self):
        with self.wheel.open('ab') as stream:
            stream.write(b'changed')
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            qa.verify_wheelhouse(self.folder, self.selected)

    def test_missing_extra_and_nonregular_files_are_rejected(self):
        extra = self.folder / 'unknown.whl'
        extra.write_bytes(b'unknown')
        with self.assertRaisesRegex(ValueError, 'inventory'):
            qa.verify_wheelhouse(self.folder, self.selected)
        extra.unlink()
        self.wheel.unlink()
        self.wheel.mkdir()
        with self.assertRaises(ValueError):
            qa.verify_wheelhouse(self.folder, self.selected)

    def test_matching_hash_cannot_substitute_another_package_metadata(self):
        self.make_wheel(name='Different', version='1')
        self.selected['example-pkg']['sha256'] = qa.sha256(self.wheel)
        with self.assertRaisesRegex(ValueError, 'metadata differs'):
            qa.verify_wheelhouse(self.folder, self.selected)

    def test_vendored_metadata_is_not_mistaken_for_wheel_primary_metadata(self):
        with zipfile.ZipFile(self.wheel, 'a') as archive:
            archive.writestr('example_pkg/_vendor/other-2.dist-info/METADATA',
                             'Metadata-Version: 2.1\nName: other\nVersion: 2\n')
        self.selected['example-pkg']['sha256'] = qa.sha256(self.wheel)
        self.assertEqual(qa.verify_wheelhouse(self.folder, self.selected)[0]['package'], 'example-pkg')

    def test_pip_report_requires_every_consumed_verified_wheel(self):
        path, entry = self.report()
        self.assertEqual(qa.verify_install_report(path, self.folder, self.selected), {'example-pkg': '1'})
        for entries in ([], [entry, entry]):
            self.report(entries)
            with self.assertRaises(ValueError):
                qa.verify_install_report(path, self.folder, self.selected)

    def test_pip_remote_other_path_wrong_hash_or_version_cannot_attest(self):
        path, entry = self.report()
        alternatives = []
        for url in ('https://example.invalid/' + self.wheel.name,
                    (self.root / self.wheel.name).as_uri()):
            item = deepcopy(entry)
            item['download_info']['url'] = url
            alternatives.append(item)
        item = deepcopy(entry)
        item['download_info']['archive_info']['hashes']['sha256'] = '0' * 64
        alternatives.append(item)
        item = deepcopy(entry)
        item['metadata']['version'] = '2'
        alternatives.append(item)
        for item in alternatives:
            with self.subTest(item=item):
                self.report([item])
                with self.assertRaises(ValueError):
                    qa.verify_install_report(path, self.folder, self.selected)

    def test_pinned_requirements_always_enable_exact_package_hash(self):
        text = qa.requirements(self.selected)
        self.assertEqual(text, 'Example_Pkg==1 --hash=sha256:' + qa.sha256(self.wheel) + '\n')

    def test_repository_lock_matches_both_profiles_and_all_version_pins(self):
        for profile in ('windows-x86_64', 'linux-x86_64'):
            for phase in ('runtime', 'build'):
                with self.subTest(profile=profile, phase=phase):
                    selected = qa.selection(phase, profile=profile)
                    pins, _ = qa.profile_lock(phase)
                    self.assertEqual({name: item['version'] for name, item in selected.items()},
                                     {name: item[1] for name, item in pins.items()})

    def test_receipt_is_bound_to_source_wheels_pip_report_and_sbom(self):
        report, _ = self.report()
        evidence = qa.verify_wheelhouse(self.folder, self.selected)
        bom = self.root / 'sbom.json'
        bom.write_text(json.dumps(qa.sbom('a' * 40, evidence, 'fixture')))
        lock = self.root / 'lock.json'
        lock.write_bytes(b'reviewed byte lock')
        value = {'schema': 1, 'phase': 'build', 'sourceRevision': 'a' * 40,
            'profile': 'fixture', 'wheelLockSha256': qa.sha256(lock),
            'wheelhouse': str(self.folder), 'wheels': evidence,
            'pipReport': str(report), 'pipReportSha256': qa.sha256(report),
            'sbom': str(bom), 'sbomSha256': qa.sha256(bom), 'scaStatus': 'not_run'}
        path = self.root / 'receipt.json'
        path.write_text(json.dumps(value))
        with patch.object(qa, 'WHEEL_LOCK', lock), patch.object(qa, 'host_profile', return_value='fixture'), \
                patch.object(qa, 'selection', return_value=self.selected):
            self.assertEqual(qa.verify_receipt(path, 'build', 'a' * 40), value)
            with self.assertRaisesRegex(ValueError, 'stale'):
                qa.verify_receipt(path, 'build', 'b' * 40)
            bom.write_text('{}')
            with self.assertRaisesRegex(ValueError, 'SBOM'):
                qa.verify_receipt(path, 'build', 'a' * 40)

    def test_sbom_is_deterministic_and_preserves_wheel_scope_limit(self):
        evidence = qa.verify_wheelhouse(self.folder, self.selected)
        bom = qa.sbom('a' * 40, evidence, 'fixture')
        self.assertEqual(bom, qa.sbom('a' * 40, evidence, 'fixture'))
        self.assertEqual(bom['components'][0]['hashes'][0]['content'], qa.sha256(self.wheel))
        self.assertIn('SCA not run', str(bom['metadata']['properties']))

    def test_installer_uses_hashes_for_download_and_consumed_local_wheels(self):
        lock = self.root / 'lock.json'
        lock.write_bytes(b'locked bytes')
        commands = []
        def pip_run(command, *, check):
            self.assertTrue(check)
            commands.append(command)
            self.assertIn('--require-hashes', command)
            self.assertIn('--only-binary=:all:', command)
            if 'download' in command:
                destination = Path(command[command.index('--dest') + 1])
                copyfile(self.wheel, destination / self.wheel.name)
            else:
                self.assertIn('--no-index', command)
                self.assertIn('--force-reinstall', command)
                destination = Path(command[command.index('--find-links') + 1])
                Path(command[command.index('--report') + 1]).write_text(json.dumps({'install': [{
                    'metadata': {'name': 'Example_Pkg', 'version': '1'},
                    'download_info': {'url': (destination / self.wheel.name).as_uri(),
                        'archive_info': {'hashes': {'sha256': qa.sha256(self.wheel)}}}}]}))
        with patch.object(qa, 'ROOT', self.root), patch.object(qa, 'WHEEL_LOCK', lock), \
                patch.object(qa, 'selection', return_value=self.selected), \
                patch.object(qa, 'host_profile', return_value='fixture'), \
                patch('sys.version_info', (3, 11, 0)), \
                patch.dict('os.environ', AIO_QA_SOURCE_SHA='a' * 40), \
                patch('sys.argv', ['wheels', '--phase', 'runtime', '--wheelhouse', str(self.root / 'staging')]), \
                patch.object(subprocess, 'run', side_effect=pip_run):
            qa.main()
            receipt = self.root / 'build/aio-qa-wheels-runtime.json'
            value = qa.verify_receipt(receipt, 'runtime', 'a' * 40)
            self.assertEqual(len(value['wheels']), 1)
            self.assertEqual(value['scaStatus'], 'not_run')
            with patch('sys.argv', ['wheels', '--phase', 'build', '--wheelhouse', str(self.root / 'staging')]):
                qa.main()
            self.assertEqual(qa.verify_receipt(receipt, 'runtime', 'a' * 40), value)
            self.assertNotEqual(value['sbom'], str(self.root / 'build/aio-qa-sbom-build.cdx.json'))
        self.assertEqual(len(commands), 4)

    def test_wrong_interpreter_refuses_before_network_or_installation(self):
        with patch('sys.argv', ['wheels', '--phase', 'runtime', '--wheelhouse', str(self.root / 'staging')]), \
                patch('sys.version_info', (3, 12, 0)), \
                patch.object(subprocess, 'run', side_effect=AssertionError('no pip')):
            with self.assertRaisesRegex(ValueError, 'CPython 3.11'):
                qa.main()


if __name__ == '__main__':
    unittest.main()
