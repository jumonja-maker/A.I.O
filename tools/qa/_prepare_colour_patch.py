"""Disposable preparation: exact-base edits, never shipped in product."""
import hashlib
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]

def modify(name,sha,pairs):
    path=ROOT/name
    text=path.read_text(encoding='utf-8')
    data=text.encode('utf-8')
    assert hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()==sha,name
    for old,new in pairs:
        assert text.count(old)==1,(name,old,text.count(old))
        text=text.replace(old,new)
    path.write_text(text,encoding='utf-8',newline='\n')

modify('app_backend/converter_guard/uno_session.py','03f5bcb0d68e2fb1b34cc01f6e2f4bdffba66afd',[
("from .native_fidelity import NATIVE_FIDELITY_SOURCE", "from .native_fidelity import NATIVE_FIDELITY_SOURCE\nfrom .native_pptx_colours import PPTX_COLOUR_SOURCE"),
("WORKER_SOURCE = DIF_CORE_SOURCE + SHEET_OUTPUT_SOURCE + NATIVE_FIDELITY_SOURCE + r'''", "WORKER_SOURCE = DIF_CORE_SOURCE + SHEET_OUTPUT_SOURCE + NATIVE_FIDELITY_SOURCE + PPTX_COLOUR_SOURCE + r'''"),
("        dif_import_check = None", "        dif_import_check = None\n        pptx_colour_check = None"),
("                            model.storeToURL(target.as_uri(), tuple(export_options))", "                            pptx_colour_check = aio_export_theme_free_pptx(\n                                model, source, actual_filter, target, export_options, req['exportFilter'])\n                            if pptx_colour_check is None:\n                                model.storeToURL(target.as_uri(), tuple(export_options))"),
("                    warnings = []", "                    warnings = []\n                    if pptx_colour_check is not None:\n                        result['pptxColourPreservation'] = pptx_colour_check\n                        if pptx_colour_check.get('status') == 'not_checked':\n                            warnings.append('NATIVE_OUTPUT_NOT_CHECKED')"),
("(AioPresentationUnverified, AioWriterPagesUnverified, AioPreservationIncomplete)", "(AioPresentationUnverified, AioWriterPagesUnverified, AioPreservationIncomplete, AioPptxColourUnverified)"),
])
modify('app_backend/converter_guard/adapter.py','a16ff8a0b5def5dbde8e8fa4d0c1f6c01b22204a',[
("                        if result.get('nativeCloseInterrupted'):", "                        colour_receipt = result.get('pptxColourPreservation')\n                        if result.get('ok') and isinstance(colour_receipt, dict):\n                            self._local.export_info['resultChecks'] = list(\n                                self._local.export_info.get('resultChecks', ())) + [colour_receipt]\n                        if result.get('nativeCloseInterrupted'):"),
])
modify('tools/qa/fulltest_vector_pages_native.py','211323785e7412a34b87c4eb91570bff09b79b68',[
("            # Diagnostic, not a thirteenth accepted page-export case. Retain\n            # the original failing FODP->PPTX roundtrip independently: the\n            # known upstream colour issue must not disappear with the fixture.", "            # The former OPEN regression is now mandatory, independently of\n            # the twelve direct-input page tests. Never repair/recolour its\n            # native output or accept altered markers as expected truth."),
("            roundtrip_audit={", "            assert all(v['status']=='MATCH' for v in observations), observations\n            roundtrip_audit={"),
("'status':'OPEN' if any(v['status']=='MISMATCH' for v in observations) else 'NOT_REPRODUCED'", "'status':'PASS'"),
("                'resolved':False,", "                'resolved':True,\n                'resolutionScope':'synthetic_theme_free_odf_solid_rgb_only',"),
("result={'status':'FAIL','error':str(error),'cases':cases,'separateRoundtripAudit':roundtrip_audit}", "result={'status':'FAIL','error':str(error),'code':getattr(error,'code',None),\n                'diagnostic':getattr(error,'diagnostic',None),'cases':cases,'separateRoundtripAudit':roundtrip_audit}"),
])
path=ROOT/'.github/workflows/aio-hosted-qa.yml'
s=path.read_text(encoding='utf-8')
old='run: python tools/qa/fulltest_vector_pages_native.py --executable "$AIO_QA_ENGINE" --output "$RUNNER_TEMP/aio-vector-pages-native"'
assert s.count(old)==1,(str(path),'native step anchor')
s=s.replace(old,'run: |\n          python tools/qa/fulltest_vector_pages_native.py --executable "$AIO_QA_ENGINE" --output "$RUNNER_TEMP/aio-vector-pages-native"\n          python tools/qa/fulltest_pptx_colours_native.py --executable "$AIO_QA_ENGINE" --output "$RUNNER_TEMP/aio-pptx-colours-native"')
s=s.replace("'aio-vector-pages-native/evidence.json','aio-hosted-frozen/evidence.json'", "'aio-vector-pages-native/evidence.json','aio-pptx-colours-native/evidence.json','aio-hosted-frozen/evidence.json'")
path.write_text(s,encoding='utf-8',newline='\n')
modify('app_backend/converter_guard/native_pptx_colours.py','61797fd08bc4a3abff549b08c1442b5b1a31da69',[
("                shape.setPropertyValue('FillComplexColor', None)", "                # Attribute assignment uses the UNO property's interface type.\n                # setPropertyValue(..., None) sends a void Any and can silently\n                # leave XComplexColor unchanged in PyUNO.\n                shape.FillComplexColor = None"),
])
modify('tests/test_pptx_colour_preservation.py','cb28539ed7502d8ce9c3f3a4320ed6329fdedc4b',[
("    def supportsService(self,name): return False\n", "    @property\n    def FillComplexColor(self): return self.values['FillComplexColor']\n    @FillComplexColor.setter\n    def FillComplexColor(self,value): Shape.setPropertyValue(self,'FillComplexColor',value)\n    def supportsService(self,name): return False\n"),
("    def test_groups_and_master_shapes_processed(self):", "    def test_interface_null_uses_typed_property_assignment_not_void_any(self):\n        class VoidAnyShape(Shape):\n            def setPropertyValue(self,name,value):\n                if name=='FillComplexColor' and value is None:\n                    return  # actual PyUNO void Any does not clear the interface\n                return super().setPropertyValue(name,value)\n        shape=VoidAnyShape();model=Model(shape)\n        result=self.run_case(model)\n        self.assertEqual(result['normalizedFills'],1)\n        self.assertEqual(shape.values,shape.original)\n    def test_groups_and_master_shapes_processed(self):"),
])
print('PREPARED_COLOUR_PRODUCT_FILES=6')
