"""Bounded State-dialog rendering acceptance in the real Qt WebEngine shell.

Only the conversion engine is controlled: an owned 1,000-task Guard queue is
held during checks. The actual server, page, CSS, dialog and mouse/keyboard
events are used. No user files, file pickers, installs or external sites are
accessed. Screenshots stay under --output as internal QA evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


class RenderFailure(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise RenderFailure(message)


def validate_geometry(value, expected_rows=50):
    """Validate browser-measured bounds; long text may ellipsize internally."""
    view, popup, listing = value['viewport'], value['popup'], value['list']
    require(view['width'] > 0 and view['height'] > 0, 'Empty browser viewport')
    require(popup['left'] >= -1 and popup['top'] >= -1 and
            popup['right'] <= view['width'] + 1 and popup['bottom'] <= view['height'] + 1,
            'State dialog escapes the rendered viewport')
    require(popup['scrollWidth'] <= popup['clientWidth'] + 1,
            'State dialog has horizontal overflow')
    require(listing['scrollWidth'] <= listing['clientWidth'] + 1,
            'Task list has horizontal overflow')
    require(listing['scrollHeight'] > listing['clientHeight'] > 0,
            'Long task list does not have a bounded scroll area')
    require(value['rows'] == expected_rows, 'Task list is not paginated to 50 DOM rows')
    require('1000' in value['page'], 'Queue total is not visible')


class HeldAdapter:
    available = True

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.cancelled = threading.Event()

    def capabilities(self, files, **_kwargs):
        return {'files': files, 'formats': []}

    def convert(self, source, target, workspace, *, cancel, on_started, **_kwargs):
        from app_backend.converter_guard import GuardError
        on_started()
        self.entered.set()
        while not self.release.wait(.01):
            if cancel.is_set():
                self.cancelled.set()
                raise GuardError('CANCELLED', 'QA teardown cancelled the controlled task.', stage='engine')
        if cancel.is_set():
            self.cancelled.set()
            raise GuardError('CANCELLED', 'QA teardown cancelled the controlled task.', stage='engine')
        output = workspace / 'output'
        output.mkdir(parents=True)
        path = output / (source.stem + '.' + target)
        path.write_bytes(b'%PDF-1.4\ncontrolled-render-QA\n%%EOF\n')
        return [path]


def _dispose_window(app, window):
    """Destroy the page before its profile while the Qt dispatcher is alive."""
    from PySide6.QtCore import QCoreApplication, QEvent
    from shiboken6 import isValid
    profile, page = window.profile, window.page
    # MainWindow intentionally parents the profile to its page. Retain it
    # explicitly during QA teardown so the profile outlives page destruction.
    profile.setParent(None)
    window.view.stop()
    window._shutdown_done = True  # The owned server is already closed below.
    window.close()
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    require(not isValid(window) and not isValid(page),
            'Owned Qt window/page did not finish deferred destruction')
    profile.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    require(not isValid(profile), 'Owned Qt profile did not finish deferred destruction')
    return {'windowDestroyed': True, 'pageDestroyed': True, 'profileDestroyed': True}


def _run_in_event_loop(app, callback):
    """Run checks inside the real application loop, then quit normally."""
    from PySide6.QtCore import QTimer
    values, errors = [], []

    def perform():
        try:
            values.append(callback())
        except BaseException as error:
            errors.append(error)
        finally:
            # Give WebEngine's destruction messages one ordinary event-loop
            # turn before normal QApplication shutdown; no forced process exit.
            QTimer.singleShot(50, app.quit)

    QTimer.singleShot(0, perform)
    code = app.exec()
    require(code == 0, 'Qt application event loop returned ' + str(code))
    if errors:
        raise errors[0]
    require(len(values) == 1, 'Qt application exited before render checks completed')
    return values[0]


def run_render(output, seconds):
    # Load WebEngine before constructing QApplication, as in the real shell.
    from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: F401
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(['aio-state-render-qa'])
    app.setApplicationName('AIO-State-Render-QA')
    app.setQuitOnLastWindowClosed(False)
    return _run_in_event_loop(app, lambda: _run_render_checks(output, seconds, app))


def _run_render_checks(output, seconds, app):
    # Imports occur after per-process QA paths are redirected by main(). A
    # missing system library produces INCOMPLETE, never a simulated PASS.
    from PySide6.QtCore import QPoint, QEventLoop, Qt, qVersion
    from PySide6.QtTest import QTest
    from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: F401
    from app_backend.desktop import MainWindow
    from app_backend.server import start_server

    deadline = time.monotonic() + seconds
    server = window = job = None
    adapter = HeldAdapter()
    facts = {'schema': 1, 'status': 'FAIL', 'platform': platform.system(), 'qtVersion': qVersion(),
             'scope': 'actual MainWindow/QtWebEngine/server; controlled engine only',
             'controlledTasks': 1000, 'themes': [], 'nativeEvents': True,
             'queueCancelledByDialog': None, 'screenshots': [], 'mainEventLoop': True}

    def pump():
        require(time.monotonic() < deadline, 'Global render acceptance deadline exceeded')
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
        time.sleep(.005)

    def until(predicate, message, timeout=15):
        end = min(deadline, time.monotonic() + timeout)
        while time.monotonic() < end:
            if predicate():
                return
            pump()
        raise RenderFailure(message)

    def js(body):
        received = []
        expression = '(function(){try{return JSON.stringify({ok:true,value:(function(){' + body + \
                     '})()});}catch(e){return JSON.stringify({ok:false,error:String(e)});}})()'
        window.page.runJavaScript(expression, lambda result: received.append(result))
        until(lambda: bool(received), 'JavaScript callback did not return', timeout=10)
        require(isinstance(received[0], str), 'Qt returned no JavaScript evidence')
        result = json.loads(received[0])
        require(result.get('ok') is True, result.get('error', 'Browser JavaScript failed'))
        return result.get('value')

    def native_click(selector):
        point = js('const n=document.querySelector(' + json.dumps(selector) + ');' +
                   'if(!n)throw Error("Missing click target");n.scrollIntoView({block:"center"});' +
                   'const r=n.getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2};')
        target = window.view.focusProxy() or window.view
        QTest.mouseClick(target, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         QPoint(round(point['x']), round(point['y'])))
        pump()

    def modal_open():
        return js('return !!document.querySelector(".aio-guard-popup")?.open;')

    def live_queue(label):
        state = server.guard.status(job)
        require(not state['finished'] and state['cancelled'] == 0 and
                not server.guard._jobs[job].cancel.is_set() and not adapter.cancelled.is_set(),
                label + ' cancelled or completed the held conversion queue')

    def open_state():
        native_click('.aio-guard-state-button')
        until(modal_open, 'State dialog did not open')

    def closed(label):
        until(lambda: not modal_open(), label + ' did not close State')
        require(js('return document.activeElement===document.querySelector(".aio-guard-state-button");'),
                label + ' did not restore focus to State')
        live_queue(label)

    def geometry():
        return js('''
          const d=document.querySelector('.aio-guard-popup'),l=d.querySelector('.aio-guard-list');
          const r=d.getBoundingClientRect(),c=getComputedStyle(d);
          return {viewport:{width:innerWidth,height:innerHeight},
            popup:{left:r.left,top:r.top,right:r.right,bottom:r.bottom,
              width:r.width,height:r.height,scrollWidth:d.scrollWidth,clientWidth:d.clientWidth},
            list:{scrollWidth:l.scrollWidth,clientWidth:l.clientWidth,
              scrollHeight:l.scrollHeight,clientHeight:l.clientHeight},
            rows:d.querySelectorAll('.aio-guard-task').length,
            page:d.querySelector('[data-g-page]').textContent,
            background:c.backgroundColor,color:c.color,theme:document.documentElement.dataset.theme};
        ''')

    try:
        server, _token = start_server(guard_recovery_parent=output / 'guard-jobs')
        server.guard.adapter = adapter
        threading.Thread(target=server.serve_forever, daemon=True, name='aio-render-qa-http').start()
        rows = [{'id': str(i), 'name': 'file-' + str(i).zfill(4) + '-' + 'long-name-' * 8 + '.docx',
                 'size': 1} for i in range(1000)]
        job = server.guard.create(rows, ['pdf'])['jobId']
        for row in rows:
            server.guard.upload(job, row['id'], io.BytesIO(b'x'), 1)
        server.guard.start(job, archive_mode='on_demand')
        until(adapter.entered.is_set, 'Controlled Guard conversion did not start')
        window = MainWindow(server, server.server_port)
        window.profile.setPersistentStoragePath(str(output / 'web-profile'))
        window.profile.setCachePath(str(output / 'web-cache'))
        window.resize(1280, 800)
        loaded = []
        window.view.loadFinished.connect(lambda successful: loaded.append(successful))
        window.show()
        until(lambda: bool(loaded), 'Production application page did not finish loading', timeout=25)
        require(loaded[-1], 'Production application page load failed')
        until(lambda: js('return !!window.AllInOneBridge?.converter && !!document.querySelector(".nav-item");'),
              'Production application scripts did not load', timeout=25)
        js('const n=[...document.querySelectorAll(".nav-item")].find(n=>n.textContent.includes("Конвертер"));'
           'if(!n)throw Error("Converter navigation missing");n.click();return true;')
        until(lambda: js('return !!document.querySelector(".aio-guard-state-button");'),
              'Production converter and queue did not mount')
        status = server.guard.status(job)
        js('window.dispatchEvent(new CustomEvent("aio-guard-state",{detail:' +
           json.dumps(status, ensure_ascii=True) + '}));return true;')
        until(lambda: js('return document.querySelector(".aio-guard-state-button")?.disabled===false;'),
              'Real Guard state did not enable State')

        for theme in ('light', 'dark'):
            if js('return document.documentElement.dataset.theme;') != theme:
                native_click('#theme-toggle')
            require(js('return document.documentElement.dataset.theme;') == theme, 'Theme switch failed')
            open_state()
            until(lambda: js('return document.querySelectorAll(".aio-guard-task").length===50;'),
                  'Long queue did not render its first page')
            measured = geometry()
            validate_geometry(measured)
            target = window.view.focusProxy() or window.view
            padding = QPoint(round(measured['popup']['left'] + 6), round(measured['popup']['top'] + 6))
            outside = QPoint(2, 2)
            QTest.mouseClick(target, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, padding)
            pump()
            require(modal_open(), 'Clicking internal State padding dismissed it')
            QTest.mousePress(target, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, padding)
            QTest.mouseMove(target, outside)
            QTest.mouseRelease(target, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, outside)
            pump()
            require(modal_open(), 'Dragging from internal padding to backdrop dismissed State')
            native_click('[data-g-next]')
            until(lambda: js('return document.querySelector(".aio-guard-task strong")?.textContent;') ==
                  rows[50]['name'] + ' → PDF',
                  'Real browser pagination did not advance to the next 50 tasks')
            native_click('[data-g-prev]')
            until(lambda: js('return document.querySelector(".aio-guard-task strong")?.textContent;') ==
                  rows[0]['name'] + ' → PDF',
                  'Real browser pagination did not return to the first 50 tasks')
            js('document.querySelector(".aio-guard-popup").scrollTop=0;return true;')
            paint_deadline = min(deadline, time.monotonic() + .2)
            while time.monotonic() < paint_deadline:
                pump()
            screenshot = output / ('state-' + theme + '.png')
            pixmap = window.view.grab()
            require(not pixmap.isNull() and pixmap.save(str(screenshot)), 'Rendered Qt screenshot is empty')
            image = pixmap.toImage()
            samples = {image.pixelColor(x, y).rgba() for x in range(0, image.width(), 37)
                       for y in range(0, image.height(), 37)}
            require(len(samples) >= 4, 'Qt screenshot contains no rendered application content')
            facts['screenshots'].append({'path': screenshot.name, 'width': image.width(),
                'height': image.height(), 'sampledColors': len(samples),
                'sha256': hashlib.sha256(screenshot.read_bytes()).hexdigest()})
            native_click('[data-g-close]')
            closed('Close button')
            open_state()
            QTest.keyClick(window.view.focusProxy() or window.view, Qt.Key.Key_Escape)
            pump()
            closed('Escape')
            open_state()
            QTest.mouseClick(window.view.focusProxy() or window.view, Qt.MouseButton.LeftButton,
                             Qt.KeyboardModifier.NoModifier, outside)
            pump()
            closed('Backdrop click')
            measured['checks'] = {'paddingKeepsOpen': True, 'dragKeepsOpen': True,
                                  'closeButton': True, 'escape': True, 'backdrop': True,
                                  'focusRestored': True, 'queueStillRunning': True, 'pagination': True}
            facts['themes'].append(measured)
            print('AIO_QT_RENDER_THEME=' + json.dumps({'theme': theme, 'status': 'PASS',
                  'geometry': measured}, ensure_ascii=True), flush=True)
        require(facts['themes'][0]['background'] != facts['themes'][1]['background'],
                'Light and dark dialog backgrounds did not change')
        facts['queueCancelledByDialog'] = False
        facts['status'] = 'PASS'
        return facts
    finally:
        if server is not None:
            if job is not None:
                server.guard.cancel(job)
            adapter.release.set()
            server.shutdown()
            server.server_close()
        if window is not None:
            facts['teardown'] = _dispose_window(app, window)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--deadline', type=int, default=120)
    parser.add_argument('--offscreen', action='store_true')
    args = parser.parse_args(argv)
    if not 15 <= args.deadline <= 600:
        parser.error('--deadline must be between 15 and 600 seconds')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    # Every profile/cache/temp/recovery path belongs to this QA invocation.
    # No system defaults or user profile settings are modified.
    for key, name in (('AIO_GUARD_DATA_DIR', 'guard-data'), ('LOCALAPPDATA', 'local-data'),
                      ('APPDATA', 'roaming-data'), ('XDG_CACHE_HOME', 'cache'),
                      ('XDG_DATA_HOME', 'data'), ('TEMP', 'temp'), ('TMP', 'temp')):
        path = output / name
        path.mkdir(exist_ok=True)
        os.environ[key] = str(path)
    if args.offscreen:
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        os.environ.setdefault('QT_OPENGL', 'software')
        os.environ.setdefault('QTWEBENGINE_CHROMIUM_FLAGS', '--disable-gpu')
    if os.name == 'nt':
        os.environ.pop('QTWEBENGINE_DISABLE_SANDBOX', None)
        require('--no-sandbox' not in os.environ.get('QTWEBENGINE_CHROMIUM_FLAGS', ''),
                'Windows QA must keep the Chromium sandbox enabled')
    # A cloud root invocation may explicitly set QTWEBENGINE_DISABLE_SANDBOX.
    # This tool never disables sandboxing itself, particularly on Windows.
    started = time.monotonic()
    try:
        result = run_render(output, args.deadline)
    except ImportError as error:
        result = {'schema': 1, 'status': 'INCOMPLETE', 'platform': platform.system(),
                  'reason': type(error).__name__ + ': ' + str(error),
                  'scope': 'actual QtWebEngine import; no simulated render acceptance'}
    except Exception as error:
        result = {'schema': 1, 'status': 'FAIL', 'platform': platform.system(),
                  'reason': type(error).__name__ + ': ' + str(error),
                  'scope': 'actual MainWindow/QtWebEngine/server; controlled engine only'}
    result['durationMs'] = round((time.monotonic() - started) * 1000)
    (output / 'qt-render-evidence.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n',
                                                   encoding='utf-8')
    print('AIO_QT_RENDER_EVIDENCE=' + json.dumps(result, ensure_ascii=True), flush=True)
    return 0 if result['status'] == 'PASS' else 2 if result['status'] == 'INCOMPLETE' else 1


if __name__ == '__main__':
    raise SystemExit(main())
