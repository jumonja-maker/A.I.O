"""One cancellable native import per file, with no requested export."""
import json
import os
from pathlib import Path
import sys
import time

from ..native_process import ProcessTree
from .errors import GuardError

# Native document-kind enums -> UNO services. This is not an extension table.
# Preserve text's specialized web/master services as a conservative union.
SERVICES = {
    0: ('com.sun.star.text.TextDocument', 'com.sun.star.text.WebDocument',
        'com.sun.star.text.GlobalDocument'),
    1: ('com.sun.star.sheet.SpreadsheetDocument',),
    2: ('com.sun.star.presentation.PresentationDocument',),
    3: ('com.sun.star.drawing.DrawingDocument',),
}


def inspect_source(executable, source, work, *, cancel, timeout, prepare_profile):
    from .routes import registry
    native = registry(executable)
    if not native.valid:
        return {'state': 'undetermined', 'code': 'PREFLIGHT_REGISTRY_UNAVAILABLE'}
    work = work.resolve()
    work.mkdir(parents=True, exist_ok=False)
    prepare_profile(work / 'profile')
    manifest = work / 'request.json'
    manifest.write_text(json.dumps({'program': str(Path(executable).resolve().parent),
                                    'source': str(source.resolve())}), encoding='utf-8')
    command = ([sys.executable, '--aio-guard-preflight', str(manifest)]
               if getattr(sys, 'frozen', False) else
               [sys.executable, str(Path(__file__).with_name('preflight_native.py')), str(manifest)])
    environment = dict(os.environ)
    if os.name != 'nt':
        environment['LD_LIBRARY_PATH'] = str(Path(executable).resolve().parent) + (
            os.pathsep + environment['LD_LIBRARY_PATH'] if environment.get('LD_LIBRARY_PATH') else '')
    deadline = time.monotonic() + timeout
    try:
        with ProcessTree(command, cwd=work, env=environment) as tree:
            while tree.process.poll() is None:
                if cancel.wait(.05):
                    raise GuardError('CANCELLED', 'Предварительная проверка отменена.', stage='preflight')
                if time.monotonic() >= deadline:
                    stage = work / 'stage.txt'
                    try:
                        marker = stage.read_text(encoding='ascii').strip()[:64]
                    except OSError:
                        marker = 'child_not_started'
                    return {'state': 'undetermined', 'code': 'PREFLIGHT_TIMEOUT',
                            'diagnosticStage': marker}
            if cancel.is_set():
                raise GuardError('CANCELLED', 'Предварительная проверка отменена.', stage='preflight')
            result = work / 'result.json'
            if tree.process.returncode or not result.is_file() or result.stat().st_size > 2048:
                return {'state': 'undetermined', 'code': 'PREFLIGHT_API_UNAVAILABLE'}
            detected = json.loads(result.read_text(encoding='utf-8'))
            kind = detected.get('kind')
            if detected.get('state') != 'detected' or type(kind) is not int or kind not in SERVICES:
                native_code = detected.get('code', 'PREFLIGHT_KIND_UNDETERMINED')
                if native_code not in ('PREFLIGHT_INIT_FAILED', 'PREFLIGHT_API_UNAVAILABLE',
                                       'PREFLIGHT_IMPORT_UNDETERMINED',
                                       'PREFLIGHT_KIND_UNDETERMINED'):
                    native_code = 'PREFLIGHT_KIND_UNDETERMINED'
                return {'state': 'undetermined', 'code': native_code}
            return {'state': 'detected', 'documentServices': list(SERVICES[kind]),
                    'basis': 'native_preflight_document_kind', 'nativeKind': kind,
                    'registryFingerprint': native.fingerprint, 'exported': False}
    except (OSError, ValueError):
        return {'state': 'undetermined', 'code': 'PREFLIGHT_API_UNAVAILABLE'}
