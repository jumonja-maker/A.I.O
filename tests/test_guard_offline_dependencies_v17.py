"""Offline QA imports must be reproducible without broadening host access."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

FILE = Path(__file__).resolve().parents[1] / 'tools/qa/guard_v17_test_dependencies.py'
SPEC = importlib.util.spec_from_file_location('guard_v17_offline_dependencies', FILE)
qa = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(qa)


class OfflineDependencyBoundary(unittest.TestCase):
    def bundle(self, root, *, altered=None, extra=False, duplicate=False, symlink=False):
        data = {'python/openpyxl/__init__.py': b'', 'python/pypdf/__init__.py': b'',
                'python/et_xmlfile/__init__.py': b'', 'node_modules/linkedom/esm/index.js': b'export{};'}
        if altered is not None:
            data[altered] = b'unsafe'
        rows = [{'name': name, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
                for name, raw in data.items()]
        manifest = {'schema': 1, 'scope': 'offline_test_dependencies_only', 'files': rows, 'packages': []}
        path = root / 'dependencies.zip'
        with zipfile.ZipFile(path, 'x') as archive:
            archive.writestr('__manifest__.json', json.dumps(manifest))
            for name, raw in data.items():
                info = zipfile.ZipInfo(name)
                info.external_attr = ((stat.S_IFLNK if symlink else stat.S_IFREG) | 0o644) << 16
                archive.writestr(info, raw)
            if extra:
                archive.writestr('unlisted.py', b'unlisted')
            if duplicate:
                archive.writestr('PYTHON/openpyxl/__init__.py', b'collision')
        return path

    def test_hash_checked_bundle_configures_only_private_test_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); bundle = self.bundle(root)
            with patch.dict(os.environ, {'PYTHONPATH': 'already-existing'}):
                result = qa.materialize(root / 'private', bundle=bundle)
            self.assertEqual(result['filesVerified'], 4)
            self.assertEqual(result['environment']['PYTHONPATH'], str(root / 'private/python') + os.pathsep + 'already-existing')
            self.assertEqual((root / 'private/environment.json').is_file(), True)

    def test_collision_never_overwrites_existing_destination(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); bundle = self.bundle(root)
            output = root / 'existing'; output.mkdir(); (output / 'owner').write_bytes(b'keep')
            with self.assertRaises(FileExistsError):
                qa.materialize(output, bundle=bundle)
            self.assertEqual((output / 'owner').read_bytes(), b'keep')

    def test_traversal_absolute_and_windows_stream_paths_are_rejected(self):
        for name in ('../escape', '/tmp/escape', 'C:escape', 'a\\b', 'folder/CON.txt', 'folder/name. ', 'a*file'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                root = Path(folder); bundle = self.bundle(root, altered=name)
                with self.assertRaises(ValueError):
                    qa.materialize(root / 'private', bundle=bundle)
                self.assertFalse((root / 'private').exists())

    def test_symlink_duplicate_and_unlisted_entries_are_rejected(self):
        for options in ({'symlink': True}, {'duplicate': True}, {'extra': True}):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as folder:
                root = Path(folder); bundle = self.bundle(root, **options)
                with self.assertRaises(ValueError):
                    qa.materialize(root / 'private', bundle=bundle)
                self.assertFalse((root / 'private').exists())

    def test_expanded_byte_budget_precedes_extraction(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); bundle = self.bundle(root)
            with patch.object(qa, 'MAX_EXPANDED_BYTES', 1), self.assertRaises(ValueError):
                qa.materialize(root / 'private', bundle=bundle)
            self.assertFalse((root / 'private').exists())

    def test_digest_failure_never_publishes_dependency_tree(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); bundle = self.bundle(root)
            with zipfile.ZipFile(bundle) as original:
                content = {item.filename: original.read(item) for item in original.infolist()}
            manifest = json.loads(content['__manifest__.json'])
            manifest['files'][0]['sha256'] = '0' * 64
            content['__manifest__.json'] = json.dumps(manifest).encode()
            bundle.unlink()
            with zipfile.ZipFile(bundle, 'x') as modified:
                for name, raw in content.items(): modified.writestr(name, raw)
            with self.assertRaisesRegex(ValueError, 'bytes differ'):
                qa.materialize(root / 'private', bundle=bundle)
            self.assertFalse((root / 'private').exists())

    def test_extraction_error_cleans_only_its_new_destination(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); bundle = self.bundle(root)
            original_open = Path.open
            def interrupted(path, *args, **kwargs):
                if path == root / 'private/python/pypdf/__init__.py':
                    raise OSError('controlled interrupted write')
                return original_open(path, *args, **kwargs)
            with patch.object(Path, 'open', interrupted), self.assertRaises(OSError):
                qa.materialize(root / 'private', bundle=bundle)
            self.assertFalse((root / 'private').exists())
            self.assertTrue(bundle.is_file())

    def test_source_checkout_and_github_env_injection_are_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); bundle = self.bundle(root)
            with patch.object(qa, 'ROOT', root), self.assertRaisesRegex(ValueError, 'outside'):
                qa.materialize(root / 'private', bundle=bundle)
            output = root / 'github-env'; output.write_text('EXISTING=value\n')
            with self.assertRaises(ValueError):
                qa.write_github_env({'PYTHONPATH': 'safe\nEVIL=1', 'AIO_DOM_MODULE': 'safe'}, output)
            self.assertEqual(output.read_text(), 'EXISTING=value\n')
            qa.write_github_env({'PYTHONPATH': 'safe path', 'AIO_DOM_MODULE': 'safe'}, output)
            self.assertIn('PYTHONPATH=safe path\n', output.read_text())


class ActualOfflineImports(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.result = qa.materialize(cls.root / 'private')

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_pure_python_imports_work_without_system_site_packages(self):
        script = ('import sys,json;sys.path.insert(0,sys.argv[1]);'
                  'import openpyxl,pypdf,et_xmlfile;'
                  'print(json.dumps([openpyxl.__version__,pypdf.__version__,et_xmlfile.__version__]))')
        process = subprocess.run([sys.executable, '-S', '-c', script, str(self.root / 'private/python')],
            cwd=self.root, capture_output=True, text=True, timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout), ['3.1.5', '6.10.0', '2.0.0'])

    @unittest.skipUnless(shutil.which('node'), 'Node is required for the actual DOM test dependency')
    def test_complete_bundled_dom_graph_runs_without_npm_or_network(self):
        script = ("import {pathToFileURL} from 'node:url';"
                  "const {parseHTML}=await import(pathToFileURL(process.argv[1]).href);"
                  "const {document}=parseHTML('<html><body><p id=one>offline</p></body></html>');"
                  "if(document.querySelector('#one').textContent!=='offline')throw Error('DOM failed');"
                  "console.log('PASS');")
        process = subprocess.run([shutil.which('node'), '--input-type=module', '-e', script,
                                  self.result['environment']['AIO_DOM_MODULE']],
            cwd=self.root, capture_output=True, text=True, timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stdout.strip(), 'PASS')

    def test_versions_license_metadata_and_published_license_files_are_retained(self):
        packages = self.result['packages']
        self.assertEqual(len(packages), 17)
        self.assertEqual(next(p['version'] for p in packages if p['name'] == 'linkedom'), '0.18.12')
        for package in packages:
            with self.subTest(package=package['name']):
                if package['name'] == 'boolbase':
                    self.assertEqual(package['license'], 'ISC')
                    self.assertIn('no separate license', package['licenseNote'])
                else:
                    self.assertTrue(package['licenses'])
                    for relative in package['licenses']:
                        self.assertTrue((self.root / 'private' / relative).is_file())


if __name__ == '__main__':
    unittest.main()
