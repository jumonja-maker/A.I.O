"""M16/M17: feature removal must not weaken live Guard ownership or diagnostics."""
import http.client
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

from app_backend.converter_guard import ConverterGuard
from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard.http_api import PREFIX
from test_converter_guard_http_v02 import start_test_server
from test_converter_guard_v02 import FakeAdapter, wait_finished

ROOT=Path(__file__).resolve().parents[1]


class RetiredHTTP(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.guard=ConverterGuard(adapter=FakeAdapter(),spool_parent=self.root/'spool',history_parent=self.root/'history')
        (self.root/'spool').mkdir()
        self.server,self.thread,self.origin,self.token=start_test_server(self.guard)
    def tearDown(self):
        self.server.api.close();self.guard.close();self.server.shutdown();self.server.server_close();self.thread.join(3);self.temp.cleanup()
    def request(self,path,body=None,*,authorized=True):
        client=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        try:
            headers={'X-AIO-Token':self.token} if authorized else {}
            if body is None:client.request('GET',PREFIX+path,headers=headers)
            else:
                headers['Content-Type']='application/json'
                client.request('POST',PREFIX+path,json.dumps(body).encode(),headers)
            response=client.getresponse();return response.status,json.loads(response.read())
        finally:client.close()
    def test_no_comparison_service_constructed(self):
        self.assertFalse(hasattr(self.server.api,'comparison'))
        self.assertFalse(self.request('/status')[1]['comparisonAvailable'])
    def test_all_retired_get_endpoints_are_explicitly_gone(self):
        with patch.object(self.guard,'_load_recovery',side_effect=AssertionError('No recovery')):
            for route in ('/compare','/compare/status','/compare/detail','/compare/preview','/compare/history'):
                with self.subTest(route=route):
                    code,value=self.request(route+'?job='+'a'*32)
                    self.assertEqual((code,value['code']),(410,'MODULE_RETIRED'))
        self.assertEqual(self.guard.storage_stats()['jobs'],0)
    def test_all_retired_post_endpoints_are_explicitly_gone(self):
        for route in ('start','control','retry','export-selected'):
            with self.subTest(route=route):
                code,value=self.request('/compare/'+route,{'jobId':'a'*32})
                self.assertEqual((code,value['code']),(410,'MODULE_RETIRED'))
        self.assertEqual(self.guard.storage_stats()['jobs'],0)
    def test_removed_routes_still_require_authentication(self):
        for body in (None,{'jobId':'a'*32}):
            code,value=self.request('/compare/start',body,authorized=False)
            self.assertEqual((code,value['code']),(403,'ACCESS_DENIED'))
    def test_retired_post_does_not_wait_for_a_stalled_body(self):
        client=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=1)
        try:
            client.putrequest('POST',PREFIX+'/compare/start');client.putheader('X-AIO-Token',self.token)
            client.putheader('Content-Type','application/json');client.putheader('Content-Length','100')
            client.endheaders(b'{');response=client.getresponse()
            self.assertEqual(response.status,410);self.assertEqual(json.loads(response.read())['code'],'MODULE_RETIRED')
        finally:client.close()
    def test_public_prepare_cannot_reenable_source_retention(self):
        for route in ('/prepare','/jobs'):
            for value in (True,1,'true',None):
                with self.subTest(route=route,value=value):
                    code,body=self.request(route,{'files':[{'id':'f','name':'a.txt','size':1}],'retainSources':value})
                    self.assertEqual((code,body['code']),(410,'MODULE_RETIRED'))
        self.assertEqual(self.guard.storage_stats()['jobs'],0)
    def test_ordinary_prepare_without_retention_remains_available(self):
        code,body=self.request('/prepare',{'files':[{'id':'f','name':'a.txt','size':1}]})
        self.assertEqual(code,201);self.assertFalse(self.guard._jobs[body['jobId']].retain_sources)
    def test_legacy_explicit_false_remains_compatible(self):
        code,body=self.request('/prepare',{'files':[{'id':'f','name':'a.txt','size':1}],'retainSources':False})
        self.assertEqual(code,201)
    def test_history_endpoint_is_not_retired_with_compare_history(self):
        code,body=self.request('/history');self.assertEqual(code,200);self.assertEqual(body['items'],[])
    def test_release_keeps_guard_error_and_legacy_acknowledgement(self):
        identity=self.guard.create([{'id':'f','name':'one.txt','size':0}])['jobId']
        with patch.object(self.guard,'release',side_effect=GuardError('JOB_BUSY','Still in use',status=409)):
            code,body=self.request('/release',{'jobId':identity})
        self.assertEqual((code,body['code']),(409,'JOB_BUSY'))
        self.assertIn(identity,self.guard._jobs)
        self.assertEqual(self.request('/release',{'jobId':identity}),(200,{'ok':True}))
    def test_shutdown_closes_admission_and_stalled_json_without_retired_service(self):
        client=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=1)
        try:
            client.putrequest('POST',PREFIX+'/jobs');client.putheader('X-AIO-Token',self.token)
            client.putheader('Content-Type','application/json');client.putheader('Content-Length','100');client.endheaders(b'{')
            deadline=time.monotonic()+1
            while not self.server.api._connections and time.monotonic()<deadline:time.sleep(.005)
            self.assertTrue(self.server.api._connections)
            self.server.api.close()
            self.assertTrue(self.server.api._closed)
            code,body=self.request('/jobs',{'files':[]});self.assertEqual((code,body['code']),(503,'GUARD_CLOSED'))
            deadline=time.monotonic()+1
            while self.server.api._connections and time.monotonic()<deadline:time.sleep(.005)
            self.assertFalse(self.server.api._connections)
        finally:client.close()


class RetiredRuntime(unittest.TestCase):
    def test_launcher_rejects_retired_worker_without_starting_gui(self):
        with tempfile.TemporaryDirectory() as temp:
            request=Path(temp)/'never-created.json'
            run=subprocess.run([sys.executable,str(ROOT/'launcher.py'),'--aio-guard-comparison',str(request)],
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=10)
            self.assertEqual(run.returncode,2);self.assertFalse(request.exists())
    def test_unknown_worker_cannot_fall_through_to_gui(self):
        run=subprocess.run([sys.executable,str(ROOT/'launcher.py'),'--aio-guard-removed'],
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=10)
        self.assertEqual(run.returncode,2)
    def test_actual_server_ignores_former_blackbox_environment(self):
        from app_backend.server import start_server
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);sink=root/'must-not-exist'/'events.jsonl'
            with patch.dict(os.environ,{'AIO_BLACKBOX_EVENTS':str(sink)}):
                server,_=start_server(guard_recovery_parent=root/'jobs',guard_history_parent=root/'history')
                try:
                    self.assertFalse(hasattr(server,'telemetry'))
                    self.assertFalse(hasattr(server.guard,'telemetry'))
                    self.assertFalse(hasattr(server.converter,'telemetry'))
                    self.assertFalse(sink.parent.exists())
                finally:server.server_close()
            self.assertFalse(sink.parent.exists())
    def test_product_modules_and_assets_are_absent(self):
        for pattern in ('app_backend/converter_guard/comparison_*.py','app_backend/converter_guard/preview*.py',
                        'src/guard-comparison*','app_backend/blackbox_telemetry.py','app_backend/qt_observation.py'):
            with self.subTest(pattern=pattern):self.assertFalse(list(ROOT.glob(pattern)))
        self.assertFalse(hasattr(ConverterGuard,'retry_comparison'))
    def test_rendered_result_controls_have_no_comparison_button(self):
        from app_backend.converter_assets import read_designer_asset
        code=read_designer_asset(str(ROOT),'file-converter.js').decode('utf-8')
        self.assertIn('data-cv-close-result',code);self.assertIn('data-cv-save',code)
        self.assertNotIn('data-cv-beforeafter',code);self.assertNotIn('До/После',code)
        self.assertNotIn('guard-comparison',(ROOT/'index.html').read_text())
        self.assertIn('conversion-history.js',(ROOT/'index.html').read_text())
        self.assertNotIn('data-g-compare',(ROOT/'src/guard-queue.js').read_text())
    def test_bridge_no_longer_requests_retained_originals(self):
        code=(ROOT/'src/converter-bridge.js').read_text(encoding='utf-8')
        self.assertNotIn('retainSources:true',code)
    def test_guard_reports_and_history_survive_ordinary_source_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            guard=ConverterGuard(adapter=FakeAdapter(),recovery_parent=root/'jobs',history_parent=root/'history')
            try:
                identity=guard.create([{'id':'f','name':'original.docx','size':4}],['pdf'])['jobId']
                guard.upload(identity,'f',io.BytesIO(b'data'),4);guard.start(identity,archive_mode='on_demand')
                state=wait_finished(guard,identity);guard._jobs[identity].future.result(timeout=10)
                self.assertFalse(guard._jobs[identity].files[0].source.exists())
                report=guard.report(identity);self.assertTrue(report['trace']['events']);self.assertEqual(report['succeeded'],1)
                self.assertFalse(state['comparisonAvailable'])
                result=guard.save_folder(state['result']['ticket'],root)
                self.assertTrue(result['saved']);self.assertTrue(result['readbackVerified'])
                self.assertEqual(guard.history_list()['items'][0]['savedOutputs'],1)
            finally:guard.close()
    def test_legacy_retained_unsaved_outputs_remain_recoverable(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            first=ConverterGuard(adapter=FakeAdapter(),recovery_parent=root/'jobs')
            try:
                identity=first.create([{'id':'f','name':'old.docx','size':4}],['pdf'],retain_sources=True)['jobId']
                first.upload(identity,'f',io.BytesIO(b'data'),4);first.start(identity,archive_mode='on_demand')
                wait_finished(first,identity);first._jobs[identity].future.result(timeout=10)
            finally:first.close()
            second=ConverterGuard(adapter=FakeAdapter(),recovery_parent=root/'jobs')
            try:
                state=second.status(identity);self.assertEqual(state['succeeded'],1);self.assertFalse(state['comparisonAvailable'])
                with second.open_result(state['result']['ticket']) as (stream,_,__):self.assertEqual(stream.read(),b'data')
            finally:second.close()
    def test_retained_structural_worker_source_surrogate(self):
        from tools.qa.runtime_frozen_acceptance import execute
        with tempfile.TemporaryDirectory() as temp:
            evidence=execute(sys.executable,Path(temp)/'probe',source_worker=True,seconds=30)
            self.assertEqual(evidence['status'],'PASS');self.assertEqual(len(evidence['cases']),4)
            self.assertFalse(evidence['frozenVerified']);self.assertTrue(evidence['retiredWorkerRejected'])


if __name__=='__main__':unittest.main()
