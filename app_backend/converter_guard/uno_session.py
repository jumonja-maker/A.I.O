"""Cancellable local stdio worker using the bundled office's own Python/UNO.

One process pair per batch, no TCP listener and no application dependencies.
Each document is closed after the operation; model reuse is deliberately not
enabled until cross-format equivalence is proved on the owner's Windows LO.
"""
import json
import os
from pathlib import Path
import queue
import secrets
import shutil
import subprocess
import threading
import time

from ..native_process import ProcessTree
from .errors import GuardError
from .native_logs import NativeLogs

WORKER_SOURCE = r'''
import json, os, sys, time, subprocess
from pathlib import Path

PREFIX = 'AIO_GUARD_RPC '
def send(value):
    print(PREFIX + json.dumps(value, ensure_ascii=True), flush=True)

def main(boot_name):
    boot = json.loads(Path(boot_name).read_text(encoding='utf-8'))
    until = time.monotonic() + 20
    while not Path(boot['start']).is_file():
        if time.monotonic() > until:
            raise RuntimeError('Job handshake unavailable')
        time.sleep(.02)
    import uno, unohelper
    from com.sun.star.task import XInteractionHandler
    class Abort(unohelper.Base, XInteractionHandler):
        blocked = False
        def handle(self, request):
            self.blocked = True
            for item in request.getContinuations():
                abort = item.queryInterface(uno.getTypeByName('com.sun.star.task.XInteractionAbort'))
                if abort is not None:
                    abort.select(); return
    def prop(name, value):
        p = uno.createUnoStruct('com.sun.star.beans.PropertyValue')
        p.Name, p.Value = name, value
        return p
    program = Path(boot['program'])
    subprocess.Popen([boot.get('executable',str(program / 'soffice.exe')),
        '-env:UserInstallation=' + Path(boot['profile']).as_uri(), '--headless', '--nodefault',
        '--nologo', '--norestore', '--accept=pipe,name=' + boot['pipe'] + ';urp;StarOffice.ServiceManager'],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    local = uno.getComponentContext()
    resolver = local.ServiceManager.createInstanceWithContext('com.sun.star.bridge.UnoUrlResolver', local)
    remote = None
    until = time.monotonic() + 40
    while time.monotonic() < until:
        try:
            remote = resolver.resolve('uno:pipe,name=' + boot['pipe'] + ';urp;StarOffice.ComponentContext')
            break
        except Exception:
            time.sleep(.05)
    if remote is None:
        raise RuntimeError('Native office connection unavailable')
    desktop = remote.ServiceManager.createInstanceWithContext('com.sun.star.frame.Desktop', remote)
    detector = remote.ServiceManager.createInstanceWithContext('com.sun.star.document.TypeDetection', remote)
    send({'id':'ready','ok':True})
    for raw in sys.stdin:
        req = json.loads(raw)
        model, result, stage = None, None, 'import'
        operation_tick = time.monotonic()
        import_tick = export_tick = None
        timings = {'source':'uno_document'}
        request_id = req['id']
        try:
            source = Path(req['source']).resolve(strict=True)
            send({'id':request_id,'stage':'import'})
            import_tick = time.monotonic()
            type_name, selected = None, None
            if req['operation'] == 'inspect':
                descriptor = (prop('URL', source.as_uri()),)
                try:
                    detected = detector.queryTypeByDescriptor(descriptor, True)
                    type_name, updated = detected if isinstance(detected, tuple) and len(detected) == 2 else (detected, ())
                except Exception:
                    # Detection failure must not veto a successful import.
                    type_name, updated = None, ()
                selected = next((p.Value for p in updated if p.Name == 'FilterName'), None)
            # Exports use the loaded model and its real services below. A deep
            # detection result was discarded here, duplicating work per target.
            handler = Abort()
            options = [prop('Hidden', True), prop('ReadOnly', True), prop('Silent', True),
                       prop('MacroExecutionMode', 0), prop('UpdateDocMode', 0), prop('InteractionHandler', handler)]
            # Inspection uses native automatic import. Forcing a guessed text
            # filter here could turn an unsupported binary container into junk.
            chosen = req.get('importFilter')
            if chosen:
                options.append(prop('FilterName', chosen))
                if req.get('importOptions'):
                    options.append(prop('FilterOptions', req['importOptions']))
            model = desktop.loadComponentFromURL(source.as_uri(), '_blank', 0, tuple(options))
            timings['importMs'] = round((time.monotonic()-import_tick)*1000)
            if model is None:
                result = {'id':request_id,'ok':False,'code':'PREFLIGHT_INTERACTION_REQUIRED'
                          if handler.blocked else 'SOURCE_IMPORT_FAILED','stage':'import'}
            else:
                services = [s for s in req['services'] if model.supportsService(s)]
                actual_filter = next((p.Value for p in model.getArgs() if p.Name == 'FilterName'), None)
                if req['operation'] == 'inspect':
                    result = {'id':request_id,'ok':True,'services':services,'typeName':type_name,
                              'filterName':actual_filter,'detectedFilterName':selected}
                elif not set(services).intersection(req['expectedServices']):
                    result = {'id':request_id,'ok':False,'code':'NATIVE_MODEL_CHANGED','stage':'import'}
                else:
                    stage = 'export'
                    send({'id':request_id,'stage':'export'})
                    target = Path(req['output']) / (source.stem + '.' + req['format'])
                    export_options = [prop('FilterName', req['exportFilter']), prop('Overwrite', False)]
                    if req.get('exportOptions') is not None:
                        export_options.append(prop('FilterOptions', req['exportOptions']))
                    export_tick = time.monotonic()
                    try:
                        model.storeToURL(target.as_uri(), tuple(export_options))
                    finally:
                        timings['exportMs'] = round((time.monotonic()-export_tick)*1000)
                    present = target.is_file() and not target.is_symlink()
                    # Result paths stay on disk. Thousands of HTML sidecars
                    # must not overflow the small control-message channel.
                    result = {'id':request_id,'ok':present,'primary':target.name,'stage':'export',
                              'code':'ENGINE_OUTPUT_MISSING' if not present else None}
        except Exception as error:
            result = {'id':request_id,'ok':False,'code':'SOURCE_IMPORT_FAILED' if stage == 'import' else 'ENGINE_FAILED',
                      'stage':stage,'diagnostic':str(error)[-4096:]}
        finally:
            if import_tick is not None and 'importMs' not in timings:
                timings['importMs'] = round((time.monotonic()-import_tick)*1000)
            if model is not None:
                close_tick = time.monotonic()
                try:
                    if hasattr(model,'close'): model.close(True)
                    else: model.dispose()
                except Exception:
                    result['recycle'] = True
                finally:
                    timings['closeMs'] = round((time.monotonic()-close_tick)*1000)
            timings['operationMs'] = round((time.monotonic()-operation_tick)*1000)
            result['timings'] = timings
        send(result)
        if result.get('recycle'):
            break

if __name__ == '__main__':
    try: main(sys.argv[1])
    except Exception:
        send({'id':'ready','ok':False,'code':'UNO_SESSION_UNAVAILABLE'})
        sys.exit(1)
'''


class UnoSession:
    def __init__(self, executable, workspace, prepare_profile, max_tasks=128):
        self.executable, self.workspace = Path(executable), Path(workspace)
        self.prepare_profile, self.max_tasks = prepare_profile, max_tasks
        self.tree = self.logs = self.reader = None
        self.completed = self.starts = 0
        self.responses = queue.Queue(maxsize=8)
        self.profile = self.pipe = None
        self.disabled = False

    def _read(self, stream, responses=None):
        # Bind to this generation; a late dying reader must not feed a newly
        # started worker's queue after cancellation or recycling.
        responses = self.responses if responses is None else responses
        try:
            while line := stream.readline(65537):
                if len(line) > 65536:
                    break
                if not line.startswith(b'AIO_GUARD_RPC '):
                    continue
                try:
                    responses.put_nowait(json.loads(line[len(b'AIO_GUARD_RPC '):]))
                except (ValueError, queue.Full):
                    break
        finally:
            stream.close()

    def _receive(self, identity, cancel, timeout, on_stage=lambda stage:None):
        until = None if timeout is None else time.monotonic() + timeout
        while True:
            if cancel.is_set():
                raise GuardError('CANCELLED','Операция отменена.',stage='engine')
            if until is not None and time.monotonic() >= until:
                raise GuardError('ENGINE_TIMEOUT','Превышено время операции LibreOffice.',stage='engine')
            try:
                result = self.responses.get(timeout=.05)
            except queue.Empty:
                if self.reader is not None and not self.reader.is_alive():
                    raise GuardError('UNO_PROTOCOL','Поток ответов LibreOffice прерван.',stage='engine')
                if self.tree.process.poll() is not None:
                    raise GuardError('UNO_SESSION_UNAVAILABLE','Рабочий процесс LibreOffice завершился.',
                                     stage='launch' if identity == 'ready' else 'engine')
                continue
            if not isinstance(result,dict) or result.get('id') != identity:
                raise GuardError('UNO_PROTOCOL','Получен ответ другого задания.',stage='engine')
            if 'stage' in result and 'ok' not in result:
                on_stage(result['stage'])
                continue
            return result

    def ensure_started(self, cancel, timeout=45):
        if self.tree and (self.completed >= self.max_tasks or self.tree.process.poll() is not None):
            self.recycle()
        if self.tree:
            return
        self.workspace.mkdir(parents=True,exist_ok=True)
        self.profile = self.workspace / secrets.token_hex(8)
        self.prepare_profile(self.profile)
        self.pipe = 'aio_guard_' + secrets.token_hex(16)
        worker, boot = self.workspace/'worker.py', self.workspace/'boot.json'
        worker.write_text(WORKER_SOURCE,encoding='utf-8')
        start = self.workspace/'start.token';start.unlink(missing_ok=True)
        boot.write_text(json.dumps({'program':str(self.executable.parent),'executable':str(self.executable),
                                   'profile':str(self.profile.resolve()),
                                   'pipe':self.pipe,'start':str(start)}),encoding='utf-8')
        environment = dict(os.environ)
        environment.pop('PYTHONHOME',None);environment.pop('PYTHONPATH',None)
        environment['PYTHONNOUSERSITE']='1'
        self.responses = queue.Queue(maxsize=8)
        self.tree = ProcessTree([str(self.executable.parent/'python.exe'),str(worker),str(boot)],
            cwd=self.workspace,env=environment,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        self.logs = NativeLogs(self.tree.process,capture_stdout=False)
        self.reader = threading.Thread(target=self._read,args=(self.tree.process.stdout,self.responses),daemon=True,name='aio-uno-response')
        self.reader.start()
        start.write_text('ready',encoding='ascii')
        self.starts += 1
        try:
            response = self._receive('ready',cancel,timeout)
        except GuardError as error:
            if error.code in ('CANCELLED','ENGINE_TIMEOUT'):
                raise
            raise GuardError('UNO_SESSION_UNAVAILABLE','Не удалось запустить нативный UNO.',stage='launch') from error
        if not response.get('ok'):
            raise GuardError('UNO_SESSION_UNAVAILABLE','Нативный UNO недоступен.',stage='launch')
        self.completed = 0

    def request(self, value, *, cancel, timeout, on_stage=lambda stage:None):
        try:
            self.ensure_started(cancel,min(45,timeout) if timeout is not None else 45)
            identity = secrets.token_hex(16)
            self.tree.process.stdin.write((json.dumps({**value,'id':identity},ensure_ascii=True)+'\n').encode())
            self.tree.process.stdin.flush()
            result = self._receive(identity,cancel,timeout,on_stage)
            self.completed += 1
            if result.get('recycle'):
                self.recycle()
            return result
        except BaseException:
            self.recycle()
            raise

    def recycle(self):
        tree, reader, logs, profile = self.tree, self.reader, self.logs, self.profile
        self.tree = self.reader = self.logs = self.profile = self.pipe = None
        try:
            # Terminate the owned process before flushing/closing its stdin.
            # BrokenPipeError previously skipped every remaining cleanup step.
            if tree:
                tree.close()
        finally:
            try:
                if tree and tree.process.stdin:
                    try:
                        tree.process.stdin.close()
                    except (OSError, ValueError):
                        pass
            finally:
                try:
                    if reader:
                        reader.join(timeout=1)
                    if logs:
                        logs.finish()
                finally:
                    if profile:
                        shutil.rmtree(profile,ignore_errors=True)

    def __enter__(self):return self
    def __exit__(self,*_):
        self.recycle();shutil.rmtree(self.workspace,ignore_errors=True)
