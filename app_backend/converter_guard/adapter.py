"""Use the existing direct native adapter. No second conversion engine."""
from contextlib import ExitStack, contextmanager
import os
from pathlib import Path
import threading
import time

from ..libreoffice_catalog import catalog
from ..libreoffice_engine import LibreOfficeError, convert_path, engine_binary, validated_outputs
from .errors import GuardError
from .routes import registry
from .csv_import import csv_import_hint, inspect_csv_stream
from .export_recipe import native_export_recipe
from .native_observation import NativeObservation
from .native_metrics import clean_timings
from .legacy_import import legacy_import_hint, validate_options
from .postconditions import check_native_output
from .source_contract import inspect_source_contract, source_import_hint, source_model_matches


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
    def batch(self, workspace, *, max_tasks=128, import_timeout=60, close_timeout=15, chart_timeout=60):
        """A snapshot and a private warm process, isolated per calling thread."""
        from .office_session import OfficeSession
        from .uno_session import UnoSession
        if not self.available:
            yield None
            return
        previous = getattr(self._local, 'batch', None)
        previous_source = (getattr(self._local,'proof',None),getattr(self._local,'import_filter',None),
                           getattr(self._local,'import_options',None))
        factory = (UnoSession if os.name == 'nt' and os.environ.get('AIO_GUARD_NATIVE_SESSION','1') != '0'
                   and (self.executable.parent/'python.exe').is_file()
                   else OfficeSession)
        with factory(self.executable, workspace, prepare_profile, max_tasks) as office:
            office.import_timeout, office.close_timeout = import_timeout, close_timeout
            office.chart_timeout = chart_timeout
            self._local.batch = (registry(self.executable), office)
            try:
                yield office
            finally:
                self._local.batch = previous
                self._local.proof, self._local.import_filter, self._local.import_options = previous_source

    def _registry(self):
        batch = getattr(self._local, 'batch', None)
        return batch[0] if batch else registry(self.executable)

    def route_decider(self):
        return self._registry().observed_decision

    def execution_mode(self):
        return getattr(self._local,'execution_mode',None)

    def native_timings(self):
        return clean_timings(getattr(self._local, 'metrics', None))

    def export_info(self):
        return getattr(self._local, 'export_info', {})

    def prepare_source(self, proof):
        self._local.proof = proof
        name = (proof or {}).get('selectedImportFilter')
        options = (proof or {}).get('selectedImportOptions')
        if (not name and (proof or {}).get('basis') == 'native_preflight_document_kind'
                and (proof or {}).get('state') == 'detected'
                and ((proof or {}).get('nativeKind') == 1 and
                     (proof or {}).get('documentServices') == ['com.sun.star.sheet.SpreadsheetDocument']
                     or (proof or {}).get('sourceContract', {}).get('contentClass') == 'flat_odf'
                     and source_model_matches(proof['sourceContract'], proof.get('documentServices')))):
            # LOK confirms the model, but does not expose its chosen filter.
            # Keep an explicit installed text-import request separate from
            # a UNO-observed selected filter; never replace a different model.
            name = proof.get('requestedImportFilter')
            options = proof.get('requestedImportOptions')
        native = self._registry()
        item = native.filters.get(name)
        self._local.import_filter = (name if item and 'IMPORT' in item[2] and
            (proof or {}).get('registryFingerprint') == native.fingerprint and
            item[0] in (proof or {}).get('documentServices', ()) else None)
        self._local.import_options = (options
            if self._local.import_filter and
            isinstance(options, str) and validate_options(self._local.import_filter, options, native)
            else None)

    @property
    def available(self):
        return bool(self.executable and self.executable.is_file())

    def capabilities(self, files, *, proofs=None):
        available = self.available
        native = self._registry()
        formats = []
        for ext, category, description, mime, registered in catalog(self.executable):
            if ext == 'xml':
                description = 'XML — нативное представление документа'
            states = {f['id']: (native.hint(f['name'], ext) if proofs is None else
                               native.observed_decision(ext, proofs.get(f['id']))) for f in files}
            compatible = [i for i, state in states.items() if state == 'registered']
            unknown = [i for i, state in states.items() if state == 'undetermined']
            formats.append({'id': ext, 'label': ext.upper(), 'category': category,
                'description': description, 'mime': mime,
                # Native compatibility is not a claim of content/visual equivalence.
                # The existing help-button reads this property in normal mode.
                'help': [{'title':'Формат', 'text':description},
                    {'title':'Совместимость', 'text':'Маршрут подтверждает установленный LibreOffice; точность результата проверяется отдельно.'},
                    {'title':'Риски', 'text':({
                    'csv': 'CSV сохраняет только один активный лист и не сохраняет формулы, форматирование и остальные листы.',
                    'tsv': 'TSV сохраняет только один активный лист без оформления и формул.',
                    'tab': 'TAB сохраняет текстовую таблицу без оформления и других листов.',
                    'png': 'Из документов и рисунков LibreOffice может создавать изображение страницы, а не исходную пиксельную копию.',
                    'jpg': 'JPEG — формат с потерями; экспорт может менять качество изображения и геометрию страницы.',
                    'jpeg': 'JPEG — формат с потерями; экспорт может менять качество изображения и геометрию страницы.',
                    'webp': 'Графический экспорт документа может менять размер полотна и формат представления.',
                    'pdf': 'PDF предназначен для просмотра: структура и редактируемость исходника могут измениться.',
                    'epub': 'EPUB создаётся фильтром экспорта; поддержка экспорта не означает, что EPUB можно открыть как исходник.',
                    'xml': 'Для Writer/Calc/Impress/Draw используется установленный штатный фильтр Flat OpenDocument XML. Это не Excel/Word 2003 XML; точный фильтр указан в отчёте.'
                }).get(ext, 'Проверка Guard подтверждает маршрут, но не гарантирует отсутствие смысловых или визуальных потерь.')}],
                'unavailableReasons': {f['id']: native.route_reason(ext, (proofs or {}).get(f['id']))
                                       for f in files if proofs and native.route_reason(ext, proofs.get(f['id']))},
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
        native = self._registry()
        contract = inspect_source_contract(source, cancel=cancel)
        if contract.get('state') == 'invalid':
            return {'state': 'undetermined', 'code': contract['code'],
                    'sourceContract': contract, 'exported': False}
        if (contract.get('contentClass') == 'epub_package' and native.valid
                and not native.imports.get('epub')):
            return {'state': 'undetermined', 'code': 'EPUB_NATIVE_IMPORT_UNAVAILABLE',
                    'sourceContract': contract, 'exported': False}
        source_hint = source_import_hint(contract, native)
        hint = source_hint or csv_import_hint(source, native) or legacy_import_hint(source, native)
        # One streaming scan, only for a content-confirmed text hint. Large or
        # ambiguous inputs retain an explicit incomplete status, never a veto.
        csv_profile = (inspect_csv_stream(source, delimiter=chr(int(hint['options'].split(',')[0])),
                                         cancel=cancel, seconds=min(.15, timeout or .15),
                                         analyze_values=False) if hint and not source_hint
                       and hint.get('basis') != 'content_confirmed_legacy' else None)
        if office and hasattr(office, 'request') and not office.disabled:
            native = self._registry()
            try:
                result = office.request({'operation':'inspect','source':str(source.resolve()),
                    'services':sorted(native.import_services),
                    'expectedSourceServices':contract.get('expectedServices', []),
                    'importFilter':hint['filter'] if hint else None,
                    'importOptions':hint['options'] if hint else None},
                    cancel=cancel,timeout=timeout)
                services = result.get('services')
                if (result.get('ok') and isinstance(services,list) and services
                        and all(s in native.import_services for s in services)):
                    selected = result.get('filterName')
                    item = native.filters.get(selected)
                    confirmed = selected if item and 'IMPORT' in item[2] and item[0] in services else None
                    if not source_model_matches(contract, services):
                        return {'state': 'undetermined', 'code': 'SOURCE_MODEL_MISMATCH',
                                'sourceContract': contract, 'exported': False}
                    if hint and (confirmed != hint['filter'] or not source_hint and
                                 services != ['com.sun.star.sheet.SpreadsheetDocument']):
                        return {'state':'undetermined','code':'PREFLIGHT_IMPORT_FILTER_MISMATCH'}
                    return {'state':'detected','basis':'native_preflight_uno_document','modelLoaded':True,
                        'nativeTextDocument': result.get('nativeTextDocument') is True,
                        'nativeImportMs': (result.get('timings') or {}).get('importMs'),
                        'documentServices':services,'typeName':result.get('typeName'),'filterName':selected,
                        'selectedImportFilter':confirmed,
                        'selectedImportOptions':hint['options'] if hint else None,
                        'sourceContract':contract,
                        'csvImportSampleOnly': bool(csv_profile and not csv_profile.get('complete')),
                        'csvInspection': csv_profile,
                        'drawPages':result.get('drawPages'),
                        'registryFingerprint':native.fingerprint,'exported':False}
                return {'state':'undetermined','code':result.get('code') or 'PREFLIGHT_IMPORT_FAILED',
                        'diagnosticStage':result.get('stage','import'),
                        **({'sourceContract': contract} if contract.get('state') != 'not_applicable' else {})}
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
            if proof.get('state') == 'detected' and not source_model_matches(contract, proof.get('documentServices')):
                return {'state': 'undetermined', 'code': 'SOURCE_MODEL_MISMATCH',
                        'sourceContract': contract, 'exported': False}
            if contract.get('state') != 'not_applicable':
                proof = {**proof, 'sourceContract': contract}
            if (hint and not source_hint and proof.get('state') == 'detected'
                    and proof.get('basis') == 'native_preflight_document_kind'
                    and proof.get('nativeKind') == 1
                    and proof.get('documentServices') == ['com.sun.star.sheet.SpreadsheetDocument']
                    and proof.get('registryFingerprint') == native.fingerprint):
                proof = {**proof, 'requestedImportFilter': hint['filter'],
                         'requestedImportOptions': hint['options'],
                         'csvImportSampleOnly': bool(csv_profile and not csv_profile.get('complete')), 'csvInspection':csv_profile}
            if (source_hint and proof.get('state') == 'detected'
                    and proof.get('basis') == 'native_preflight_document_kind'
                    and proof.get('registryFingerprint') == native.fingerprint):
                proof = {**proof, 'requestedImportFilter': source_hint['filter']}
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
        from .media_fidelity import copy_media_identity
        from .native_identity import copy_chart_identity
        proof = getattr(self._local, 'proof', None) or {}
        paths = None
        identity_control = (cancel if timeout is None and output_limit is None else
                            _RunControl(cancel, workspace, timeout, output_limit))
        if proof.get('state') == 'detected' and self.route_decision(target, proof) == 'registered':
            paths = copy_media_identity(source, workspace / 'output', target, cancel=identity_control)
            identity = 'media_identity' if paths else None
            if paths is None:
                paths = copy_chart_identity(source, workspace / 'output', target, proof, cancel=identity_control)
                identity = 'native_identity' if paths else None
            if paths:
                self._local.execution_mode = identity
                self._local.metrics = None
                self._local.export_info = {'executionReason': 'validated_' + identity,
                    'resultChecks': [{'check': identity, 'status': 'byte_preserved', 'qualityCompared': False}]}
        if paths is None:
            paths = self._convert_native(source, target, workspace, cancel=cancel, timeout=timeout,
                on_started=on_started, output_limit=output_limit,
                on_document_service=on_document_service, on_stage=on_stage)
        check = check_native_output(paths, target, source, cancel=cancel)
        if check:
            checks = [{key: value for key, value in check.items() if key != 'additionalChecks'}]
            checks.extend(check.get('additionalChecks', []))
            self._local.export_info['resultChecks'] = list(self._local.export_info.get('resultChecks', [])) + checks
            warnings = list(self._local.export_info.get('warnings', ()))
            for result_check in checks:
                if result_check.get('status') == 'not_checked':
                    code = ('ODF_TABLE_CONTENT_NOT_CHECKED' if result_check.get('check') == 'odf_table_text'
                            else 'NATIVE_OUTPUT_NOT_CHECKED')
                    if code not in warnings:
                        warnings.append(code)
            self._local.export_info['warnings'] = tuple(warnings)
        return paths

    def _convert_native(self, source, target, workspace, *, cancel, timeout,
                on_started, output_limit=None, on_document_service=None, on_stage=None):
        self._local.execution_mode = None
        self._local.metrics = None
        self._local.export_info = {}
        adapter_tick = time.monotonic()
        if not self.available:
            raise GuardError('ENGINE_NOT_FOUND', 'LibreOffice не найден.', stage='launch', status=503)
        if cancel.is_set():
            raise GuardError('CANCELLED', 'Конвертация отменена.', stage='launch')
        batch = getattr(self._local, 'batch', None)
        proof = getattr(self._local, 'proof', None) or {}
        if 'com.sun.star.chart2.ChartDocument' in proof.get('documentServices', ()):
            chart_budget = getattr(batch[1], 'chart_timeout', 60) if batch else 60
            timeout = min(timeout, chart_budget) if timeout is not None else chart_budget
        control = (cancel if timeout is None and output_limit is None else
                   _RunControl(cancel, workspace, timeout, output_limit))
        try:
            native = self._registry()
            recipe = native_export_recipe(native, target, getattr(self._local, 'proof', None))
            if recipe:
                self._local.export_info = {'filter': recipe.filter_name, 'warnings': recipe.warnings}
            export_args = {'convert_spec': recipe.cli_spec(target)} if recipe else {}
            # The first requested native export identifies the actual model.
            # Filename hints never veto a file, including renamed/unlisted ones.
            batch = getattr(self._local, 'batch', None)
            self._local.export_info['executionReason'] = (
                'native_filter_unresolved' if not recipe else
                'native_observation_required' if on_document_service else
                'no_native_session' if not batch or not hasattr(batch[1], 'request') else
                'native_session_disabled' if batch[1].disabled else 'native_session')
            if batch and on_document_service is None and hasattr(batch[1],'request') and not batch[1].disabled:
                office, native = batch[1], batch[0]
                proof = getattr(self._local,'proof',None) or {}
                services = proof.get('documentServices', [proof.get('documentService')])
                filter_name = recipe.filter_name if recipe else None
                if filter_name:
                    output = workspace / 'output'
                    output.mkdir(parents=True)
                    request_sent = response_received = False
                    try:
                        startup_tick = time.monotonic()
                        office.ensure_started(control,min(45,timeout) if timeout is not None else 45)
                        startup_ms = round((time.monotonic()-startup_tick)*1000)
                        self._local.execution_mode = 'native_uno'
                        self._local.metrics = {'source':'uno_document', 'workerStartupMs':startup_ms}
                        on_started()
                        request_sent = True
                        result = office.request({'operation':'export','source':str(source.resolve()),
                            'format':target,'output':str(output.resolve()),'exportFilter':filter_name,
                            'exportOptions':recipe.filter_options,
                            'requiredDrawPages':recipe.required_draw_pages,
                            'importFilter':getattr(self._local,'import_filter',None),
                            'expectedImportFilter':getattr(self._local,'import_filter',None),
                            'importOptions':getattr(self._local,'import_options',None),
                            'importTimeout': max(getattr(office, 'import_timeout', 60),
                                min(timeout, max(getattr(office, 'import_timeout', 60),
                                    (proof.get('nativeImportMs') or 0) / 1000 * 3 + 5)))
                                if timeout is not None else getattr(office, 'import_timeout', 60),
                            'services':sorted(native.import_services),'expectedServices':services,
                            'expectedSourceServices':proof.get('sourceContract', {}).get('expectedServices', [])},
                            cancel=control,timeout=timeout,on_stage=on_stage or (lambda stage:None))
                        response_received = True
                        if result.get('nativeCloseInterrupted'):
                            self._local.export_info['warnings'] = tuple(
                                self._local.export_info.get('warnings', ())) + ('NATIVE_CLOSE_INTERRUPTED',)
                        self._local.metrics = {**(clean_timings(result.get('timings')) or
                                                 {'source':'uno_document'}),
                                               'workerStartupMs':startup_ms}
                        if not result.get('ok'):
                            diagnostic=str(result.get('diagnostic','')).replace(str(source.parent.parent.parent),'[local]')
                            code = result.get('code') or 'ENGINE_FAILED'
                            interaction = result.get('interaction')
                            if isinstance(interaction, dict):
                                kind = interaction.get('requestType')
                                number = interaction.get('errorCode')
                                if (isinstance(kind, str) and len(kind) <= 128
                                        and all(c.isalnum() or c in '._' for c in kind)):
                                    diagnostic += '\nnative_interaction:' + kind
                                if type(number) is int:
                                    diagnostic += ':error_code=' + str(number)
                            message = ('LibreOffice запросил дополнительные данные при экспорте. '
                                       'Направление не завершено; остальные задачи продолжаются.'
                                       if code == 'EXPORT_INTERACTION_REQUIRED' else
                                       'Для открытия исходника LibreOffice нужны дополнительные данные. '
                                       'Автоматическая обработка этого файла невозможна.'
                                       if code == 'PREFLIGHT_INTERACTION_REQUIRED' else
                                       'LibreOffice не смог '+('открыть исходник.'
                                       if result.get('stage')=='import' else 'создать выбранный результат.'))
                            raise GuardError(code, message,
                                stage=result.get('stage','engine'),diagnostic=diagnostic)
                        try:
                            return validated_outputs(source, target, output)
                        except LibreOfficeError as error:
                            raise GuardError(error.code, str(error), stage='result') from error
                    except GuardError as error:
                        # A completed error reply has already closed its model.
                        # Keep that healthy worker for the next task. Transport,
                        # timeout/cancel failures still destroy the owned tree.
                        if not response_received:
                            office.recycle()
                        if (request_sent or error.code != 'UNO_SESSION_UNAVAILABLE'
                                or error.stage != 'launch'):
                            raise
                        office.disabled = True
                        output.rmdir()  # No requested export ran; original CLI remains available.
            prepare_profile(workspace / 'profile')
            self._local.execution_mode = 'native_cli'
            filtered = getattr(self._local, 'import_filter', None)
            filter_opts = getattr(self._local, 'import_options', None)
            if filtered and filter_opts:
                filtered = filtered + ':' + filter_opts
            if on_document_service is None or not native.valid:
                return convert_path(source, target, workspace, executable=self.executable,
                                    cancel=control, on_started=on_started,
                                    import_filter=filtered, metrics_sink=self._capture_metrics, **export_args)
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
                                            import_filter=filtered, metrics_sink=self._capture_metrics, **export_args)
                    paths = convert_path(source, target, workspace, executable=self.executable,
                        cancel=control, on_started=on_started, stdout=observation.writer,
                        import_filter=filtered, metrics_sink=self._capture_metrics, **export_args)
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
        finally:
            if self._local.metrics is not None:
                self._local.metrics['adapterMs'] = round((time.monotonic()-adapter_tick)*1000)

    def _capture_metrics(self, value):
        self._local.metrics = clean_timings(value)

