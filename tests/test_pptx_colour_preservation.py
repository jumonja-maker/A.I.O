"""Actual worker helper, controlled UNO boundary, real source XML and ZIP."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile
from app_backend.converter_guard.native_pptx_colours import PPTX_COLOUR_SOURCE

OFFICE='urn:oasis:names:tc:opendocument:xmlns:office:1.0'
DRAW='urn:oasis:names:tc:opendocument:xmlns:drawing:1.0'
IMPORT='OpenDocument Presentation Flat XML'
EXPORT='Impress MS PowerPoint 2007 XML'

def xml(root='document', extra='', attrs=''):
    return ('<office:'+root+' xmlns:office="'+OFFICE+'" xmlns:draw="'+DRAW+'" '+attrs+'>'
            '<draw:rect draw:fill-color="#FF0000"/>'+extra+'</office:'+root+'>').encode()

class Colour:
    def __init__(self, theme=4): self.theme=theme
    def getThemeColorType(self): return self.theme

class Shape:
    def __init__(self, rgb=0xff0000, theme=4, style='SOLID', alpha=0, failure=None):
        self.values={'FillColor':rgb, 'FillComplexColor':Colour(theme),
                     'FillStyle':SimpleNamespace(value=style), 'FillTransparence':alpha}
        self.original=self.values.copy(); self.writes=[]; self.failure=failure
    def supportsService(self,name): return False
    def getPropertySetInfo(self):
        return SimpleNamespace(hasPropertyByName=lambda name:name in self.values)
    def getPropertyValue(self,name): return self.values[name]
    def setPropertyValue(self,name,value):
        self.writes.append((name,value))
        self.values[name]=value
        if name=='FillComplexColor' and value is None:
            if self.failure=='setter': raise RuntimeError('setter failed after modification')
            if self.failure=='rgb': self.values['FillColor']=1
            if self.failure=='binding': self.values[name]=Colour(4)
            if self.failure=='alpha': self.values['FillTransparence']=70
        if name=='FillComplexColor' and value is not None and self.failure=='restore':
            raise RuntimeError('restore failed')

class Collection:
    def __init__(self,*items, count=None): self.items=list(items); self.count=count
    def getCount(self): return len(self.items) if self.count is None else self.count
    def getByIndex(self,n): return self.items[n]
    def supportsService(self,name): return name=='com.sun.star.drawing.GroupShape'

class Model:
    def __init__(self,*shapes,master=None, failure=False):
        self.pages=Collection(Collection(*shapes)); self.masters=Collection(Collection(*(master or [])))
        self.shapes=shapes; self.stores=[]; self.failure=failure
    def getDrawPages(self): return self.pages
    def getMasterPages(self): return self.masters
    def storeToURL(self,url,options):
        self.stores.append((url,options))
        if self.failure: raise RuntimeError('native export failed')

class PptxColourTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.source=self.root/'input.fodp'
        self.source.write_bytes(xml());self.target=self.root/'result.pptx'
        self.ns={'Path':Path};exec(PPTX_COLOUR_SOURCE,self.ns)
        self.scope=self.ns['aio_pptx_colour_scope'];self.run_export=self.ns['aio_export_theme_free_pptx']
        self.error=self.ns['AioPptxColourUnverified']
    def run_case(self,model,**kwargs):
        return self.run_export(model,self.source,kwargs.get('import_filter',IMPORT),
                               kwargs.get('target',self.target),('native-options',),kwargs.get('export_filter',EXPORT))
    def package(self,content=None,styles=None):
        path=self.root/'source.odp'
        with ZipFile(path,'w') as z:
            z.writestr('content.xml',content or xml('document-content'))
            if styles is not False:z.writestr('styles.xml',styles or xml('document-styles'))
        return path
    def test_simple_flat_source_proven(self):
        result=self.scope(self.source,IMPORT,EXPORT)
        self.assertTrue(result['eligible']);self.assertEqual(result['rgbDeclarations'],1)
    def test_filter_identity_not_suffix(self):
        path=self.root/'renamed.bin';path.write_bytes(xml())
        self.assertTrue(self.scope(path,IMPORT,EXPORT)['eligible'])
    def test_unrelated_filter_never_reads_source(self):
        for imp,exp in [('Impress MS PowerPoint 2007 XML',EXPORT),(IMPORT,'other')]:
            with patch.object(Path,'open',side_effect=AssertionError('unexpected read')):
                self.assertIsNone(self.scope(self.source,imp,exp))
    def test_unrelated_target_never_queries_model(self):
        self.assertIsNone(self.run_case(object(),target=self.root/'result.pdf'))
    def test_themed_pptx_not_inspected_or_exported_by_helper(self):
        with patch.object(Path,'open',side_effect=AssertionError('unexpected read')):
            self.assertIsNone(self.run_case(object(),import_filter=EXPORT))
    def test_declared_theme_preserved(self):
        self.source.write_bytes(xml(extra='<office:theme/>'))
        self.assertEqual(self.scope(self.source,IMPORT,EXPORT)['reason'],'declared_theme')
    def test_foreign_extension_preserved(self):
        self.source.write_bytes(xml(extra='<x:colour/>',attrs='xmlns:x="urn:foreign"'))
        self.assertEqual(self.scope(self.source,IMPORT,EXPORT)['reason'],'extension_semantics')
    def test_unknown_attributes_preserved(self):
        self.source.write_bytes(xml(attrs='xmlns:x="urn:foreign" x:binding="accent1"'))
        self.assertFalse(self.scope(self.source,IMPORT,EXPORT)['eligible'])
    def test_dtd_not_expanded(self):
        self.source.write_bytes(b'<!DOCTYPE x [<!ENTITY a "red">]>'+xml())
        self.assertEqual(self.scope(self.source,IMPORT,EXPORT)['reason'],'doctype_or_entity')
    def test_malformed_xml_not_proven(self):
        self.source.write_bytes(xml()[:-8])
        self.assertFalse(self.scope(self.source,IMPORT,EXPORT)['eligible'])
    def test_budget_is_bounded_and_not_success(self):
        self.ns['_AIO_COLOUR_XML_BUDGET']=10
        self.assertEqual(self.scope(self.source,IMPORT,EXPORT)['reason'],'xml_inspection_budget')
    def test_packaged_styles_included(self):
        path=self.package(styles=xml('document-styles','<office:theme/>'))
        self.assertEqual(self.scope(path,'impress8',EXPORT)['reason'],'declared_theme')
    def test_package_without_styles_unknown(self):
        self.assertFalse(self.scope(self.package(styles=False),'impress8',EXPORT)['eligible'])
    def test_valid_package(self):
        self.assertTrue(self.scope(self.package(),'impress8',EXPORT)['eligible'])
    def test_duplicate_styles_unknown(self):
        path=self.package()
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            with ZipFile(path,'a') as z:z.writestr('styles.xml',xml('document-styles'))
        self.assertFalse(self.scope(path,'impress8',EXPORT)['eligible'])
    def test_no_rgb_not_proven(self):
        self.source.write_bytes(xml().replace(b' draw:fill-color="#FF0000"',b''))
        self.assertEqual(self.scope(self.source,IMPORT,EXPORT)['reason'],'no_explicit_rgb_fill')
    def test_bad_rgb_not_proven(self):
        self.source.write_bytes(xml().replace(b'#FF0000',b'#broken'))
        self.assertFalse(self.scope(self.source,IMPORT,EXPORT)['eligible'])
    def test_unknown_scope_keeps_native_export_without_mutation(self):
        self.source.write_bytes(xml(extra='<office:theme/>'))
        shape=Shape();model=Model(shape);result=self.run_case(model)
        self.assertEqual(result['status'],'not_checked');self.assertEqual(shape.writes,[])
        self.assertEqual(len(model.stores),1);self.assertFalse(result['qualityCompared'])
    def test_clear_only_binding_and_restore_after_single_native_export(self):
        shape=Shape(alpha=25);model=Model(shape)
        observed=[]
        def store(url,options):
            observed.append(shape.values.copy());model.stores.append((url,options))
        model.storeToURL=store
        before=self.source.read_bytes();result=self.run_case(model)
        self.assertIsNone(observed[0]['FillComplexColor'])
        self.assertEqual(observed[0]['FillColor'],0xff0000)
        self.assertEqual(observed[0]['FillTransparence'],25)
        self.assertEqual(shape.values,shape.original);self.assertEqual(len(model.stores),1)
        self.assertEqual(result['normalizedFills'],1);self.assertEqual(result['status'],'checked_subset')
        self.assertFalse(result['qualityCompared']);self.assertEqual(before,self.source.read_bytes())
    def test_groups_and_master_shapes_processed(self):
        nested=Shape();master=Shape(rgb=255)
        result=self.run_case(Model(Collection(nested),master=[master]))
        self.assertEqual(result['normalizedFills'],2)
        self.assertEqual(nested.values,nested.original);self.assertEqual(master.values,master.original)
    def test_rgb_without_binding_untouched(self):
        shape=Shape(theme=-1);result=self.run_case(Model(shape))
        self.assertEqual(result['normalizedFills'],0);self.assertEqual(shape.writes,[])
    def test_gradient_and_no_fill_untouched(self):
        shapes=[Shape(style='GRADIENT'),Shape(style='NONE')]
        result=self.run_case(Model(*shapes));self.assertEqual(result['normalizedFills'],0)
        self.assertTrue(all(not s.writes for s in shapes))
    def test_shape_budget_before_any_mutation(self):
        self.ns['_AIO_COLOUR_SHAPE_BUDGET']=1
        shape=Shape();model=Model(shape)
        with self.assertRaises(self.error):self.run_case(model)
        self.assertEqual(shape.writes,[]);self.assertEqual(model.stores,[])
    def test_unknown_count_before_mutation(self):
        shape=Shape();model=Model(shape);model.pages.count=True
        with self.assertRaises(self.error):self.run_case(model)
        self.assertEqual(shape.writes,[]);self.assertEqual(model.stores,[])
    def test_bad_rgb_before_mutation(self):
        shape=Shape(rgb=-1);model=Model(shape)
        with self.assertRaises(self.error):self.run_case(model)
        self.assertEqual(shape.writes,[]);self.assertEqual(model.stores,[])
    def test_changed_rgb_rejects_export_and_restores(self):
        shape=Shape(failure='rgb');model=Model(shape)
        with self.assertRaises(self.error):self.run_case(model)
        self.assertEqual(model.stores,[]);self.assertEqual(shape.values,shape.original)
    def test_binding_not_cleared_rejects_export(self):
        shape=Shape(failure='binding');model=Model(shape)
        with self.assertRaises(self.error):self.run_case(model)
        self.assertEqual(model.stores,[]);self.assertEqual(shape.values,shape.original)
    def test_partial_setter_failure_restores_prior_shapes(self):
        a=Shape();b=Shape(failure='setter');model=Model(a,b)
        with self.assertRaises(self.error):self.run_case(model)
        self.assertEqual(model.stores,[])
        self.assertEqual(a.values,a.original);self.assertEqual(b.values,b.original)
    def test_native_failure_restores(self):
        shape=Shape();model=Model(shape,failure=True)
        with self.assertRaisesRegex(RuntimeError,'native export failed'):self.run_case(model)
        self.assertEqual(shape.values,shape.original)
    def test_restore_failure_not_reported_as_success(self):
        shape=Shape(failure='restore');model=Model(shape)
        with self.assertRaisesRegex(self.error,'restore_failed'):self.run_case(model)
        self.assertEqual(len(model.stores),1)
    def test_changed_transparency_not_reported_as_success(self):
        shape=Shape(failure='alpha');model=Model(shape)
        with self.assertRaises(self.error):self.run_case(model)
        self.assertEqual(model.stores,[]);self.assertEqual(shape.values,shape.original)

if __name__=='__main__':unittest.main()
