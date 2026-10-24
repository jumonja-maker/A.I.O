"""Live native TXT capability resolves overlay uncertainty for one exact route."""
from types import SimpleNamespace
import unittest

from app_backend.converter_guard.export_recipe import native_export_recipe
from app_backend.converter_guard.routes import Registry
from app_backend.converter_guard.uno_session import WORKER_SOURCE

W='com.sun.star.text.TextDocument'
WEB='com.sun.star.text.WebDocument'
C='com.sun.star.sheet.SpreadsheetDocument'


def inspector():
    namespace={'__name__':'runtime_text_capability'}
    exec(compile(WORKER_SOURCE,'runtime_text_capability','exec'),namespace)
    return namespace['inspect_native_text_export']


def native_registry():
    native=Registry([], '/private/native/program/soffice.exe')
    native.valid=True
    native.import_services=frozenset({W,WEB,C})
    native.exports={'txt':{W}}
    native.uncertain_extensions={'txt'}
    return native


def proof(native, services=None):
    return {'basis':'native_preflight_uno_document','state':'detected','modelLoaded':True,
        'nativeTextDocument':True,'registryFingerprint':native.fingerprint,
        'documentServices':services or [W],'nativeExportFilters':{'txt':'Text (encoded)'}}


def remote(flags=3, service=W, extensions=('txt',), *, filter_exists=True, type_exists=True):
    events=[]
    def properties(values):
        return tuple(SimpleNamespace(Name=k,Value=v) for k,v in values.items())
    factory=SimpleNamespace(hasByName=lambda name:filter_exists and name=='Text (encoded)',
        getByName=lambda name:properties({'Flags':flags,'DocumentService':service,'Type':'text_Text'}))
    types=SimpleNamespace(hasByName=lambda name:type_exists and name=='text_Text',
        getByName=lambda name:properties({'Extensions':extensions}))
    def create(name, context):
        events.append(name)
        if name=='com.sun.star.document.FilterFactory':return factory
        if name=='com.sun.star.document.TypeDetection':return types
        raise AssertionError('Unrequested native service '+name)
    return SimpleNamespace(ServiceManager=SimpleNamespace(createInstanceWithContext=create)),events


class NativeTextCapability(unittest.TestCase):
    def test_live_registry_proves_exact_filter_service_export_flag_and_type_extension(self):
        native,events=remote()
        filters,reason=inspector()(native,True,[W])
        self.assertEqual(filters,{'txt':'Text (encoded)'})
        self.assertEqual(reason,'verified')
        self.assertEqual(events,['com.sun.star.document.FilterFactory','com.sun.star.document.TypeDetection'])

    def test_filter_existence_alone_does_not_admit_import_only_or_wrong_service(self):
        for kwargs,reason in (({'flags':1},'export_flag_missing'),
                             ({'flags':True},'export_flag_missing'),
                             ({'service':C},'document_service_mismatch'),
                             ({'filter_exists':False},'filter_missing')):
            with self.subTest(kwargs=kwargs):
                native,_=remote(**kwargs)
                self.assertEqual(inspector()(native,True,[W]),({},reason))

    def test_live_type_must_exist_and_explicitly_include_txt(self):
        for kwargs,reason in (({'extensions':('csv',)},'extensions_mismatch'),
                             ({'extensions':'txt'},'extensions_mismatch'),
                             ({'type_exists':False},'type_missing')):
            with self.subTest(kwargs=kwargs):
                native,_=remote(**kwargs)
                self.assertEqual(inspector()(native,True,[W]),({},reason))

    def test_real_text_interface_is_required_before_any_capability_query(self):
        for text_document,services in ((False,[W]),(True,[C]),(None,[WEB])):
            native,events=remote()
            self.assertEqual(inspector()(native,text_document,services),({},'text_document_not_confirmed'))
            self.assertEqual(events,[])

    def test_live_writer_web_can_use_writer_text_export_only_with_xtext_proof(self):
        native,_=remote()
        self.assertEqual(inspector()(native,True,[WEB])[0],{'txt':'Text (encoded)'})
        registry=native_registry();current=proof(registry,[WEB])
        self.assertEqual(registry.observed_decision('txt',current),'registered')
        self.assertEqual(native_export_recipe(registry,'txt',current).filter_options,'UTF8')
        current['nativeTextDocument']=False
        self.assertEqual(registry.observed_decision('txt',current),'undetermined')

    def test_verified_runtime_filter_resolves_only_current_txt_overlay_uncertainty(self):
        native=native_registry();current=proof(native)
        self.assertEqual(native.decide('txt',{W}),'undetermined')
        self.assertEqual(native.observed_decision('txt',current),'registered')
        recipe=native_export_recipe(native,'txt',current)
        self.assertEqual((recipe.filter_name,recipe.filter_options),('Text (encoded)','UTF8'))
        self.assertIsNone(native.route_reason('txt',current))
        self.assertEqual(native.observed_decision('csv',current),'undetermined')
        self.assertIsNone(native_export_recipe(native,'csv',current))

    def test_stale_nonloaded_nontext_and_unverified_filter_proofs_do_not_override_static_unknown(self):
        native=native_registry()
        for change in ({'registryFingerprint':'other-installation'}, {'modelLoaded':False},
                       {'nativeTextDocument':False},{'state':'undetermined'},
                       {'basis':'native_preflight_document_kind'},
                       {'documentServices':[C]}, {'documentServices':['unknown-service']},
                       {'nativeExportFilters':{'txt':'Unverified custom filter'}},
                       {'nativeExportFilters':{}}):
            with self.subTest(change=change):
                current={**proof(native),**change}
                self.assertNotEqual(native.observed_decision('txt',current),'registered')
                self.assertIsNone(native_export_recipe(native,'txt',current))


if __name__=='__main__':
    unittest.main()
