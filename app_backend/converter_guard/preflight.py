"""One cancellable native import per file, with no requested export."""
import json
import os
from pathlib import Path
import sys
import subprocess
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


def _diagnostic_stage(work):
    try:
        return (work / 'stage.txt').read_text(encoding='ascii').strip()[:64]
    except OSError:
        return 'child_not_started'


def inspect_source(executable, source, work, *, cancel, timeout, prepare_profile,
                   native=None, office_session=None):
    from .routes import registry
    from .csv_import import csv_import_hint
    native = native if native is not None else registry(executable)
    hint = csv_import_hint(source, native)
    if not native.valid:
        return {'state': 'undetermined', 'code': 'PREFLIGHT_REGISTRY_UNAVAILABLE'}
    work = work.resolve()
    work.mkdir(parents=True, exist_ok=False)
    prepare_profile(work / 'profile')
    manifest = work / 'request.json'
    request = {'program': str(Path(executable).resolve().parent), 'source': str(source.resolve())}
    request['documentServices'] = sorted({service for services in native.imports.values() for service in services})
    if hint:
        request['csvImportHint'] = hint
    if office_session is not None:
        request.update(officePipe=office_session.pipe, profile=str(office_session.profile.resolve()))
    manifest.write_text(json.dumps(request), encoding='utf-8')
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
                # Handshake puts the worker-spawned soffice in our Job Object.
                (work / 'start.token').write_text('ready', encoding='ascii')
            while tree.process.poll() is None:
                if cancel.is_set():
                    raise GuardError('CANCELLED', 'Предварительная проверка отменена.', stage='preflight')
                if time.monotonic() >= deadline:
                    return {'state': 'undetermined', 'code': 'PREFLIGHT_TIMEOUT',
                            'diagnosticStage': _diagnostic_stage(work)}
                try:
                    tree.process.wait(timeout=min(.05, max(0, deadline-time.monotonic())))
                except subprocess.TimeoutExpired:
                    pass
            if cancel.is_set():
                raise GuardError('CANCELLED', 'Предварительная проверка отменена.', stage='preflight')
            result = work / 'result.json'
            if tree.process.returncode or not result.is_file() or result.stat().st_size > 2048:
                return {'state': 'undetermined', 'code': 'PREFLIGHT_API_UNAVAILABLE',
                        'diagnosticStage': _diagnostic_stage(work)}
            detected = json.loads(result.read_text(encoding='utf-8'))
            if not isinstance(detected, dict):
                return {'state': 'undetermined', 'code': 'PREFLIGHT_API_UNAVAILABLE'}
            if os.name == 'nt':
                services = detected.get('documentServices')
                installed = {service for values in native.imports.values() for service in values}
                if (detected.get('state') == 'detected' and detected.get('modelLoaded') is True
                        and isinstance(services, list) and services and all(s in installed for s in services)):
                    selected = detected.get('filterName')
                    item = native.filters.get(selected)
                    import_filter = selected if item and 'IMPORT' in item[2] and item[0] in services else None
                    if hint and (import_filter != hint['filter'] or
                                 sorted(set(services)) != ['com.sun.star.sheet.SpreadsheetDocument']):
                        return {'state':'undetermined','code':'PREFLIGHT_IMPORT_FILTER_MISMATCH'}
                    return {'state':'detected','basis':'native_preflight_uno_document','modelLoaded':True,
                            'documentServices':sorted(set(services)), 'typeName':detected.get('typeName'),
                            'filterName':selected, 'selectedImportFilter':import_filter,
                            'selectedImportOptions':hint['options'] if hint else None,
                            'drawPages':detected.get('drawPages'),
                            'registryFingerprint':native.fingerprint,'exported':False}
            if os.name == 'nt':
                code = (detected.get('code') if detected.get('state') != 'detected'
                        else 'PREFLIGHT_UNO_TYPE_UNMAPPED')
                if code not in ('PREFLIGHT_UNO_API_UNAVAILABLE', 'PREFLIGHT_UNO_TYPE_UNKNOWN',
                                'PREFLIGHT_UNO_TYPE_UNMAPPED', 'PREFLIGHT_IMPORT_FAILED',
                                'PREFLIGHT_INTERACTION_REQUIRED','PREFLIGHT_IMPORT_FILTER_MISMATCH'):
                    code = 'PREFLIGHT_UNO_API_UNAVAILABLE'
                try:
                    stage = (work / 'stage.txt').read_text(encoding='ascii').strip()[:64]
                except OSError:
                    stage = 'unknown'
                return {'state': 'undetermined', 'code': code,
                        'diagnosticStage': stage,
                        'diagnosticType': str(detected.get('typeName') or '')[:96]}
            kind = detected.get('kind')
            if (os.name != 'nt' and detected.get('state') == 'detected'
                    and type(kind) is int and kind == 4 and detected.get('contentClass')=='odf_chart'):
                chart='com.sun.star.chart2.ChartDocument'
                if chart in native.import_services:
                    return {'state':'detected','basis':'native_preflight_chart_content',
                            'documentServices':[chart],'nativeKind':4,'chartPackage':True,
                            'registryFingerprint':native.fingerprint,'exported':False}
            if (os.name != 'nt' and detected.get('state') == 'detected'
                    and type(kind) is int and kind == 4
                    and detected.get('contentClass') == 'mathml'):
                formula = 'com.sun.star.formula.FormulaProperties'
                # The MathML XML root and successful LOK load identify the
                # input. Also require an installed native formula import.
                if formula in {svc for values in native.imports.values() for svc in values}:
                    return {'state':'detected','basis':'native_preflight_mathml_content',
                            'documentServices':[formula], 'nativeKind':kind,
                            'mathmlRoot':True, 'registryFingerprint':native.fingerprint,
                            'exported':False}
            if detected.get('state') != 'detected' or type(kind) is not int or kind not in SERVICES:
                native_code = detected.get('code', 'PREFLIGHT_KIND_UNDETERMINED')
                if native_code not in ('PREFLIGHT_INIT_FAILED', 'PREFLIGHT_API_UNAVAILABLE',
                                       'PREFLIGHT_IMPORT_UNDETERMINED',
                                       'PREFLIGHT_KIND_UNDETERMINED'):
                    native_code = 'PREFLIGHT_KIND_UNDETERMINED'
                return {'state': 'undetermined', 'code': native_code}
            return {'state': 'detected', 'documentServices': list(SERVICES[kind]),
                    'basis': 'native_preflight_document_kind', 'nativeKind': kind,
                    'drawPages': detected.get('drawPages') if kind == 3 else None,
                    'registryFingerprint': native.fingerprint, 'exported': False}
    except (OSError, ValueError):
        return {'state': 'undetermined', 'code': 'PREFLIGHT_API_UNAVAILABLE'}
