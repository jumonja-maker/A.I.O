"""Use the existing direct native adapter. No second conversion engine."""
from pathlib import Path
import time

from ..libreoffice_catalog import catalog
from ..libreoffice_engine import LibreOfficeError, convert_path, engine_binary
from .errors import GuardError


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

    def capabilities(self, files):
        ids = [item['id'] for item in files]
        available = self.available
        formats = [{'id': ext, 'label': ext.upper(), 'category': category,
                    'description': description, 'mime': mime,
                    'registeredExport': registered, 'enabled': available,
                    'compatibleFileIds': ids if available else [],
                    'compatibility': 'native_decision'}
                   for ext, category, description, mime, registered in catalog(self.executable)]
        return {'files': [{**item, 'compatibility': 'native_decision'} for item in files],
                'formats': formats, 'acceptedExtensions': '',
                'compatibility': 'native_decision', 'available': available}

    def convert(self, source, target, workspace, *, cancel, timeout,
                on_started, output_limit=None):
        if not self.available:
            raise GuardError('ENGINE_NOT_FOUND', 'LibreOffice не найден.', stage='launch', status=503)
        if cancel.is_set():
            raise GuardError('CANCELLED', 'Конвертация отменена.', stage='launch')
        prepare_profile(workspace / 'profile')
        control = (cancel if timeout is None and output_limit is None else
                   _RunControl(cancel, workspace, timeout, output_limit))
        try:
            # No family mapping, custom renderer, signature check or inferred
            # lossless preset. This is the exact native target extension.
            return convert_path(source, target, workspace, executable=self.executable,
                                cancel=control, on_started=on_started)
        except LibreOfficeError as error:
            raise GuardError(error.code, str(error), stage='engine') from error
        except OSError as error:
            raise GuardError('STORAGE_ERROR', 'Ошибка временного хранилища Guard.',
                             stage='storage', status=507) from error
