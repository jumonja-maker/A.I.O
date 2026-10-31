"""Calc raster pages: test the exact embedded worker helper without office I/O."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from urllib.parse import unquote, urlsplit
from app_backend.converter_guard.native_fidelity import NATIVE_FIDELITY_SOURCE

N = {}
exec(compile(NATIVE_FIDELITY_SOURCE, '<aio_native_fidelity>', 'exec'), N)


def local_path(url):
    path = unquote(urlsplit(url).path)
    if len(path) > 2 and path[0] == '/' and path[2] == ':':
        path = path[1:]
    return Path(path)


class Device:
    def __init__(self, owner, w, h):
        self.owner, self.w, self.h = owner, w, h
        self.scaled = False
        self.pushed = self.popped = 0
    def getInfo(self):
        return NS(PixelPerMeterX=4000., PixelPerMeterY=4000.)
    def createGraphics(self): return self
    def setFillColor(self, value): pass
    def setLineColor(self, value): pass
    def drawRect(self, *args): pass
    def push(self): self.pushed += 1
    def pop(self): self.popped += 1; self.scaled = False
    def createBitmap(self, x, y, w, h):
        if self.scaled:
            raise AssertionError('Native scale was not restored before bitmap capture')
        if (w,h) != (self.w,self.h):
            raise AssertionError('Bitmap capture was not in device pixels')
        width = w - 1 if self.owner.bad_bitmap else w
        return NS(getSize=lambda: NS(Width=width, Height=h))


class Fixture:
    def __init__(self):
        self.count = 3
        self.count_calls = 0
        self.rendered = []
        self.devices = []
        self.bad_bitmap = False
        self.missing_output = False
        self.fail_page = None
        self.sheet_names = ('../unsafe name', 'Second')
        self.bad_range = False
        self.huge = False
        self.controller = object()
        self.remote = NS(ServiceManager=NS(createInstanceWithContext=self.service))
    def service(self, name, context):
        if name == 'com.sun.star.awt.Toolkit':
            return NS(createScreenCompatibleDevice=self.device)
        if name == 'com.sun.star.graphic.GraphicProvider':
            return NS(queryGraphic=lambda props: dict(props)['Bitmap'], storeGraphic=self.store)
        raise AssertionError('Unexpected service/source reload: '+name)
    def device(self, w, h):
        dev=Device(self,w,h);self.devices.append(dev);return dev
    def getCurrentController(self): return self.controller
    def getSheets(self): return NS(getElementNames=lambda:self.sheet_names)
    def getRendererCount(self, selection, options):
        assert selection is self
        self.count_calls += 1
        self.check_options(options)
        return self.count
    def check_options(self, values):
        opts=dict(values)
        assert opts['PrintContent'] == 0 and opts['PrintRange'] == 0
        assert opts['PageRange'] == '' and opts['RenderToGraphic'] is True
        assert opts['View'] is self.controller
    def getRenderer(self, index, selection, options):
        self.check_options(options)
        width,height=(10000,20000) if index<2 else (20000,10000)
        if self.huge: width=height=10000000
        return (NS(Name='PageSize',Value=NS(Width=width,Height=height)),
                NS(Name='SourceRange',Value=NS(Sheet=99 if self.bad_range else int(index==2),
                    StartColumn=0,EndColumn=1,StartRow=index*10,EndRow=index*10+9)))
    def render(self, index, selection, options):
        self.check_options(options)
        opts=dict(options)
        self.rendered.append(index)
        opts['RenderDevice'].scaled=True
        if index==self.fail_page: raise RuntimeError('controlled render failure')
        assert opts['IsFirstPage'] == (index==0)
        assert opts['IsLastPage'] == (index==self.count-1)
    def store(self, graphic, options):
        path=local_path(dict(options)['URL'])
        if not self.missing_output: path.write_bytes(b'native-test-output')
    def storeToURL(self, *args):
        raise AssertionError('No temporary PDF/second export is permitted')


class CalcPageTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.fixture=Fixture()
    def run_export(self, kind='png', name='calc_png_Export'):
        return N['aio_export_calc_pages'](self.fixture,self.fixture.remote,
            lambda k,v:(k,v),self.root/('book.'+kind),name)
    def test_all_print_pages_and_source_mapping(self):
        paths,plan=self.run_export()
        self.assertEqual(self.fixture.count_calls,1)
        self.assertEqual(self.fixture.rendered,[0,1,2])
        self.assertEqual(len(paths),4)
        self.assertEqual(plan.expected,3)
        m=json.loads(paths[-1].read_text(encoding='utf-8'))
        self.assertEqual(m['pageCount'],3)
        self.assertEqual([x['sheetIndex'] for x in m['pages']],[0,0,1])
        self.assertEqual([x['pixelSize'] for x in m['pages']],[[400,800],[400,800],[800,400]])
        self.assertEqual(m['scope'],'loaded_calc_print_layout')
        self.assertTrue(m['respectsPrintSettings'])
        self.assertFalse(m['qualityCompared'])
    def test_native_scale_restored_before_capture(self):
        self.run_export()
        self.assertEqual([(d.pushed,d.popped) for d in self.fixture.devices[1:]],[(1,1)]*3)
    def test_native_scale_restored_on_failure(self):
        self.fixture.fail_page=1
        with self.assertRaisesRegex(RuntimeError,'controlled render'): self.run_export()
        self.assertEqual(self.fixture.devices[-1].popped,1)
        self.assertEqual(list(self.root.iterdir()),[])
    def test_filename_uses_no_sheet_supplied_path(self):
        paths,plan=self.run_export()
        self.assertTrue(all(p.parent==self.root for p in paths))
        self.assertEqual(paths[1].name,'book__page_0002.png')
    def test_matching_guard_receipt_has_print_scope(self):
        paths,plan=self.run_export()
        receipt=N['aio_verify_preservation'](plan,[p.name for p in paths])
        self.assertEqual(receipt['expectedUnits'],3)
        self.assertEqual(receipt['observedOutputs'],3)
        self.assertEqual(receipt['scope'],'loaded_calc_print_layout')
        self.assertFalse(receipt['qualityCompared'])
    def test_missing_page_cannot_obtain_receipt(self):
        paths,plan=self.run_export()
        with self.assertRaises(N['AioPreservationLoss']):
            N['aio_verify_preservation'](plan,[p.name for p in paths[1:]])
    def test_invalid_counts_never_default_to_one(self):
        for count in (None,True,False,0,-1,1.5,'3',4097):
            with self.subTest(count=count):
                self.fixture.count=count
                with self.assertRaises(N['AioPreservationIncomplete']): self.run_export()
                self.assertEqual(list(self.root.iterdir()),[])
    def test_bad_page_range_rejected(self):
        self.fixture.bad_range=True
        with self.assertRaisesRegex(N['AioPreservationIncomplete'],'source_range'): self.run_export()
        self.assertEqual(list(self.root.iterdir()),[])
    def test_canvas_budget_before_full_allocation(self):
        self.fixture.huge=True
        with self.assertRaisesRegex(N['AioPreservationIncomplete'],'pixel_budget'): self.run_export()
        self.assertEqual(len(self.fixture.devices),1)
    def test_bitmap_dimension_mismatch_rejected(self):
        self.fixture.bad_bitmap=True
        with self.assertRaisesRegex(N['AioPreservationIncomplete'],'mapping'): self.run_export()
        self.assertEqual(list(self.root.iterdir()),[])
    def test_missing_encoded_file_rejected(self):
        self.fixture.missing_output=True
        with self.assertRaises(N['AioPreservationLoss']): self.run_export()
        self.assertEqual(list(self.root.iterdir()),[])
    def test_existing_page_never_overwritten_or_deleted(self):
        keep=self.root/'book__page_0002.png';keep.write_bytes(b'keep')
        with self.assertRaises(FileExistsError): self.run_export()
        self.assertEqual(keep.read_bytes(),b'keep')
        self.assertEqual(list(self.root.iterdir()),[keep])
    def test_existing_manifest_never_overwritten_or_deleted(self):
        keep=self.root/'AIO-pages.json';keep.write_bytes(b'keep')
        with self.assertRaises(FileExistsError): self.run_export()
        self.assertEqual(keep.read_bytes(),b'keep')
        self.assertEqual(list(self.root.iterdir()),[keep])
    def test_all_jpeg_aliases_use_same_native_encoder(self):
        for kind in ('jpg','jpeg','jfif','jif','jpe'):
            with self.subTest(kind=kind):
                paths,plan=self.run_export(kind,'calc_jpg_Export')
                self.assertEqual(plan.target,kind)
                for p in paths:p.unlink()
    def test_unrelated_or_mismatched_routes_do_no_work(self):
        for kind,filter_name in (('pdf','calc_pdf_Export'),('png','writer_png_Export'),
                                 ('jpg','calc_png_Export'),('svg','calc_svg_Export')):
            self.assertIsNone(N['aio_export_calc_pages'](None,None,None,Path('book.'+kind),filter_name))
    def test_invalid_geometry_or_dpi_is_not_silently_scaled(self):
        dimensions=N['aio_calc_page_dimensions']
        for width,height,dpi in ((0,1000,4000),(True,1000,4000),(1000,-1,4000),
                                 (1000,1000,float('nan')),(1000,1000,float('inf'))):
            with self.subTest(width=width,height=height,dpi=dpi):
                with self.assertRaises(N['AioPreservationIncomplete']):
                    dimensions(NS(Width=width,Height=height),NS(PixelPerMeterX=dpi,PixelPerMeterY=dpi))


if __name__=='__main__': unittest.main()
