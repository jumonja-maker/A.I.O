"""QA-only wheel transport validates bytes before exposing any package path."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('materialize_qa_data',
    ROOT/'tools/qa/materialize_fulltest_corpus.py')
qa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qa)


class PillowQaTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def archive(self, extra=(), *, role='offline_windows_pillow_qa_only'):
        files = [('PIL/__init__.py', b'qa stub; never imported'),
                 ('PIL/_imaging.cp311-win_amd64.pyd', b'qa stub'),
                 ('pillow-11.3.0.dist-info/licenses/LICENSE', b'original license'),
                 ('pillow-11.3.0.dist-info/METADATA', b'Name: pillow\nVersion: 11.3.0'),
                 ('pillow-11.3.0.dist-info/WHEEL', b'Tag: cp311-cp311-win_amd64'),
                 ('pillow-11.3.0.dist-info/RECORD', b'qa records')]
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as package:
            for name, data in [*files, *extra]:
                if isinstance(name, str):
                    # Preserve deliberately invalid raw names even on Windows,
                    # where the constructor normally canonicalizes backslashes.
                    entry = zipfile.ZipInfo(name)
                    entry.filename = entry.orig_filename = name
                else:
                    entry = name
                package.writestr(entry, data)
        data = buffer.getvalue()
        parts = self.root/'parts'
        parts.mkdir()
        (parts/'part-000.bin').write_bytes(data)
        fingerprint = hashlib.sha256(data).hexdigest()
        blob = hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
        (parts/'manifest.json').write_text(json.dumps({'schema': 1,
            'kind': role, 'wheel': qa.PILLOW_WINDOWS_WHEEL,
            'zipSha256': fingerprint, 'zipBytes': len(data),
            'parts': [{'path': 'part-000.bin', 'gitBlobSha': blob}]}))
        pin = patch.object(qa, 'PILLOW_WINDOWS_SHA256', fingerprint)
        pin.start()
        self.addCleanup(pin.stop)
        return parts

    def test_preserves_package_and_licenses_without_installing_or_importing(self):
        parts = self.archive(extra=[('pillow.libs/qa-library.dll', b'qa dll')])
        folder, proof = qa.materialize_pillow_windows(parts, self.root/'out')
        self.assertEqual((folder/'pillow-11.3.0.dist-info/licenses/LICENSE').read_bytes(), b'original license')
        self.assertEqual((folder/'pillow.libs/qa-library.dll').read_bytes(), b'qa dll')
        self.assertFalse(proof['dependencyInstallation'])
        self.assertFalse(proof['productionDependency'])
        self.assertEqual(proof['files'], 7)
        self.assertTrue((folder/'pillow-qa-manifest.json').is_file())

    def test_rejects_unsafe_paths_duplicates_and_pth_before_any_package_extraction(self):
        for extra in ('../outside', 'PIL/../outside', 'PIL//outside',
                      '/PIL/outside', 'PIL\\outside', 'PIL/x:ads',
                      'PIL/evil.PTH', 'PIL/directory.pth/', 'PIL/trailing. ',
                      'other/escape.py', 'PIL/__INIT__.py'):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as temp:
                self.root = Path(temp)
                parts = self.archive(extra=[(extra, b'forbidden')])
                with self.assertRaises(ValueError):
                    qa.materialize_pillow_windows(parts, self.root/'out')
                self.assertFalse((self.root/'out/PIL').exists())

    def test_rejects_symlink_entry(self):
        linked = zipfile.ZipInfo('PIL/linked.py')
        linked.create_system = 3
        linked.external_attr = (stat.S_IFLNK | 0o777) << 16
        parts = self.archive(extra=[(linked, b'../../outside')])
        with self.assertRaisesRegex(ValueError, 'Unsafe'):
            qa.materialize_pillow_windows(parts, self.root/'out')

    def test_windows_separator_normalization_cannot_hide_raw_archive_name(self):
        parts = self.archive(extra=[('PIL\\outside', b'forbidden')])
        # Exercise the Windows ZipInfo parsing rule on every QA platform.
        with patch.object(zipfile.os, 'sep', '\\'):
            with zipfile.ZipFile(parts/'part-000.bin') as archive:
                entry = archive.infolist()[-1]
                self.assertEqual(entry.orig_filename, 'PIL\\outside')
                self.assertEqual(entry.filename, 'PIL/outside')
            with self.assertRaisesRegex(ValueError, 'ZIP parsing'):
                qa.materialize_pillow_windows(parts, self.root/'out')
        self.assertFalse((self.root/'out/PIL').exists())

    def test_rejects_part_mutation_before_package_extraction(self):
        parts = self.archive()
        with (parts/'part-000.bin').open('ab') as target:
            target.write(b'corruption')
        with self.assertRaisesRegex(ValueError, 'Git object'):
            qa.materialize_pillow_windows(parts, self.root/'out')
        self.assertFalse((self.root/'out/PIL').exists())

    def test_existing_output_and_wrong_role_are_rejected(self):
        parts = self.archive(role='production_dependency')
        output = self.root/'existing'
        output.mkdir()
        (output/'keep').write_bytes(b'keep')
        with self.assertRaisesRegex(ValueError, 'overwrite'):
            qa.materialize_pillow_windows(parts, output)
        self.assertEqual((output/'keep').read_bytes(), b'keep')
        with self.assertRaisesRegex(ValueError, 'role'):
            qa.materialize_pillow_windows(parts, self.root/'fresh')


if __name__ == '__main__':
    unittest.main()
