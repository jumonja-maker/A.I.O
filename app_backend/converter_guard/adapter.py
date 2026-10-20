"""Use the existing direct native adapter. No second conversion engine."""
from contextlib import ExitStack
from pathlib import Path
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

    @property
    def available(self):
        return bool(self.executable and self.executable.is_file())

    def capabilities(self, files, *, proofs=None):
        available = self.available
        native = registry(self.executable)
        formats = []
        for ext, category, description, mime, registered in catalog(self.executable):
            states = {f['id']: (native.hint(f['name'], ext) if proofs is None else
                               native.observed_decision(ext, proofs.get(f['id']))) for f in files}
            compatible = [i for i, state in states.items() if state == 'registered']
            unknown = [i for i, state in states.items() if state == 'undetermined']
            formats.append({'id': ext, 'label': ext.upper(), 'category': category,
                'description': description, 'mime': mime,
                'registeredExport': ext in native.exports if native.valid else registered,
                'enabled': available and (not files or bool(compatible or unknown)),
                # A filename recommendation cannot veto the native model of
                # renamed/unknown content. Selection and routing are separate.
                'nativeAttemptAvailable': available and (proofs is None or not files or bool(compatible or unknown)),
                'selectableFileIds': [f['id'] for f in files if proofs is None or states[f['id']] != 'unavailable'] if available else [],
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
                'message': ('Для части файлов направления не подтверждены. Их можно выбрать вручную; '
                            '«Выбрать всё» включает только подтверждённые направления.'
                            if proofs is not None and any(not p or p.get('state') != 'detected'
                                                          for p in proofs.values()) else ''),
                'qualityCompared': False}

    def preflight(self, source, workspace, *, cancel, timeout):
        from .preflight import inspect_source
        if not self.available:
            return {'state': 'undetermined', 'code': 'ENGINE_NOT_FOUND'}
        return inspect_source(self.executable, source, workspace, cancel=cancel,
                              timeout=timeout, prepare_profile=prepare_profile)

    def route_decision(self, target, proof):
        return registry(self.executable).observed_decision(target, proof)

    def observation_needed(self, proof):
        native = registry(self.executable)
        return native.valid and not (proof and proof.get('registryFingerprint') == native.fingerprint
            and (proof.get('basis') == 'native_selected_export_filter' or
                 proof.get('basis') in ('native_preflight_document_kind', 'native_preflight_uno_type', 'native_preflight_uno_graphic')
                 and proof.get('state') == 'detected' and proof.get('documentServices')))

    def convert(self, source, target, workspace, *, cancel, timeout,
                on_started, output_limit=None, on_document_service=None):
        if not self.available:
            raise GuardError('ENGINE_NOT_FOUND', 'LibreOffice не найден.', stage='launch', status=503)
        if cancel.is_set():
            raise GuardError('CANCELLED', 'Конвертация отменена.', stage='launch')
        prepare_profile(workspace / 'profile')
        control = (cancel if timeout is None and output_limit is None else
                   _RunControl(cancel, workspace, timeout, output_limit))
        try:
            # The first requested native export identifies the actual model.
            # Filename hints never veto a file, including renamed/unlisted ones.
            native = registry(self.executable)
            if on_document_service is None or not native.valid:
                return convert_path(source, target, workspace, executable=self.executable,
                                    cancel=control, on_started=on_started)
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
                                            cancel=control, on_started=on_started)
                    paths = convert_path(source, target, workspace, executable=self.executable,
                        cancel=control, on_started=on_started, stdout=observation.writer)
                completed = True
                return paths
            finally:
                proof = native.proof(observation.selected_filter(), target) if completed else None
                if proof:
                    on_document_service(proof)
        except LibreOfficeError as error:
            raise GuardError(error.code, str(error), stage='engine') from error
        except OSError as error:
            raise GuardError('STORAGE_ERROR', 'Ошибка временного хранилища Guard.',
                             stage='storage', status=507) from error
