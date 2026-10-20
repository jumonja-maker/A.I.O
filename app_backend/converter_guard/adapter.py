"""Use the existing direct native adapter. No second conversion engine."""
from contextlib import ExitStack, contextmanager
import os
from pathlib import Path
import threading
import time

from ..libreoffice_catalog import catalog
from ..libreoffice_engine import LibreOfficeError, convert_path, engine_binary
from .errors import GuardError
from .routes import registry
from .native_observation import NativeObservation


def prepare_profile(profile):
    # Macro prevention is independent from document import/export selection.
    # Do not use DisableActiveContent: it hid native formula objects in QA.
    user = profile / 'user'
    user.mkdir(parents=True)
    (user / 'registrymodifications.xcu').write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<oor:items xmlns:oor="http://openoffice.org/2001/registry">\n'
        '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
        '<prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop>'
        '<prop oor:name="BlockUntrustedReferrerLinks" oor:op="fuse"><value>true</value></prop>'
        '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>'
        '</item></oor:items>\n', encoding='utf-8')


class _RunControl:
    """Piggyback explicit policies on native cancellation; no watcher thread."""
    def __init__(self, cancel, workspace, timeout, output_limit):
        self.cancel, self.workspace = cancel, workspace
        self.deadline = None if timeout is None else time.monotonic() + timeout
        self.output_limit = output_limit
        self.next_scan = 0

    def is_set(self):
        if self.cancel.is_set():
            return True
        now = time.monotonic()
        if self.deadline is not None and now >= self.deadline:
            raise GuardError('ENGINE_TIMEOUT', 'Превышено явно настроенное время обработки.',
                             stage='engine')
        # Only configured byte quotas cause disk scans, at most twice a second.
        # They count native output bytes, not LO memory or profile/internal temp.
        if self.output_limit is not None and now >= self.next_scan:
            self.next_scan = now + .5
            output = self.workspace / 'output'
            used = sum(p.stat().st_size for p in output.rglob('*')
                       if p.is_file() and not p.is_symlink())
            if used > self.output_limit():
                raise GuardError('SPOOL_QUOTA', 'Превышена настроенная квота временных файлов.',
                                 stage='result', status=507)
        return False

    def wait(self, seconds):
        self.cancel.wait(seconds)
        return self.is_set()


class LibreOfficeAdapter:
    def __init__(self, executable=None):
        self.executable = Path(executable).absolute() if executable else engine_binary()
        self._local = threading.local()

    @contextmanager
    def batch(self, workspace, *, max_tasks=128):
        """A snapshot and a private warm process, isolated per calling thread."""
        from .office_session import OfficeSession
        from .uno_session import UnoSession
        if not self.available:
            yield None
            return
        previous = getattr(self._local, 'batch', None)
        previous_source = (getattr(self._local,'proof',None),getattr(self._local,'import_filter',None))
        factory = (UnoSession if os.name == 'nt' and os.environ.get('AIO_GUARD_NATIVE_SESSION','1') != '0'
                   and (self.executable.parent/'python.exe').is_file()
                   else OfficeSession)
        with factory(self.executable, workspace, prepare_profile, max_tasks) as office:
            self._local.batch = (registry(self.executable), office)
            try:
                yield office
            finally:
                self._local.batch = previous
                self._local.proof, self._local.import_filter = previous_source

    def _registry(self):
        batch = getattr(self._local, 'batch', None)
        return batch[0] if batch else registry(self.executable)

    def route_decider(self):
        return self._registry().observed_decision

    def execution_mode(self):
        return getattr(self._local,'execution_mode',None)

    def prepare_source(self, proof):
        self._local.proof = proof
        name = (proof or {}).get('selectedImportFilter')
        native = self._registry()
        item = native.filters.get(name)
        self._local.import_filter = (name if item and 'IMPORT' in item[2] and
            (proof or {}).get('registryFingerprint') == native.fingerprint and
            item[0] in (proof or {}).get('documentServices', ()) else None)

    @property
    def available(self):
        return bool(self.executable and self.executable.is_file())

    def capabilities(self, files, *, proofs=None):
        available = self.available
        native = self._registry()
        formats = []
        for ext, category, description, mime, registered in catalog(self.executable):
            states = {f['id']: (native.hint(f['name'], ext) if proofs is None else
                               native.observed_decision(ext, proofs.get(f['id']))) for f in files}
            compatible = [i for i, state in states.items() if state == 'registered']
            unknown = [i for i, state in states.items() if state == 'undetermined']
            formats.append({'id': ext, 'label': ext.upper(), 'category': category,
                'description': description, 'mime': mime,
                # Native compatibility is not a claim of content/visual equivalence.
                # The existing help-button reads this property in normal mode.
                'help': ({
                    'csv': 'CSV сохраняет только один активный лист и не сохраняет формулы, форматирование и остальные листы.',
                    'tsv': 'TSV сохраняет только один активный лист без оформления и формул.',
                    'tab': 'TAB сохраняет текстовую таблицу без оформления и других листов.',
                    'png': 'Из документов и рисунков LibreOffice может создавать изображение страницы, а не исходную пиксельную копию.',
                    'jpg': 'JPEG — формат с потерями; экспорт может менять качество изображения и геометрию страницы.',
                    'jpeg': 'JPEG — формат с потерями; экспорт может менять качество изображения и геометрию страницы.',
                    'webp': 'Графический экспорт документа может менять размер полотна и формат представления.',
                    'pdf': 'PDF предназначен для просмотра: структура и редактируемость исходника могут измениться.',
                    'epub': 'EPUB создаётся фильтром экспорта; поддержка экспорта не означает, что EPUB можно открыть как исходник.'
                }).get(ext, 'Проверка Guard подтверждает маршрут, но не гарантирует отсутствие смысловых или визуальных потерь.'),
                'registeredExport': ext in native.exports if native.valid else registered,
                'enabled': available and proofs is not None and bool(compatible),
                # Hints and undetermined routes remain visible diagnostics,
                # but only a native preflight proof admits a UI selection.
                'nativeAttemptAvailable': False,
                'selectableFileIds': compatible if available and proofs is not None else [],
                'verifiedFileIds': compatible if available and proofs is not None else [],
                'compatibleFileIds': compatible if available else [],
                'undeterminedFileIds': unknown if available else [],
                'incompatibleFileIds': [i for i, state in states.items() if state == 'unavailable'],
                'compatibility': 'native_registry_hint' if proofs is None else 'native_preflight',
                'reasonCode': ('NATIVE_ROUTE_UNAVAILABLE' if files and not compatible and not unknown
                               else 'NATIVE_ROUTE_UNDETERMINED' if unknown else 'NATIVE_ROUTE_REGISTERED')})
        return {'files': [{**item, 'compatibility': 'native_decision',
                           **({'preflight': proofs.get(item['id'])} if proofs is not None else {})}
                          for item in files],
                'formats': formats, 'acceptedExtensions': '',
                'compatibility': 'native_registry_hint' if proofs is None else 'native_preflight',
                'available': available,
                'registryAvailable': native.valid, 'registryFingerprint': native.fingerprint,
                'registryWarning': 'EXTERNAL_FILTER_REGISTRY' if native.external_filters else
                                   None if native.valid else 'REGISTRY_UNAVAILABLE',
                'preflightComplete': proofs is not None,
                'message': ('Для части файлов направления не подтверждены и пока недоступны. '
                            'Причина указана в «Проблемах». Можно повторить проверку.'
                            if proofs is not None and any(not p or p.get('state') != 'detected'
                                                          for p in proofs.values()) else ''),
                'qualityCompared': False}

    def preflight_fingerprint(self):
        """Installation identity for a bounded, in-memory content-proof cache."""
        native = self._registry()
        if not self.available or not native.valid:
            return None
        try:
            stat = self.executable.stat()
            return (native.fingerprint, str(self.executable), stat.st_mtime_ns, stat.st_size)
        except OSError:
            return None

    def preflight(self, source, workspace, *, cancel, timeout):
        from .preflight import inspect_source
        if not self.available:
            return {'state': 'undetermined', 'code': 'ENGINE_NOT_FOUND'}
        batch = getattr(self._local, 'batch', None)
        office = batch[1] if batch else None
        if office and hasattr(office, 'request') and not office.disabled:
            native = self._registry()
            try:
                result = office.request({'operation':'inspect','source':str(source.resolve()),
                    'services':sorted(native.import_services),
                    'importFilters':[name for name,item in native.filters.items() if 'IMPORT' in item[2]]},
                    cancel=cancel,timeout=timeout)
                services = result.get('services')
                if (result.get('ok') and isinstance(services,list) and services
                        and all(s in native.import_services for s in services)):
                    selected = result.get('filterName')
                    item = native.filters.get(selected)
                    return {'state':'detected','basis':'native_preflight_uno_document','modelLoaded':True,
                        'documentServices':services,'typeName':result.get('typeName'),'filterName':selected,
                        'selectedImportFilter':selected if item and 'IMPORT' in item[2] and item[0] in services else None,
                        'registryFingerprint':native.fingerprint,'exported':False}
                return {'state':'undetermined','code':result.get('code') or 'PREFLIGHT_IMPORT_FAILED',
                        'diagnosticStage':result.get('stage','import')}
            except GuardError as error:
                if error.code == 'ENGINE_TIMEOUT':
                    return {'state':'undetermined','code':'PREFLIGHT_TIMEOUT','diagnosticStage':'native_import'}
                if error.code != 'UNO_SESSION_UNAVAILABLE' or error.stage != 'launch':
                    raise
                office.disabled = True  # Startup-only fallback; no export was attempted.
        if office and not hasattr(office,'request') and os.name == 'nt' and (self.executable.parent/'python.exe').is_file():
            office.ensure_started()
        try:
            proof = inspect_source(self.executable, source, workspace, cancel=cancel,
                timeout=timeout, prepare_profile=prepare_profile, native=self._registry(),
                office_session=office if os.name == 'nt' and not hasattr(office,'request') else None)
            if office and proof.get('code') in ('PREFLIGHT_TIMEOUT', 'PREFLIGHT_API_UNAVAILABLE',
                                                'PREFLIGHT_UNO_API_UNAVAILABLE'):
                office.recycle()
            return proof
        except BaseException:
            if office:
                office.recycle()
            raise
        finally:
            if office and not hasattr(office,'request'):
                office.completed += 1

    def route_decision(self, target, proof):
        return self._registry().observed_decision(target, proof)

    def observation_needed(self, proof):
        native = self._registry()
        return native.valid and not (proof and proof.get('registryFingerprint') == native.fingerprint
            and (proof.get('basis') == 'native_selected_export_filter' or
                 proof.get('basis') in ('native_preflight_document_kind', 'native_preflight_uno_type',
                                        'native_preflight_uno_graphic', 'native_preflight_uno_document')
                 and proof.get('state') == 'detected' and proof.get('documentServices')))

    def convert(self, source, target, workspace, *, cancel, timeout,
                on_started, output_limit=None, on_document_service=None, on_stage=None):
        self._local.execution_mode = None
        if not self.available:
            raise GuardError('ENGINE_NOT_FOUND', 'LibreOffice не найден.', stage='launch', status=503)
        if cancel.is_set():
            raise GuardError('CANCELLED', 'Конвертация отменена.', stage='launch')
        control = (cancel if timeout is None and output_limit is None else
                   _RunControl(cancel, workspace, timeout, output_limit))
        try:
            # The first requested native export identifies the actual model.
            # Filename hints never veto a file, including renamed/unlisted ones.
            batch = getattr(self._local, 'batch', None)
            if batch and on_document_service is None and hasattr(batch[1],'request') and not batch[1].disabled:
                office, native = batch[1], batch[0]
                proof = getattr(self._local,'proof',None) or {}
                services = proof.get('documentServices', [proof.get('documentService')])
                filter_name = native.export_filter(target, services)
                if filter_name:
                    output = workspace / 'output'
                    output.mkdir(parents=True)
                    try:
                        office.ensure_started(control,min(45,timeout) if timeout is not None else 45)
                        self._local.execution_mode = 'native_uno'
                        on_started()
                        result = office.request({'operation':'export','source':str(source.resolve()),
                            'format':target,'output':str(output.resolve()),'exportFilter':filter_name,
                            'importFilter':getattr(self._local,'import_filter',None),
                            'services':sorted(native.import_services),'expectedServices':services,
                            'importFilters':[name for name,item in native.filters.items() if 'IMPORT' in item[2]]},
                            cancel=control,timeout=timeout,on_stage=on_stage or (lambda stage:None))
                        if not result.get('ok'):
                            diagnostic=str(result.get('diagnostic','')).replace(str(source.parent.parent.parent),'[local]')
                            raise GuardError(result.get('code') or 'ENGINE_FAILED','LibreOffice не смог '+
                                ('открыть исходник.' if result.get('stage')=='import' else 'создать выбранный результат.'),
                                stage=result.get('stage','engine'),diagnostic=diagnostic)
                        return sorted(p for p in output.rglob('*') if p.is_file() and not p.is_symlink())
                    except GuardError as error:
                        office.recycle()
                        if error.code != 'UNO_SESSION_UNAVAILABLE' or error.stage != 'launch':
                            raise
                        office.disabled = True
                        output.rmdir()  # No requested export ran; original CLI remains available.
            prepare_profile(workspace / 'profile')
            self._local.execution_mode = 'native_cli'
            native = self._registry()
            if on_document_service is None or not native.valid:
                return convert_path(source, target, workspace, executable=self.executable,
                                    cancel=control, on_started=on_started,
                                    import_filter=getattr(self._local,'import_filter',None))
            observation = NativeObservation()
            completed = False
            try:
                with ExitStack() as stack:
                    try:
                        stack.enter_context(observation)
                    except (OSError, RuntimeError):
                        # Observability is optional; inability to create a pipe
                        # or drain thread must not veto a native conversion.
                        return convert_path(source, target, workspace, executable=self.executable,
                                            cancel=control, on_started=on_started,
                                    import_filter=getattr(self._local,'import_filter',None))
                    paths = convert_path(source, target, workspace, executable=self.executable,
                        cancel=control, on_started=on_started, stdout=observation.writer,
                        import_filter=getattr(self._local,'import_filter',None))
                completed = True
                return paths
            finally:
                proof = native.proof(observation.selected_filter(), target) if completed else None
                if proof:
                    on_document_service(proof)
        except LibreOfficeError as error:
            raise GuardError(error.code, str(error), stage='import' if error.code == 'SOURCE_IMPORT_FAILED'
                             else 'engine', diagnostic=error.diagnostic) from error
        except OSError as error:
            raise GuardError('STORAGE_ERROR', 'Ошибка временного хранилища Guard.',
                             stage='storage', status=507) from error
