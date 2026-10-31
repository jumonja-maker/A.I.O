"""Exercise real embedded helpers with counted model calls and controlled exports."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from urllib.parse import urlsplit
from urllib.request import url2pathname
import unittest

from app_backend.converter_guard.native_fidelity import NATIVE_FIDELITY_SOURCE

N = {}
exec(compile(NATIVE_FIDELITY_SOURCE, '<native contract test>', 'exec'), N)
WRITER = ('com.sun.star.text.TextDocument',)
DRAW = ('com.sun.star.drawing.DrawingDocument',)
CALC = ('com.sun.star.sheet.SpreadsheetDocument',)


def local_path(uri):
    return Path(url2pathname(urlsplit(uri).path))


class CountedItems:
    def __init__(self, count):
        self.count, self.queries = count, 0
    def getCount(self):
        self.queries += 1
        return self.count
    def getByIndex(self, index):
        if not 0 <= index < self.count:
            raise IndexError(index)
        return index


class NativeContractBridgeTests(unittest.TestCase):
    def test_unrelated_export_does_not_query_model(self):
        class Untouchable:
            def __getattr__(self, name):
                raise AssertionError('unnecessary model query: ' + name)
        self.assertIsNone(N['aio_native_preservation_plan'](Untouchable(), WRITER, 'pdf'))

    def test_sheet_count_is_read_once_before_export(self):
        sheets = CountedItems(4)
        model = SimpleNamespace(getSheets=lambda: sheets)
        plan = N['aio_native_preservation_plan'](model, CALC, 'sylk')
        self.assertEqual(plan.expected, 4)
        self.assertEqual(sheets.queries, 1)

    def test_missing_model_metadata_fails_instead_of_assuming_one_page(self):
        with self.assertRaises(N['AioPreservationIncomplete']):
            N['aio_native_preservation_plan'](object(), WRITER, 'png')

    def test_draw_executor_reuses_count_and_receipt_blocks_dropped_output(self):
        pages = CountedItems(3)
        model = SimpleNamespace(getDrawPages=lambda: pages)
        class Exporter:
            def supportsMimeType(self, mime):
                return mime == 'image/png'
            def setSourceDocument(self, page):
                self.page = page
            def filter(self, props):
                local_path(dict(props)['URL']).write_bytes(('PAGE_%d' % self.page).encode())
                return True
        exporter = Exporter()
        remote = SimpleNamespace(ServiceManager=SimpleNamespace(
            createInstanceWithContext=lambda *args: exporter))
        plan = N['aio_native_preservation_plan'](model, DRAW, 'png')
        with TemporaryDirectory() as directory:
            outputs = N['aio_export_graphic_pages'](model, remote,
                lambda name, value: (name, value), Path(directory) / 'book.png', plan)
            self.assertEqual(pages.queries, 1)
            receipt = N['aio_verify_preservation'](plan, [p.name for p in outputs])
            self.assertEqual(receipt['observedOutputs'], 3)
            # Keep the manifest and two pages: metadata must not hide page loss.
            with self.assertRaises(N['AioPreservationLoss']):
                N['aio_verify_preservation'](plan, [p.name for p in outputs if '__page_0003' not in p.name])

    def test_writer_executor_reuses_layout_count_and_restores_cursor(self):
        class Cursor:
            page = 2
            def getPage(self):
                return self.page
            def jumpToPage(self, page):
                self.page = page
                return True
        cursor = Cursor()
        class Controller:
            queries = 0
            @property
            def PageCount(self):
                self.queries += 1
                return 3
            def getViewCursor(self):
                return cursor
        controller = Controller()
        class Model:
            def getCurrentController(self):
                return controller
            def storeToURL(self, uri, options):
                local_path(uri).write_bytes(('PAGE_%d' % cursor.page).encode())
        model = Model()
        plan = N['aio_native_preservation_plan'](model, WRITER, 'png')
        with TemporaryDirectory() as directory:
            outputs = N['aio_export_writer_pages'](model, None,
                Path(directory) / 'book.png', (), 'writer_png_Export', plan)
            self.assertEqual(controller.queries, 1)
            self.assertEqual(cursor.page, 2)
            self.assertEqual(N['aio_verify_preservation'](plan, [p.name for p in outputs])['observedOutputs'], 3)


if __name__ == '__main__':
    unittest.main()
