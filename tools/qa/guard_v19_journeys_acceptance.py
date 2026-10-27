"""Actual Qt/HTTP recovery, second-output preview and save/discard journeys.

Native conversion uses synthetic fixtures, explicitly. The comparison worker,
QtWebEngine page, folder copy/readback, leases and HTTP bridge are real. Only
the destination picker is supplied a new synthetic-test folder; no packages,
application, user files or global office settings are changed.
"""
import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import threading
import time
import traceback
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'tests'))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--files', type=int, default=24)
    parser.add_argument('--discard-cycles', type=int, default=1,
                        help='Repeat the real close/discard journey, without resetting the Qt page')
    parser.add_argument('--rapid-reopen-cycles', type=int, default=5,
                        help='Exercise queued native close events followed by another open in the same JS turn')
    parser.add_argument('--emit-snapshots', action='store_true')
    args = parser.parse_args(argv)
    if not 12 <= args.files <= 100:
        parser.error('--files must be between 12 and 100')
    if not 1 <= args.discard_cycles <= 10:
        parser.error('--discard-cycles must be between 1 and 10')
    if not 1 <= args.rapid_reopen_cycles <= 10:
        parser.error('--rapid-reopen-cycles must be between 1 and 10')
    output = args.output.resolve()
    if output == ROOT or output.is_relative_to(ROOT):
        parser.error('Evidence must remain outside the source checkout')
    output.mkdir(parents=True, exist_ok=False)
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QFileDialog
    from app_backend.server import start_server
    from app_backend.desktop import MainWindow
    from app_backend.converter_guard import ConverterGuard, GuardError
    from app_backend.converter_guard.http_api import GuardAPI
    from test_comparison_journeys_v19 import JourneyAdapter, workbook_bytes, saved_payload_hashes
    from test_converter_guard_v02 import wait_finished

    app = QApplication.instance() or QApplication([])
    evidence = {'schema': 1, 'status': 'FAIL', 'platform': sys.platform,
        'sourceSha': os.environ.get('AIO_QA_SOURCE_SHA', 'local'),
        'scope': 'actual QtWebEngine + production bridge/native folder save + real isolated comparison workers; native conversion fixture controlled',
        'picker': 'new synthetic QA folder supplied; all file copies and independent readback are production code',
        'fullFormatMatrix': 'NOT_RUN', 'packagedEXE': 'NOT_RUN', 'manualVisibleGuiAcceptance': 'NOT_RUN',
        'journeys': [], 'snapshots': []}
    server = window = None
    thread = timer = None
    ticks = []
    api_events = []
    current_job = None
    try:
        recovery = output / 'owned'
        server, token = start_server(guard_recovery_parent=output / 'unused-host-initial')
        server.guard_api.close(); server.guard.close()
        server.guard = ConverterGuard(adapter=JourneyAdapter(), recovery_parent=recovery)
        origin = 'http://127.0.0.1:' + str(server.server_port)
        server.guard_api = GuardAPI(server.guard, origin=origin, token=token)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        thread.start()

        def create(count=1, *, workbook=False):
            raw = workbook_bytes() if workbook else b'QA original content retained for a real isolated analysis.\n'
            suffix = '.xlsx' if workbook else '.txt'
            rows = [{'id': str(i), 'name': 'journey-' + str(i) + suffix, 'size': len(raw)} for i in range(count)]
            identity = server.guard.create(rows, ['csv'] if workbook else ['txt'], retain_sources=True)['jobId']
            for row in rows:
                server.guard.upload(identity, row['id'], io.BytesIO(raw), len(raw))
            server.guard.start(identity, archive_mode='on_demand')
            state = wait_finished(server.guard, identity, seconds=30)
            assert state['succeeded'] == count and state['failed'] == 0, state
            return state

        # A real former process session creates this package and disk report.
        recovered_job = create(workbook=True)['jobId']
        server.guard_api.comparison.start(recovered_job)
        deadline = time.monotonic() + 30
        while server.guard_api.comparison.snapshot(recovered_job)['state'] not in ('completed', 'failed'):
            if time.monotonic() > deadline:
                raise TimeoutError('Initial report preparation deadline')
            time.sleep(.03)
        prior = server.guard_api.comparison.snapshot(recovered_job)
        assert prior['state'] == 'completed', prior
        server.guard_api.close(); server.guard.close()
        server.guard = ConverterGuard(adapter=JourneyAdapter(), recovery_parent=recovery)
        server.guard_api = GuardAPI(server.guard, origin=origin, token=token)
        # Transparent HTTP observation: call the actual API unchanged and
        # record only bounded synthetic-job metadata, never request bodies.
        original_post, original_json = server.guard_api.post, server.guard_api._json
        def observed_post(handler, path):
            if '/compare' in path or path.endswith('/release'):
                api_events.append({'phase': 'request', 'path': path, 'at': time.monotonic()})
            return original_post(handler, path)
        def observed_json(handler, status, body):
            if '/compare' in handler.path or '/release' in handler.path:
                api_events.append({'phase': 'response', 'path': handler.path.split('?', 1)[0],
                    'status': status, 'code': body.get('code'), 'jobId': body.get('jobId'),
                    'state': body.get('state'), 'processed': body.get('processed'), 'at': time.monotonic()})
            return original_json(handler, status, body)
        server.guard_api.post, server.guard_api._json = observed_post, observed_json
        window = MainWindow(server, server.server_port); window.resize(1280, 800); window.show()
        timer = QTimer(); timer.timeout.connect(lambda: ticks.append(time.monotonic())); timer.start(10)

        def javascript(code):
            returned = []
            window.page.runJavaScript(code, lambda value: returned.append(value))
            end = time.monotonic() + 8
            while not returned and time.monotonic() < end:
                app.processEvents(); time.sleep(.005)
            if not returned:
                raise TimeoutError('Qt JavaScript callback did not arrive')
            return returned[0]

        def until(predicate, seconds=40):
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                app.processEvents()
                if predicate():
                    return
                time.sleep(.015)
            raise TimeoutError('Qt journey did not reach its required state')

        def open_comparison(identity, *, close_first=False):
            nonlocal current_job
            current_job = identity
            close_code = ("window.qaLifecycle.push({event:'close-call',open:document.querySelector('.aio-compare-dialog')?.open});window.AIOGuardComparison.close();"
                          if close_first else '')
            javascript("(()=>{" + close_code + "window.qaLifecycle.push({event:'open-call',job:" + json.dumps(identity) +
                ",open:document.querySelector('.aio-compare-dialog')?.open});const attempt={job:" + json.dumps(identity) +
                ",state:'pending'};window.qaOpen=attempt;window.AIOGuardComparison.open(" + json.dumps(identity) +
                ").then(()=>attempt.state='resolved').catch(error=>{attempt.state='rejected';attempt.message=error.message});return true})()")

        def close_comparison():
            javascript("window.qaLifecycle.push({event:'close-call',open:document.querySelector('.aio-compare-dialog')?.open});window.AIOGuardComparison.close();true")

        def snapshot(name):
            # WebEngine composition lags the DOM callback; wait for a real
            # painted frame rather than capturing the preceding closed view.
            settle = time.monotonic() + .3
            while time.monotonic() < settle:
                app.processEvents(); time.sleep(.01)
            path = output / (name + '.png')
            assert window.grab().save(str(path))
            evidence['snapshots'].append(path.name)
            if args.emit_snapshots:
                raw = path.read_bytes()
                if len(raw) > 768 * 1024:
                    raise AssertionError('Synthetic snapshot exceeds bounded log allowance')
                print('AIO_V19_JOURNEY_SNAPSHOT=' + json.dumps({'name': path.name,
                    'pngBase64': base64.b64encode(raw).decode('ascii')}), flush=True)

        until(lambda: javascript("typeof window.AIOGuardComparison==='object'&&!!window.AllInOneBridge?.converter?.saveRetained"))
        javascript("window.qaLifecycle=[];document.addEventListener('close',event=>{if(event.target.matches?.('.aio-compare-dialog'))window.qaLifecycle.push({event:'close-event',open:event.target.open,progress:document.querySelector('[data-c-progress]')?.textContent});},true);true")
        until(lambda: javascript("[...document.querySelectorAll('#tool-nav button')].some(button=>button.textContent.includes('Конвертер'))"))
        javascript("[...document.querySelectorAll('#tool-nav button')].find(button=>button.textContent.includes('Конвертер')).click();true")
        recovered_option = '[data-g-jobs] option[value="' + recovered_job + '"]'
        until(lambda: javascript('!!document.querySelector(' + json.dumps(recovered_option) + ')'))
        javascript("(()=>{const s=document.querySelector('[data-g-jobs]');s.value=" + json.dumps(recovered_job) + ";s.dispatchEvent(new Event('change',{bubbles:true}));return true})()")
        until(lambda: javascript("!!document.querySelector('[data-g-compare]')&&!document.querySelector('[data-g-compare]').disabled"))
        javascript("document.querySelector('[data-g-compare]').click();true")
        until(lambda: javascript("document.querySelector('.aio-compare-dialog')?.open&&document.querySelector('[data-c-progress]')?.textContent.includes('Анализ завершён')"))
        restored = server.guard_api.comparison.snapshot(recovered_job)
        assert restored['analysisId'] == prior['analysisId'] and restored['recovered'], restored
        assert not server.guard.adapter.calls, 'Recovered report UI must not repeat native exports'
        javascript("document.querySelector('[data-c-filter-choice=all]')?.click();true")
        until(lambda:javascript("!!document.querySelector('[data-task]')"))
        javascript("document.querySelector('[data-task]').dispatchEvent(new MouseEvent('dblclick',{bubbles:true}));true")
        until(lambda: javascript("document.querySelector('[data-c-output]')?.options.length===2&&!document.querySelector('[data-c-output]').disabled"))
        javascript("(()=>{let s=document.querySelector('[data-c-output]');s.value='1';s.dispatchEvent(new Event('change',{bubbles:true}));return true})()")
        until(lambda: javascript("document.querySelector('[data-c-preview] pre')?.textContent.includes('SECOND_SHEET')"))
        snapshot('recovered-second-output')
        evidence['journeys'].append({'name': 'recovered_report_and_second_output', 'status': 'PASS',
            'sameAnalysisId': True, 'nativeExportsOnReopen': 0, 'secondSheetVisible': True})
        close_comparison()

        # Deliberately damage only this synthetic QA report. The source and
        # converted outputs remain untouched and can still be saved. Opening
        # must offer an explicit restart rather than silently starting work or
        # falsely treating the whole retained package as deleted.
        report_before = server.guard_api.comparison._get(recovered_job)
        original_analysis_id = report_before.identity
        with server.guard.comparison_inputs(recovered_job) as records:
            unchanged = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                         for record in records for path in [record['source'], *record['paths']]}
        Path(report_before.db).write_bytes(b'Controlled corrupt comparison report; synthetic QA only')
        open_comparison(recovered_job)
        until(lambda: javascript("document.querySelector('.aio-compare-dialog')?.open&&document.querySelector('[data-c-feedback]')?.textContent.includes('Отчёт')"))
        assert javascript("!document.querySelector('[data-c-restart]').hidden&&!document.querySelector('[data-c-restart]').disabled&&document.querySelector('[data-c-save-current]').disabled")
        assert not server.guard.adapter.calls
        # The damaged report stays damaged until the user's explicit action.
        assert Path(report_before.db).read_bytes().startswith(b'Controlled corrupt comparison report')
        cached = server.guard_api.comparison.jobs.get(recovered_job)
        assert cached is None or cached.identity == original_analysis_id, 'Opening a damaged report must not automatically create a replacement analysis'
        snapshot('corrupt-report-explicit-restart')
        javascript("document.querySelector('[data-c-restart]').click();true")
        until(lambda: javascript("document.querySelector('[data-c-progress]')?.textContent.includes('Анализ завершён')"))
        repaired_report = server.guard_api.comparison.snapshot(recovered_job)
        assert repaired_report['state'] == 'completed' and repaired_report['analysisId'] != original_analysis_id
        assert all(hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest for path, digest in unchanged.items())
        evidence['journeys'].append({'name': 'corrupt_report_requires_explicit_restart', 'status': 'PASS',
            'restartOffered': True, 'automaticRestart': False, 'newAnalysisId': True,
            'originalAndConvertedBytesUnchanged': True, 'nativeExports': 0})
        close_comparison()

        late_closes = 0
        for cycle in range(args.rapid_reopen_cycles):
            identity = create(4)['jobId']
            open_comparison(recovered_job)
            until(lambda: javascript("document.querySelector('[data-c-progress]')?.textContent.includes('Анализ завершён')"))
            before = javascript("window.qaLifecycle.filter(event=>event.event==='close-event'&&event.open===true).length")
            # One actual JavaScript turn, not a synthetic close event. Chromium
            # queues the old dialog's native close event after the new open.
            open_comparison(identity, close_first=True)
            target_prefix = json.dumps(identity + ':')
            until(lambda: javascript("document.querySelector('.aio-compare-dialog')?.open&&document.querySelector('[data-c-progress]')?.textContent.includes('Анализ завершён')&&document.querySelectorAll('[data-task]').length===4&&document.querySelector('[data-task]')?.dataset.task.startsWith(" + target_prefix + ")"))
            report = server.guard_api.comparison.snapshot(identity)
            assert report['state'] == 'completed' and report['processed'] == report['total'] == 4
            after = javascript("window.qaLifecycle.filter(event=>event.event==='close-event'&&event.open===true).length")
            assert after > before, 'This journey must observe a real late native close event after reopening'
            late_closes += after - before
            close_comparison()
            server.guard.release(identity, discard=True)
        evidence['journeys'].append({'name': 'native_dialog_close_reopen_preserves_new_session', 'status': 'PASS',
            'cycles': args.rapid_reopen_cycles, 'lateNativeCloseEventsObserved': late_closes,
            'newJobReportVisible': True, 'newAnalysisCancelledByOldClose': False})

        modes = [('save', 1), *[('discard', cycle) for cycle in range(1, args.discard_cycles + 1)]]
        for kind, cycle in modes:
            state = create(args.files); identity = state['jobId']
            current_job = identity
            job = server.guard._jobs[identity]
            expected = {artifact.path.name: hashlib.sha256(artifact.path.read_bytes()).hexdigest() for artifact in job.artifacts}
            calls_before = len(server.guard.adapter.calls)
            open_comparison(identity)

            def active_report():
                try:
                    report = server.guard_api.comparison.snapshot(identity)
                except GuardError as error:
                    if error.code == 'COMPARISON_NOT_STARTED':
                        return False
                    raise
                return report['state'] in ('running', 'paused') and 0 < report['processed'] < report['total']
            until(active_report)
            before_close = server.guard_api.comparison.snapshot(identity)
            assert job.io > 0 and before_close['processed'] < args.files
            close_comparison()
            assert not javascript("document.querySelector('.aio-compare-dialog').open")
            active = server.guard_api.comparison.snapshot(identity)
            assert active['state'] not in ('cancelled', 'failed'), active
            ticks_before = len(ticks)
            if kind == 'save':
                destination = output / 'saved'; destination.mkdir()
                with patch.object(QFileDialog, 'getExistingDirectory', return_value=str(destination)):
                    javascript('window.qaJourney=null;window.AllInOneBridge.converter.saveRetained(' + json.dumps(identity) + ",'folder').then(value=>window.qaJourney={ok:true,value}).catch(error=>window.qaJourney={ok:false,error:error.message});true")
                    until(lambda: javascript('window.qaJourney!==null'))
                result = json.loads(javascript('JSON.stringify(window.qaJourney)'))
                assert result['ok'] and result['value'].get('saved') is True, result
                actual = saved_payload_hashes(destination, identity, args.files)
                assert actual == expected, (actual, expected)
                assert job.root.exists(), 'Verified save retains its sources/results for later analysis'
                # The owner-requested lifecycle now keeps a saved session
                # until explicit Close. Save itself must not discard it.
                javascript('window.qaJourney=null;window.AllInOneBridge.converter.closeConversion('+json.dumps(identity)+').then(value=>window.qaJourney={ok:true,value}).catch(error=>window.qaJourney={ok:false,error:error.message});true')
                until(lambda:javascript('window.qaJourney!==null'))
                closed=json.loads(javascript('JSON.stringify(window.qaJourney)'))
                assert closed['ok'] and closed['value'].get('closed') is True,closed
            else:
                javascript('window.qaJourney=null;window.AllInOneBridge.converter.discardRetained(' + json.dumps(identity) + ').then(()=>window.qaJourney={ok:true}).catch(error=>window.qaJourney={ok:false,error:error.message});true')
                until(lambda: javascript('window.qaJourney!==null'))
                result = json.loads(javascript('JSON.stringify(window.qaJourney)'))
                assert result['ok'], result
            # The operation may cancel analysis or let its already leased worker
            # complete. Either way it must relinquish every job lease promptly.
            until(lambda: job.io == 0, seconds=60)
            until(lambda: not job.root.exists(), seconds=10)
            assert len(server.guard.adapter.calls) == calls_before, 'Analysis/save must not trigger native conversion'
            assert len(ticks) > ticks_before, 'Qt event loop must keep running during folder save/discard and worker cleanup'
            open_comparison(identity)
            until(lambda: javascript("document.querySelector('.aio-compare-dialog')?.open&&(document.querySelector('[data-c-progress]')?.textContent.includes('недоступен')||!!document.querySelector('[data-c-feedback]')?.textContent)"))
            unavailable_view = json.loads(javascript("JSON.stringify({open:document.querySelector('.aio-compare-dialog').open,feedback:document.querySelector('[data-c-feedback]').textContent,retryDisabled:document.querySelector('[data-c-retry]').disabled,resumeDisabled:document.querySelector('[data-c-resume]').disabled,restartDisabled:document.querySelector('[data-c-restart]').disabled})"))
            assert unavailable_view['open'] and unavailable_view['feedback']
            assert all(unavailable_view[key] for key in ('retryDisabled', 'resumeDisabled', 'restartDisabled'))
            snapshot('closed-analysis-' + kind + ('-' + str(cycle) if kind == 'discard' and args.discard_cycles > 1 else ''))
            close_comparison()
            evidence['journeys'].append({'name': 'close_comparison_then_' + kind + '_during_real_analysis',
                'status': 'PASS', 'cycle': cycle, 'processedBeforeClose': before_close['processed'], 'total': args.files,
                'liveWorkerObserved': True, 'releasedAllLeases': True, 'privateJobRemoved': True,
                'qtTimerTicks': len(ticks) - ticks_before,
                'savedByteHashesMatch': kind == 'save', 'nativeExportsDuringAnalysis': 0,
                'releasedJobView': unavailable_view})
        evidence['lifecycle'] = json.loads(javascript('JSON.stringify(window.qaLifecycle.slice(-50))'))
        evidence['apiEvents'] = api_events[-80:]
        evidence['status'] = 'PASS'
    except Exception as error:
        evidence.update(error=type(error).__name__, reason=str(error))
        traceback.print_exc()
        if window is not None:
            try:
                evidence['failureView'] = json.loads(javascript("JSON.stringify({open:document.querySelector('.aio-compare-dialog')?.open,openAttempt:window.qaOpen,lifecycle:window.qaLifecycle?.slice(-50),progress:document.querySelector('[data-c-progress]')?.textContent,feedback:document.querySelector('[data-c-feedback]')?.textContent,output:document.querySelector('[data-c-output]')?.value,outputs:[...document.querySelectorAll('[data-c-output] option')].map(x=>({value:x.value,label:x.textContent})),preview:document.querySelector('[data-c-preview]')?.textContent})"))
                evidence['apiEvents'] = api_events[-80:]
                evidence['serviceState'] = [{'jobId': item.job_id, 'state': item.state, 'processed': item.processed,
                    'total': item.total, 'analysisId': item.identity, 'finalizing': not server.guard_api.comparison._settled(item)}
                    for item in server.guard_api.comparison.jobs.values()]
                evidence['currentJob'] = current_job
                snapshot('failure')
            except Exception as diagnostic_error:
                evidence['failureViewError'] = type(diagnostic_error).__name__
    finally:
        if timer is not None:
            timer.stop()
        if window is not None:
            window._shutdown_done = True; window.hide(); window.page.deleteLater(); app.processEvents()
        if server is not None:
            server.guard_api.close(); server.guard.close(); server.shutdown(); server.server_close()
        if thread is not None:
            thread.join(3)
        (output / 'evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print('AIO_V19_JOURNEYS_ACCEPTANCE=' + json.dumps(evidence, ensure_ascii=True), flush=True)
    return 0 if evidence['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
