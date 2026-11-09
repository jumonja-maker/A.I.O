"""Real temporary files, native record fixtures and failure/ownership controls."""
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard import native_metafile as module
from test_guard_native_metafile_v17 import emf, wmf


class CompressedPages(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cancel = threading.Event()

    def fixture(self, kind='emf', count=3):
        make = emf if kind == 'emf' else wmf
        files = []
        for i in range(count):
            path = self.root / ('native__page_%04d.' % (i + 1) + kind)
            path.write_bytes(make(('ONLY_PAGE_%d' % i).encode() * 100))
            files.append(path)
        manifest = self.root / 'AIO-pages.json'
        manifest.write_text(json.dumps({'schema': 1, 'pageCount': count, 'pages': [
            {'page': i + 1, 'file': p.name} for i, p in enumerate(files)]}), encoding='utf-8')
        return files + [manifest]

    def snapshot(self, paths):
        return {p: p.read_bytes() for p in paths}

    def assert_unchanged(self, before):
        for p, raw in before.items():
            self.assertEqual(p.read_bytes(), raw, p.name)
        self.assertFalse(list(self.root.glob('*.emz')))
        self.assertFalse(list(self.root.glob('*.wmz')))
        self.assertFalse(list(self.root.glob('*.partial')))

    def test_manifest_points_to_all_compressed_pages_and_bytes_match(self):
        for target, kind in (('emz', 'emf'), ('wmz', 'wmf')):
            with self.subTest(target=target):
                paths = self.fixture(kind)
                before = self.snapshot(paths)
                result, receipt = module.wrap_native_metafiles(paths, target)
                manifest = json.loads(result[-1].read_text(encoding='utf-8'))
                self.assertEqual([r['page'] for r in manifest['pages']], [1, 2, 3])
                self.assertEqual(manifest['pageCount'], 3)
                self.assertEqual([r['file'] for r in manifest['pages']], [p.name for p in result[:-1]])
                self.assertEqual(receipt['pageManifest'], {'file': 'AIO-pages.json', 'pageCount': 3})
                self.assertEqual(receipt['files'], 3)
                self.assertFalse(receipt['qualityCompared'])
                for old, new, facts in zip(paths, result, receipt['metafiles']):
                    self.assertFalse(old.exists())
                    self.assertEqual(gzip.decompress(new.read_bytes()), before[old])
                    self.assertEqual(facts['file'], new.name)
                    self.assertEqual(facts['sha256'], hashlib.sha256(before[old]).hexdigest())
                for p in result:
                    p.unlink()

    def test_manifest_uses_page_order_not_input_order(self):
        paths = self.fixture()
        result, _ = module.wrap_native_metafiles([paths[2], paths[0], paths[3], paths[1]], 'emz')
        data = json.loads(paths[-1].read_text())
        self.assertEqual([r['file'] for r in data['pages']], [p.with_suffix('.emz').name for p in paths[:-1]])
        self.assertEqual(result[0].name, paths[2].with_suffix('.emz').name)

    def test_single_page_without_manifest_remains_supported(self):
        p = self.root / 'one.emf'
        raw = emf(b'one')
        p.write_bytes(raw)
        results, receipt = module.wrap_native_metafiles([p], 'emz')
        self.assertEqual(gzip.decompress(results[0].read_bytes()), raw)
        self.assertNotIn('pageManifest', receipt)
        self.assertFalse(p.exists())

    def test_unrelated_sidecar_is_unchanged_and_returned(self):
        paths = self.fixture()
        extra = self.root / 'notes.txt'
        extra.write_bytes(b'private notes')
        results, _ = module.wrap_native_metafiles(paths + [extra], 'emz')
        self.assertEqual(results[-1], extra)
        self.assertEqual(extra.read_bytes(), b'private notes')

    def test_invalid_later_page_rolls_back_earlier_containers(self):
        paths = self.fixture()
        paths[1].write_bytes(b'broken EMF')
        before = self.snapshot(paths)
        with self.assertRaises(GuardError):
            module.wrap_native_metafiles(paths, 'emz')
        self.assert_unchanged(before)

    def test_cancel_on_second_page_rolls_back_all_containers(self):
        paths = self.fixture()
        before = self.snapshot(paths)
        original = module.inspect_metafile
        def inspect(path, *args, **kwargs):
            if path == paths[1]:
                self.cancel.set()
            return original(path, *args, **kwargs)
        with patch.object(module, 'inspect_metafile', side_effect=inspect):
            with self.assertRaises(GuardError) as caught:
                module.wrap_native_metafiles(paths, 'emz', cancel=self.cancel)
        self.assertEqual(caught.exception.code, 'CANCELLED')
        self.assert_unchanged(before)

    def test_cancel_during_compression_cleans_only_owned_file(self):
        paths = self.fixture()
        before = self.snapshot(paths)
        original = gzip.GzipFile.write
        def write(stream, block):
            value = original(stream, block)
            self.cancel.set()
            return value
        with patch.object(gzip.GzipFile, 'write', write):
            with self.assertRaises(GuardError) as caught:
                module.wrap_native_metafiles(paths, 'emz', cancel=self.cancel)
        self.assertEqual(caught.exception.code, 'CANCELLED')
        self.assert_unchanged(before)

    def test_later_collision_keeps_competing_file_and_originals(self):
        paths = self.fixture()
        before = self.snapshot(paths)
        competing = paths[1].with_suffix('.emz')
        competing.write_bytes(b'not ours')
        with self.assertRaises(GuardError):
            module.wrap_native_metafiles(paths, 'emz')
        self.assertEqual(competing.read_bytes(), b'not ours')
        competing.unlink()
        self.assert_unchanged(before)

    def test_competing_file_created_immediately_before_exclusive_open_survives(self):
        paths = self.fixture()
        before = self.snapshot(paths)
        competing = paths[1].with_suffix('.emz')
        original = Path.open
        def open_path(path, mode='r', *args, **kwargs):
            if path == competing and mode == 'xb':
                with original(path, 'wb') as stream:
                    stream.write(b'race owner')
            return original(path, mode, *args, **kwargs)
        with patch.object(Path, 'open', open_path):
            with self.assertRaises(GuardError):
                module.wrap_native_metafiles(paths, 'emz')
        self.assertEqual(competing.read_bytes(), b'race owner')
        competing.unlink()
        self.assert_unchanged(before)

    def test_preexisting_partial_is_never_removed(self):
        paths = self.fixture()
        before = self.snapshot(paths)
        other = paths[1].with_suffix('.emz.partial')
        other.write_bytes(b'someone else')
        with self.assertRaises(GuardError):
            module.wrap_native_metafiles(paths, 'emz')
        self.assertEqual(other.read_bytes(), b'someone else')
        other.unlink()
        self.assert_unchanged(before)

    def test_manifest_replace_failure_keeps_old_manifest_and_all_plain_files(self):
        paths = self.fixture()
        before = self.snapshot(paths)
        with patch.object(module.os, 'replace', side_effect=PermissionError('locked')):
            with self.assertRaises(PermissionError):
                module.wrap_native_metafiles(paths, 'emz')
        self.assert_unchanged(before)

    def test_manifest_changed_during_export_is_not_overwritten(self):
        paths = self.fixture()
        raw_pages = self.snapshot(paths[:-1])
        original = module.inspect_metafile
        def inspect(path, *args, **kwargs):
            facts = original(path, *args, **kwargs)
            if path == paths[-2]:
                paths[-1].write_bytes(b'changed by other writer')
            return facts
        with patch.object(module, 'inspect_metafile', side_effect=inspect):
            with self.assertRaises(GuardError):
                module.wrap_native_metafiles(paths, 'emz')
        self.assertEqual(paths[-1].read_bytes(), b'changed by other writer')
        self.assert_unchanged(raw_pages)

    def test_final_cleanup_failure_is_not_success_and_retains_verified_containers(self):
        paths = self.fixture()
        original = Path.unlink
        def unlink(path, *args, **kwargs):
            if path == paths[1]:
                raise PermissionError('locked intermediate')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'unlink', unlink):
            with self.assertRaises(PermissionError):
                module.wrap_native_metafiles(paths, 'emz')
        self.assertEqual(len(list(self.root.glob('*.emz'))), 3)
        self.assertTrue(paths[1].is_file())
        self.assertTrue(all((self.root / r['file']).is_file()
                            for r in json.loads(paths[-1].read_text())['pages']))

    def test_invalid_manifests_never_mutate_any_output(self):
        mutators = [
            lambda d: d.update(pageCount=2), lambda d: d.update(pageCount=True),
            lambda d: d.update(schema=2), lambda d: d.update(schema=True),
            lambda d: d.update(pages=d['pages'][:-1]),
            lambda d: d['pages'][1].update(file=d['pages'][0]['file']),
            lambda d: d['pages'][0].update(file='../escape.emf'),
            lambda d: d['pages'][0].update(file='C:\\escape.emf'),
            lambda d: d['pages'][0].update(page=True),
            lambda d: d['pages'][1].update(page=3),
            lambda d: d['pages'][0].update(file='absent.emf'),
            lambda d: d['pages'][0].update(file='native__page_0001.wmf'),
            lambda d: d.update(unknownIntegrityField='not updated'),
        ]
        for change in mutators:
            with self.subTest(change=change):
                paths = self.fixture()
                data = json.loads(paths[-1].read_text())
                change(data)
                paths[-1].write_text(json.dumps(data))
                before = self.snapshot(paths)
                with self.assertRaises(GuardError):
                    module.wrap_native_metafiles(paths, 'emz')
                self.assert_unchanged(before)

    def test_json_duplicate_keys_malformed_and_budget_are_rejected(self):
        for raw in (b'{"schema":1,"schema":1}', b'{bad}', b' ' * (1024 * 1024 + 1)):
            paths = self.fixture()
            paths[-1].write_bytes(raw)
            before = self.snapshot(paths)
            with self.assertRaises(GuardError):
                module.wrap_native_metafiles(paths, 'emz')
            self.assert_unchanged(before)

    def test_multiple_pages_require_existing_manifest(self):
        paths = self.fixture()
        paths[-1].unlink()
        before = self.snapshot(paths[:-1])
        with self.assertRaises(GuardError):
            module.wrap_native_metafiles(paths[:-1], 'emz')
        self.assert_unchanged(before)

    def test_duplicate_input_rejected_before_writes(self):
        paths = self.fixture()
        before = self.snapshot(paths)
        with self.assertRaises(GuardError):
            module.wrap_native_metafiles(paths + [paths[0]], 'emz')
        self.assert_unchanged(before)

    def test_manifest_from_different_directory_is_rejected(self):
        paths = self.fixture()
        sub = self.root / 'elsewhere'
        sub.mkdir()
        other = sub / 'AIO-pages.json'
        paths[-1].rename(other)
        paths[-1] = other
        before = self.snapshot(paths)
        with self.assertRaises(GuardError):
            module.wrap_native_metafiles(paths, 'emz')
        self.assert_unchanged(before)

    def test_gzip_readback_corruption_does_not_publish_partial_set(self):
        paths = self.fixture()
        before = self.snapshot(paths)
        original = module.gzip.open
        def read(path, *args, **kwargs):
            if Path(path) == paths[1].with_suffix('.emz'):
                Path(path).write_bytes(b'not gzip')
            return original(path, *args, **kwargs)
        with patch.object(module.gzip, 'open', side_effect=read):
            with self.assertRaises(gzip.BadGzipFile):
                module.wrap_native_metafiles(paths, 'emz')
        self.assert_unchanged(before)

    def test_cancel_before_start_does_not_read_or_write(self):
        self.cancel.set()
        with self.assertRaises(GuardError) as caught:
            module.wrap_native_metafiles([self.root / 'missing.emf'], 'emz', cancel=self.cancel)
        self.assertEqual(caught.exception.code, 'CANCELLED')
        self.assertEqual(list(self.root.iterdir()), [])

    def test_unsupported_target_does_not_write(self):
        with self.assertRaises(ValueError):
            module.wrap_native_metafiles([], 'pdf')
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
