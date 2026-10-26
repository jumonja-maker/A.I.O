"""Bundled LibreOffice Python UNO worker source for Windows pre-export import.

The parent writes this fixed script into its private temporary workspace and
launches the Python that came with the same LibreOffice installation.
The worker detects content, loads one hidden native model, obtains its actual
services and closes it. It never starts an export or a requested conversion.
"""
UNO_WORKER_SOURCE = r'''
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time


def model_metadata(model):
    # Report the filter/type used by the model that was actually opened,
    # rather than performing a second deep content scan for diagnostics.
    values = {p.Name:p.Value for p in model.getArgs()
              if p.Name in ('FilterName', 'TypeName')}
    return {'filterName':values.get('FilterName'), 'typeName':values.get('TypeName')}


def stage(folder, name):
    with (folder / 'stage.txt').open('w', encoding='ascii') as stream:
        stream.write(name)
        stream.flush()
        os.fsync(stream.fileno())


def main(manifest_name):
    manifest = Path(manifest_name).resolve(strict=True)
    work = manifest.parent
    req = json.loads(manifest.read_text(encoding='utf-8'))
    program = Path(req['program']).resolve(strict=True)
    profile = Path(req.get('profile', work / 'profile')).resolve(strict=True)
    source = Path(req['source']).resolve(strict=True)
    if program.name.lower() != 'program' or not source.is_file():
        raise ValueError('Invalid preflight paths')

    stage(work, 'uno_python_start')
    # Wait until the controller has assigned this interpreter to its
    # kill-on-close Windows Job Object, before spawning LibreOffice.
    ready = work / 'start.token'
    until = time.monotonic() + 10
    while not ready.is_file():
        if time.monotonic() >= until:
            raise TimeoutError('Job handshake timeout')
        time.sleep(.02)

    import uno
    stage(work, 'uno_python_imported')
    suffix = req.get('officePipe') or ('aio_preflight_' + secrets.token_hex(12))
    if not isinstance(suffix, str) or not suffix.replace('_', '').isalnum():
        raise ValueError('Invalid private office pipe')
    office = program / 'soffice.exe'
    if not office.is_file():
        office = program / 'soffice.com'
    if not office.is_file():
        raise FileNotFoundError('Native LibreOffice executable not found')
    arg = '--accept=pipe,name=' + suffix + ';urp;StarOffice.ServiceManager'
    stage(work, 'uno_office_start')
    child = None if req.get('officePipe') else subprocess.Popen(
        [str(office), '-env:UserInstallation=' + profile.as_uri(),
         '--headless', '--nologo', '--nodefault', '--norestore', '--nofirststartwizard', arg],
        cwd=str(program), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    stage(work, 'uno_resolver')
    local = uno.getComponentContext()
    resolver = local.ServiceManager.createInstanceWithContext(
        'com.sun.star.bridge.UnoUrlResolver', local)
    url = 'uno:pipe,name=' + suffix + ';urp;StarOffice.ComponentContext'
    until = time.monotonic() + 10
    remote = None
    while time.monotonic() < until:
        try:
            remote = resolver.resolve(url)
            break
        except Exception:
            if child is not None and child.poll() is not None:
                break
            time.sleep(.1)
    if remote is None:
        raise RuntimeError('UNO connection unavailable')

    # A registered type alone does not prove this document can be opened.
    # Load once, without exporting, and obtain the actual native model.
    stage(work, 'uno_document_load')
    import unohelper
    from com.sun.star.task import XInteractionHandler

    class AbortInteraction(unohelper.Base, XInteractionHandler):
        blocked = False
        def handle(self, request):
            self.blocked = True
            for continuation in request.getContinuations():
                abort = continuation.queryInterface(uno.getTypeByName('com.sun.star.task.XInteractionAbort'))
                if abort is not None:
                    abort.select()
                    return

    def property_value(name, value):
        item = uno.createUnoStruct('com.sun.star.beans.PropertyValue')
        item.Name, item.Value = name, value
        return item

    handler = AbortInteraction()
    options = [property_value('Hidden', True), property_value('ReadOnly', True),
               property_value('Silent', True), property_value('MacroExecutionMode', 0),
               property_value('UpdateDocMode', 0), property_value('InteractionHandler', handler)]
    hint = req.get('csvImportHint')
    # A sampled UTF-8 tabular stream plus a uniquely selected *installed*
    # Calc native importer is still only a hint. The live model must prove
    # Calc service and the selected FilterName before any route is admitted.
    if isinstance(hint, dict):
        options.append(property_value('FilterName', hint['filter']))
        if hint.get('options') is not None:
            options.append(property_value('FilterOptions', hint['options']))
    desktop = remote.ServiceManager.createInstanceWithContext('com.sun.star.frame.Desktop', remote)
    model = None
    try:
        model = desktop.loadComponentFromURL(source.as_uri(), '_blank', 0, tuple(options))
        services = [s for s in req.get('documentServices', []) if model is not None and model.supportsService(s)]
        # Aborting a load interaction can still return a partial native model.
        # Keep it undetermined rather than offering routes for damaged content.
        if handler.blocked or not services:
            result = {'state':'undetermined','code':'PREFLIGHT_INTERACTION_REQUIRED'
                      if handler.blocked else 'PREFLIGHT_IMPORT_FAILED'}
        elif req.get('expectedSourceServices') and not set(services).intersection(req['expectedSourceServices']):
            result = {'state': 'undetermined', 'code': 'SOURCE_MODEL_MISMATCH'}
        else:
            try:
                text_document = bool(model.queryInterface(
                    uno.getTypeByName('com.sun.star.text.XTextDocument')))
            except Exception:
                text_document = False
            result = {**model_metadata(model), 'state':'detected','modelLoaded':True,
                      'documentServices':services, 'nativeTextDocument':text_document}
            if 'com.sun.star.drawing.DrawingDocument' in services:
                try:
                    result['drawPages'] = model.getDrawPages().getCount()
                except Exception:
                    pass  # Optional metadata must not reject a valid model.
    except Exception:
        result = {'state':'undetermined','code':'PREFLIGHT_INTERACTION_REQUIRED'
                  if handler.blocked else 'PREFLIGHT_IMPORT_FAILED'}
    finally:
        if model is not None:
            if hasattr(model, 'close'):
                model.close(True)
            else:
                model.dispose()
    stage(work, 'uno_document_closed')
    with (work / 'result.part').open('w', encoding='utf-8') as stream:
        json.dump(result, stream)
        stream.flush()
        os.fsync(stream.fileno())
    (work / 'result.part').replace(work / 'result.json')
    stage(work, 'result_written')
    # The worker is owned by its Job Object; a reused office belongs to the
    # outer batch Job Object. Both are private to this AIO request.
    os._exit(0)


if __name__ == '__main__':
    try:
        main(sys.argv[1])
    except Exception as error:
        # No raw input paths, UNO exception messages or tokens leave this worker.
        folder = Path(sys.argv[1]).resolve().parent
        result = {'state': 'undetermined', 'code': 'PREFLIGHT_UNO_API_UNAVAILABLE'}
        try:
            part = folder / 'result.part'
            with part.open('w', encoding='utf-8') as stream:
                json.dump(result, stream)
                stream.flush()
            part.replace(folder / 'result.json')
        finally:
            os._exit(0)
'''

