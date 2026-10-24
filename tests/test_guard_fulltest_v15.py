"""Bug-bag regressions: real Guard ownership, bounded checks, controlled native edges."""
import io
import json
from pathlib import Path
import queue
import struct
import tempfile
import threading
import tracemalloc
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from app_backend.converter_guard import ConverterGuard, GuardError, GuardLimits, LibreOfficeAdapter
from app_backend.converter_guard.core import _html_resource_warning
from app_backend.converter_guard.html_resources import resource_manifest
from app_backend.converter_guard.legacy_import import legacy_import_hint
from app_backend.converter_guard.postconditions import check_native_output
from app_backend.converter_guard.routes import Registry
from app_backend.converter_guard.uno_session import UnoSession
from app_backend.converter_guard.export_recipe import native_export_recipe, DRAW, WRITER, CALC
from test_converter_guard_v02 import FakeAdapter, wait_finished
from test_converter_guard_v04 import component, node


class Prepared(FakeAdapter):
    def convert(self, source, target, workspace, *, on_document_service=None, **kwargs):
        return super().convert(source,target,workspace,**kwargs)

    def preflight_fingerprint(self):
        return ('test-installation',)

    def preflight(self, source, workspace, **kwargs):
        return {'state':'detected', 'documentServices':[WRITER], 'registryFingerprint':'test-installation'}

    def capabilities(self, files, *, proofs=None):
        return {'files':[{**f,'preflight':(proofs or {}).get(f['id'])} for f in files], 'formats':[]}

    def route_decision(self, target, proof):
        return 'registered' if proof and proof.get('state') == 'detected' else 'undetermined'


class BugBag(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def guard(self, **kwargs):
        g = ConverterGuard(adapter=kwargs.pop('adapter',FakeAdapter()), recovery_parent=self.root/'owned', **kwargs)
        self.addCleanup(g.close)
        return g

    def ready(self, g, formats=('pdf','odt')):
        job = g.create([{'id':'source','name':'original.bin','size':4}],list(formats))['jobId']
        g.upload(job,'source',io.BytesIO(b'keep'),4)
        g.start(job)
        return job, wait_finished(g,job)

    def registry(self, types, filters):
        p = self.root/'registry.xcd'
        p.write_text('<oor:data xmlns:oor="http://openoffice.org/2001/registry">'+
            component('Types','Types',types)+component('Filter','Filters',filters)+'</oor:data>')
        return Registry(((str(p),0,p.stat().st_size),),p)

    def test_empty_html_is_not_success_for_any_input_alias(self):
        g = LibreOfficeAdapter(self.root/'soffice')
        for ext in 'apng bmp emf emz eps fodg gif jfif jif jpe jpeg jpg odg png svg svgz svm tif tiff webp wmf wmz'.split():
            with self.subTest(ext=ext):
                source = self.root/('original.'+ext)
                result = self.root/'original.html'
                result.write_text('<!DOCTYPE html><html><head><title>original</title></head><body><h1></h1></body></html>')
                with patch.object(g,'_convert_native',return_value=[result]), self.assertRaises(GuardError) as err:
                    g.convert(source,'html',self.root,cancel=threading.Event(),timeout=5,on_started=lambda:None)
                self.assertEqual(err.exception.code,'ENGINE_OUTPUT_CONTENT_EMPTY')

    def test_html_body_image_is_content_not_quality_certificate(self):
        p = self.root/'picture.html';p.write_text('<html><body><img src="data:image/png;base64,AA=="></body></html>')
        result = check_native_output([p],'html',self.root/'picture.bmp',cancel=threading.Event())
        self.assertEqual(result['status'],'content_present')
        self.assertFalse(result['qualityCompared'])

    def test_dynamic_output_fails_closed_and_large_text_is_streamed(self):
        p = self.root/'picture.html';p.write_text('<body><canvas></canvas><script>draw()</script></body>')
        with self.assertRaises(GuardError) as error:
            check_native_output([p],'html',self.root/'picture.bmp',cancel=threading.Event())
        self.assertEqual(error.exception.code,'ENGINE_OUTPUT_VALIDATION_INCOMPLETE')
        p.write_bytes(b'<body>'+b'a'*(1024*1024))
        self.assertEqual(check_native_output([p],'html',self.root/'picture.bmp',
            cancel=threading.Event())['status'],'content_present')

    def test_html_over_32_mib_is_streamed_without_size_veto(self):
        p = self.root/'large.html'
        with p.open('wb') as f:
            f.write(b'<html><body>')
            for _ in range(33):f.write(b' '*1024*1024)
            f.write(b'<img src="tail.png"></body></html>')
        tracemalloc.start()
        missing = []
        code = _html_resource_warning(p,missing=missing)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.assertEqual(code,'HTML_LOCAL_RESOURCES_NOT_UPLOADED')
        self.assertEqual(missing,['tail.png'])
        self.assertLess(peak,3*1024*1024)

    def test_native_primary_extension_avoids_auxiliary_alias_ambiguity(self):
        native = self.registry(node('pdf',Extensions='pdf')+node('alt',Extensions='other pdf'),
            node('import',Type='pdf',DocumentService=WRITER,Flags='IMPORT')+
            node('writer_pdf_Export',Type='pdf',DocumentService=WRITER,Flags='EXPORT')+
            node('alias',Type='alt',DocumentService=WRITER,Flags='EXPORT'))
        self.assertEqual(native.export_filter('pdf',[WRITER]),'writer_pdf_Export')

    def test_ooxml_standard_is_used_only_if_installed(self):
        native = self.registry(node('xml',Extensions='docx'),
            node('MS Word 2007 XML',Type='xml',DocumentService=WRITER,Flags='IMPORT EXPORT')+
            node('Office Open XML Text',Type='xml',DocumentService=WRITER,Flags='IMPORT EXPORT'))
        self.assertEqual(native.export_filter('docx',[WRITER]),'MS Word 2007 XML')
        native.uncertain_extensions.add('docx')
        self.assertIsNone(native.export_filter('docx',[WRITER]))

    def test_whole_document_svg_does_not_disable_draw_svg(self):
        native = self.registry(node('svg',Extensions='svg'),
            node('writer_svg_Export',Type='svg',DocumentService=WRITER,Flags='IMPORT EXPORT')+
            node('calc_svg_Export',Type='svg',DocumentService=CALC,Flags='IMPORT EXPORT')+
            node('draw_svg_Export',Type='svg',DocumentService=DRAW,Flags='IMPORT EXPORT'))
        self.assertEqual(native.decide('svg',[WRITER]),'unavailable')
        self.assertEqual(native.decide('svg',[CALC]),'unavailable')
        self.assertEqual(native.decide('svg',[DRAW]),'registered')

    def test_writer_web_graphics_require_live_interface_proof(self):
        web = 'com.sun.star.text.WebDocument'
        proof = {'documentServices':[web]}
        self.assertEqual(Registry.export_services(proof),[web])
        proof['nativeTextDocument'] = True
        self.assertEqual(Registry.export_services(proof),[web,WRITER])

    def test_legacy_hints_are_content_based_and_do_not_guess_dbf_codepage(self):
        native = SimpleNamespace(filters={'DIF':(CALC,{'dif'},{'IMPORT'}),
                                          'dBase':(CALC,{'dbf'},{'IMPORT'})})
        p = self.root/'renamed.bin';p.write_bytes(b'TABLE\n0,1\n""\nVECTORS\n0,2\n""\nTUPLES\n0,1\n""\nDATA\n0,0\n""\n')
        self.assertEqual(legacy_import_hint(p,native)['filter'],'DIF')
        p.write_bytes(b'BM not DIF');self.assertIsNone(legacy_import_hint(p,native))
        header = bytearray(65);header[0]=3
        struct.pack_into('<IHH',header,4,1,65,2);header[32:34]=b'ID';header[64]=13
        p.write_bytes(header+b' A')
        self.assertEqual(legacy_import_hint(p,native)['filter'],'dBase')
        p.write_bytes(header+b' \xff');self.assertIsNone(legacy_import_hint(p,native))

    def test_html_resources_nested_css_and_private_source_ownership(self):
        g = self.guard(adapter=Prepared())
        html = b'<html><body><link href="css/site.css"><img src="images/logo.png"></body></html>'
        job = g.create([{'id':'html','name':'page.html','size':len(html)}],prepare_only=True)['jobId']
        g.upload(job,'html',io.BytesIO(html),len(html))
        self.assertEqual(g.preflight(job)['files'][0]['preflight']['code'],'HTML_LOCAL_RESOURCES_NOT_UPLOADED')
        resources = {'css/site.css':b'body{background:url("../images/logo.png")}','images/logo.png':b'PNG'}
        g.configure_resources(job,'html',[{'path':p,'size':len(b)} for p,b in resources.items()])
        with self.assertRaises(GuardError):
            g.preflight(job)  # incomplete companion uploads cannot be admitted
        for p,b in resources.items():g.upload_resource(job,'html',p,io.BytesIO(b),len(b))
        self.assertEqual(g.preflight(job)['files'][0]['preflight']['state'],'detected')
        self.assertFalse(g.adapter.calls)
        g.start(job,file_ids=['html'],formats=['pdf'],archive_mode='on_demand')
        wait_finished(g,job)
        self.assertFalse(any(g._jobs[job].root.glob('input/**/*')))
        self.assertEqual(g.storage_stats()['reservedBytes'],4)

    def test_resource_path_collisions_and_traversal_are_rejected(self):
        for paths in (['../x'],['/x'],['a\\b'],['page.html'],['A','a'],['a','a/b'],['CON.png'],['x:stream']):
            with self.subTest(paths=paths), self.assertRaises(GuardError):
                resource_manifest([{'path':p,'size':1} for p in paths],'page.html')

    def test_short_resource_upload_keeps_lease_and_quota_consistent(self):
        g = self.guard(adapter=Prepared())
        job = g.create([{'id':'html','name':'a.html','size':6}],prepare_only=True)['jobId']
        g.upload(job,'html',io.BytesIO(b'<body>'),6)
        g.configure_resources(job,'html',[{'path':'asset','size':3}])
        with self.assertRaises(GuardError):
            g.upload_resource(job,'html','asset',io.BytesIO(b'x'),3)
        self.assertEqual(g._jobs[job].io,0)
        self.assertFalse(g._jobs[job].files[0].resources_uploading)
        g.release(job)
        self.assertEqual(g.storage_stats()['reservedBytes'],0)

    def test_all_folder_outputs_are_read_back_before_ack(self):
        g = self.guard();job,state = self.ready(g)
        result = g.save_folder(state['result']['ticket'],self.root,verify=True)
        self.assertEqual(result['outputsVerified'],2)
        self.assertTrue(result['readbackVerified'])
        manifest=json.loads((self.root/result['name']/'AIO-results-manifest.json').read_text())
        self.assertEqual(len(manifest['outputs']),2)
        self.assertTrue(all(len(e['sha256'])==64 for e in manifest['outputs']))

    def test_same_size_corruption_is_not_acknowledged_or_deleted(self):
        g = self.guard();job,state = self.ready(g)
        def corrupt():
            folder=next(self.root.glob('AIO_Results_*'))
            next(folder.rglob('*.pdf')).write_bytes(b'evil')
        with self.assertRaises(GuardError) as err:
            g.save_folder(state['result']['ticket'],self.root,verify=True,on_verifying=corrupt)
        self.assertEqual(err.exception.code,'SAVE_VERIFY_FAILED')
        self.assertEqual(g.recoverable()['jobs'][0]['outputs'],2)
        self.assertTrue(all(not a.saved and a.path.exists() for a in g._jobs[job].artifacts))

    def test_zip_has_hashes_for_every_output_and_folder_readback_is_default(self):
        g=self.guard();job,state=self.ready(g)
        with g.open_result(state['result']['ticket']) as (stream,_,_):
            with zipfile.ZipFile(stream) as z:
                manifest=json.loads(z.read('AIO-results-manifest.json'))
                self.assertEqual(len(manifest['outputs']),2)
                import hashlib
                for e in manifest['outputs']:
                    self.assertEqual(hashlib.sha256(z.read(e['path'])).hexdigest(),e['sha256'])
        result=g.save_folder(state['result']['ticket'],self.root)
        self.assertTrue(result['readbackVerified'])
        self.assertEqual(result['outputsVerified'],2)

    def test_download_readback_does_not_acknowledge_itself(self):
        g=self.guard();job,state=self.ready(g,('pdf',))
        path=self.root/'saved.pdf';path.write_bytes(b'data')
        self.assertTrue(g.verify_saved_download(state['result']['ticket'],path)['readbackVerified'])
        self.assertFalse(g._jobs[job].artifacts[0].saved)
        path.write_bytes(b'evil')
        with self.assertRaises(GuardError):g.verify_saved_download(state['result']['ticket'],path)

    def test_unplanned_sources_remain_in_report_after_restart(self):
        g=self.guard(adapter=Prepared())
        files=[{'id':str(i),'name':str(i)+'.bin','size':1} for i in range(14)]
        job=g.create(files,prepare_only=True)['jobId']
        for f in files:g.upload(job,f['id'],io.BytesIO(b'x'),1)
        g.preflight(job);g.start(job,file_ids=['0'],formats=['pdf']);wait_finished(g,job)
        self.assertEqual(len(g.report(job)['inputsWithoutTasks']),13)
        g.close();restored=self.guard(adapter=Prepared())
        self.assertEqual(len(restored.report(job)['inputsWithoutTasks']),13)

    def session(self, messages):
        s=UnoSession(self.root/'soffice',self.root,lambda _:None)
        s.tree=SimpleNamespace(process=SimpleNamespace(poll=lambda:None))
        s.reader=SimpleNamespace(is_alive=lambda:True)
        for m in messages:s.responses.put_nowait(m)
        return s

    def test_import_timeout_does_not_wait_for_whole_file_budget(self):
        s=self.session([{'id':'task','stage':'import'}])
        with self.assertRaises(GuardError) as err:
            s._receive('task',threading.Event(),10,stage_budgets={'import':.01})
        self.assertEqual((err.exception.code,err.exception.stage),('ENGINE_TIMEOUT','import'))

    def test_only_positive_committed_export_can_survive_close_timeout(self):
        committed={'id':'task','ok':True,'stage':'export','primary':'original.pdf'}
        s=self.session([{'id':'task','stage':'import'},{'id':'task','stage':'export'},
                        {'id':'task','nativeResult':committed},{'id':'task','stage':'close'}])
        result=s._receive('task',threading.Event(),10,stage_budgets={'close':.01})
        self.assertTrue(result['nativeCloseInterrupted'])
        self.assertTrue(result['recycle'])
        s=self.session([{'id':'task','stage':'close'}])
        with self.assertRaises(GuardError):s._receive('task',threading.Event(),10,stage_budgets={'close':.01})

    def test_foreign_export_commit_is_rejected(self):
        s=self.session([{'id':'task','stage':'export'},
                        {'id':'task','nativeResult':{'id':'other','ok':True,'stage':'export'}}])
        with self.assertRaises(GuardError) as err:s._receive('task',threading.Event(),1)
        self.assertEqual(err.exception.code,'UNO_PROTOCOL')

    def test_export_timeout_continues_next_target_and_next_source(self):
        class Adapter(FakeAdapter):
            def convert(self, source, target, workspace, **kwargs):
                if target=='bad':
                    self.calls.append((source.name,target))
                    raise GuardError('ENGINE_TIMEOUT','Export wedged',stage='export')
                return super().convert(source,target,workspace,**kwargs)
        g=self.guard(adapter=Adapter())
        job=g.create([{'id':i,'name':i+'.bin','size':1} for i in ('first','second')],['bad','pdf'])['jobId']
        for i in ('first','second'):g.upload(job,i,io.BytesIO(b'x'),1)
        g.start(job);state=wait_finished(g,job)
        self.assertEqual((state['failed'],state['succeeded']),(2,2))
        self.assertEqual(len(g.adapter.calls),4)

    def test_duplicate_stage_messages_cannot_renew_import_deadline(self):
        s=self.session([{'id':'task','stage':'import'} for _ in range(8)])
        clock=iter(n*.01 for n in range(200))
        with patch('app_backend.converter_guard.uno_session.time.monotonic',side_effect=lambda:next(clock)):
            with self.assertRaises(GuardError):
                s._receive('task',threading.Event(),10,stage_budgets={'import':.025})
        self.assertGreater(s.responses.qsize(),2)

    def test_cancellation_during_readback_leaves_unsaved_results(self):
        g=self.guard();job,state=self.ready(g)
        cancel=threading.Event()
        with self.assertRaises(GuardError) as err:
            g.save_folder(state['result']['ticket'],self.root,verify=True,cancel=cancel,on_verifying=cancel.set)
        self.assertEqual(err.exception.code,'SAVE_CANCELLED')
        self.assertTrue(all(not a.saved for a in g._jobs[job].artifacts))

    def test_help_is_three_ui_records_and_svg_has_concrete_reason(self):
        native=self.registry(node('svg',Extensions='svg'),
            node('writer_svg_Export',Type='svg',DocumentService=WRITER,Flags='IMPORT EXPORT'))
        adapter=LibreOfficeAdapter(native.fingerprint)
        proof={'state':'detected','basis':'native_preflight_uno_document','modelLoaded':True,
               'documentServices':[WRITER],'registryFingerprint':native.fingerprint}
        with patch.object(adapter,'_registry',return_value=native):
            caps=adapter.capabilities([{'id':'a','name':'source.bin','size':1}],proofs={'a':proof})
        fmt=next(f for f in caps['formats'] if f['id']=='svg')
        self.assertEqual(len(fmt['help']),3)
        self.assertTrue(all(isinstance(p['text'],str) for p in fmt['help']))
        self.assertEqual(fmt['unavailableReasons']['a'],'NATIVE_SVG_SELECTION_ONLY')


if __name__ == '__main__':
    unittest.main()

