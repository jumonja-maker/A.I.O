"""Adversarial session boundaries: finalizers and damaged persisted metadata."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode

from app_backend.converter_guard import GuardError
from app_backend.converter_guard.comparison_service import ComparisonService
import test_comparison_service_v19 as fixtures


class SessionAdversaries(unittest.TestCase):
    setUp=fixtures.ComparisonServiceContracts.setUp
    tearDown=fixtures.ComparisonServiceContracts.tearDown
    request=fixtures.ComparisonServiceContracts.request
    json_request=fixtures.ComparisonServiceContracts.json_request
    converted=fixtures.ComparisonServiceContracts.converted
    analyze=fixtures.ComparisonServiceContracts.analyze
    wait_analysis=fixtures.ComparisonServiceContracts.wait_analysis

    def settled(self, service, identity):
        item=service.jobs[identity]
        if item.future is not None:item.future.result(timeout=10)
        return service.snapshot(identity)

    def test_resume_during_previous_analysis_finalizer_cannot_replace_its_lease(self):
        state=self.converted();identity=state['jobId'];service=self.server.api.comparison
        entered,release=threading.Event(),threading.Event();original=service._unpin
        def held(item):
            entered.set()
            if not release.wait(10):raise TimeoutError('test finalizer barrier')
            return original(item)
        def cancelled(item,*_):item.cancel.set()
        pin=None
        try:
            with patch.object(service,'_worker',side_effect=cancelled),patch.object(service,'_unpin',side_effect=held):
                service.start(identity)
                self.assertTrue(entered.wait(5))
                item=service.jobs[identity];future=item.future;pin=item.pin
                self.assertEqual(item.state,'cancelled')
                with self.assertRaises(GuardError) as error:service.control(identity,'resume')
                self.assertIn(error.exception.code,('COMPARISON_BUSY','COMPARISON_RETRY_BUSY'))
                self.assertIs(item.pin,pin);self.assertIs(item.future,future)
                release.set();future.result(timeout=5)
            service.control(identity,'resume')
            result=self.settled(service,identity)
            self.assertEqual(result['state'],'completed');self.assertEqual(result['processed'],result['total'])
            self.assertEqual(self.guard._jobs[identity].io,0)
        finally:
            release.set()
            # Before the regression fix a replaced pin can strand the old
            # generator. Explicitly drain that fixture-owned lease on failure.
            if pin is not None and getattr(pin,'gen',None) is not None:
                item=service.jobs[identity]
                if item.future is not None:
                    try:item.future.result(timeout=5)
                    except Exception:pass
                if pin.gen.gi_frame is not None:pin.__exit__(None,None,None)

    def test_new_retry_during_previous_retry_finalizer_cannot_rewrite_its_history(self):
        self.adapter.break_csv=True
        state=self.converted(name='values.csv',data=b'key,value\nA,42\n',formats=['csv'])
        identity=state['jobId'];service=self.server.api.comparison
        analysis=self.analyze(state);self.settled(service,identity)
        tid=analysis['items'][0]['taskId']
        entered,release=threading.Event(),threading.Event();original=service._unpin_retry
        def held(item):
            entered.set()
            if not release.wait(10):raise TimeoutError('test retry finalizer barrier')
            return original(item)
        pin=None
        try:
            with patch.object(service,'_unpin_retry',side_effect=held):
                service.retry(identity,analysis['analysisId'],[tid])
                self.assertTrue(entered.wait(8))
                item=service.jobs[identity];old=item.retry;future=item.retry_future;pin=item.retry_pin
                self.assertEqual(old['state'],'completed')
                with self.assertRaises(GuardError) as error:service.retry(identity,analysis['analysisId'],[tid])
                self.assertEqual(error.exception.code,'COMPARISON_RETRY_BUSY')
                self.assertIs(item.retry,old);self.assertIs(item.retry_pin,pin)
                release.set();future.result(timeout=5)
            first=old['runId']
            service.retry(identity,analysis['analysisId'],[tid])
            item.retry_future.result(timeout=10)
            runs=service.retry_history(identity)['items']
            self.assertEqual(len(runs),2);self.assertIn(first,{run['runId'] for run in runs})
            self.assertEqual(self.guard._jobs[identity].io,0)
        finally:
            release.set()
            if pin is not None and getattr(pin,'gen',None) is not None:
                item=service.jobs[identity]
                if item.retry_future is not None:
                    try:item.retry_future.result(timeout=5)
                    except Exception:pass
                if pin.gen.gi_frame is not None:pin.__exit__(None,None,None)

    def test_malformed_retry_event_returns_structured_error_instead_of_attribute_error(self):
        state=self.converted();identity=state['jobId'];service=self.server.api.comparison
        self.analyze(state);self.settled(service,identity);catalog=service.jobs[identity].catalog
        with closing(sqlite3.connect(catalog)) as db,db:
            db.execute('INSERT INTO retry_events(run,analysis,payload) VALUES (?,?,?)',
                       ('1'*32,service.jobs[identity].identity,'[]'))
        service.close();replacement=ComparisonService(self.guard);self.server.api.comparison=replacement
        with self.assertRaises(GuardError) as error:replacement.snapshot(identity)
        self.assertIn(error.exception.code,('COMPARISON_REPORT_UNAVAILABLE','COMPARISON_HISTORY_CORRUPT'))
        self.assertTrue(self.guard.status(identity)['result']['outputs'])

    def test_completed_session_with_missing_report_rows_is_not_claimed_completed(self):
        state=self.converted(count=2);identity=state['jobId'];service=self.server.api.comparison
        self.analyze(state);self.settled(service,identity);item=service.jobs[identity]
        with closing(sqlite3.connect(item.db)) as db,db:
            db.execute('DELETE FROM results WHERE rowid=(SELECT MAX(rowid) FROM results)')
        service.close();replacement=ComparisonService(self.guard);self.server.api.comparison=replacement
        try:result=replacement.snapshot(identity)
        except GuardError as error:
            self.assertEqual(error.code,'COMPARISON_REPORT_UNAVAILABLE')
        else:
            self.assertIn(result['state'],('interrupted','failed'))
            self.assertLess(result['processed'],result['total'])
            self.assertTrue(result['canResume'] or result['canRestart'])
        self.assertTrue(self.guard.status(identity)['result']['outputs'])

    def test_cached_catalogue_is_revalidated_before_reads_or_persistence(self):
        state=self.converted();identity=state['jobId'];service=self.server.api.comparison
        self.analyze(state);self.settled(service,identity);item=service.jobs[identity]
        catalogue=item.catalog;external=self.root/'outside-catalogue.sqlite'
        shutil.copyfile(catalogue,external)
        original_bytes=catalogue.read_bytes();outside_sha=hashlib.sha256(external.read_bytes()).hexdigest()
        from app_backend.converter_guard import core as core_module
        original_link=core_module.path_is_link
        redirected=False
        try:
            catalogue.unlink()
            try:
                catalogue.symlink_to(external);redirected=True
            except (OSError,NotImplementedError):
                # A Windows account need not have file-symlink privilege.
                # The reparse check uses the same lstat predicate; inject only
                # that flag for this existing owned fixture, never skip it.
                catalogue.write_bytes(original_bytes)
            def marked(path):
                return Path(path)==catalogue or original_link(path)
            context=patch.object(core_module,'path_is_link',side_effect=marked) if not redirected else patch.object(
                core_module,'path_is_link',side_effect=original_link)
            with context:
                with self.assertRaises(GuardError) as error:service.snapshot(identity)
                self.assertEqual(error.exception.code,'COMPARISON_REPORT_UNAVAILABLE')
                # Persist must fail closed without modifying the foreign DB.
                service._persist(item)
                self.assertIsNotNone(item.persistence_error)
            self.assertEqual(hashlib.sha256(external.read_bytes()).hexdigest(),outside_sha)
        finally:
            if catalogue.exists() or catalogue.is_symlink():catalogue.unlink()
            catalogue.write_bytes(original_bytes)

    def test_submit_persistence_failure_releases_lease_and_can_be_restarted(self):
        state=self.converted();identity=state['jobId'];service=self.server.api.comparison
        persist=service._persist
        def fail_after_lease(item,*,strict=False):
            if strict and item.pin is not None:
                raise GuardError('COMPARISON_PERSISTENCE_FAILED','Controlled full disk',status=507)
            return persist(item,strict=strict)
        with patch.object(service,'_persist',side_effect=fail_after_lease):
            with self.assertRaises(GuardError) as error:service.start(identity)
            self.assertEqual(error.exception.code,'COMPARISON_PERSISTENCE_FAILED')
        item=service.jobs[identity]
        self.assertIn(item.state,('interrupted','failed'))
        self.assertIsNone(item.pin);self.assertIsNone(item.records)
        self.assertEqual(self.guard._jobs[identity].io,0)
        service.start(identity,restart=True)
        result=self.settled(service,identity)
        self.assertEqual(result['state'],'completed')
        self.assertEqual(result['processed'],result['total'])
        self.assertEqual(self.guard._jobs[identity].io,0)

    def test_malformed_report_rows_fail_closed_over_http_and_allow_explicit_restart(self):
        for mutation in ('invalid_json','list','scalar','foreign_task','invalid_status','wrong_sql_status',
                         'foreign_file','foreign_format'):
            with self.subTest(mutation=mutation):
                state=self.converted();identity=state['jobId'];service=self.server.api.comparison
                analysis=self.analyze(state);self.settled(service,identity);item=service.jobs[identity]
                tid=analysis['items'][0]['taskId'];exports=len(self.adapter.calls)
                with closing(sqlite3.connect(item.db)) as db,db:
                    payload=json.loads(db.execute('SELECT payload FROM results WHERE task=?',(tid,)).fetchone()[0])
                    damaged={'invalid_json':'{','list':'[]','scalar':'0'}.get(mutation)
                    if mutation=='foreign_task':payload['taskId']='f'*32+':0'
                    elif mutation=='invalid_status':payload['status']='made_up_green'
                    elif mutation=='foreign_file':payload['fileId']='not-this-input'
                    elif mutation=='foreign_format':payload['format']='pdf'
                    if damaged is None:damaged=json.dumps(payload)
                    db.execute('UPDATE results SET payload=? WHERE task=?',(damaged,tid))
                    if mutation=='wrong_sql_status':
                        db.execute("UPDATE results SET status='not_checked' WHERE task=?",(tid,))
                operations=[('compare?'+urlencode({'job':identity}),None),
                    ('compare/item?'+urlencode({'job':identity,'task':tid}),None),
                    ('compare/control',{'jobId':identity,'action':'resume'}),
                    ('compare/retry',{'jobId':identity,'analysisId':analysis['analysisId'],'taskIds':[tid]})]
                for phase in ('cached','recovered'):
                    if phase=='recovered':
                        service.close();service=ComparisonService(self.guard)
                        self.server.api.comparison=service
                    for route,body in operations:
                        with self.subTest(phase=phase,operation=route.split('?')[0]):
                            with self.assertRaises(HTTPError) as error:self.json_request(route,body)
                            self.assertIn(error.exception.code,(404,409))
                            self.assertTrue(error.exception.headers['Content-Type'].startswith('application/json'))
                            message=json.loads(error.exception.read())
                            self.assertEqual(message['code'],'COMPARISON_REPORT_UNAVAILABLE')
                self.assertEqual(len(self.adapter.calls),exports)
                fresh=self.json_request('compare/start',{'jobId':identity,'restart':True})
                self.assertNotEqual(fresh['analysisId'],analysis['analysisId'])
                recovered=self.wait_analysis(identity)
                self.assertEqual(recovered['state'],'completed')
                self.assertEqual(recovered['processed'],recovered['total'])
                self.assertEqual(recovered['items'][0]['taskId'],tid)
