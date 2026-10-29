"""M04: durable metadata, genuine Guard/filesystem/HTTP; conversion is controlled."""
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app_backend.converter_guard import ConverterGuard, GuardError
from app_backend.converter_guard.conversion_history import ConversionHistory
from test_converter_guard_v02 import FakeAdapter, wait_finished


def report(identity='a'*32, state='ready', saved=False):
    return {'jobId':identity,'state':state,'total':2,'succeeded':1,'failed':1,
        'qualityCompared':False,'files':[{'fileId':'f','name':'пример <img>.docx','targets':[
            {'format':'pdf','state':'saved' if saved else 'ready','outputs':[
                {'path':'out.pdf','size':4,'sha256':'b'*64,'saved':saved,'readbackVerified':saved}]},
            {'format':'bad','state':'error','code':'ENGINE_OUTPUT_MISSING','outputs':[]}]}]}


class HistoryStoreTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.history=ConversionHistory(self.root/'history')
    def test_construction_and_empty_read_are_lazy(self):
        with patch('sqlite3.connect',side_effect=AssertionError('unexpected database open')):
            self.assertEqual(self.history.list()['items'],[])
        self.assertFalse(self.history.root.exists())
    def test_report_survives_reconstruction_and_preserves_failed_conversion(self):
        original=report();self.history.record('a'*32,4,original)
        restarted=ConversionHistory(self.history.root)
        got=restarted.report('a'*32)
        self.assertEqual(got['files'],original['files']);self.assertFalse(got['qualityCompared'])
        self.assertEqual(got['failed'],1);self.assertFalse(got['historySnapshot']['containsResultFiles'])
        self.assertNotIn('historySnapshot',original)
    def test_capabilities_are_not_persisted(self):
        value=report();value['ticket']='secret';value['files'][0]['downloadUrl']='secret-url'
        self.history.record('a'*32,1,value)
        encoded=json.dumps(self.history.report('a'*32))
        self.assertNotIn('secret',encoded);self.assertNotIn('downloadUrl',encoded)
    def test_newer_revision_wins_and_order_does_not_move(self):
        self.history.record('a'*32,3,report(saved=True));self.history.record('b'*32,1,report('b'*32))
        self.history.record('a'*32,2,report(saved=False));self.history.record('a'*32,3,report(saved=False))
        self.assertTrue(self.history.report('a'*32)['files'][0]['targets'][0]['outputs'][0]['saved'])
        self.assertEqual([r['jobId'] for r in self.history.list()['items']],['b'*32,'a'*32])
    def test_saved_snapshot_does_not_relabel_conversion_error(self):
        self.history.record('a'*32,1,report());self.history.record('a'*32,2,report(saved=True))
        data=self.history.report('a'*32)
        self.assertEqual(data['files'][0]['targets'][1]['state'],'error')
        self.assertEqual(self.history.list()['items'][0]['savedOutputs'],1)
    def test_keyset_paging_is_stable_when_new_records_arrive(self):
        for i in range(32):self.history.record(f'{i:032x}',1,report(f'{i:032x}'))
        first=self.history.list();self.assertEqual(len(first['items']),25)
        self.history.record('f'*32,1,report('f'*32))
        second=self.history.list(before=first['nextCursor'])
        self.assertEqual(len(second['items']),7);self.assertIsNone(second['nextCursor'])
        self.assertFalse({x['jobId'] for x in first['items']}&{x['jobId'] for x in second['items']})
    def test_list_does_not_decode_full_reports(self):
        self.history.record('a'*32,1,report())
        with sqlite3.connect(self.history.path) as db:
            db.execute("UPDATE history SET report='not JSON'")
        self.assertEqual(len(self.history.list()['items']),1)
        with self.assertRaises(GuardError) as caught:self.history.report('a'*32)
        self.assertEqual(caught.exception.code,'HISTORY_STORAGE_FAILED')
    def test_delete_tombstone_rejects_delayed_and_newer_writes(self):
        self.history.record('a'*32,1,report())
        self.assertFalse(self.history.delete('a'*32)['resultFilesTouched'])
        self.history.record('a'*32,999,report(saved=True))
        self.assertEqual(self.history.list()['items'],[])
        with self.assertRaises(GuardError):self.history.report('a'*32)
        with sqlite3.connect(self.history.path) as db:
            self.assertEqual(db.execute('SELECT summary,report,deleted FROM history').fetchone(),(None,None,1))
    def test_delete_is_idempotent_and_does_not_read_output_paths(self):
        output=self.root/'keep.pdf';output.write_bytes(b'keep')
        self.history.record('a'*32,1,report())
        self.history.delete('a'*32);self.history.delete('a'*32)
        self.assertEqual(output.read_bytes(),b'keep')
    def test_invalid_identifiers_and_page_limits(self):
        for value in ('../job',None,'A'*32,'a'*31):
            for operation in (self.history.report,self.history.delete):
                with self.subTest(value=value,operation=operation):
                    with self.assertRaises(GuardError):operation(value)
        for kwargs in ({'limit':0},{'limit':51},{'limit':True},{'before':0},{'before':True},{'before':2**63}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(GuardError):self.history.list(**kwargs)
        self.assertFalse(self.history.root.exists())
    def test_corrupt_database_is_not_replaced_or_reported_empty(self):
        self.history.root.mkdir();self.history.path.write_bytes(b'not a sqlite database')
        before=self.history.path.read_bytes()
        with self.assertRaises(GuardError):self.history.list()
        with self.assertRaises(GuardError):self.history.record('a'*32,1,report())
        self.assertEqual(self.history.path.read_bytes(),before)
    def test_unknown_schema_is_not_migrated_silently(self):
        self.history.record('a'*32,1,report())
        with sqlite3.connect(self.history.path) as db:db.execute('PRAGMA user_version=99')
        with self.assertRaises(GuardError):self.history.list()
        with sqlite3.connect(self.history.path) as db:self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],99)
    def test_locked_database_raises_bounded_explicit_failure(self):
        self.history.record('a'*32,1,report())
        with sqlite3.connect(self.history.path) as db:
            db.execute('BEGIN EXCLUSIVE')
            with self.assertRaises(GuardError) as caught:self.history.record('a'*32,2,report())
            self.assertEqual(caught.exception.code,'HISTORY_STORAGE_FAILED')
    def test_link_boundary_is_rejected(self):
        with patch('app_backend.converter_guard.conversion_history.path_is_link',return_value=True):
            with self.assertRaises(GuardError):self.history.list()
        self.assertFalse(self.history.root.exists())
    def test_concurrent_revision_updates_are_not_rolled_back(self):
        self.history.record('a'*32,1,report())
        errors=[]
        def write(revision):
            try:self.history.record('a'*32,revision,report(saved=revision==9))
            except Exception as error:errors.append(error)
        threads=[threading.Thread(target=write,args=(i,)) for i in (9,3,8,2,5)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(3)
        self.assertFalse(errors)
        self.assertEqual(self.history.report('a'*32)['historySnapshot']['revision'],9)


class HistoryGuardIntegration(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);self.root=Path(temporary.name)
        self.guard=ConverterGuard(adapter=FakeAdapter(),spool_parent=self.root/'spool',history_parent=self.root/'history')
        self.addCleanup(self.guard.close)
    def ready(self):
        identity=self.guard.create([{'id':'f','name':'example.docx','size':4}],['pdf','bad'])['jobId']
        self.guard.upload(identity,'f',io.BytesIO(b'keep'),4)
        self.guard.start(identity,archive_mode='on_demand')
        state=wait_finished(self.guard,identity);self.guard._jobs[identity].future.result(timeout=10)
        return identity,state
    def test_terminal_report_survives_result_cleanup_and_restart(self):
        identity,state=self.ready();owned=self.guard._jobs[identity].root
        self.assertEqual(len(self.guard.history_list()['items']),1)
        self.guard.release(identity,discard=True)
        self.assertFalse(owned.exists())
        self.assertEqual(ConversionHistory(self.root/'history').report(identity)['failed'],1)
    def test_folder_save_updates_history_only_after_verified_save(self):
        identity,state=self.ready()
        self.assertEqual(self.guard.history_list()['items'][0]['savedOutputs'],0)
        saved=self.guard.save_folder(state['result']['ticket'],self.root)
        self.assertTrue(saved['readbackVerified'])
        self.assertEqual(self.guard.history_list()['items'][0]['savedOutputs'],1)
        self.assertEqual(self.guard.history_report(identity)['failed'],1)
    def test_cancelled_save_does_not_claim_saved_in_history(self):
        identity,state=self.ready();cancel=threading.Event()
        with self.assertRaises(GuardError):
            self.guard.save_folder(state['result']['ticket'],self.root,cancel=cancel,on_verifying=cancel.set)
        self.assertEqual(self.guard.history_list()['items'][0]['savedOutputs'],0)
    def test_deleting_history_never_releases_results_or_reappears_after_save(self):
        identity,state=self.ready();owned=self.guard._jobs[identity];paths=[a.path for a in owned.artifacts]
        self.guard.delete_history(identity)
        self.assertTrue(all(p.is_file() for p in paths));self.assertFalse(owned.released)
        self.guard.save_folder(state['result']['ticket'],self.root)
        self.assertEqual(self.guard.history_list()['items'],[])
    def test_history_storage_failure_never_fails_conversion_or_ack(self):
        with patch.object(self.guard._history,'record',side_effect=OSError('private failure')):
            identity,state=self.ready()
            saved=self.guard.save_folder(state['result']['ticket'],self.root)
        self.assertEqual(state['state'],'partial');self.assertTrue(saved['saved'])
        self.assertEqual(self.guard.history_list()['writeWarning'],'HISTORY_WRITE_FAILED')
    def test_history_disk_io_does_not_hold_guard_state_lock(self):
        identity,_=self.ready();entered=threading.Event();release=threading.Event()
        def blocked(*args):entered.set();release.wait(3)
        with patch.object(self.guard._history,'record',side_effect=blocked):
            writer=threading.Thread(target=self.guard._remember_history,args=(self.guard._jobs[identity],))
            writer.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertTrue(self.guard._lock.acquire(timeout=.25))
                self.guard._lock.release()
            finally:release.set();writer.join(3)
    def test_history_reads_never_start_recovery_or_native_work(self):
        with patch.object(self.guard,'_load_recovery',side_effect=AssertionError('recovery started')):
            self.assertEqual(self.guard.history_list()['items'],[])
        self.assertEqual(self.guard.adapter.calls,[]);self.assertFalse((self.root/'history').exists())
    def test_history_cannot_live_inside_result_cleanup_tree(self):
        with self.assertRaises(ValueError):
            ConverterGuard(adapter=FakeAdapter(),spool_parent=self.root/'other',history_parent=self.root/'other'/'history')
    def test_disabled_history_keeps_legacy_constructor_contract(self):
        with ConverterGuard(adapter=FakeAdapter()) as guard:
            self.assertFalse(guard.history_list()['enabled'])


class HistoryHTTP(unittest.TestCase):
    def setUp(self):
        from app_backend.server import start_server
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);root=Path(temporary.name)
        self.server,self.token=start_server(guard_recovery_parent=root/'jobs',guard_history_parent=root/'history')
        worker=threading.Thread(target=self.server.serve_forever,daemon=True);worker.start()
        self.addCleanup(self.server.server_close);self.addCleanup(worker.join,5);self.addCleanup(self.server.shutdown)
        self.base='http://127.0.0.1:'+str(self.server.server_port)
        self.server.guard._history.record('a'*32,1,report())
    def request(self,path,body=None,authorized=True):
        headers={'X-AIO-Token':self.token} if authorized else {}
        if body is not None:headers['Content-Type']='application/json'
        request=Request(self.base+'/api/converter-guard'+path,
            data=None if body is None else json.dumps(body).encode(),headers=headers)
        try:
            with urlopen(request,timeout=5) as response:return response.status,json.load(response)
        except HTTPError as error:return error.code,json.load(error)
    def test_history_requires_token(self):
        self.assertEqual(self.request('/history',authorized=False)[0],403)
    def test_authenticated_list_and_report(self):
        code,value=self.request('/history?limit=25');self.assertEqual(code,200);self.assertEqual(len(value['items']),1)
        code,value=self.request('/history/report?job='+'a'*32);self.assertEqual(code,200)
        self.assertEqual(value['historySnapshot']['jobId'],'a'*32)
    def test_unknown_duplicate_and_invalid_queries_rejected(self):
        for suffix in ('?limit=25&limit=2','?limit=51','?before=0','?path=/etc','/report?job='+'a'*32+'&x=1'):
            with self.subTest(suffix=suffix):self.assertEqual(self.request('/history'+suffix)[0],400)
    def test_explicit_delete_confirmation_and_no_job_access(self):
        code,_=self.request('/history/delete',{'jobId':'a'*32,'confirm':False});self.assertEqual(code,400)
        with patch.object(self.server.guard,'_get',side_effect=AssertionError('history looked up a live job')):
            code,value=self.request('/history/delete',{'jobId':'a'*32,'confirm':True})
        self.assertEqual(code,200);self.assertFalse(value['resultFilesTouched'])
        self.assertEqual(self.request('/history/report?job='+'a'*32)[0],404)


class HistoryDOM(unittest.TestCase):
    def test_real_history_dom(self):
        root=Path(__file__).resolve().parents[1]
        completed=subprocess.run(['node',str(root/'tests'/'conversion_history_dom.mjs')],
            cwd=root,capture_output=True,text=True,encoding='utf-8',timeout=30)
        self.assertEqual(completed.returncode,0,completed.stdout+completed.stderr)
        self.assertIn('HISTORY_DOM=PASS',completed.stdout)


if __name__=='__main__':unittest.main()
