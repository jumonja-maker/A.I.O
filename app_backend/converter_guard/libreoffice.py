"""Native PDF adapter. No UNO service, custom renderer or content rewriting."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import time

from .catalog import PDF_FILTERS, family_for
from .errors import GuardError
from .process import ProcessTree


def find_libreoffice() -> Path | None:
    configured = os.environ.get('AIO_LIBREOFFICE')
    if configured:
        # An explicit setting is authoritative; do not silently run another copy.
        p = Path(configured).expanduser()
        return p.resolve() if p.is_file() else None
    for name in ('soffice.com', 'soffice', 'libreoffice'):
        candidate = shutil.which(name)
        if candidate:
            return Path(candidate).absolute()
    candidates = [Path('/Applications/LibreOffice.app/Contents/MacOS/soffice')]
    for key in ('ProgramFiles', 'ProgramFiles(x86)'):
        if os.environ.get(key):
            candidates.append(Path(os.environ[key]) / 'LibreOffice/program/soffice.com')
    return next((p for p in candidates if p.is_file()), None)


def prepare_profile(profile: Path):
    # Do not blanket-disable active content: the qualified LibreOffice build
    # then omits native Math formula objects. The native QA fixture covers this.
    # Macro prevention and a fresh profile are not an OS document sandbox.
    user = profile / 'user'
    user.mkdir(parents=True)
    (user / 'registrymodifications.xcu').write_text('''<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry">
 <item oor:path="/org.openoffice.Office.Common/Security/Scripting">
  <prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop>
  <prop oor:name="BlockUntrustedReferrerLinks" oor:op="fuse"><value>true</value></prop>
  <prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>
 </item>
</oor:items>
''', encoding='utf-8')


class LibreOfficeAdapter:
    def __init__(self, executable: str | Path | None = None):
        self.executable = Path(executable).absolute() if executable else find_libreoffice()

    @property
    def available(self):
        return bool(self.executable and self.executable.is_file())

    def export_pdf(self, source: Path, output: Path, profile: Path, *,
                   cancel, timeout: float, on_started, output_limit=lambda: float('inf')) -> Path:
        if not self.available:
            raise GuardError('ENGINE_NOT_FOUND', 'LibreOffice не найден.', stage='launch', status=503)
        family = family_for(source.name)
        if family is None:
            raise GuardError('INPUT_NOT_CONNECTED', 'Этот тип файла пока не подключён к Guard.', stage='route')
        if cancel.is_set():
            raise GuardError('CANCELLED', 'Конвертация отменена.', stage='launch')
        output.mkdir(parents=True)
        prepare_profile(profile)
        params = json.dumps({'UseLosslessCompression': {'type': 'boolean', 'value': 'true'},
            'ReduceImageResolution': {'type': 'boolean', 'value': 'false'}}, separators=(',', ':'))
        command = [str(self.executable), '-env:UserInstallation=' + profile.resolve().as_uri(),
            '--headless', '--nologo', '--nodefault', '--norestore',
            '--convert-to', 'pdf:' + PDF_FILTERS[family] + ':' + params,
            '--outdir', str(output.resolve()), str(source.resolve())]
        started = time.monotonic()
        result = output / (source.stem + '.pdf')
        try:
            with ProcessTree(command, cwd=output, on_started=on_started) as tree:
                while tree.process.poll() is None:
                    if cancel.wait(0.1):
                        raise GuardError('CANCELLED', 'Конвертация отменена.', stage='engine')
                    if time.monotonic() - started >= timeout:
                        raise GuardError('ENGINE_TIMEOUT', 'LibreOffice превысил время обработки файла.', stage='engine')
                    if result.exists() and result.stat().st_size > output_limit():
                        raise GuardError('SPOOL_QUOTA', 'Результат превышает квоту временного хранилища.', stage='result', status=507)
                if cancel.is_set():
                    raise GuardError('CANCELLED', 'Конвертация отменена.', stage='engine')
                if tree.process.returncode != 0:
                    raise GuardError('ENGINE_EXIT', 'LibreOffice завершился с кодом ' +
                        str(tree.process.returncode) + '.', stage='engine')
        except OSError as exc:
            raise GuardError('ENGINE_START_FAILED', 'Не удалось запустить отдельный процесс LibreOffice.',
                             stage='launch', status=503) from exc
        if not result.is_file() or result.is_symlink():
            raise GuardError('ENGINE_OUTPUT_MISSING', 'LibreOffice не создал ожидаемый PDF.', stage='result')
        with result.open('rb') as stream:
            header = stream.read(5)
            stream.seek(max(0, result.stat().st_size - 2048))
            trailer = stream.read(2048)
        if header != b'%PDF-' or b'%%EOF' not in trailer:
            raise GuardError('OUTPUT_INVALID', 'Результат не имеет законченной структуры PDF.', stage='result')
        return result
