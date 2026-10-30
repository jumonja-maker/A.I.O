"""M20: all Writer pages must succeed together or leave no partial output."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlparse
from urllib.request import url2pathname

from app_backend.converter_guard.native_fidelity import NATIVE_FIDELITY_SOURCE

WORKER = {}
exec(compile(NATIVE_FIDELITY_SOURCE, '<writer-page-rollback>', 'exec'), WORKER)
WRITER = 'com.sun.star.text.TextDocument'


class Cursor:
    def __init__(self):
        self.page = 2
        self.blocked = None
        self.movements = []

    def getPage(self):
        return self.page

    def jumpToPage(self, page):
        self.movements.append(page)
        if page == self.blocked:
            return False
        self.page = page
        return True


class Writer:
    def __init__(self, count=3):
        self.cursor = Cursor()
        self.count = count
        self.calls = []
        self.raise_at = self.empty_at = None

    def getCurrentController(self):
        return SimpleNamespace(PageCount=self.count,
                               getViewCursor=lambda: self.cursor)

    def storeToURL(self, url, options):
        dest = Path(url2pathname(urlparse(url).path))
        props = {option.Name: option.Value for option in options}
        assert props['Overwrite'] is False
        assert not dest.exists(), 'overwrite existing user data'
        self.calls.append(dest.name)
        if self.cursor.page == self.raise_at:
            dest.write_bytes(b'PARTIAL_NATIVE_OUTPUT')
            raise OSError('native_write_failed')
        if self.cursor.page != self.empty_at:
            dest.write_bytes(('WRITER_PAGE_%d' % self.cursor.page).encode('ascii'))


class WriterPageRollback(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.root = Path(work.name).resolve()
        self.model = Writer()
        self.target = self.root / 'writer.png'
        self.plan = WORKER['aio_plan_preservation']([WRITER], 'png', 3)
        self.options = [SimpleNamespace(Name='Overwrite', Value=False),
                        SimpleNamespace(Name='FilterName', Value='writer_png_Export')]

    def run_export(self, filter_name='writer_png_Export', plan=None):
        return WORKER['aio_export_writer_pages'](
            self.model, None, self.target, self.options, filter_name,
            self.plan if plan is None else plan)

    def test_complete_batch_has_ordered_manifest_and_restores_cursor(self):
        outputs = self.run_export()
        self.assertEqual([p.read_bytes() for p in outputs[:3]],
                         [b'WRITER_PAGE_1', b'WRITER_PAGE_2', b'WRITER_PAGE_3'])
        manifest = json.loads(outputs[-1].read_text(encoding='utf-8'))
        self.assertEqual(manifest['pageCount'], 3)
        self.assertEqual(manifest['pages'],
                         [{'page': i, 'file': p.name}
                          for i, p in enumerate(outputs[:3], 1)])
        self.assertEqual(self.model.cursor.page, 2)
        receipt = WORKER['aio_verify_preservation'](
            self.plan, [p.name for p in outputs])
        self.assertEqual(receipt['observedOutputs'], 3)
        self.assertFalse(receipt['qualityCompared'])

    def test_late_native_exception_removes_written_and_partial_pages(self):
        self.model.raise_at = 2
        with self.assertRaisesRegex(OSError, 'native_write_failed'):
            self.run_export()
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(self.model.cursor.page, 2)

    def test_cursor_failure_removes_previous_page(self):
        self.model.cursor.blocked = 2
        with self.assertRaisesRegex(WORKER['AioWriterPagesUnverified'],
                                    'writer_page_cursor_mismatch:2'):
            self.run_export()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_missing_native_output_removes_previous_pages(self):
        self.model.empty_at = 2
        with self.assertRaisesRegex(WORKER['AioWriterPagesUnverified'],
                                    'writer_page_output_missing:2'):
            self.run_export()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_existing_primary_is_preserved_before_any_export(self):
        self.target.write_bytes(b'EXISTING')
        with self.assertRaises(FileExistsError):
            self.run_export()
        self.assertEqual(self.target.read_bytes(), b'EXISTING')
        self.assertEqual(self.model.calls, [])

    def test_late_page_collision_is_preserved_before_any_export(self):
        file = self.root / 'writer__page_0003.png'
        file.write_bytes(b'EXISTING')
        with self.assertRaises(FileExistsError):
            self.run_export()
        self.assertEqual(list(self.root.iterdir()), [file])
        self.assertEqual(file.read_bytes(), b'EXISTING')
        self.assertEqual(self.model.calls, [])

    def test_existing_manifest_does_not_get_truncated(self):
        manifest = self.root / 'AIO-pages.json'
        manifest.write_bytes(b'EXISTING')
        with self.assertRaises(FileExistsError):
            self.run_export()
        self.assertEqual(manifest.read_bytes(), b'EXISTING')
        self.assertEqual(self.model.calls, [])

    def test_existing_symlink_is_not_used_as_writer_destination(self):
        alternate = self.root / 'different.txt'
        alternate.write_bytes(b'KEEP')
        try:
            self.target.symlink_to(alternate)
        except (OSError, NotImplementedError):
            self.skipTest('symlinks unavailable without Windows privileges')
        with self.assertRaises(FileExistsError):
            self.run_export()
        self.assertEqual(alternate.read_bytes(), b'KEEP')
        self.assertEqual(self.model.calls, [])

    def test_manifest_write_failure_cleans_every_owned_output(self):
        with patch.object(WORKER['json'], 'dump', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                self.run_export()
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(self.model.cursor.page, 2)

    def test_single_page_and_unrelated_filter_keep_old_native_path(self):
        self.model.count = 1
        self.assertIsNone(self.run_export())
        self.assertEqual(self.model.calls, [])
        self.model.count = 3
        self.assertIsNone(self.run_export('writer_wrong_Export'))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_invalid_count_cannot_pass_first_page_only(self):
        for count in (None, False, 0, 2.5, '3', 4097):
            with self.subTest(count=count):
                self.model.count = count
                with self.assertRaises(WORKER['AioWriterPagesUnverified']):
                    self.run_export(plan=False)
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
