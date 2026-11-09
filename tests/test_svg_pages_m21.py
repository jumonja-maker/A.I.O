"""M21: SVG page manifests must match all actual, correctly ordered outputs."""
import json
from pathlib import Path
import tempfile
import threading
import unittest

from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard.svg_page_manifest import verify_svg_page_manifest


class SvgPageManifestM21(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.cancel = threading.Event()

    def sample(self, count=4):
        files = []
        for n in range(1, count + 1):
            filename = 'example.svg' if n == 1 else 'example__page_%04d.svg' % n
            p = self.root / filename
            p.write_bytes(('<svg><text>PAGE%d</text></svg>' % n).encode('utf-8'))
            files.append(p)
        manifest = self.root / 'AIO-pages.json'
        manifest.write_text(json.dumps({'schema': 1, 'pageCount': count,
                           'pages': [{'page': n, 'file': p.name}
                                     for n, p in enumerate(files, 1)]}), encoding='utf-8')
        return files, manifest

    def check(self, items):
        return verify_svg_page_manifest(items, self.root, cancel=self.cancel)

    def assert_rejected(self, items, reason=None):
        with self.assertRaises(GuardError) as ctx:
            self.check(items)
        self.assertEqual(ctx.exception.code, 'ENGINE_OUTPUT_VALIDATION_INCOMPLETE')
        self.assertEqual(ctx.exception.stage, 'result')
        if reason:
            self.assertIn(reason, ctx.exception.diagnostic)

    def test_four_pages_preserved_with_exact_ordered_mapping(self):
        pages, manifest = self.sample()
        saved = {p: p.read_bytes() for p in [*pages, manifest]}
        result = self.check([pages[2], manifest, pages[0], pages[3], pages[1]])
        self.assertEqual(result['status'], 'checked_subset')
        self.assertEqual(result['expectedUnits'], 4)
        self.assertEqual(result['observedOutputs'], 4)
        self.assertFalse(result['qualityCompared'])
        self.assertIn('svg_visual_equivalence', result['notChecked'])
        self.assertEqual({p: p.read_bytes() for p in saved}, saved)

    def test_one_page_without_manifest_is_unchanged(self):
        p = self.root / 'only.svg'
        p.write_bytes(b'<svg/>')
        self.assertIsNone(self.check([p]))

    def test_one_page_with_manifest_is_checked(self):
        pages, manifest = self.sample(1)
        self.assertEqual(self.check([*pages, manifest])['expectedUnits'], 1)

    def test_unrelated_result_never_has_an_svg_page_policy(self):
        p = self.root / 'source.pdf'
        p.write_bytes(b'%PDF-')
        self.assertIsNone(self.check([p]))

    def test_multi_page_without_manifest_is_not_ready(self):
        pages, manifest = self.sample()
        manifest.unlink()
        self.assert_rejected(pages, 'missing_manifest')

    def test_missing_middle_page_is_not_masked_by_sidecar(self):
        pages, manifest = self.sample()
        self.assert_rejected([pages[0], pages[2], pages[3], manifest], 'schema_or_count')

    def test_wrong_record_count_is_not_ready(self):
        pages, manifest = self.sample()
        data = json.loads(manifest.read_text())
        data['pageCount'] = 3
        manifest.write_text(json.dumps(data))
        self.assert_rejected([*pages, manifest], 'schema_or_count')

    def test_reordered_page_mapping_is_not_ready(self):
        pages, manifest = self.sample()
        data = json.loads(manifest.read_text())
        data['pages'][1]['file'], data['pages'][2]['file'] = (data['pages'][2]['file'], data['pages'][1]['file'])
        manifest.write_text(json.dumps(data))
        self.assert_rejected([*pages, manifest], 'reordered_page')

    def test_repeated_page_reference_is_not_ready(self):
        pages, manifest = self.sample()
        data = json.loads(manifest.read_text())
        data['pages'][3]['file'] = data['pages'][2]['file']
        manifest.write_text(json.dumps(data))
        self.assert_rejected([*pages, manifest], 'reordered_page')

    def test_traversal_and_external_paths_cannot_be_manifest_members(self):
        for value in ('../example.svg', 'folder/example.svg', 'C:\\example.svg'):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                pages = [root / 'example.svg', root / 'example__page_0002.svg']
                for p in pages:
                    p.write_bytes(b'<svg/>')
                manifest = root / 'AIO-pages.json'
                manifest.write_text(json.dumps({'schema': 1, 'pageCount': 2,
                    'pages': [{'page': 1, 'file': pages[0].name},
                              {'page': 2, 'file': value}]}))
                with self.assertRaises(GuardError):
                    verify_svg_page_manifest([*pages, manifest], root)

    def test_duplicate_json_keys_are_rejected(self):
        pages, manifest = self.sample(2)
        manifest.write_text('{"schema":1,"schema":1,"pageCount":2,"pages":[]}')
        self.assert_rejected([*pages, manifest], 'not_strict_json')

    def test_missing_or_empty_page_rejected_before_ready(self):
        pages, manifest = self.sample()
        pages[2].write_bytes(b'')
        self.assert_rejected([*pages, manifest], 'missing_or_empty')

    def test_manifests_over_one_megabyte_are_not_read_in_full(self):
        pages, manifest = self.sample()
        manifest.write_bytes(b' ' * (1024 * 1024 + 1))
        self.assert_rejected([*pages, manifest], 'type_or_size')

    def test_nonregular_manifest_rejected(self):
        pages, manifest = self.sample()
        manifest.unlink()
        directory = self.root / 'AIO-pages.json'
        directory.mkdir()
        self.assert_rejected([*pages, directory], 'type_or_size')

    def test_cancellation_stops_before_any_manifests(self):
        pages, manifest = self.sample()
        self.cancel.set()
        with self.assertRaises(GuardError) as ctx:
            self.check([*pages, manifest])
        self.assertEqual(ctx.exception.code, 'CANCELLED')

    def test_unrelated_nested_svg_sidecar_does_not_count_as_a_page(self):
        pages, manifest = self.sample(2)
        nested = self.root / 'images'
        nested.mkdir()
        another = nested / 'inline.svg'
        another.write_bytes(b'<svg/>')
        result = self.check([*pages, manifest, another])
        self.assertEqual(result['observedOutputs'], 2)

    def test_symlink_page_is_rejected_when_os_permits_symlinks(self):
        pages, manifest = self.sample(2)
        other = self.root / 'legitimate.txt'
        other.write_bytes(b'UNCHANGED')
        pages[-1].unlink()
        try:
            pages[-1].symlink_to(other)
        except (OSError, NotImplementedError):
            self.assertFalse(pages[-1].exists())
            return
        self.assert_rejected([*pages, manifest], 'missing_or_empty')
        self.assertEqual(other.read_bytes(), b'UNCHANGED')

    def test_invalid_schema_cannot_be_accepted(self):
        pages, manifest = self.sample()
        data = json.loads(manifest.read_text())
        data['schema'] = True
        manifest.write_text(json.dumps(data))
        self.assert_rejected([*pages, manifest], 'schema_or_count')


if __name__ == '__main__':
    unittest.main()
