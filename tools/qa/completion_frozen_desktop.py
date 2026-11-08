"""Drive the ordinary desktop through Qt's loopback DevTools, without QA product hooks.

The frozen mode must use its bundled LibreOffice, not the runner's external engine.
This proves portable GUI startup/conversion/close, not installer or file-dialog QA.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


class DevTools:
    def __init__(self, app, url):
        from PySide6.QtCore import QUrl
        from PySide6.QtWebSockets import QWebSocket
        self.app, self.counter, self.responses = app, 0, {}
        self.socket = QWebSocket()
        self.socket.textMessageReceived.connect(self._receive)
        ready = []
        self.socket.connected.connect(lambda: ready.append(True))
        self.socket.open(QUrl(url))
        self.pump_until(lambda: ready, 15, 'DevTools connection')

    def _receive(self, message):
        value = json.loads(message)
        if 'id' in value:
            self.responses[value['id']] = value

    def pump_until(self, test, seconds, label):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.app.processEvents()
            value = test()
            if value:
                return value
            time.sleep(.01)
        raise AssertionError(label + ' timed out')

    def command(self, method, params=None, seconds=30):
        self.counter += 1
        identity = self.counter
        self.socket.sendTextMessage(json.dumps({'id': identity, 'method': method, 'params': params or {}}))
        response = self.pump_until(lambda: self.responses.pop(identity, None), seconds, method)
        if 'error' in response:
            raise AssertionError(response['error'])
        return response.get('result', {})

    def evaluate(self, expression):
        answer = self.command('Runtime.evaluate', {'expression': expression, 'returnByValue': True,
                                                  'awaitPromise': True})
        if 'exceptionDetails' in answer:
            raise AssertionError(answer['exceptionDetails'])
        return answer.get('result', {}).get('value')

    def wait(self, expression, seconds=120):
        return self.pump_until(lambda: self.evaluate(expression), seconds, expression[:180])


def _close_windows(pid):
    windows = []
    callback = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
    user32 = ctypes.windll.user32
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]

    def visit(handle, _):
        actual = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(handle, ctypes.byref(actual))
        if actual.value == pid and ctypes.windll.user32.IsWindowVisible(handle):
            windows.append(handle)
        return True

    ctypes.windll.user32.EnumWindows(callback(visit), 0)
    if not windows:
        raise AssertionError('Ordinary desktop top-level window not found')
    for handle in windows:
        if not ctypes.windll.user32.PostMessageW(ctypes.c_void_p(handle), 0x0010, 0, 0):
            raise AssertionError('WM_CLOSE delivery failed')


def execute(executable, output, *, source=False, engine=None):
    from PySide6.QtCore import QCoreApplication
    from app_backend.native_process import ProcessTree
    from pypdf import PdfReader

    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    # Resolving a venv Python symlink loses its pyvenv.cfg and dependencies.
    executable = Path(executable).absolute()
    if not executable.is_file():
        raise ValueError('Executable missing')
    app = QCoreApplication.instance() or QCoreApplication([])
    evidence = {'schema': 1, 'status': 'INCOMPLETE', 'frozenVerified': not source,
                'scope': 'ordinary portable desktop + real native conversion + independent PDF marker',
                'installer': 'NOT_RUN', 'fileDialog': 'NOT_RUN', 'physicalMonitorDPI': 'NOT_RUN',
                'externalDisks': 'NOT_RUN', 'gracefulClose': 'NOT_RUN'}
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    environment = dict(os.environ)
    environment['QTWEBENGINE_REMOTE_DEBUGGING'] = '127.0.0.1:' + str(port)
    environment['AIO_GUARD_DATA_DIR'] = str(output / 'guard-data')
    for name in ('PYTHONPATH', 'PYTHONHOME'):
        environment.pop(name, None)
    if source:
        if engine is None:
            raise ValueError('Source control needs an explicit engine')
        environment['AIO_LIBREOFFICE'] = str(Path(engine).resolve(strict=True))
        command = [str(executable), str(ROOT / 'launcher.py')]
    else:
        for name in ('AIO_LIBREOFFICE', 'AIO_LIBREOFFICE_ROOT', 'AIO_LIBREOFFICE_REGISTRY', 'AIO_QA_ENGINE'):
            environment.pop(name, None)
        bundled = executable.parent / '_internal/libreoffice/program/soffice.com'
        if not bundled.is_file():
            raise AssertionError('Frozen QA requires its own complete bundled LibreOffice')
        command = [str(executable)]
        if os.name == 'nt':
            environment['QT_QPA_PLATFORM'] = 'windows'
        evidence['engineSelection'] = 'bundled engine; external overrides removed'
    marker = 'AIO_FROZEN_NATIVE_20261109'
    control = ('{\\rtf1\\ansi\\deff0 {\\fonttbl {\\f0 Arial;}}\\f0\\fs24 ' + marker + ' desktop control.}').encode('ascii')
    source_path = output / 'desktop-control.rtf'
    source_path.write_bytes(control)
    before = hashlib.sha256(control).hexdigest()
    devtools = None
    try:
        with (output / 'desktop.log').open('wb') as log:
            with ProcessTree(command, cwd=output, env=environment, stdout=log, stderr=log) as tree:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                deadline = time.monotonic() + 90
                page = None
                while time.monotonic() < deadline and tree.process.poll() is None:
                    try:
                        with opener.open(f'http://127.0.0.1:{port}/json', timeout=1) as response:
                            pages = json.load(response)
                        page = next((p for p in pages if p.get('type') == 'page'
                                     and p.get('url', '').startswith('http://127.0.0.1:')), None)
                        if page:
                            break
                    except (OSError, ValueError):
                        pass
                    time.sleep(.1)
                if page is None:
                    raise AssertionError('Desktop did not expose its ordinary loopback application page')
                devtools = DevTools(app, page['webSocketDebuggerUrl'])
                devtools.wait('!!document.querySelector(".nav-item")')
                if not devtools.evaluate('window.AllInOneNativeShell===true'):
                    raise AssertionError('This is not the native desktop page')
                devtools.evaluate('''[...document.querySelectorAll('.nav-item')].find(e=>e.textContent.includes('Конвертер')).click()''')
                devtools.wait('!!document.querySelector("[data-cv-picker]")&&!document.querySelector("[data-cv-add]").disabled')
                payload = json.dumps(base64.b64encode(control).decode('ascii'))
                devtools.evaluate('''(()=>{const d=new DataTransfer();d.items.add(new File([Uint8Array.from(atob(PAYLOAD),x=>x.charCodeAt(0))],'desktop-control.rtf'));const p=document.querySelector('[data-cv-picker]');p.files=d.files;p.dispatchEvent(new Event('change',{bubbles:true}));return true;})()'''.replace('PAYLOAD', payload))
                devtools.wait('document.querySelectorAll(".aio-cv__file[data-file-id]").length===1&&!document.querySelector("[data-cv-add]").disabled', 180)
                uploaded = list((output / 'guard-data').rglob('*.rtf'))
                if len(uploaded) != 1 or hashlib.sha256(uploaded[0].read_bytes()).hexdigest() != before:
                    raise AssertionError('Actual uploaded source differs before conversion')
                evidence['uploadedSource'] = {'sha256': before, 'verifiedBeforeConversion': True,
                    'afterConversion': 'NOT_CHECKED; ordinary mode removes staged sources after conversion'}
                devtools.evaluate('''(()=>{const c=[...document.querySelectorAll('.aio-cv__category')].find(e=>e.textContent==='Документы');if(c)c.click();document.querySelector('[aria-label="Выбрать PDF"]').click();return true;})()''')
                devtools.wait('!document.querySelector("[data-cv-run]").disabled')
                devtools.evaluate('document.querySelector("[data-cv-run]").click()')
                devtools.wait('!document.querySelector("[data-cv-save]").hidden&&!document.querySelector("[data-cv-save]").disabled', 180)
                found = []
                for path in (output / 'guard-data').rglob('*.pdf'):
                    with path.open('rb') as stream:
                        pdf = PdfReader(stream)
                        text = '\n'.join(p.extract_text() or '' for p in pdf.pages)
                        if marker in text:
                            found.append({'bytes': path.stat().st_size, 'pages': len(pdf.pages),
                                          'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
                if not found:
                    raise AssertionError('GUI did not produce an independently readable PDF with the source marker')
                shot = devtools.command('Page.captureScreenshot', {'format': 'png', 'captureBeyondViewport': False})
                (output / 'desktop-ready.png').write_bytes(base64.b64decode(shot['data']))
                evidence.update(nativeConversion='PASS', desktopGUI='PASS', nativeShell=True,
                                markerVerified=True, generatedPDFs=found,
                                controlFixtureUnchanged=hashlib.sha256(source_path.read_bytes()).hexdigest() == before)
                if not evidence['controlFixtureUnchanged']:
                    raise AssertionError('Source control changed')
                devtools.socket.close()
                devtools = None
                if os.name == 'nt':
                    _close_windows(tree.process.pid)
                    code = tree.process.wait(timeout=30)
                    if code != 0:
                        raise AssertionError('Desktop graceful close failed: ' + str(code))
                    until = time.monotonic() + 30
                    while tree._windows_job_active():
                        if time.monotonic() >= until:
                            raise AssertionError('Owned desktop descendants survived normal close')
                        time.sleep(.05)
                    evidence['gracefulClose'] = 'PASS'
                    evidence['ownedDescendantsExitedBeforeForcedCleanup'] = True
                else:
                    evidence['gracefulClose'] = 'NOT_RUN; source offscreen process ended by owned tree'
                evidence['status'] = 'PASS'
    except Exception as error:
        evidence.update(status='FAIL', error=type(error).__name__ + ': ' + str(error))
        raise
    finally:
        if devtools is not None:
            devtools.socket.close()
        (output / 'evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
        print('AIO_COMPLETION_FROZEN_DESKTOP=' + json.dumps(evidence, ensure_ascii=True), flush=True)
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--source', action='store_true')
    parser.add_argument('--engine')
    args = parser.parse_args()
    execute(args.executable, args.output, source=args.source, engine=args.engine)


if __name__ == '__main__':
    main()
