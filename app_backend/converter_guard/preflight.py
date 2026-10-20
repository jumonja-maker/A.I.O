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
    program = Path(executable).resolve().parent
    environment = dict(os.environ)
    if os.name == 'nt':
        # LOK documentLoad hangs on bundled Windows LO 26.8.1.1. Use the
        # same office installation's native UNO TypeDetection instead. This
        # reads source bytes but never starts a requested export.
        from .preflight_uno import UNO_WORKER_SOURCE
        bundled_python = program / 'python.exe'
        if not bundled_python.is_file():
            return {'state': 'undetermined', 'code': 'PREFLIGHT_UNO_API_UNAVAILABLE'}
        worker = work / 'uno_worker.py'
        worker.write_text(UNO_WORKER_SOURCE, encoding='utf-8')
        command = [str(bundled_python), str(worker), str(manifest)]
        environment.pop('PYTHONHOME', None)
        environment.pop('PYTHONPATH', None)
        environment['PYTHONNOUSERSITE'] = '1'
    else:
        command = ([sys.executable, '--aio-guard-preflight', str(manifest)]
                   if getattr(sys, 'frozen', False) else
                   [sys.executable, str(Path(__file__).with_name('preflight_native.py')), str(manifest)])
        environment['LD_LIBRARY_PATH'] = str(program) + (
            os.pathsep + environment['LD_LIBRARY_PATH'] if environment.get('LD_LIBRARY_PATH') else '')
    deadline = time.monotonic() + timeout
    try:
        with ProcessTree(command, cwd=work, env=environment) as tree:
            if os.name == 'nt':
                # A short handshake keeps the UNO-spawned soffice process
                # inside our Windows kill-on-close Job Object.
                (work / 'start.token').write_text('ready', encoding='ascii')
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
            if os.name == 'nt':
                type_name = detected.get('typeName')
                services = native.services_for_type(type_name)
                if detected.get('state') == 'detected' and services:
                    return {'state': 'detected', 'documentServices': list(services),
                            'basis': 'native_preflight_uno_type', 'typeName': type_name,
                            'registryFingerprint': native.fingerprint, 'exported': False}
                code = (detected.get('code') if detected.get('state') != 'detected'
                        else 'PREFLIGHT_UNO_TYPE_UNMAPPED')
                if code not in ('PREFLIGHT_UNO_API_UNAVAILABLE', 'PREFLIGHT_UNO_TYPE_UNKNOWN',
                                'PREFLIGHT_UNO_TYPE_UNMAPPED'):
                    code = 'PREFLIGHT_UNO_API_UNAVAILABLE'
                try:
                    stage = (work / 'stage.txt').read_text(encoding='ascii').strip()[:64]
                except OSError:
                    stage = 'unknown'
                return {'state': 'undetermined', 'code': code,
                        'diagnosticStage': stage,
                        'diagnosticType': str(type_name or '')[:96]}
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
