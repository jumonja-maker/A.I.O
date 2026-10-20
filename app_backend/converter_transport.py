"""Disk ownership and transport for direct LO requests, without conversion policy."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import json
from pathlib import Path
import secrets
import tempfile
import threading
import time
from zipfile import ZipFile, ZIP_STORED

from .libreoffice_catalog import mime_for
from .libreoffice_engine import LibreOfficeError, convert_path


@dataclass
class Request:
    identity: str
    connection: object
    temporary: object
    cancel: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    files: list = field(default_factory=list)
    completed: int = 0
    total: int = 0

    @property
    def root(self):
        return Path(self.temporary.name)


@dataclass
class Result:
    temporary: object
    path: Path
    name: str
    mime: str
    leases: int = 0
    released: bool = False


class ConverterTransport:
    def __init__(self, *, telemetry=None):
        self.telemetry = telemetry
        self.lock = threading.RLock()
        self.active = {}
        self.results = {}
        self.cleanup_pending = []
        self.closed = False

    def begin(self, identity, connection):
        with self.lock:
            if self.closed or identity in self.active:
                raise LibreOfficeError('Задание недоступно.', 'REQUEST_BUSY')
            request = Request(identity, connection, tempfile.TemporaryDirectory(prefix='aio-lo-'))
            self.active[identity] = request
            self._telemetry('job_created', request_id=identity, stage='queue', status='queued')
            return request

    def _telemetry(self, kind, **fields):
        try:
            if self.telemetry is not None:
                self.telemetry.emit(kind, **fields)
        except Exception:
            pass

    def cancel(self, identity):
        with self.lock:
            request = self.active.get(identity)
            if request:
                request.cancel.set()
                self._telemetry('cancel_requested', request_id=identity,
                                stage='cancel', status='cancelled')
                # Polling wakes stalled uploads on cancellation without
                # shutting down their response socket (WinError 10053).
            return bool(request)

    def progress(self, identity):
        with self.lock:
            request = self.active.get(identity)
            if request is None:
                return None
            return json.loads(json.dumps({
                'files': request.files, 'completed': request.completed,
                'total': request.total,
                'percent': 100 * request.completed / request.total if request.total else None
            }))

    def finish(self, request, retained=False):
        # Report the actual terminal snapshot even if the HTTP handler fails
        # while packaging, responding, or retaining the files.
        tasks = [task for row in request.files for task in row['targets']]
        finished = sum(task.get('state') == 'ready' for task in tasks)
        failed = sum(task.get('state') == 'error' for task in tasks)
        outcome = ('cancelled' if request.cancel.is_set() and not retained else
                   'failed' if not retained else
                   'ready' if finished and not failed else
                   'partial' if finished else 'failed')
        self._telemetry('job_finished', request_id=request.identity,
                        stage='complete', status=outcome)
        try:
            if not retained:
                request.temporary.cleanup()
        except OSError:
            with self.lock:
                self.cleanup_pending.append(request.temporary)
        finally:
            with self.lock:
                self.active.pop(request.identity, None)
                request.done.set()

    def execute(self, request, files, ids, formats):
        artifacts = []
        with self.lock:
            request.total = len(files) * len(formats)
            request.files = [{'fileId': identity, 'targets': [
                {'format': target, 'state': 'queued', 'libreOfficeStarted': False}
                for target in formats]} for identity in ids]
        self._telemetry('job_queued', request_id=request.identity, stage='queue', status='queued')
        for file_id in ids:
            for target in formats:
                self._telemetry('task_queued', request_id=request.identity,
                                file_id=file_id, target=target,
                                stage='queue', status='queued')
        for i, source in enumerate(files):
            for j, target in enumerate(formats):
                row = request.files[i]['targets'][j]
                row['state'] = 'running'
                started_at = time.monotonic()
                self._telemetry('task_started', request_id=request.identity,
                                file_id=ids[i], target=target, stage='engine', status='running')
                try:
                    if request.cancel.is_set():
                        raise LibreOfficeError('Конвертация отменена.', 'CANCELLED')

                    def started():
                        with self.lock:
                            row['libreOfficeStarted'] = True
                        self._telemetry('libreoffice_started', request_id=request.identity,
                                        file_id=ids[i], target=target,
                                        stage='launch', status='started')

                    outputs = convert_path(source['path'], target,
                        request.root / 'native' / str(i) / str(j),
                        cancel=request.cancel, on_started=started)
                    output_root = request.root / 'native' / str(i) / str(j) / 'output'
                    for path in outputs:
                        archive_path = f'{i + 1:03d}_{target}/' + path.relative_to(output_root).as_posix()
                        artifacts.append((archive_path, path))
                    row['state'] = 'ready'
                    row['warnings'] = source['warnings']
                    self._telemetry('result_created', request_id=request.identity,
                                    file_id=ids[i], target=target, stage='result', status='ready')
                    self._telemetry('task_succeeded', request_id=request.identity,
                                    file_id=ids[i], target=target, stage='result',
                                    status='ready',
                                    duration_ms=round((time.monotonic()-started_at)*1000))
                except LibreOfficeError as exc:
                    row.update(state='error', code=exc.code, error=str(exc))
                    self._telemetry('task_cancelled' if exc.code == 'CANCELLED' else 'task_failed',
                                    request_id=request.identity, file_id=ids[i],
                                    target=target, stage='engine', reason_code=exc.code,
                                    status='cancelled' if exc.code == 'CANCELLED' else 'failed',
                                    duration_ms=round((time.monotonic()-started_at)*1000))
                except OSError:
                    row.update(state='error', code='STORAGE_ERROR',
                               error='Ошибка чтения или записи файла. Проверьте свободное место и права доступа.')
                    self._telemetry('task_failed', request_id=request.identity,
                                    file_id=ids[i], target=target, stage='storage',
                                    reason_code='STORAGE_ERROR', status='failed',
                                    duration_ms=round((time.monotonic()-started_at)*1000))
                finally:
                    with self.lock:
                        request.completed += 1
        report = self.progress(request.identity)
        report['succeeded'] = sum(t['state'] == 'ready' for f in report['files'] for t in f['targets'])
        report['failed'] = report['total'] - report['succeeded']
        if not artifacts:
            error = LibreOfficeError('Конвертация отменена.' if request.cancel.is_set()
                                    else 'LibreOffice не создал ни одного результата.',
                                    'CANCELLED' if request.cancel.is_set() else 'CONVERSION_FAILED')
            error.batch_report = report
            raise error
        if len(artifacts) == 1 and not report['failed']:
            path = artifacts[0][1]
            name, mime = path.name, mime_for(path.suffix.lower().lstrip('.'))
        else:
            path = request.root / 'Конвертированные_файлы.zip'
            self._telemetry('archive_started', request_id=request.identity,
                            stage='packaging', status='started')
            with ZipFile(path, 'w', compression=ZIP_STORED, allowZip64=True) as archive:
                for archive_name, output in artifacts:
                    archive.write(output, archive_name)
                if report['failed']:
                    archive.writestr('AIO-conversion-report.json',
                                     json.dumps(report, ensure_ascii=False, indent=2).encode())
            name, mime = path.name, 'application/zip'
            self._telemetry('archive_ready', request_id=request.identity,
                            stage='bundle', status='ready')
        ticket = secrets.token_urlsafe(32)
        with self.lock:
            if self.closed:
                raise LibreOfficeError('Программа закрывается.', 'CANCELLED')
            self.results[ticket] = Result(request.temporary, path, name, mime)
        return {'ok': True, 'result': {'name': name, 'mime': mime, 'size': path.stat().st_size,
                'ticket': ticket, 'downloadUrl': '/api/converter/result?ticket=' + ticket,
                'targetReport': report, 'formatIds': formats},
                'message': ('Готово преобразований: ' + str(report['succeeded']) +
                            '. Ошибок: ' + str(report['failed']) + '. Можно сохранить результат.')}

    @contextmanager
    def lease(self, ticket):
        with self.lock:
            result = self.results.get(ticket)
            if result is None or result.released:
                raise LibreOfficeError('Результат недоступен.', 'RESULT_NOT_FOUND')
            result.leases += 1
        try:
            yield result
        finally:
            with self.lock:
                result.leases -= 1
                if result.released and not result.leases:
                    self.release(ticket)

    def release(self, ticket):
        with self.lock:
            result = self.results.get(ticket)
            if result:
                result.released = True
                if not result.leases:
                    try:
                        result.temporary.cleanup()
                    except OSError:
                        return False
                    self.results.pop(ticket, None)
                else:
                    return False
            return True

    def close(self):
        with self.lock:
            self.closed = True
            active = list(self.active.values())
            tickets = list(self.results)
        for request in active:
            request.cancel.set()
        for request in active:
            request.done.wait(3)
        for ticket in tickets:
            self.release(ticket)
        with self.lock:
            pending = list(self.cleanup_pending)
        for temporary in pending:
            try:
                temporary.cleanup()
            except OSError:
                continue
            with self.lock:
                self.cleanup_pending.remove(temporary)
