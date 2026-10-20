"""Bundled LibreOffice Python UNO worker source for Windows pre-export detection.

The parent writes this fixed script into its private temporary workspace and
launches the Python that came with the same LibreOffice installation.
The worker asks LibreOffice's TypeDetection to inspect document *content*;
it never calls saveAs, convertTo, or documentLoad.
"""
UNO_WORKER_SOURCE = r'''
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time


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
    profile = (work / 'profile').resolve(strict=True)
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
    suffix = secrets.token_hex(12)
    office = program / 'soffice.exe'
    if not office.is_file():
        office = program / 'soffice.com'
    if not office.is_file():
        raise FileNotFoundError('Native LibreOffice executable not found')
    arg = '--accept=pipe,name=aio_preflight_' + suffix + ';urp;StarOffice.ServiceManager'
    stage(work, 'uno_office_start')
    child = subprocess.Popen(
        [str(office), '-env:UserInstallation=' + profile.as_uri(),
         '--headless', '--nologo', '--nodefault', '--norestore', '--nofirststartwizard', arg],
        cwd=str(program), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    stage(work, 'uno_resolver')
    local = uno.getComponentContext()
    resolver = local.ServiceManager.createInstanceWithContext(
        'com.sun.star.bridge.UnoUrlResolver', local)
    url = 'uno:pipe,name=aio_preflight_' + suffix + ';urp;StarOffice.ComponentContext'
    until = time.monotonic() + 10
    remote = None
    while time.monotonic() < until:
        try:
            remote = resolver.resolve(url)
            break
        except Exception:
            if child.poll() is not None:
                break
            time.sleep(.1)
    if remote is None:
        raise RuntimeError('UNO connection unavailable')

    stage(work, 'uno_type_detection')
    detector = remote.ServiceManager.createInstanceWithContext(
        'com.sun.star.document.TypeDetection', remote)
    if detector is None:
        raise RuntimeError('UNO TypeDetection service unavailable')
    prop = uno.createUnoStruct('com.sun.star.beans.PropertyValue')
    prop.Name = 'URL'
    prop.Value = source.as_uri()
    # AllowDeep=true makes LibreOffice inspect bytes, not merely the suffix.
    detected = detector.queryTypeByDescriptor((prop,), True)
    stage(work, 'uno_type_detected')
    if isinstance(detected, str) and detected:
        result = {'state': 'detected', 'typeName': detected}
    else:
        # The document TypeDetection service does not classify all graphic
        # streams. Let the *same native LibreOffice* GraphicProvider inspect
        # bytes; getType() = PIXEL/VECTOR means decoded graphic, not filename.
        stage(work, 'uno_graphic_detection')
        result = {'state': 'undetermined', 'code': 'PREFLIGHT_UNO_TYPE_UNKNOWN'}
        try:
            provider = remote.ServiceManager.createInstanceWithContext(
                'com.sun.star.graphic.GraphicProvider', remote)
            graphic = provider.queryGraphic((prop,)) if provider else None
            kind = int(graphic.getType()) if graphic is not None else 0
            if kind in (1, 2):
                result = {'state': 'detected', 'graphicType': kind}
        except Exception:
            pass
    part = work / 'result.part'
    with part.open('x', encoding='utf-8') as stream:
        json.dump(result, stream)
        stream.flush()
        os.fsync(stream.fileno())
    part.replace(work / 'result.json')
    stage(work, 'result_written')
    # The Job Object owns both this Python worker and the connected office.
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
