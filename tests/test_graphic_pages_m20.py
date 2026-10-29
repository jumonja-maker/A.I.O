"""Actual embedded native-page executor; controlled UNO and real temp files."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlparse
from urllib.request import url2pathname

from app_backend.converter_guard.native_fidelity import NATIVE_FIDELITY_SOURCE

N = {}
exec(compile(NATIVE_FIDELITY_SOURCE, '<native-fidelity-m20>', 'exec'), N)
DRAW = 'com.sun.star.drawing.DrawingDocument'
IMPRESS = 'com.sun.star.presentation.PresentationDocument'


def prop(name, value):
    return SimpleNamespace(Name=name, Value=value)


class Pages:
    def __init__(self, count=3):
        self.count, self.count_calls, self.indices = count, 0, []
    def getCount(self):
        self.count_calls += 1
        return self.count
    def getByIndex(self, index):
        self.indices.append(index)
        return index


class Encoder:
    def __init__(self, supported):
        self.supported = set(supported)
        self.probes, self.calls = [], []
        self.fail_at = self.raise_at = self.empty_at = None
    def supportsMimeType(self, mime):
        self.probes.append(mime)
        return mime in self.supported
    def setSourceDocument(self, page):
        self.page = page
    def filter(self, options):
        data = {item.Name:item.Value for item in options}
        self.calls.append(data)
        path = Path(url2pathname(urlparse(data['URL']).path))
        if self.page == self.raise_at:
            path.write_bytes(b'incomplete')
            raise RuntimeError('controlled_native_failure')
        if self.page != self.empty_at:
            path.write_bytes(('PAGE_' + str(self.page)).encode())
        return self.page != self.fail_at


class GraphicPagesM20(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.pages = Pages()
        self.model = SimpleNamespace(getDrawPages=lambda:self.pages)
        self.encoder = Encoder(('image/x-emf','image/x-wmf','image/x-MS-bmp','image/png','image/svg+xml'))
        self.service_calls = []
        def service(name, context):
            self.service_calls.append(name)
            return self.encoder
        self.remote = SimpleNamespace(ServiceManager=SimpleNamespace(createInstanceWithContext=service))
    def export(self, kind, plan=None):
        return N['aio_export_graphic_pages'](self.model,self.remote,prop,
                                             self.root/('source.'+kind),plan)
    def check_pages(self, kind, mime):
        plan = N['aio_plan_preservation']([IMPRESS],kind,3)
        self.assertIsNotNone(plan)
        outputs = self.export(kind,plan)
        self.assertEqual(len(outputs),4)
        self.assertEqual([p.read_bytes() for p in outputs[:-1]], [b'PAGE_0',b'PAGE_1',b'PAGE_2'])
        self.assertEqual([x['MediaType'] for x in self.encoder.calls], [mime]*3)
        self.assertEqual(self.pages.indices,[0,1,2])
        self.assertEqual(self.pages.count_calls,0,'reuse the preservation count')
        manifest = json.loads(outputs[-1].read_text(encoding='utf-8'))
        self.assertEqual(manifest['pageCount'],3)
        self.assertEqual(manifest['pages'],[{'page':i+1,'file':p.name} for i,p in enumerate(outputs[:-1])])
        receipt = N['aio_verify_preservation'](plan,[p.name for p in outputs])
        self.assertEqual(receipt['observedOutputs'],3)
        self.assertFalse(receipt['qualityCompared'])
    def test_emf_all_pages_with_vector_encoder(self):
        self.check_pages('emf','image/x-emf')
    def test_wmf_all_pages_with_vector_encoder(self):
        self.check_pages('wmf','image/x-wmf')
    def test_bmp_uses_native_registered_mime(self):
        self.check_pages('bmp','image/x-MS-bmp')
    def test_bmp_alias_only_when_native_supports_it(self):
        self.encoder.supported={'image/bmp'}
        self.check_pages('bmp','image/bmp')
        self.assertEqual(self.encoder.probes,['image/x-MS-bmp','image/bmp'])
    def test_existing_png_route_is_preserved(self):
        self.check_pages('png','image/png')
    def test_existing_svg_route_is_preserved(self):
        self.check_pages('svg','image/svg+xml')
    def test_unrelated_target_never_queries_native_page_model(self):
        self.model.getDrawPages=lambda: self.fail('unrelated route queried Draw pages')
        self.assertIsNone(self.export('xhtml'))
        self.assertEqual(self.service_calls,[])
    def test_uncontracted_count_is_read_once(self):
        self.assertEqual(len(self.export('emf')),4)
        self.assertEqual(self.pages.count_calls,1)
    def test_single_page_preserves_existing_store_to_url_path(self):
        self.pages.count=1
        self.assertIsNone(self.export('emf'))
        self.assertEqual(self.service_calls,[])
        self.assertEqual(list(self.root.iterdir()),[])
    def test_invalid_counts_do_not_default_to_first_page(self):
        for count in (None,False,True,0,-1,2.5,'3',4097):
            with self.subTest(count=count):
                self.pages.count=count
                with self.assertRaises(N['AioPreservationIncomplete']):self.export('emf')
        self.assertEqual(self.encoder.calls,[])
    def test_missing_encoder_fails_before_output_creation(self):
        self.encoder=None
        with self.assertRaisesRegex(RuntimeError,'NATIVE_PAGE_ENCODER_UNAVAILABLE'):self.export('emf')
        self.assertEqual(list(self.root.iterdir()),[])
    def test_unsupported_mime_cannot_fall_back_to_page_one(self):
        self.encoder.supported=set()
        with self.assertRaisesRegex(RuntimeError,'NATIVE_PAGE_ENCODER_UNAVAILABLE'):self.export('emf')
        self.assertEqual(self.pages.indices,[])
    def test_existing_primary_is_never_overwritten(self):
        p=self.root/'source.emf';p.write_bytes(b'keep')
        with self.assertRaises(FileExistsError):self.export('emf')
        self.assertEqual(p.read_bytes(),b'keep')
        self.assertEqual(self.encoder.calls,[])
    def test_late_collision_removes_only_own_new_pages(self):
        p=self.root/'source__page_0002.emf';p.write_bytes(b'keep')
        with self.assertRaises(FileExistsError):self.export('emf')
        self.assertEqual(list(self.root.iterdir()),[p])
        self.assertEqual(p.read_bytes(),b'keep')
    def test_existing_manifest_is_never_replaced(self):
        p=self.root/'AIO-pages.json';p.write_bytes(b'keep')
        with self.assertRaises(FileExistsError):self.export('emf')
        self.assertEqual(list(self.root.iterdir()),[p])
        self.assertEqual(p.read_bytes(),b'keep')
    def test_false_native_return_rolls_back_partial_batch(self):
        self.encoder.fail_at=1
        with self.assertRaisesRegex(RuntimeError,'NATIVE_PAGE_EXPORT_FAILED:2'):self.export('emf')
        self.assertEqual(list(self.root.iterdir()),[])
    def test_native_exception_rolls_back_partial_batch(self):
        self.encoder.raise_at=1
        with self.assertRaisesRegex(RuntimeError,'controlled_native_failure'):self.export('wmf')
        self.assertEqual(list(self.root.iterdir()),[])
    def test_empty_native_output_rolls_back_partial_batch(self):
        self.encoder.empty_at=1
        with self.assertRaisesRegex(RuntimeError,'NATIVE_PAGE_OUTPUT_MISSING:2'):self.export('emf')
        self.assertEqual(list(self.root.iterdir()),[])
    def test_manifest_write_failure_rolls_back_pages(self):
        with patch.object(N['json'],'dump',side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError,'disk full'):self.export('emf')
        self.assertEqual(list(self.root.iterdir()),[])
    def test_new_contract_does_not_enable_calc_or_writer_routes(self):
        for target in ('emf','wmf'):
            for service in (DRAW,IMPRESS):
                self.assertEqual(N['aio_plan_preservation']([service],target,3).expected,3)
            for service in ('com.sun.star.sheet.SpreadsheetDocument','com.sun.star.text.TextDocument'):
                self.assertIsNone(N['aio_plan_preservation']([service],target,3))
    def test_sidecars_cannot_mask_a_missing_vector_page(self):
        for target in ('emf','wmf'):
            plan=N['aio_plan_preservation']([DRAW],target,3)
            self.assertIsNotNone(plan)
            with self.assertRaises(N['AioPreservationLoss']):
                N['aio_verify_preservation'](plan,['one.'+target,'AIO-pages.json','extra.json'])


if __name__ == '__main__':
    unittest.main()
