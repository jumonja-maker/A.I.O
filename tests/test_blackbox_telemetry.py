"""A.I.O -> BLACK.BOX v0.1 diagnostics tests (no LibreOffice required).

Run: python -m unittest discover -s tests -p 'test_blackbox_telemetry.py' -v
"""
from collections import Counter, defaultdict
import io
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from app_backend.blackbox_telemetry import TelemetryEmitter, from_environment, FIELDS
from app_backend.converter_guard import ConverterGuard
from test_converter_guard_v02 import FakeAdapter, wait_finished


class BlackBoxEmitterTests(unittest.TestCase):
    def setUp(self):
        # The protocol forbids writing into A.I.O's temporary conversion tree.
        self.temp = tempfile.TemporaryDirectory(prefix='aio-blackbox-qa-', dir=Path.cwd())
        self.root = Path(self.temp.name)
        self.emitters = []
        self.guards = []

    def tearDown(self):
        for guard in self.guards:
            guard.close()
        for emitter in self.emitters:
            emitter.close()
        self.temp.cleanup()

    def emitter(self, name='events.jsonl', capacity=8192):
        emitter = TelemetryEmitter(self.root / name, capacity=capacity)
        self.emitters.append(emitter)
        return emitter

    @staticmethod
    def events(emitter):
        emitter.close()
        return [json.loads(x) for x in emitter.path.read_text(encoding='utf-8').splitlines()]

    def run_guard(self, files, targets, emitter=None):
        fake = FakeAdapter()
        guard = ConverterGuard(adapter=fake, spool_parent=self.root,
                               telemetry=emitter)
        self.guards.append(guard)
        inputs = [{'id':str(i), 'name':name, 'size':len(data)}
                  for i,(name,data) in enumerate(files)]
        identity=guard.create(inputs,targets)['jobId']
        for row,(_,data) in zip(inputs,files):
            guard.upload(identity,row['id'],io.BytesIO(data),len(data))
        guard.start(identity)
        return wait_finished(guard,identity),fake,guard

    def test_disabled_by_default_does_not_create_file_or_worker(self):
        with patch.dict(os.environ, {'AIO_BLACKBOX_EVENTS':''}):
            self.assertIsNone(from_environment())
        self.assertEqual(list(self.root.glob('*.jsonl')), [])

    def test_normal_task_lifecycle_has_stable_ids_and_utc_seq(self):
        emitter=self.emitter()
        result,adapter,_=self.run_guard([('demo.docx',b'xyz')], ['pdf'],emitter)
        self.assertEqual(result['succeeded'],1)
        events=self.events(emitter)
        types=[e['type'] for e in events]
        self.assertTrue({'job_created','task_queued','task_started',
                         'task_succeeded','result_created','job_finished'}.issubset(types))
        self.assertEqual(types.count('task_succeeded'),1)
        self.assertEqual(types.count('task_failed'),0)
        self.assertEqual(types.count('task_cancelled'),0)
        self.assertEqual([e['seq'] for e in events],list(range(1,len(events)+1)))
        tasks=[e for e in events if e['type'].startswith('task_')]
        self.assertEqual(len(set((e['request_id'],e['file_id'],e['target']) for e in tasks)),1)
        self.assertTrue(all(e['timestamp'].endswith('+00:00') for e in events))
        self.assertEqual(len(adapter.calls),1)

    def test_failed_task_reports_failure_not_task_finished_success(self):
        emitter=self.emitter()
        result,_,_=self.run_guard([('bad.docx',b'xyz')], ['pdf'], emitter)
        self.assertEqual(result['failed'],1)
        events=self.events(emitter)
        self.assertEqual([e['type'] for e in events].count('task_failed'),1)
        self.assertFalse(any(e['type']=='task_succeeded' for e in events))
        failed=next(e for e in events if e['type']=='task_failed')
        self.assertEqual(failed['reason_code'],'ENGINE_OUTPUT_MISSING')

    def test_cancelled_task_stays_cancelled(self):
        emitter=self.emitter()
        fake=FakeAdapter()
        guard=ConverterGuard(adapter=fake, spool_parent=self.root,telemetry=emitter)
        self.guards.append(guard)
        identity=guard.create([{'id':'s','name':'hold.docx','size':1}],['pdf'])['jobId']
        guard.upload(identity,'s',io.BytesIO(b'x'),1)
        guard.start(identity)
        self.assertTrue(fake.waiting.wait(timeout=3))
        guard.cancel(identity)
        result=wait_finished(guard,identity)
        self.assertEqual(result['state'],'cancelled')
        events=self.events(emitter)
        self.assertEqual(sum(e['type']=='task_cancelled' for e in events),1)
        self.assertFalse(any(e['type']=='task_succeeded' for e in events))

    def test_multiple_files_and_targets_reconcile_terminals(self):
        emitter=self.emitter()
        results,_,_=self.run_guard(
            [('first.docx',b'data'),('bad.docx',b'data'),('other.bmp',b'data')],
            ['pdf','png'],emitter)
        events=self.events(emitter)
        kinds=Counter(e['type'] for e in events)
        self.assertEqual(kinds['task_queued'],6)
        self.assertEqual(kinds['task_started'],6)
        self.assertEqual(kinds['task_succeeded'],4)
        self.assertEqual(kinds['task_failed'],2)
        self.assertEqual(kinds['task_cancelled'],0)
        self.assertEqual(results['completed'],6)
        by_key=defaultdict(list)
        for event in events:
            if event['type'].startswith('task_'):
                by_key[(event['request_id'],event['file_id'],event['target'])].append(event['type'])
        self.assertEqual(len(by_key),6)
        for history in by_key.values():
            self.assertEqual(history[0],'task_queued')
            self.assertEqual(history[1],'task_started')
            self.assertEqual(len(history),3)
            self.assertIn(history[2],('task_succeeded','task_failed'))

    def test_overflow_is_explicit_and_never_blocks_producer(self):
        emitter=self.emitter(capacity=1)
        started=time.monotonic()
        for _ in range(10000):
            emitter.emit('task_queued',request_id='request',file_id='file',target='pdf')
        duration=time.monotonic()-started
        self.assertLess(duration,5)
        self.assertGreater(emitter.dropped,0)
        events=self.events(emitter)
        self.assertTrue(any(e['type']=='telemetry_dropped' and
                            e.get('reason_code')=='QUEUE_OVERFLOW' for e in events))
        self.assertTrue(any(b['seq'] > a['seq']+1 for a,b in zip(events,events[1:]))
                        or any(e['type']=='telemetry_dropped' for e in events))

    def test_unavailable_file_fails_only_diagnostics(self):
        existing=self.root/'already.jsonl'
        existing.write_text('preserved\n',encoding='utf-8')
        emitter=self.emitter('already.jsonl')
        deadline=time.monotonic()+2
        while not emitter.failed and time.monotonic()<deadline:
            time.sleep(.01)
        self.assertTrue(emitter.failed)
        result,adapter,_=self.run_guard([('x.docx',b'abc')],['pdf'],emitter)
        self.assertEqual(result['succeeded'],1)
        self.assertEqual(len(adapter.calls),1)
        self.assertEqual(existing.read_text(encoding='utf-8'),'preserved\n')

    def test_close_flushes_pending_events_without_delaying_guard(self):
        emitter=self.emitter()
        for i in range(800):
            emitter.emit('task_queued',request_id='request',file_id=str(i),target='pdf')
        events=self.events(emitter)
        self.assertEqual(sum(e['type']=='task_queued' for e in events),800)
        emitter.emit('task_started',request_id='request',file_id='1',target='pdf')
        self.assertEqual(self.events(emitter),events)

    def test_opted_in_and_disabled_guard_results_equal(self):
        emitter=self.emitter()
        inputs=[('good.docx',b'data'),('bad.docx',b'data')]
        baseline,baseline_adapter,_=self.run_guard(inputs,['pdf','png'])
        observed,observed_adapter,_=self.run_guard(inputs,['pdf','png'],emitter)
        self.assertEqual([(t['state'],t.get('code')) for f in baseline['files']
                          for t in f['targets']],
                         [(t['state'],t.get('code')) for f in observed['files']
                          for t in f['targets']])
        self.assertEqual(baseline_adapter.calls,observed_adapter.calls)
        self.assertEqual(Counter(e['type'] for e in self.events(emitter))['task_queued'],4)

    def test_schema_redaction_blackbox_v01(self):
        emitter=self.emitter()
        secret='C:\\Users\\Private Person\\Documents\\SENSITIVE_TOKEN.docx'
        emitter.emit('task_failed',request_id=secret,file_id=secret,
                     target='pdf',reason_code=secret,stage='engine',
                     status='failed',duration_ms=300,process_pid=123)
        events=self.events(emitter)
        blob=json.dumps(events)
        self.assertNotIn(secret,blob)
        self.assertNotIn('SENSITIVE_TOKEN',blob)
        self.assertTrue(all(set(e).issubset(FIELDS) for e in events))
        self.assertTrue(all(re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_.-]{0,63}',e['type'])
                            for e in events))
        failure=next(e for e in events if e['type']=='task_failed')
        self.assertEqual(failure['reason_code'],'REDACTED')
        self.assertEqual(failure['request_id'],failure['file_id'])
        self.assertEqual(failure['duration_ms'],300)
        self.assertEqual(failure['process_pid'],123)

    def test_concurrent_emitters_preserve_admitted_sequence_order(self):
        emitter=self.emitter(capacity=8192)
        def produce(worker):
            for i in range(200):
                emitter.emit('task_started',request_id='job',file_id=str(worker*200+i),
                             target='pdf')
        jobs=[threading.Thread(target=produce,args=(x,)) for x in range(8)]
        for t in jobs:t.start()
        for t in jobs:t.join(timeout=5)
        self.assertTrue(all(not t.is_alive() for t in jobs))
        events=self.events(emitter)
        self.assertEqual(emitter.dropped,0)
        self.assertEqual([e['seq'] for e in events],list(range(1,len(events)+1)))
        self.assertEqual(sum(e['type']=='task_started' for e in events),1600)

    def test_unsafe_destination_is_rejected_and_app_can_remain_off(self):
        with self.assertRaises(ValueError):
            TelemetryEmitter(Path(tempfile.gettempdir())/'aio-guard-unsafe'/'events.jsonl')
        with patch.dict(os.environ,{'AIO_BLACKBOX_EVENTS':str(
                Path(tempfile.gettempdir())/'unsafe.jsonl')}):
            self.assertIsNone(from_environment())


if __name__=='__main__':
    unittest.main()
