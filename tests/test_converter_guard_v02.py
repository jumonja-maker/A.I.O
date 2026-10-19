"""Guard ownership, independent routes and bounded transfer regressions."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
import unittest
from unittest.mock import patch
import zipfile

from app_backend.converter_guard import ConverterGuard, GuardError, GuardLimits, GuardTrace, LibreOfficeAdapter
from app_backend.converter_guard.adapter import _RunControl
from app_backend.converter_guard.core import CHUNK


class FakeAdapter:
    available = True

    def __init__(self):
        self.calls = []
        self.waiting = threading.Event()
        self.resume = threading.Event()
        self.active = 0
        self.maximum_active = 0

    def capabilities(self, files):
        return {'files': files, 'compatibility': 'native_decision'}

    def convert(self, source, target, workspace, *, cancel, timeout, on_started, output_limit):
        self.calls.append((source.name, target))
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        try:
            on_started()
            if source.name.startswith('bad') or target == 'bad':
                raise GuardError('ENGINE_OUTPUT_MISSING', 'Fixture cannot export.', stage='engine')
            if source.name.startswith('unexpected'):
                raise RuntimeError('private /path/must/not/leak')
            if source.name.startswith('hold'):
                self.waiting.set()
                while not self.resume.is_set():
                    if cancel.wait(.01):
                        raise GuardError('CANCELLED', 'Cancelled.', stage='engine')
            if cancel.is_set():
                raise GuardError('CANCELLED', 'Cancelled.', stage='engine')
            output = workspace / 'output'
            output.mkdir(parents=True)
            result = output / (source.stem + '.' + target)
            result.write_bytes(b'' if target == 'txt' else b'data')
            if target == 'html':
                sidecar = output / 'images' / 'plot.png'
                sidecar.parent.mkdir()
                sidecar.write_bytes(b'image-data')
                result.write_text('<img src="images/plot.png">')
                return [result, sidecar]
            return [result]
        finally:
            self.active -= 1


def wait_finished(guard, identity, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        state = guard.status(identity)
        if state['finished']:
            return state
        time.sleep(.005)
    raise AssertionError('Guard did not finish: ' + repr(state))


def ticket(output):
    return output['downloadUrl'].split('ticket=')[1]


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.adapter = FakeAdapter()
        self.guard = ConverterGuard(adapter=self.adapter, spool_parent=self.temp.name)

    def tearDown(self):
        self.guard.close()
        self.assertEqual(self.guard.storage_stats()['jobs'], 0)
        self.assertEqual(self.guard.storage_stats()['reservedBytes'], 0)
        self.temp.cleanup()

    def prepare(self, files, formats=None):
        rows = [{'id': 'f' + str(i), 'name': name, 'size': len(data)}
                for i, (name, data) in enumerate(files)]
        identity = self.guard.create(rows, formats)['jobId']
        for i, (_, data) in enumerate(files):
            self.guard.upload(identity, 'f' + str(i), io.BytesIO(data), len(data))
        return identity

    def run_job(self, files, formats=None):
        identity = self.prepare(files, formats)
        self.guard.start(identity)
        return wait_finished(self.guard, identity)

    def test_unknown_input_and_target_reach_adapter_without_family_veto(self):
        state = self.run_job([('image.BMP', b'x'), ('source.unknown', b'x'), ('noextension', b'')], ['custom', 'pdf'])
        self.assertEqual(state['state'], 'ready')
        self.assertEqual(state['total'], 6)
        self.assertEqual(len(self.adapter.calls), 6)
        self.assertTrue(all(t['libreOfficeStarted'] for f in state['files'] for t in f['targets']))

    def test_first_failed_file_does_not_hold_remaining_files_or_targets(self):
        state = self.run_job([('bad.docx', b'x'), ('good.bmp', b'x'), ('next.odt', b'x')], ['pdf','bad','png'])
        self.assertEqual(state['state'], 'partial')
        self.assertEqual(len(self.adapter.calls), 9)
        self.assertEqual(sum(t['state'] == 'ready' for f in state['files'] for t in f['targets']), 4)
        self.assertEqual(state['completed'], 9)

    def test_unexpected_adapter_error_is_private_and_does_not_stop_queue(self):
        state = self.run_job([('unexpected.docx', b'x'), ('good.docx', b'x')])
        target = state['files'][0]['targets'][0]
        self.assertEqual(target['code'], 'ENGINE_INTERNAL')
        self.assertNotIn('/path/', json.dumps(state))
        self.assertEqual(state['files'][1]['state'], 'ready')

    def test_source_is_retained_for_all_targets_then_reclaimed(self):
        self.guard.close()
        self.guard = ConverterGuard(adapter=self.adapter, limits=GuardLimits(max_spool_bytes=12),
                                    spool_parent=self.temp.name)
        state = self.run_job([('one.bmp', b'abcd'), ('two.docx', b'abcd')])
        self.assertEqual(state['state'], 'ready')
        self.assertEqual(self.guard.storage_stats()['reservedBytes'], 8)
        self.assertEqual(state['packagingError'], 'BUNDLE_QUOTA')
        self.assertEqual(len(state['result']['outputs']), 2)
        self.assertFalse(any(self.guard._jobs[state['jobId']].root.joinpath('input').rglob('*.*')))

    def test_native_sidecars_and_failure_report_keep_relative_hierarchy(self):
        state = self.run_job([('bad.docx', b'x'), ('web.docx', b'x')], ['html'])
        with self.guard.open_result(ticket(state['result'])) as (stream, name, _):
            self.assertEqual(name, 'AIO_Results.zip')
            with zipfile.ZipFile(stream) as archive:
                names = archive.namelist()
                self.assertIn('0002-html/web.html', names)
                self.assertIn('0002-html/images/plot.png', names)
                report = json.loads(archive.read('AIO-conversion-report.json'))
                self.assertEqual(report['files'][0]['targets'][0]['code'], 'ENGINE_OUTPUT_MISSING')
                self.assertFalse(report['qualityCompared'])
                self.assertNotIn('ticket=', json.dumps(report))

    def test_zip64_is_forced_for_every_streamed_entry(self):
        original = zipfile.ZipFile.open
        writes = []
        def record(archive, name, mode='r', pwd=None, *, force_zip64=False):
            if mode == 'w' and isinstance(name, zipfile.ZipInfo) and name.filename != 'AIO-conversion-report.json':
                writes.append(force_zip64)
            return original(archive, name, mode, pwd=pwd, force_zip64=force_zip64)
        with patch.object(zipfile.ZipFile, 'open', record):
            self.run_job([('one.bmp', b'x'), ('two.docx', b'x')])
        self.assertGreaterEqual(len(writes), 2)
        self.assertTrue(all(writes))

    def test_zero_byte_native_output_is_not_silently_rejected(self):
        state = self.run_job([('empty.docx', b'')], ['txt'])
        self.assertEqual(state['state'], 'ready')
        self.assertEqual(state['result']['size'], 0)
        with self.guard.open_result(ticket(state['result'])) as (stream, name, size):
            self.assertEqual((stream.read(), name, size), (b'', 'empty.txt', 0))

    def test_save_ack_is_separate_from_download_and_scoped_to_owner(self):
        state = self.run_job([('one.docx', b'x')])
        identity, key = state['jobId'], ticket(state['result'])
        for _ in range(2):
            with self.guard.open_result(key) as (stream, _, _):
                self.assertEqual(stream.read(), b'data')
        self.assertEqual(self.guard.status(identity)['files'][0]['state'], 'ready')
        other = self.prepare([('other.docx', b'x')])
        with self.assertRaises(GuardError): self.guard.confirm_saved(other, key)
        saved = self.guard.confirm_saved(identity, key)
        self.assertEqual(saved['files'][0]['state'], 'saved')
        self.assertTrue(saved['result']['outputs'][0]['saved'])
        self.assertEqual(self.guard.confirm_saved(identity, key)['files'][0]['state'], 'saved')

    def test_bundle_ack_marks_all_ready_targets_saved(self):
        state = self.run_job([('one.bmp', b'x')], ['pdf','png'])
        saved = self.guard.confirm_saved(state['jobId'], ticket(state['result']))
        self.assertEqual(saved['files'][0]['state'], 'saved')
        self.assertTrue(all(t['state'] == 'saved' for t in saved['files'][0]['targets']))

    def test_partial_sidecar_ack_does_not_claim_whole_html_saved(self):
        state = self.run_job([('web.docx', b'x')], ['html'])
        saved = self.guard.confirm_saved(state['jobId'], ticket(state['result']['outputs'][0]))
        self.assertEqual(saved['files'][0]['state'], 'ready')
        self.assertFalse(saved['result']['outputs'][1]['saved'])

    def test_release_invalidates_new_downloads_but_keeps_open_lease(self):
        state = self.run_job([('one.docx', b'x')])
        key = ticket(state['result'])
        root = self.guard._jobs[state['jobId']].root
        with self.guard.open_result(key) as (stream, _, _):
            self.guard.release(state['jobId'])
            self.assertTrue(root.exists())
            with self.assertRaises(GuardError):
                with self.guard.open_result(key): pass
            self.assertEqual(stream.read(), b'data')
        self.assertFalse(root.exists())

    def test_ready_outputs_do_not_expire_while_waiting_for_save(self):
        state = self.run_job([('one.docx', b'x')])
        future = time.monotonic() + 100000
        with patch('app_backend.converter_guard.core.time.monotonic', return_value=future):
            with self.guard.open_result(ticket(state['result'])) as (stream, _, _):
                self.assertEqual(stream.read(), b'data')

    def test_expired_abandoned_upload_releases_reservation(self):
        identity = self.prepare([('one.docx', b'x')])
        future = time.monotonic() + 100000
        with patch('app_backend.converter_guard.core.time.monotonic', return_value=future):
            with self.assertRaises(GuardError): self.guard.status(identity)
        self.assertEqual(self.guard.storage_stats()['reservedBytes'], 0)

    def test_explicit_policy_and_metadata_validation_precede_disk_allocation(self):
        self.guard.close()
        self.guard = ConverterGuard(adapter=self.adapter,
            limits=GuardLimits(max_files=1, max_file_bytes=3, max_spool_bytes=3), spool_parent=self.temp.name)
        for rows in ([{'id':'f','name':'a.bmp','size':4}],
                     [{'id':'f','name':'../a.bmp','size':1}],
                     [{'id':'f','name':'a.bmp','size':True}],
                     [{'id':'f','name':'a.bmp','size':1}]*2):
            with self.subTest(rows=rows), self.assertRaises(GuardError):
                self.guard.create(rows)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_no_default_document_size_count_or_time_barrier(self):
        rows = [{'id':str(i),'name':str(i)+'.bmp','size':10*1024**3} for i in range(120)]
        self.guard.capabilities(rows)
        state = self.guard.create(rows, ['pdf'] * 20)
        self.assertEqual(state['total'],120)
        self.assertEqual(self.guard.storage_stats()['reservedBytes'],1200*1024**3)
        self.assertIsNone(self.guard.limits.max_files)
        self.assertIsNone(self.guard.limits.file_timeout)

    def test_large_upload_is_chunked_without_file_sized_memory(self):
        class Generated:
            remaining = 64 * 1024 * 1024
            largest = 0
            def read(self, size):
                self.largest = max(self.largest, size)
                count = min(size, self.remaining)
                self.remaining -= count
                return b'x' * count
        stream = Generated()
        size = stream.remaining
        identity = self.guard.create([{'id':'f','name':'large.bmp','size':size}])['jobId']
        tracemalloc.start()
        try:
            self.guard.upload(identity, 'f', stream, size)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLessEqual(stream.largest, CHUNK)
        self.assertLess(peak, 2 * 1024 * 1024)

    def test_cancel_stalled_upload_calls_interrupt_and_unblocks_reader(self):
        entered, unblock, errors = threading.Event(), threading.Event(), []
        class Blocked:
            def read(self, _size):
                entered.set()
                unblock.wait(3)
                return b''
        identity = self.guard.create([{'id':'f','name':'a.bmp','size':4}])['jobId']
        def upload():
            try: self.guard.upload(identity, 'f', Blocked(), 4, interrupt=unblock.set)
            except GuardError as error: errors.append(error.code)
        worker = threading.Thread(target=upload)
        worker.start()
        self.assertTrue(entered.wait(2))
        self.guard.cancel(identity)
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, ['CANCELLED'])

    def test_cancel_running_preserves_previous_result_and_continues_new_job(self):
        identity = self.prepare([('good.bmp', b'x'), ('hold.docx', b'x'), ('never.odt', b'x')])
        self.guard.start(identity)
        self.assertTrue(self.adapter.waiting.wait(2))
        self.guard.cancel(identity)
        state = wait_finished(self.guard, identity)
        self.assertEqual(state['state'], 'cancelled')
        self.assertEqual(state['files'][0]['state'], 'ready')
        self.assertEqual(state['files'][2]['state'], 'cancelled')
        self.assertNotIn(('never.odt','pdf'), self.adapter.calls)
        with self.guard.open_result(ticket(state['result']['outputs'][0])) as (stream,_,_):
            self.assertEqual(stream.read(),b'data')
        state2 = self.run_job([('after.bmp', b'x')])
        self.assertEqual(state2['state'],'ready')

    def test_queued_cancel_does_not_wait_behind_long_native_job(self):
        first = self.prepare([('hold.docx',b'x')])
        self.guard.start(first)
        self.assertTrue(self.adapter.waiting.wait(2))
        second = self.prepare([('queued.bmp',b'x')])
        self.guard.start(second)
        state = self.guard.cancel(second)
        self.assertTrue(state['finished'])
        self.assertEqual(state['state'],'cancelled')
        self.guard.release(second)
        self.guard.cancel(first)
        wait_finished(self.guard,first)
        self.assertNotIn(('queued.bmp','pdf'),self.adapter.calls)

    def test_skip_current_file_keeps_job_alive_and_runs_the_next_file(self):
        identity = self.prepare([('hold.docx', b'x'), ('next.bmp', b'x')], ['pdf','png'])
        self.guard.start(identity)
        self.assertTrue(self.adapter.waiting.wait(2))
        self.guard.cancel_file(identity,'f0')
        state = wait_finished(self.guard,identity)
        self.assertEqual(state['state'],'partial')
        self.assertEqual(state['files'][0]['state'],'cancelled')
        self.assertEqual(state['files'][1]['state'],'ready')
        self.assertFalse(self.guard._jobs[identity].cancel.is_set())
        self.assertNotIn(('hold.docx','png'),self.adapter.calls)
        self.assertIn(('next.bmp','png'),self.adapter.calls)

    def test_skip_unuploaded_file_reclaims_its_reserved_bytes(self):
        identity = self.guard.create([{'id':'a','name':'never.bmp','size':10000},
                                     {'id':'b','name':'good.bmp','size':1}])['jobId']
        self.guard.cancel_file(identity,'a')
        self.guard.upload(identity,'b',io.BytesIO(b'x'),1)
        self.guard.start(identity)
        state = wait_finished(self.guard,identity)
        self.assertEqual(state['state'],'partial')
        # Reservation consists only of the real output and real bundle.
        self.assertEqual(self.guard.storage_stats()['reservedBytes'],
                         sum(a['size'] for a in state['result']['outputs']) + state['result']['size'])

    def test_skip_finished_file_does_not_discard_a_ready_output(self):
        state = self.run_job([('good.bmp',b'x')])
        result = self.guard.cancel_file(state['jobId'],'f0')
        self.assertEqual(result['files'][0]['state'],'ready')
        self.assertFalse(result['files'][0]['cancellationRequested'])

    def test_skip_all_unuploaded_files_finishes_without_starting_engine(self):
        identity = self.guard.create([{'id':'a','name':'never.bmp','size':10000}])['jobId']
        self.guard.cancel_file(identity,'a')
        self.guard.start(identity)
        state=wait_finished(self.guard,identity)
        self.assertEqual(state['state'],'cancelled')
        self.assertEqual(self.adapter.calls,[])
        self.assertEqual(self.guard.storage_stats()['reservedBytes'],0)

    def test_jobs_are_serialized_even_when_submitted_concurrently(self):
        first = self.prepare([('hold.docx',b'x')])
        second = self.prepare([('second.bmp',b'x')])
        self.guard.start(first)
        self.assertTrue(self.adapter.waiting.wait(2))
        self.guard.start(second)
        self.assertNotIn(('second.bmp','pdf'),self.adapter.calls)
        self.adapter.resume.set()
        wait_finished(self.guard,first)
        wait_finished(self.guard,second)
        self.assertEqual(self.adapter.maximum_active,1)

    def test_retriable_truncated_upload_is_not_a_permanent_file_failure(self):
        identity = self.guard.create([{'id':'f','name':'a.bmp','size':4}])['jobId']
        with self.assertRaises(GuardError): self.guard.upload(identity,'f',io.BytesIO(b'x'),4)
        self.guard.upload(identity,'f',io.BytesIO(b'good'),4)
        self.guard.start(identity)
        self.assertEqual(wait_finished(self.guard,identity)['state'],'ready')

    def test_cleanup_failure_keeps_quota_until_next_prune(self):
        identity = self.prepare([('good.bmp',b'xxxx')])
        with patch('app_backend.converter_guard.core.shutil.rmtree',side_effect=PermissionError()):
            self.guard.release(identity)
        self.assertEqual(self.guard.storage_stats()['jobs'],1)
        self.assertEqual(self.guard.storage_stats()['reservedBytes'],4)
        self.guard._prune()
        self.assertEqual(self.guard.storage_stats()['jobs'],0)

    def test_bad_output_path_cannot_ticket_a_source_or_private_file(self):
        self.adapter.convert = lambda source,*args,**kwargs:[source]
        state = self.run_job([('good.bmp',b'x')])
        self.assertEqual(state['state'],'failed')
        self.assertEqual(state['files'][0]['targets'][0]['code'],'ENGINE_OUTPUT_PATH')
        self.assertEqual(self.guard.storage_stats()['tickets'],0)

    def test_invalid_tickets_are_structured_errors(self):
        state = self.run_job([('good.bmp',b'x')])
        for value in ([],{},None,'../private'):
            with self.subTest(value=value), self.assertRaises(GuardError):
                self.guard.confirm_saved(state['jobId'],value)
            with self.subTest(value=value), self.assertRaises(GuardError):
                with self.guard.open_result(value): pass

    def test_trace_is_opt_in_bounded_and_reports_dropped_events(self):
        trace = GuardTrace()
        for _ in range(600): trace.add('example')
        self.assertEqual(len(trace.snapshot()['events']),512)
        self.assertEqual(trace.snapshot()['droppedEvents'],88)
        state = self.run_job([('good.bmp',b'x')])
        self.assertIsNone(state['trace'])
        self.assertIsNotNone(self.guard.status(state['jobId'],include_trace=True)['trace'])

    def test_close_is_idempotent_and_does_not_remove_unrelated_files(self):
        sentinel = Path(self.temp.name)/'unrelated'
        sentinel.write_text('keep')
        self.prepare([('good.bmp',b'x')])
        self.guard.close()
        self.guard.close()
        self.assertEqual(sentinel.read_text(),'keep')
        with self.assertRaises(GuardError):
            self.guard.create([{'id':'f','name':'a.bmp','size':0}])


class AdapterAndPolicyTests(unittest.TestCase):
    def test_invalid_limits(self):
        for kwargs in ({'max_files':0},{'max_file_bytes':True},{'file_timeout':float('inf')},
                       {'file_timeout':True},{'retention_seconds':None},{'upload_idle_timeout':-1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError): GuardLimits(**kwargs)

    def test_native_defaults_do_not_allocate_guard_watchers_or_filter_specs(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory)/'soffice'
            executable.touch()
            adapter = LibreOfficeAdapter(executable)
            cancel = threading.Event()
            with patch('app_backend.converter_guard.adapter.convert_path',return_value=[]) as run:
                adapter.convert(Path(directory)/'input.bmp','custom',Path(directory)/'work',
                    cancel=cancel, timeout=None, on_started=lambda:None)
            self.assertIs(run.call_args.kwargs['cancel'], cancel)
            self.assertNotIn('convert_spec',run.call_args.kwargs)
            self.assertEqual(run.call_args.args[1],'custom')

    def test_configured_timeout_does_not_cancel_parent_or_other_files(self):
        cancel = threading.Event()
        control = _RunControl(cancel,Path('.'),.001,None)
        with patch('app_backend.converter_guard.adapter.time.monotonic',return_value=control.deadline+1):
            with self.assertRaises(GuardError) as error: control.is_set()
        self.assertEqual(error.exception.code,'ENGINE_TIMEOUT')
        self.assertFalse(cancel.is_set())

    def test_explicit_quota_checks_only_native_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'output').mkdir()
            (root/'output'/'large.pdf').write_bytes(b'x'*50)
            control=_RunControl(threading.Event(),root,None,lambda:10)
            with self.assertRaises(GuardError) as error: control.is_set()
            self.assertEqual(error.exception.code,'SPOOL_QUOTA')

    def test_missing_engine_has_explicit_launch_error(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter=LibreOfficeAdapter(Path(directory)/'missing')
            with self.assertRaises(GuardError) as error:
                adapter.convert(Path('a.bmp'),'pdf',Path(directory)/'work',
                    cancel=threading.Event(),timeout=None,on_started=lambda:None)
            self.assertEqual(error.exception.code,'ENGINE_NOT_FOUND')
            self.assertFalse((Path(directory)/'work').exists())

    def test_importing_guard_does_not_load_ui_or_start_background_threads(self):
        code = ('import threading,sys; before={t.ident for t in threading.enumerate()};'
                'from app_backend.converter_guard import ConverterGuard;'
                'g=ConverterGuard(); g.capabilities([dict(id="f",name="a.bmp",size=1)]);'
                'assert {t.ident for t in threading.enumerate()} == before;'
                'assert not any(k.startswith(("PySide6","PyQt","app_backend.desktop",'
                '"app_backend.server","app_backend.excel")) for k in sys.modules);'
                'g.close()')
        result=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_current_application_never_activates_guard_implicitly(self):
        code = ('import sys;from app_backend.server import start_server;'
                's,_=start_server();'
                'assert not any(k.startswith("app_backend.converter_guard") for k in sys.modules);'
                's.server_close()')
        result=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)


if __name__ == '__main__':
    unittest.main()
