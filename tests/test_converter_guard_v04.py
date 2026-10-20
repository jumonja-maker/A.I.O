"""Native route evidence, mixed sources, renamed files and bounded observation."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from xml.sax.saxutils import escape
import zipfile

from app_backend.converter_guard import ConverterGuard, GuardError, LibreOfficeAdapter
from app_backend.converter_guard.native_observation import NativeObservation
from app_backend.converter_guard.routes import registry, _load
from app_backend.native_process import ProcessTree
from app_backend.libreoffice_engine import LibreOfficeError
from test_converter_guard_v02 import FakeAdapter, wait_finished, ticket
from test_converter_guard_http_v02 import start_test_server

D = 'com.sun.star.drawing.DrawingDocument'
W = 'com.sun.star.text.TextDocument'
C = 'com.sun.star.sheet.SpreadsheetDocument'


def node(name, **props):
    return '<node oor:name="' + escape(name, {'"': '&quot;'}) + '">' + ''.join(
        '<prop oor:name="' + key + '"><value>' + escape(value) + '</value></prop>'
        for key, value in props.items()) + '</node>'


def component(name, group, rows):
    return '<oor:component-data oor:name="' + name + '"><node oor:name="' + group + '">' + rows + '</node></oor:component-data>'


def write_registry(directory, *, extra=''):
    root = Path(directory)
    exe = root / 'program' / 'soffice.exe'
    exe.parent.mkdir(exist_ok=True);exe.touch()
    folder = root / 'share' / 'registry'
    folder.mkdir(parents=True, exist_ok=True)
    types = ''.join(node(ext, Extensions=ext) for ext in ('bmp','docx','xlsx','pdf','dbf','png','odt'))
    rows = []
    for name, kind, service, flags in [('bmp','bmp',D,'IMPORT'), ('docx','docx',W,'IMPORT EXPORT'),
        ('xlsx','xlsx',C,'IMPORT EXPORT'),('draw_pdf','pdf',D,'EXPORT'),
        ('writer_pdf','pdf',W,'EXPORT'),('calc_pdf','pdf',C,'EXPORT'),
        ('dBase','dbf',C,'IMPORT EXPORT'),('draw_png','png',D,'EXPORT'),
        ('writer_odt','odt',W,'IMPORT EXPORT')]:
        rows.append(node(name, Type=kind, DocumentService=service, Flags=flags))
    xml = component('Types','Types',types) + component('Filter','Filters',''.join(rows)) + extra
    path = folder / 'registry.xcd'
    path.write_text('<oor:data xmlns:oor="http://openoffice.org/2001/registry">' + xml + '</oor:data>')
    return exe, path


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.exe,self.path = write_registry(self.temp.name)
        self.env = patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop('AIO_LIBREOFFICE_REGISTRY', None)

    def tearDown(self):
        self.env.stop();self.temp.cleanup()

    def test_native_import_and_export_services_decide_bmp_dbf_without_a_family_table(self):
        native = registry(self.exe)
        self.assertTrue(native.valid)
        for target in ('pdf','png'):
            self.assertEqual(native.hint('image.bmp',target),'registered')
        for target in ('dbf','docx','xlsx'):
            self.assertEqual(native.hint('image.bmp',target),'unavailable')
        self.assertEqual(native.hint('book.xlsx','dbf'),'registered')
        self.assertEqual(native.hint('word.docx','odt'),'registered')

    def test_unknown_source_target_or_missing_registry_remain_undetermined(self):
        native=registry(self.exe)
        self.assertEqual(native.hint('no-extension','pdf'),'undetermined')
        self.assertEqual(native.hint('image.bmp','custom'),'undetermined')
        self.assertEqual(registry(Path(self.temp.name)/'missing').hint('image.bmp','dbf'),'undetermined')

    def test_actual_selected_filter_proof_is_required_for_execution_filtering(self):
        native=registry(self.exe)
        self.assertEqual(native.observed_decision('dbf',None),'undetermined')
        proof=native.proof('draw_pdf','pdf')
        self.assertEqual(native.observed_decision('dbf',proof),'unavailable')
        self.assertIsNone(native.proof('docx','pdf'))
        self.assertIsNone(native.proof('bmp','bmp'))  # import-only is not export proof
        self.assertIsNone(native.proof('unknown','pdf'))

    def test_changed_registry_invalidates_both_cached_routes_and_old_proof(self):
        native=registry(self.exe);proof=native.proof('draw_pdf','pdf')
        self.assertIs(registry(self.exe),native)
        text=self.path.read_text().replace('</oor:data>',
            component('Filter','Filters',node('draw_dbf',Type='dbf',DocumentService=D,Flags='EXPORT'))+'</oor:data>')
        self.path.write_text(text)
        changed=registry(self.exe)
        self.assertNotEqual(changed.fingerprint,native.fingerprint)
        self.assertEqual(changed.hint('image.bmp','dbf'),'registered')
        self.assertEqual(changed.observed_decision('dbf',proof),'undetermined')

    def test_incomplete_or_malformed_registry_does_not_create_a_denial(self):
        (self.path.parent/'broken.xcd').write_text('<bad')
        self.assertFalse(registry(self.exe).valid)
        self.assertEqual(registry(self.exe).hint('image.bmp','dbf'),'undetermined')

    def test_graphic_encoder_table_is_not_mistaken_for_document_filter_factory(self):
        text=self.path.read_text().replace('</oor:data>',component('GraphicFilter','Filters',
            node('fake',Type='dbf',DocumentService=D,Flags='EXPORT'))+'</oor:data>')
        self.path.write_text(text)
        self.assertEqual(registry(self.exe).hint('image.bmp','dbf'),'unavailable')

    def test_fragment_properties_merge_and_ambiguous_imports_keep_all_models(self):
        fragment=component('Filter','Filters',node('docx',UIName='Translated name')+
            node('bmp_calc',Type='bmp',DocumentService=C,Flags='IMPORT'))
        (self.path.parent/'zz-extra.xcd').write_text(
            '<oor:data xmlns:oor="http://openoffice.org/2001/registry">'+fragment+'</oor:data>')
        native=registry(self.exe)
        self.assertEqual(native.hint('image.bmp','dbf'),'registered')
        self.assertEqual(native.hint('word.docx','odt'),'registered')

    def test_mixed_metadata_keeps_the_target_for_compatible_files(self):
        adapter=LibreOfficeAdapter(self.exe)
        before=set(threading.enumerate())
        value=adapter.capabilities([{'id':'a','name':'x.bmp','size':1},
            {'id':'b','name':'x.xlsx','size':1},{'id':'c','name':'x.unknown','size':1}])
        self.assertEqual(set(threading.enumerate()),before)
        dbf=next(f for f in value['formats'] if f['id']=='dbf')
        self.assertTrue(dbf['enabled'])
        self.assertEqual(dbf['compatibleFileIds'],['b'])
        self.assertEqual(dbf['incompatibleFileIds'],['a'])
        self.assertEqual(dbf['undeterminedFileIds'],['c'])
        only_bmp=adapter.capabilities([{'id':'a','name':'x.bmp','size':1}])
        self.assertFalse(next(f for f in only_bmp['formats'] if f['id']=='dbf')['enabled'])
        self.assertFalse(value['qualityCompared'])

    def test_registry_remove_replace_and_notinstalled_flags_are_respected(self):
        extra=component('Filter','Filters',
            '<node oor:name="docx" oor:op="remove"/>'+
            '<node oor:name="draw_png" oor:op="replace">'+
            '<prop oor:name="UIName"><value>incomplete</value></prop></node>'+
            node('disabled_draw_dbf',Type='dbf',DocumentService=D,Flags='EXPORT NOTINSTALLED'))
        (self.path.parent/'zz-ops.xcd').write_text(
            '<oor:data xmlns:oor="http://openoffice.org/2001/registry">'+extra+'</oor:data>')
        value=registry(self.exe)
        self.assertNotIn('docx',value.filters)
        self.assertNotIn('draw_png',value.filters)
        self.assertEqual(value.hint('image.bmp','dbf'),'unavailable')

    def test_native_type_without_document_export_is_not_an_unknown_extension(self):
        extra=component('Types','Types',node('native_read_only',Extensions='readonly'))
        (self.path.parent/'readonly.xcd').write_text(
            '<oor:data xmlns:oor="http://openoffice.org/2001/registry">'+extra+'</oor:data>')
        value=registry(self.exe)
        self.assertEqual(value.hint('image.bmp','readonly'),'unavailable')
        self.assertEqual(value.hint('x.unknown','readonly'),'unavailable')
        self.assertEqual(value.hint('image.bmp','unlisted'),'undetermined')

    def test_shared_extension_filter_overlay_disables_incomplete_base_denials(self):
        config=Path(self.temp.name)/'share/extensions/addon/filters.xcu'
        config.parent.mkdir(parents=True)
        config.write_text('<oor:items xmlns:oor="http://openoffice.org/2001/registry">'
            '<item oor:path="/org.openoffice.TypeDetection.Filter/Filters/custom"/></oor:items>')
        native=registry(self.exe)
        self.assertFalse(native.valid)
        self.assertTrue(native.external_filters)
        self.assertEqual(native.hint('image.bmp','dbf'),'undetermined')

    def test_explicit_filter_extension_overlay_only_uncertains_affected_format(self):
        # A stock LibreOffice distribution may include Wiki Publisher as a
        # well-formed Types.xcu + Filter.xcu. It must NOT invalidate BMP→PDF
        # or wrongly allow BMP→DBF before export.
        addon=Path(self.temp.name)/'share/extensions/wiki-publisher'
        addon.mkdir(parents=True)
        prefix='<oor:component-data xmlns:oor="http://openoffice.org/2001/registry" '
        (addon/'Types.xcu').write_text(
            prefix+'oor:name="Types"><node oor:name="Types">'+
            node('MediaWiki',Extensions='txt')+'</node></oor:component-data>',encoding='utf-8')
        (addon/'Filter.xcu').write_text(
            prefix+'oor:name="Filter"><node oor:name="Filters">'+
            node('MediaWiki',Type='MediaWiki',DocumentService=W,Flags='EXPORT')+
            '</node></oor:component-data>',encoding='utf-8')
        native=registry(self.exe)
        self.assertTrue(native.valid)
        self.assertTrue(native.external_filters)
        self.assertIn('txt',native.uncertain_extensions)
        self.assertEqual(native.hint('picture.bmp','pdf'),'registered')
        self.assertEqual(native.hint('picture.bmp','dbf'),'unavailable')
        self.assertEqual(native.decide('txt',{W}),'undetermined')
        self.assertEqual(native.hint('renamed.txt','pdf'),'undetermined')

    def test_deep_uno_type_proof_uses_installed_import_filters_only(self):
        native = registry(self.exe)
        self.assertEqual(native.services_for_type('bmp'), (D,))
        self.assertEqual(native.services_for_type('docx'), (W,))
        self.assertEqual(native.services_for_type('xlsx'), (C,))
        self.assertEqual(native.services_for_type('unknown_native_type'), ())
        proof = {'state':'detected', 'basis':'native_preflight_uno_type',
                 'typeName':'bmp', 'documentServices':[D],
                 'registryFingerprint':native.fingerprint}
        self.assertEqual(native.observed_decision('pdf',proof),'registered')
        self.assertEqual(native.observed_decision('dbf',proof),'unavailable')
        self.assertEqual(native.observed_decision('dbf',{**proof,'documentServices':[C]}),
                         'undetermined')
        self.assertEqual(native.observed_decision('dbf',{**proof,
                         'registryFingerprint':'stale'}),'undetermined')

    def test_native_uno_graphic_provider_is_pre_export_draw_proof(self):
        native=registry(self.exe)
        proof={'state':'detected','basis':'native_preflight_uno_graphic',
               'documentServices':[D], 'graphicType':1,
               'registryFingerprint':native.fingerprint}
        self.assertEqual(native.observed_decision('pdf',proof),'registered')
        self.assertEqual(native.observed_decision('dbf',proof),'unavailable')
        self.assertEqual(native.observed_decision('pdf',{**proof,'graphicType':0}),
                         'undetermined')
        self.assertEqual(native.observed_decision('pdf',{**proof,
                         'documentServices':[W]}),'undetermined')

    def test_failed_native_export_cannot_become_document_model_proof(self):
        adapter=LibreOfficeAdapter(self.exe);observed=[]
        def failed(*args,**kwargs):
            os.write(kwargs['stdout'],b'convert a as a Draw document -> b using filter : draw_pdf\n')
            raise LibreOfficeError('No result','ENGINE_OUTPUT_MISSING')
        with patch('app_backend.converter_guard.adapter.convert_path',side_effect=failed):
            with self.assertRaises(GuardError):
                adapter.convert(Path('x.bmp'),'pdf',Path(self.temp.name)/'work',
                    cancel=threading.Event(),timeout=None,on_started=lambda:None,
                    on_document_service=observed.append)
        self.assertEqual(observed,[])

    def test_observation_resource_failure_falls_back_to_native_export(self):
        adapter=LibreOfficeAdapter(self.exe)
        with patch('app_backend.converter_guard.adapter.NativeObservation.__enter__',side_effect=OSError('no pipe')):
            with patch('app_backend.converter_guard.adapter.convert_path',return_value=[]) as engine:
                adapter.convert(Path('x.bmp'),'pdf',Path(self.temp.name)/'work',
                    cancel=threading.Event(),timeout=None,on_started=lambda:None,
                    on_document_service=lambda value:None)
        self.assertNotIn('stdout',engine.call_args.kwargs)


class ObservationTests(unittest.TestCase):
    def capture(self, code):
        observed=NativeObservation()
        with observed:
            with ProcessTree([sys.executable,'-c',code],cwd=Path.cwd(),stdout=observed.writer) as tree:
                self.assertEqual(tree.process.wait(timeout=5),0)
        self.assertFalse(observed.thread.is_alive())
        return observed

    def test_native_stdout_filter_is_observed_without_retaining_it_in_status(self):
        value=self.capture("print('convert /private/source.bmp as a Draw document -> /private/out.pdf using filter : draw_pdf')")
        self.assertEqual(value.selected_filter(),'draw_pdf')

    def test_large_native_output_is_drained_without_pipe_deadlock_and_memory_growth(self):
        value=self.capture("import sys;sys.stdout.buffer.write(b'x'*2000000)")
        self.assertEqual(len(value.data),64*1024)
        self.assertTrue(value.truncated)
        self.assertIsNone(value.selected_filter())

    def test_ambiguous_output_never_becomes_a_model_proof(self):
        value=self.capture("print('convert a using filter : draw_pdf\\nconvert b using filter : writer_pdf')")
        self.assertIsNone(value.selected_filter())


class RoutedAdapter(FakeAdapter):
    def __init__(self, exe):
        super().__init__();self.exe=exe;self.observations=0

    def route_decision(self,target,proof):
        return registry(self.exe).observed_decision(target,proof)

    def observation_needed(self,proof):
        return not proof

    def convert(self,source,target,workspace,*,on_document_service,**kwargs):
        model={'D':D,'W':W,'C':C}[source.read_text()]
        native=registry(self.exe)
        if native.decide(target,{model})=='unavailable':
            self.calls.append((source.name,target));kwargs['on_started']()
            raise GuardError('ENGINE_OUTPUT_MISSING','Native cannot export this model.',stage='engine')
        paths=super().convert(source,target,workspace,**kwargs)
        for name,(service,extensions,flags) in native.filters.items():
            if model==service and target in extensions and 'EXPORT' in flags:
                if on_document_service:
                    self.observations+=1
                    on_document_service(native.proof(name,target))
                break
        return paths


class RuntimeRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.exe,_=write_registry(self.temp.name)
        self.adapter=RoutedAdapter(self.exe)
        self.guard=ConverterGuard(adapter=self.adapter,spool_parent=self.temp.name)

    def tearDown(self):
        self.guard.close();self.assertEqual(self.guard.storage_stats()['reservedBytes'],0)
        self.temp.cleanup()

    def run_job(self,files,formats):
        state=self.guard.create([{'id':str(i),'name':name,'size':1} for i,(name,_) in enumerate(files)],formats)
        for i,(_,data) in enumerate(files):self.guard.upload(state['jobId'],str(i),io.BytesIO(data.encode()),1)
        self.guard.start(state['jobId'])
        return wait_finished(self.guard,state['jobId'])

    def test_impossible_pairs_are_skipped_before_engine_launch_with_explanations(self):
        done=self.run_job([('x.bmp','D')],['pdf','dbf','docx','png'])
        self.assertEqual(self.adapter.calls,[('x.bmp','pdf'),('x.bmp','png')])
        self.assertEqual((done['total'],done['attempted'],done['filtered']),(4,2,2))
        self.assertEqual(done['state'],'ready')
        for target in done['files'][0]['targets'][1:3]:
            self.assertEqual(target['state'],'skipped')
            self.assertEqual(target['code'],'NATIVE_ROUTE_UNAVAILABLE')
            self.assertFalse(target['libreOfficeStarted'])
            self.assertEqual(target['route']['documentService'],D)
        self.assertEqual(self.guard.retry_plan(done['jobId'])['plan'],[])
        with self.guard.open_result(ticket(done['result'])) as (stream,_,_):
            with zipfile.ZipFile(stream) as bundle:
                report=json.loads(bundle.read('AIO-conversion-report.json'))
                self.assertFalse(report['qualityCompared'])
                self.assertEqual(report['files'][0]['targets'][1]['state'],'skipped')
        saved=self.guard.confirm_saved(done['jobId'],ticket(done['result']))
        self.assertEqual(saved['files'][0]['state'],'saved')

    def test_source_name_never_vetoes_the_actual_native_model(self):
        done=self.run_job([('renamed.bmp','W')],['pdf','docx','odt'])
        self.assertEqual(done['filtered'],0)
        self.assertEqual(done['state'],'ready')
        self.assertEqual(done['files'][0]['nativeRoute']['documentService'],W)
        self.assertEqual(len(self.adapter.calls),3)
        self.assertEqual(self.adapter.observations,1)

    def test_model_proof_is_scoped_to_one_file_not_to_extension_or_batch(self):
        done=self.run_job([('draw.bmp','D'),('renamed.bmp','C')],['pdf','dbf'])
        self.assertEqual((done['attempted'],done['filtered']),(3,1))
        self.assertIn(('renamed.bmp','dbf'),self.adapter.calls)

    def test_first_unsupported_target_is_tried_and_does_not_create_a_negative_cache(self):
        done=self.run_job([('x.bmp','D')],['dbf','pdf','docx','png'])
        self.assertEqual(self.adapter.calls,[('x.bmp','dbf'),('x.bmp','pdf'),('x.bmp','png')])
        self.assertEqual(done['state'],'partial')
        self.assertEqual(done['filtered'],1)
        self.assertEqual(self.guard.retry_plan(done['jobId'])['plan'],[{'fileId':'0','formats':['dbf']}])

    def test_unknown_target_reaches_native_and_cannot_be_called_unsupported(self):
        done=self.run_job([('x.bmp','D')],['pdf','custom'])
        self.assertEqual(done['filtered'],0)
        self.assertIn(('x.bmp','custom'),self.adapter.calls)

    def test_returned_route_metadata_cannot_modify_future_status(self):
        done=self.run_job([('x.bmp','D')],['pdf','dbf'])
        done['files'][0]['nativeRoute']['documentService']='wrong'
        done['files'][0]['targets'][1]['route']['documentService']='wrong'
        state=self.guard.status(done['jobId'])
        self.assertEqual(state['files'][0]['nativeRoute']['documentService'],D)
        self.assertEqual(state['files'][0]['targets'][1]['route']['documentService'],D)

    def test_http_inspection_exposes_filtered_ids_without_launching_libreoffice(self):
        native_guard=ConverterGuard(adapter=LibreOfficeAdapter(self.exe),spool_parent=self.temp.name)
        server,thread,origin,token=start_test_server(native_guard)
        try:
            data=json.dumps({'files':[{'id':'a','name':'x.bmp','size':1}]}).encode()
            with patch('app_backend.converter_guard.adapter.convert_path',side_effect=AssertionError('engine started')):
                with urlopen(Request(origin+'/api/converter-guard/inspect',data=data,
                    headers={'X-AIO-Token':token,'Content-Type':'application/json'}),timeout=5) as response:
                    result=json.load(response)
            dbf=next(f for f in result['formats'] if f['id']=='dbf')
            self.assertFalse(dbf['enabled'])
            self.assertEqual(dbf['reasonCode'],'NATIVE_ROUTE_UNAVAILABLE')
            self.assertNotIn(self.temp.name,json.dumps(result))
        finally:
            server.api.close();native_guard.close();server.shutdown();server.server_close();thread.join(2)


if __name__=='__main__':unittest.main()
