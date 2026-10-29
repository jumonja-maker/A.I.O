"""A late GUI status/report request must never discover old jobs implicitly."""
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app_backend.converter_guard import ConverterGuard, GuardError
from app_backend.converter_guard.http_api import GuardAPI, PREFIX
from test_converter_guard_v02 import FakeAdapter, wait_finished


class CurrentRecoveryLookup(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.guard=ConverterGuard(adapter=FakeAdapter(),recovery_parent=Path(temp.name).resolve()/'owned')
        self.addCleanup(self.guard.close)
        self.api=GuardAPI(self.guard,origin='http://127.0.0.1:8877',token='token')
        self.addCleanup(self.api.close)

    def get(self, route, headers=None):
        codes=[]
        self.response_headers={}
        handler=SimpleNamespace(path=PREFIX+route,headers=headers or {},wfile=io.BytesIO(),
            send_response=codes.append,
            send_header=lambda key,value:self.response_headers.__setitem__(key,value),
            end_headers=lambda:None,
            _json=lambda code,body:(code,body))
        with patch.object(self.api,'_authorize'):
            result=self.api.get(handler,PREFIX+route.split('?')[0])
        if result is not None:
            return result
        self.assertEqual(len(codes),1)
        raw=handler.wfile.getvalue()
        return codes[0],json.loads(raw) if raw else None

    def test_late_gui_get_never_triggers_full_recovery(self):
        for route in ('jobs','report'):
            with self.subTest(route=route),patch.object(self.guard,'_load_recovery') as loader:
                status,body=self.get('/'+route+'?job='+'a'*32+'&lookup=current')
                self.assertEqual((status,body['code']),(404,'JOB_NOT_FOUND'))
                self.assertEqual(loader.call_count,0,'LATE_LOOKUP_MUST_NOT_SCAN_DISK')

    def test_legacy_lookup_remains_compatible(self):
        for route in ('jobs','report'):
            with self.subTest(route=route),patch.object(self.guard,'_load_recovery') as loader:
                self.assertEqual(self.get('/'+route+'?job='+'a'*32)[0],404)
                loader.assert_called_once_with()

    def test_invalid_lookup_fails_before_disk_work(self):
        for route in ('jobs','report'):
            for query in ('lookup=','lookup=old','lookup=current&lookup=current'):
                with self.subTest(route=route,query=query),patch.object(self.guard,'_load_recovery') as loader:
                    self.assertEqual(self.get('/'+route+'?job='+'a'*32+'&'+query)[0],400)
                    loader.assert_not_called()

    def test_current_job_remains_readable_and_discard_stays_missing(self):
        identity=self.guard.create([{'id':'f','name':'source.docx','size':0}],['pdf'])['jobId']
        self.guard.upload(identity,'f',io.BytesIO(b''),0)
        self.guard.start(identity,archive_mode='on_demand');wait_finished(self.guard,identity)
        with patch.object(self.guard,'_load_recovery') as loader:
            route='/jobs?job='+identity+'&lookup=current'
            status,state=self.get(route)
            self.assertEqual((status,state['jobId']),(200,identity))
            etag=self.response_headers['ETag']
            self.assertEqual(self.get(route,headers={'If-None-Match':etag}),(304,None))
            self.assertEqual(self.response_headers['ETag'],etag)
            status,report=self.get('/report?job='+identity+'&lookup=current')
            self.assertEqual((status,report['jobId']),(200,identity))
            state=self.guard.status(identity,recover=False)
            self.assertTrue(self.guard.status(identity,recover=False,since_revision=state['revision'])['unchanged'])
            self.guard.release(identity,discard=True)
            self.assertEqual(self.get(route)[0],404)
            self.assertEqual(self.get('/report?job='+identity+'&lookup=current')[0],404)
            loader.assert_not_called()

    def test_invalid_core_flags_do_not_load(self):
        for method in (self.guard.status,self.guard.report):
            for flag in (None,0,1,'false'):
                with patch.object(self.guard,'_load_recovery') as loader,self.assertRaises(GuardError):
                    method('a'*32,recover=flag)
                loader.assert_not_called()

    def test_current_status_prunes_without_synchronous_delete(self):
        with patch.object(self.guard,'_prune') as prune,self.assertRaises(GuardError):
            self.guard.status('a'*32,recover=False)
        prune.assert_called_once_with(defer_cleanup=True)

    def test_manual_scan_still_requires_the_session_token(self):
        handler=SimpleNamespace(headers={'Host':'127.0.0.1:8877','X-AIO-Token':'wrong'},
            _json=lambda status,body:(status,body),connection=Mock())
        with patch.object(self.guard,'start_recovery_scan') as scan:
            code,body=self.api.post(handler,PREFIX+'/recovery-scan')
        self.assertEqual((code,body['code']),(403,'ACCESS_DENIED'))
        scan.assert_not_called()


if __name__=='__main__':unittest.main()
