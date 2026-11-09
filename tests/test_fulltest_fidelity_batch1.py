"""Focused regressions for the first FULLTEST fidelity patch."""
from collections import Counter
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from urllib.parse import unquote, urlsplit
from app_backend.converter_guard.graphic_source import tga_contract, tga_import_hint, probe_tga_stream, DRAW
from app_backend.converter_guard.native_fidelity import NATIVE_FIDELITY_SOURCE

N = {}
exec(compile(NATIVE_FIDELITY_SOURCE, '<native_fidelity>', 'exec'), N)


class Items:
    def __init__(self, *items): self.items = items
    def getCount(self): return len(self.items)
    def getByIndex(self, n): return self.items[n]

class Text:
    def __init__(self, value): self.value = value
    def getString(self): return self.value

class Model:
    def __init__(self, *pages): self.pages = Items(*pages)
    def getDrawPages(self): return self.pages


def tga_bytes(width=2, height=3, footer=True):
    head = struct.pack('<BBBHHBHHHHBB', 0, 0, 2, 0, 0, 0, 0, 0, width, height, 32, 8)
    return head + b'\x20\x40\x60\xff' * width * height + (b'\0' * 8 + b'TRUEVISION-XFILE.\0' if footer else b'')


class TgaContractTests(unittest.TestCase):
    def test_unrelated_prefix_does_not_touch_stream(self):
        class Untouchable:
            def read(self, *args):raise AssertionError('extra read')
            def seek(self, *args):raise AssertionError('extra seek')
        for prefix in (b'<?xml version="1.0"?><office:document', b'PK\x03\x04' + b'\0'*30, b'ordinary text '*10):
            self.assertIsNone(probe_tga_stream(Untouchable(), prefix))
    def test_tga_probe_restores_existing_prefix_position(self):
        import io
        stream=io.BytesIO(tga_bytes())
        prefix=stream.read(18)
        self.assertEqual(probe_tga_stream(stream, prefix)['contentClass'],'tga_image')
        self.assertEqual(stream.tell(),len(prefix))
    def inspect(self, raw, name='renamed.txt'):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / name
            path.write_bytes(raw)
            return tga_contract(path)
    def test_footer_identifies_image_regardless_of_extension(self):
        info = self.inspect(tga_bytes())
        self.assertEqual(info['expectedServices'], [DRAW])
        self.assertEqual((info['width'], info['height']), (2, 3))
        self.assertTrue(info['nativeImportRequired'])
    def test_exact_uncompressed_v1(self):
        self.assertEqual(self.inspect(tga_bytes(footer=False))['contentClass'], 'tga_image')
    def test_plain_text_is_not_an_image(self):
        self.assertIsNone(self.inspect(b'Ordinary prose that must remain ordinary text.'))
    def test_truncated_or_bad_layout_is_not_admitted(self):
        raw = tga_bytes(footer=False)
        self.assertIsNone(self.inspect(raw[:-1]))
        self.assertIsNone(self.inspect(raw + b'extra'))
    def test_never_choose_writer_filter(self):
        native = SimpleNamespace(valid=True, filters={'Text': ('com.sun.star.text.TextDocument', ['tga'], {'IMPORT'})})
        self.assertIsNone(tga_import_hint({'contentClass':'tga_image'}, native))
    def test_native_tga_filter_must_be_unambiguous(self):
        native = SimpleNamespace(valid=True, filters={'Targa': (DRAW, ['tga'], {'IMPORT'})})
        self.assertEqual(tga_import_hint({'contentClass':'tga_image'}, native)['filter'], 'Targa')
        native.filters['Other'] = (DRAW, ['tga'], {'IMPORT'})
        self.assertIsNone(tga_import_hint({'contentClass':'tga_image'}, native))


class PresentationTests(unittest.TestCase):
    def test_uses_all_pages_and_nested_shapes(self):
        model = Model(Items(Text('One'), Items(Text('Two'))), Items(Text('Three')))
        self.assertEqual(N['aio_presentation_text'](model), Counter(['One','Two','Three']))
    def test_matching_body_passes(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'a.xhtml';p.write_text('<html><body>AIO <b>00123</b> 42,50 €</body></html>',encoding='utf-8')
            N['aio_verify_presentation_xhtml']([p], Counter({'AIO0012342,50€':1}))
    def test_empty_export_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'a.xhtml';p.write_text('<html><body><img src="illustration.png"/></body></html>',encoding='utf-8')
            with self.assertRaises(N['AioPresentationTextLoss']):
                N['aio_verify_presentation_xhtml']([p], Counter({'missing slide title':1}))
    def test_metadata_does_not_mask_text_loss(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'a.xhtml';p.write_text('<html><head><title>Missing</title></head><body></body></html>')
            with self.assertRaises(N['AioPresentationTextLoss']):
                N['aio_verify_presentation_xhtml']([p], Counter({'Missing':1}))
    def test_alternative_graphics_not_claimed_loss_or_pass(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'a.xhtml';p.write_text('<html><body><svg/></body></html>')
            with self.assertRaises(N['AioPresentationUnverified']):
                N['aio_verify_presentation_xhtml']([p], Counter({'Text':1}))
    def test_duplicate_paragraph_counts(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'a.xhtml';p.write_text('<html><body>Repeat</body></html>')
            with self.assertRaises(N['AioPresentationTextLoss']):
                N['aio_verify_presentation_xhtml']([p], Counter({'Repeat':2}))


class PageTests(unittest.TestCase):
    def test_each_page_has_own_output_and_mapping(self):
        class Exporter:
            def supportsMimeType(self, mime):return mime=='image/png'
            def setSourceDocument(self, page):self.page=page
            def filter(self, props):
                value=dict(props)['URL']; path=unquote(urlsplit(value).path)
                if len(path)>2 and path[0]=='/' and path[2]==':':path=path[1:]
                Path(path).write_bytes(('PAGE'+str(self.page)).encode())
                return True
        exporter=Exporter()
        remote=SimpleNamespace(ServiceManager=SimpleNamespace(createInstanceWithContext=lambda *a:exporter))
        with tempfile.TemporaryDirectory() as d:
            outputs=N['aio_export_graphic_pages'](Model(0,1,2,3),remote,lambda a,b:(a,b),Path(d)/'book.png')
            self.assertEqual(len(outputs),5)
            for n,p in enumerate(outputs[:4]):self.assertEqual(p.read_bytes(),('PAGE'+str(n)).encode())
            import json
            self.assertEqual(json.loads(outputs[-1].read_text())['pageCount'],4)
    def test_no_one_page_behavior_change(self):
        self.assertIsNone(N['aio_export_graphic_pages'](Model(0),None,None,Path('book.png')))
    def test_unknown_page_codec_does_not_fall_back_to_first(self):
        remote=SimpleNamespace(ServiceManager=SimpleNamespace(createInstanceWithContext=lambda *a:None))
        with self.assertRaisesRegex(RuntimeError,'NATIVE_PAGE_ENCODER_UNAVAILABLE'):
            N['aio_export_graphic_pages'](Model(0,1),remote,None,Path('book.png'))


if __name__=='__main__':unittest.main()
