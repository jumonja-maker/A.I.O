"""Use the existing direct native adapter. No second conversion engine."""
from contextlib import ExitStack, contextmanager
import copy
import os
from pathlib import Path
import secrets
import shutil
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
from .postconditions import SourceEvidence, check_native_output
from .source_contract import inspect_source_contract, source_import_hint, source_model_matches


def _source_identity(source):
    """Local batch identity, not a persistent content-compatibility proof."""
    source = Path(source)
    try:
        if source.is_symlink() or not source.is_file():
            return None
        stamp = source.stat()
        return (str(source.resolve(strict=True)), stamp.st_dev, stamp.st_ino,
                stamp.st_size, stamp.st_mtime_ns, stamp.st_ctime_ns)
    except OSError:
        return None



def _native_dbf_structure(path):
    """Bounded native DBF container check, not a replacement DBF decoder."""
    try:
        with Path(path).open('rb') as source:
            header = source.read(32)
        if len(header) != 32 or header[0] not in (0x03, 0x04, 0x30, 0x83, 0x8b, 0xf5):
            return False
        rows = int.from_bytes(header[4:8], 'little')
        offset = int.from_bytes(header[8:10], 'little')
        stride = int.from_bytes(header[10:12], 'little')
        size = Path(path).stat().st_size
        return offset >= 33 and stride >= 1 and rows <= (size - offset) // stride
    except (OSError, ValueError):
        return False


class _PreparedSourceCache:
    """Keep one private immutable import copy; never reuse a live LO model.

    Expensive ODF column insertion / EPUB spine staging is target-independent.
    The previous file's staging is discarded before preparing another source.
    A locked private copy is left to its owning batch's workspace cleanup.
    Original and prepared-file identities are checked on every reuse; nothing
    survives its owning batch or authorizes a new conversion route.
    """
    def __init__(self, workspace):
        self.workspace = Path(workspace)
        self.current = self.entry = None

    def _clear(self):
        current = self.current
        # Detach first: a Windows scanner / native file lock must not poison
        # every later source or replace the original preparation exception.
        # Only this helper's generated directory is attempted here. Results
        # and source files live elsewhere; the batch owns all deferred copies.
        self.entry = self.current = None
        if current is not None:
            try:
                shutil.rmtree(current)
            except OSError:
                pass

    def prepare(self, source, *, epub, cancel):
        from .odf_table_import import prepare_odf_table_source
        source = Path(source)
        if cancel.is_set():
            raise GuardError('CANCELLED', 'Подготовка исходника отменена.', stage='import')
        identity = _source_identity(source)
        if identity is not None and self.entry is not None:
            old_identity, old_epub, prepared, prepared_identity, summary = self.entry
            if (identity == old_identity and epub == old_epub and
                    _source_identity(prepared) == prepared_identity):
                return prepared, copy.deepcopy(summary)
        self._clear()
        self.current = self.workspace / secrets.token_hex(12)
        try:
            if epub:
                from .epub_import import prepare_epub
                staged = prepare_epub(source, self.current, cancel=cancel)
                prepared, summary = staged.source, staged.summary
            else:
                prepared, summary = prepare_odf_table_source(source, self.current, cancel=cancel)
            if identity is not None and _source_identity(source) != identity:
                raise GuardError('SOURCE_CHANGED_DURING_PREPARATION',
                    'Исходник изменился во время подготовки. Повторите проверку файла.', stage='import')
            prepared_identity = _source_identity(prepared)
            if identity is not None and prepared_identity is not None:
                self.entry = (identity, epub, prepared, prepared_identity, copy.deepcopy(summary))
            return prepared, summary
        except BaseException:
            self._clear()
            raise


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


class _LegacySheetControl:
    """Apply an explicit spool quota to Calc's private legacy staging files."""
    def __init__(self, control, workspace):
        self.control, self.workspace = control, Path(workspace)
        self.deadline = getattr(control, 'deadline', None)
        self.output_limit = getattr(control, 'output_limit', None)
        self.next_scan = 0

    def is_set(self):
        if self.control.is_set():
            return True
        now = time.monotonic()
        if self.output_limit is not None and now >= self.next_scan:
            self.next_scan = now + .5
            roots = [self.workspace / 'output',
                     self.workspace / 'legacy-native-snapshot' / 'output',
                     self.workspace / 'legacy-sheet-inputs']
            roots.extend(path / 'output' for path in self.workspace.glob('legacy-export-*')
                         if path.is_dir() and not path.is_symlink())
            used = 0
            for root in roots:
                if root.is_symlink():
                    continue
                for path in root.rglob('*'):
                    if path.is_file() and not path.is_symlink():
                        used += path.stat().st_size
            if used > self.output_limit():
                raise GuardError('SPOOL_QUOTA', 'Превышена настроенная квота временных файлов.',
                                 stage='result', status=507)
        return False

    def check_now(self):
        self.next_scan = 0
        return self.is_set()

    def wait(self, seconds):
        self.control.wait(seconds)
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
        previous_preparation = getattr(self._local, 'source_preparation', None)
        previous_evidence = getattr(self._local, 'source_evidence', None)
        factory = (UnoSession if os.name == 'nt' and os.environ.get('AIO_GUARD_NATIVE_SESSION','1') != '0'
                   and (self.executable.parent/'python.exe').is_file()
                   else OfficeSession)
        with factory(self.executable, workspace, prepare_profile, max_tasks) as office:
            office.import_timeout, office.close_timeout = import_timeout, close_timeout
            office.chart_timeout = chart_timeout
            self._local.batch = (registry(self.executable), office)
            self._local.source_preparation = _PreparedSourceCache(Path(workspace) / 'source-preparation')
            self._local.source_evidence = None
            try:
                yield office
            finally:
                self._local.batch = previous
                self._local.proof, self._local.import_filter, self._local.import_options = previous_source
                self._local.source_preparation = previous_preparation
                self._local.source_evidence = previous_evidence

    def _registry(self):
        batch = getattr(self._local, 'batch', None)
        return batch[0] if batch else registry(self.executable)

    @staticmethod
    def _route_with_registry(native, target, proof):
        """Decide from one immutable installed-registry snapshot, no file I/O."""
        if (proof and proof.get('state') == 'detected'
                and proof.get('registryFingerprint') == native.fingerprint
                and isinstance(proof.get('mediaProfile'), dict)
                and target in proof.get('qtMediaTargets', ())):
            return 'registered'
        return native.observed_decision(target, proof)

    def _bound_route_decider(self, native):
        # Preserve explicit test/integration subclasses which override route_decision.
        # The standard decision must not re-stat every LibreOffice XCD/XCU file
        # for every source x target pair in a 510 x 85 matrix.
        chosen = self.route_decision
        if getattr(chosen, '__func__', None) is LibreOfficeAdapter.route_decision:
            return lambda target, proof: self._route_with_registry(native, target, proof)
        return chosen

    def route_decider(self):
        # Guard.start() can plan 14,000+ target pairs. Pin one registry identity
        # across the plan; the observed source proof still verifies fingerprint.
        return self._bound_route_decider(self._registry())

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

    def capabilities(self, files, *, proofs=None, cancel=None, on_progress=None):
        """Build verified routes from one native snapshot; permit cancellation.

        The 510 x 85 matrix is metadata-only. Check the owned cancellation
        lease while walking it; a finished import count is not a commit of
        the preflight result. Optional progress reports only format counts.
        """
        def check_cancel():
            if cancel is not None and cancel.is_set():
                raise GuardError('CANCELLED', 'Проверка направлений отменена.',
                                 stage='preflight')

        check_cancel()
        available = self.available
        native = self._registry()
        decision = self._bound_route_decider(native)
        check_cancel()
        formats = []
        entries = list(catalog(self.executable))
        known = {row[0] for row in entries}
        media_targets = set(t for proof in (proofs or {}).values() if proof
                            for t in proof.get('qtMediaTargets', ()))
        live_targets = set(t for proof in (proofs or {}).values() if proof
                           for t in proof.get('nativeExportFilters', ())
                           if native.runtime_export_filter(t, proof))
        for ext in sorted((media_targets | live_targets) - known):
            entries.append((ext, 'documents' if ext == 'txt' else 'images',
                            'TXT — нативный текст UTF-8' if ext == 'txt' else
                            ext.upper() + ' — сохранение кадров Qt',
                            {'apng': 'image/apng', 'tif': 'image/tiff', 'txt': 'text/plain',
                             'tiff': 'image/tiff'}.get(ext, 'image/' + ext), True))
        total_formats = len(entries)
        if on_progress is not None:
            on_progress(0, total_formats)
        for format_index, (ext, category, description, mime, registered) in enumerate(entries):
            check_cancel()
            if ext == 'xml':
                description = 'XML — нативное представление документа'
            states = {}
            for file_index, source in enumerate(files):
                if file_index % 32 == 0:
                    check_cancel()
                identity = source['id']
                states[identity] = (native.hint(source['name'], ext) if proofs is None
                                    else decision(ext, proofs.get(identity)))
            compatible = [identity for identity, state in states.items() if state == 'registered']
            unknown = [identity for identity, state in states.items() if state == 'undetermined']
            # A large corpus may contain 510 files and dozens of formats.
            # Don't evaluate the same reason (and its native service lookups)
            # twice per file/format just to decide whether to include it.
            reasons = {}
            if proofs is not None:
                for file_index, file in enumerate(files):
                    if file_index % 32 == 0:
                        check_cancel()
                    reason = native.route_reason(ext, proofs.get(file['id']))
                    if reason:
                        reasons[file['id']] = reason
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
                    'epub': 'EPUB без нативного импортёра открывается через HTML в порядке OPF spine. При смене формата вёрстка книги может измениться; активные, зашифрованные и фиксированные макеты требуют отдельной поддержки.',
                    'xml': 'Для Writer/Calc/Impress/Draw используется установленный штатный фильтр Flat OpenDocument XML. Это не Excel/Word 2003 XML; точный фильтр указан в отчёте.'
                }).get(ext, 'Проверка Guard подтверждает маршрут, но не гарантирует отсутствие смысловых или визуальных потерь.')}],
                'unavailableReasons': reasons,
                'registeredExport': (ext in native.exports or ext in media_targets or ext in live_targets or
                                     ext == 'svm' and bool(compatible)) if native.valid else registered,
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
            if on_progress is not None:
                on_progress(format_index + 1, total_formats)
        check_cancel()
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
        from .odf_table_import import prepare_odf_table_source
        from .qt_media import media_preflight
        if not self.available:
            return {'state': 'undetermined', 'code': 'ENGINE_NOT_FOUND'}
        control = _RunControl(cancel, workspace, timeout, None)
        try:
            media = media_preflight(source, cancel=control)
        except GuardError as error:
            if error.code not in ('MEDIA_QT_UNAVAILABLE', 'MEDIA_CANVAS_BUDGET'):
                raise
            media = None
        prepared, preparation = prepare_odf_table_source(source, workspace, cancel=control)
        remaining = (None if control.deadline is None else
                     max(.001, control.deadline - time.monotonic()))
        result = self._preflight_native(prepared, workspace / 'native', cancel=control,
                                        timeout=remaining)
        if preparation:
            result = {**result, 'sourcePreparation': preparation}
            if prepared != source:
                result['sourceContract'] = inspect_source_contract(source, cancel=control)
        if media and media['targets']:
            if result.get('state') != 'detected':
                result = {'state': 'detected', 'basis': 'qt_media_content',
                          'nativePreflightCode': result.get('code'),
                          'registryFingerprint': self._registry().fingerprint,
                          'exported': False}
            result = {**result, 'mediaProfile': media['source'],
                      'qtMediaTargets': media['targets'], 'mediaDecoder': media['decoder']}
        return result

    def _preflight_native(self, source, workspace, *, cancel, timeout):
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
        if contract.get('contentClass') == 'epub_package' and not native.imports.get('epub'):
            # EPUB has an export filter but normally no native import filter.
            # Decode its declared reading order into one private HTML source,
            # then require the real installed HTML importer to load that model.
            from .epub_import import prepare_epub, WRITER, WEB
            prepared = prepare_epub(source, workspace / 'epub-package', cancel=cancel)
            result = self.preflight(prepared.source, workspace / 'native-preflight',
                                    cancel=cancel, timeout=timeout)
            return {**result, 'sourceContract': {**contract, 'expectedServices': [WRITER, WEB]},
                    'sourcePreparation': prepared.summary, 'exported': False}
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
                        'nativeGraphicExports': result.get('nativeGraphicExports', []),
                        'nativeExportFilters': result.get('nativeExportFilters', {}),
                        'nativeExportInspection': result.get('nativeExportInspection', {}),
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
                office_session=office if os.name == 'nt' and not hasattr(office,'request') else None,
                _source_inspection=(contract, hint))
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
        # Standalone callers still consult the installed registry on demand;
        # bulk operations use a pinned per-operation snapshot.
        return self._route_with_registry(self._registry(), target, proof)

    def observation_needed(self, proof):
        native = self._registry()
        if (proof and proof.get('basis') == 'qt_media_content'
                and proof.get('registryFingerprint') == native.fingerprint):
            return False
        return native.valid and not (proof and proof.get('registryFingerprint') == native.fingerprint
            and (proof.get('basis') == 'native_selected_export_filter' or
                 proof.get('basis') in ('native_preflight_document_kind', 'native_preflight_uno_type',
                                        'native_preflight_uno_graphic', 'native_preflight_uno_document')
                 and proof.get('state') == 'detected' and proof.get('documentServices')))

    def convert(self, source, target, workspace, *, cancel, timeout,
                on_started, output_limit=None, on_document_service=None, on_stage=None):
        # Preparation, identity-copy and media validation can fail before the
        # native branch is entered. The queue captures metrics in its finally
        # block, so clear the previous target's provenance before any work.
        self._local.execution_mode = None
        self._local.metrics = None
        self._local.export_info = {}
        from .media_fidelity import copy_media_identity
        from .native_identity import copy_chart_identity
        from .native_metafile import native_metafile_target, wrap_native_metafiles
        from .odf_table_import import prepare_odf_table_source
        from .qt_media import convert_media_frames, convert_document_pages
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
            if paths is None and target in proof.get('qtMediaTargets', ()):
                tick = time.monotonic()
                paths = convert_document_pages(source, workspace / 'output', target,
                    cancel=identity_control, on_started=on_started)
                if paths is None:
                    paths = convert_media_frames(source, workspace / 'output', target,
                        cancel=identity_control, on_started=on_started)
                if paths:
                    self._local.execution_mode = 'qt_media_frames'
                    self._local.metrics = {'source': 'qt_media',
                        'adapterMs': round((time.monotonic() - tick) * 1000)}
                    self._local.export_info = {'executionReason': 'qt_media_frames',
                        'warnings': ('QT_PDF_RASTER_144_DPI',) if
                            proof.get('mediaProfile', {}).get('format') == 'pdf' else (),
                        'mediaCodec': 'existing_application_Qt', 'resultChecks': []}
        if paths is None:
            native_source = source
            preparation = None
            source_preparation = getattr(self._local, 'source_preparation', None)
            epub = proof.get('sourcePreparation', {}).get('kind') == 'epub_spine_html'
            if source_preparation is not None:
                native_source, preparation = source_preparation.prepare(
                    source, epub=epub, cancel=identity_control)
            elif epub:
                from .epub_import import prepare_epub
                prepared = prepare_epub(source, workspace / 'epub-package', cancel=identity_control)
                native_source, preparation = prepared.source, prepared.summary
            else:
                native_source, preparation = prepare_odf_table_source(
                    source, workspace, cancel=identity_control)
            plain = native_metafile_target(self._registry(), target, proof)
            remaining = (None if timeout is None else
                         max(.001, identity_control.deadline - time.monotonic()))
            batch = getattr(self._local, 'batch', None)
            live_uno = (batch and hasattr(batch[1], 'request') and not batch[1].disabled)
            if (target in ('dbf', 'dif') and not live_uno and
                    proof.get('documentServices') == ['com.sun.star.sheet.SpreadsheetDocument']):
                paths = self._convert_legacy_sheets(native_source, target, workspace,
                    control=identity_control, on_started=on_started, on_stage=on_stage)
            else:
                paths = self._convert_native(native_source, plain or target, workspace,
                    cancel=identity_control, timeout=remaining,
                    on_started=on_started, output_limit=output_limit,
                    on_document_service=on_document_service, on_stage=on_stage)
            if plain:
                paths, metafile_check = wrap_native_metafiles(paths, target, cancel=identity_control)
                self._local.export_info['executionReason'] = 'native_plain_metafile_gzip'
                self._local.export_info['resultChecks'] = [metafile_check]
            if preparation:
                self._local.export_info['sourcePreparation'] = preparation
                self._local.export_info['warnings'] = tuple(dict.fromkeys(
                    (*self._local.export_info.get('warnings', ()),
                     *preparation.get('warnings', ()),
                     *(['ODF_IMPLICIT_COLUMNS_EXPLICIT'] if preparation.get('state') == 'prepared'
                       and preparation.get('kind') == 'odf_explicit_table_columns' else []))))
        source_evidence = None
        if getattr(self._local, 'batch', None) is not None:
            source_evidence = getattr(self._local, 'source_evidence', None)
            if source_evidence is None:
                source_evidence = SourceEvidence(source)
                self._local.source_evidence = source_evidence
        check = check_native_output(paths, target, source, cancel=identity_control,
                                    source_evidence=source_evidence)
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

    def _convert_legacy_sheets(self, source, target, workspace, *, control,
                               on_started, on_stage):
        """Use native FODS cached values to preserve every CLI Calc sheet."""
        from .legacy_sheets import prepare_calc_sheet_snapshots
        control = _LegacySheetControl(control, workspace)
        native = self._registry()
        flat = 'OpenDocument Spreadsheet Flat XML'
        item = native.filters.get(flat)
        if not item or item[0] != 'com.sun.star.sheet.SpreadsheetDocument' or not {'IMPORT', 'EXPORT'} <= item[2]:
            raise GuardError('LEGACY_SHEET_SNAPSHOT_UNAVAILABLE',
                'В этой установке отсутствует штатный промежуточный формат для сохранения всех листов.',
                stage='route')
        started = time.monotonic()
        metrics = []
        def export(path, fmt, work):
            control.is_set()
            deadline = getattr(control, 'deadline', None)
            remaining = None if deadline is None else max(.001, deadline - time.monotonic())
            outputs = self._convert_native(path, fmt, work, cancel=control, timeout=remaining,
                on_started=on_started, on_stage=on_stage, on_document_service=None)
            control.check_now()
            if self._local.metrics:
                metrics.append(dict(self._local.metrics))
            return outputs
        initial = export(source, 'fods', workspace / 'legacy-native-snapshot')
        fods = next((p for p in initial if p.suffix.lower() == '.fods'), None)
        if fods is None:
            raise GuardError('ENGINE_OUTPUT_MISSING', 'Не создан полный снимок листов Calc.', stage='result')
        snapshots, check = prepare_calc_sheet_snapshots(fods, workspace / 'legacy-sheet-inputs',
                                                       stem=source.stem, cancel=control)
        control.check_now()
        saved = (getattr(self._local, 'import_filter', None),
                 getattr(self._local, 'import_options', None))
        output = workspace / 'output'
        output.mkdir(parents=True, exist_ok=False)
        paths = []
        try:
            self._local.import_filter, self._local.import_options = flat, None
            for index, snapshot in enumerate(snapshots):
                for result in export(snapshot, target, workspace / ('legacy-export-%04d' % index)):
                    control.is_set()
                    destination = output / result.name
                    if destination.exists():
                        raise GuardError('ENGINE_OUTPUT_COLLISION', 'Совпали имена результатов листов.', stage='result')
                    result.replace(destination)
                    paths.append(destination)
        finally:
            self._local.import_filter, self._local.import_options = saved
        self._local.execution_mode = 'native_cli'
        self._local.metrics = {'source': 'cli_process',
            'adapterMs': round((time.monotonic() - started) * 1000),
            'processMs': sum(m.get('processMs', 0) for m in metrics)}
        self._local.export_info = {'filter': native.export_filter(target,
            ['com.sun.star.sheet.SpreadsheetDocument']),
            'executionReason': 'native_cli_all_calc_sheets',
            'warnings': ('NATIVE_TABLE_FORMAT_LIMITS',), 'resultChecks': [check]}
        control.check_now()
        return paths

    def _recover_missing_csv_dbf(self, source, workspace, control, recipe, native):
        """One native CLI recovery for a *verified* Calc CSV -> dBase UNO miss.

        Never guess a filter, retry an interaction, overwrite an existing
        product, or authorize an unsupported route. On any uncertainty the
        caller reports its original ENGINE_OUTPUT_MISSING failure. The cold
        CLI produces the DBF; this helper only checks and links those bytes.
        """
        proof = getattr(self._local, 'proof', None) or {}
        selected = getattr(self._local, 'import_filter', None)
        options = getattr(self._local, 'import_options', None)
        output = Path(workspace) / 'output'
        if (recipe is None or recipe.filter_name != 'dBase'
                or proof.get('state') != 'detected'
                or proof.get('registryFingerprint') != native.fingerprint
                or 'com.sun.star.sheet.SpreadsheetDocument' not in proof.get('documentServices', ())
                or selected != 'Text - txt - csv (StarCalc)'
                or proof.get('selectedImportFilter') != selected
                or not options or Path(source).suffix.lower() != '.csv'
                or not output.is_dir() or any(output.iterdir())):
            return None
        if control.is_set():
            return None
        recovery = Path(workspace) / 'dbf-native-cli-recovery'
        import_filter = selected + ':' + options
        try:
            prepare_profile(recovery / 'profile')
            native_files = convert_path(source, 'dbf', recovery, executable=self.executable,
                cancel=control, on_started=lambda: None,
                convert_spec=recipe.cli_spec('dbf'), import_filter=import_filter,
                metrics_sink=self._capture_metrics)
            if (len(native_files) != 1 or native_files[0].suffix.lower() != '.dbf'
                    or not _native_dbf_structure(native_files[0]) or control.is_set()
                    or any(output.iterdir())):
                return None
            candidate = output / (Path(source).stem + '.dbf')
            # Hardlink is an exclusive publication on the same owned volume:
            # it fails if the original UNO exporter created a late result.
            os.link(native_files[0], candidate)
            if not _native_dbf_structure(candidate):
                candidate.unlink(missing_ok=True)
                return None
            confirmed = validated_outputs(source, 'dbf', output)
            self._local.execution_mode = 'native_cli'
            self._local.export_info['executionReason'] = 'native_cli_dbase_recovery'
            self._local.export_info['warnings'] = tuple(dict.fromkeys(
                (*self._local.export_info.get('warnings', ()), 'NATIVE_DBF_CLI_RECOVERY')))
            self._local.export_info['resultChecks'] = [{
                'check': 'dbf_native_container', 'status': 'checked_subset',
                'qualityCompared': False, 'recovery': 'native_cli_one_attempt'}]
            return confirmed
        except (LibreOfficeError, OSError):
            return None

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
                            'graphicExport': 'svm' if filter_name == 'svm_Export' else None,
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
                        if 'NATIVE_DBF_MIXED_COLUMN_TEXT' in result.get('warnings', ()):
                            self._local.export_info['warnings'] = tuple(
                                self._local.export_info.get('warnings', ())) + ('NATIVE_DBF_MIXED_COLUMN_TEXT',)
                        if result.get('nativeCloseInterrupted'):
                            self._local.export_info['warnings'] = tuple(
                                self._local.export_info.get('warnings', ())) + ('NATIVE_CLOSE_INTERRUPTED',)
                        self._local.metrics = {**(clean_timings(result.get('timings')) or
                                                 {'source':'uno_document'}),
                                               'workerStartupMs':startup_ms}
                        if (not result.get('ok') and result.get('code') == 'ENGINE_OUTPUT_MISSING'
                                and target == 'dbf' and filter_name == 'dBase'):
                            recovered = self._recover_missing_csv_dbf(
                                source, workspace, control, recipe, native)
                            if recovered is not None:
                                return recovered
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

