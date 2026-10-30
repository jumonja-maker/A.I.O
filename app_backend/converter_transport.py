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
from zipfile import ZipFile, ZipInfo, ZIP_STORED

from .libreoffice_catalog import mime_for
from .libreoffice_engine import LibreOfficeError, convert_path

ARCHIVE_CHUNK = 128 * 1024


@dataclass
class Request:
    identity: str
    connection: object
    temporary: object
    cancel: threading.Event = field(default_factory=threading.Event)
    packaging_cancel: threading.Event = field(default_factory=threading.Event)
    packaging: bool = False
    published: bool = False  # No cancellation accepted after ticket publication.
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
    request_id: str = ''
    size: int = 0
    target_report: dict | None = None
    format_ids: tuple = ()
    leases: int = 0
    released: bool = False
    cleaning: bool = False


class ConverterTransport:
    def __init__(self):
        self.lock = threading.RLock()
        self.active = {}
        self.results = {}
        self.results_by_request = {}
        self.cleanup_pending = []
        self.closed = False
        self._cleanup_timer = None
        self._cleanup_retries = 0

    def begin(self, identity, connection):
        # Retry only already-released storage. Unsaved tickets are never
        # eligible for automatic cleanup.
        self.retry_cleanup()
        with self.lock:
            if self.closed or identity in self.active or identity in self.results_by_request:
                raise LibreOfficeError('Задание недоступно.', 'REQUEST_BUSY')
            request = Request(identity, connection, tempfile.TemporaryDirectory(prefix='aio-lo-'))
            self.active[identity] = request
            return request


    def recover(self, identity):
        """Recover a previously retained ticket after a lost HTTP response.

        The caller must already know the 32-hex request identity and the
        session token. Never read unrelated jobs or visit the recovery spool.
        """
        with self.lock:
            ticket = self.results_by_request.get(identity)
            result = self.results.get(ticket)
            if result is not None and not result.released:
                return {'state': 'ready', 'requestId': identity,
                    'result': {'name': result.name, 'mime': result.mime,
                        'size': result.size, 'ticket': ticket,
                        'downloadUrl': '/api/converter/result?ticket=' + ticket,
                        'targetReport': result.target_report,
                        'formatIds': list(result.format_ids)}}
            if identity in self.active:
                return {'state': 'running', 'requestId': identity}
        return {'state': 'not_found', 'requestId': identity}

    def cancel(self, identity):
        with self.lock:
            request = self.active.get(identity)
            if request is None or request.published:
                return False
            request.cancel.set()
            if request.packaging:
                request.packaging_cancel.set()
            # Polling wakes stalled uploads on cancellation without
            # shutting down their response socket (WinError 10053).
            return True

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
        try:
            if not retained:
                request.temporary.cleanup()
        except OSError:
            with self.lock:
                self.cleanup_pending.append(request.temporary)
            self._schedule_cleanup()
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
        for i, source in enumerate(files):
            for j, target in enumerate(formats):
                row = request.files[i]['targets'][j]
                row['state'] = 'running'
                try:
                    if request.cancel.is_set():
                        raise LibreOfficeError('Конвертация отменена.', 'CANCELLED')

                    def started():
                        with self.lock:
                            row['libreOfficeStarted'] = True

                    outputs = convert_path(source['path'], target,
                        request.root / 'native' / str(i) / str(j),
                        cancel=request.cancel, on_started=started)
                    output_root = request.root / 'native' / str(i) / str(j) / 'output'
                    for path in outputs:
                        archive_path = f'{i + 1:03d}_{target}/' + path.relative_to(output_root).as_posix()
                        artifacts.append((archive_path, path))
                    row['state'] = 'ready'
                    row['warnings'] = source['warnings']
                except LibreOfficeError as exc:
                    row.update(state='error', code=exc.code, error=str(exc))
                except OSError:
                    row.update(state='error', code='STORAGE_ERROR',
                               error='Ошибка чтения или записи файла. Проверьте свободное место и права доступа.')
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
            with self.lock:
                request.packaging = True
            try:
                with ZipFile(path, 'w', compression=ZIP_STORED, allowZip64=True) as archive:
                    for archive_name, output in artifacts:
                        self._check_packaging_cancel(request)
                        info = ZipInfo.from_file(output, archive_name)
                        with output.open('rb') as source, archive.open(info, 'w', force_zip64=True) as target:
                            while True:
                                self._check_packaging_cancel(request)
                                chunk = source.read(ARCHIVE_CHUNK)
                                if not chunk:
                                    break
                                target.write(chunk)
                    self._check_packaging_cancel(request)
                    if report['failed']:
                        archive.writestr('AIO-conversion-report.json',
                                         json.dumps(report, ensure_ascii=False, indent=2).encode())
                    self._check_packaging_cancel(request)
            finally:
                with self.lock:
                    request.packaging = False
            name, mime = path.name, 'application/zip'
        size = path.stat().st_size
        ticket = secrets.token_urlsafe(32)
        with self.lock:
            self._check_packaging_cancel(request)
            if self.closed:
                raise LibreOfficeError('Программа закрывается.', 'CANCELLED')
            self.results[ticket] = Result(request.temporary, path, name, mime,
                request_id=request.identity, size=size, target_report=report,
                format_ids=tuple(formats))
            self.results_by_request[request.identity] = ticket
            request.published = True
            report['cancelledRequested'] = bool(request.cancel.is_set())
        return {'ok': True, 'cancelledRequested': report['cancelledRequested'], 'result': {'name': name, 'mime': mime, 'size': size,
                'ticket': ticket, 'downloadUrl': '/api/converter/result?ticket=' + ticket,
                'targetReport': report, 'formatIds': formats},
                'message': ('Готово преобразований: ' + str(report['succeeded']) +
                            '. Ошибок: ' + str(report['failed']) + '. Можно сохранить результат.')}

    @staticmethod
    def _check_packaging_cancel(request):
        # Cancelling native conversion deliberately retains already converted
        # outputs. A second/explicit cancel while packaging cancels the ZIP.
        if request.packaging_cancel.is_set():
            raise LibreOfficeError('Упаковка результатов отменена.', 'CANCELLED')

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
                clean = result.released and not result.leases
            if clean:
                self.release(ticket)

    def release(self, ticket):
        # The lock protects ownership/leases, not filesystem deletion. A slow
        # Windows sharing violation must not stall status, cancel or new work.
        with self.lock:
            result = self.results.get(ticket)
            if result is None:
                return True
            result.released = True
            if result.leases or result.cleaning:
                return False
            result.cleaning = True
        try:
            result.temporary.cleanup()
        except OSError:
            self._schedule_cleanup()
            return False
        else:
            with self.lock:
                if self.results.get(ticket) is result:
                    self.results.pop(ticket, None)
                    if self.results_by_request.get(result.request_id) == ticket:
                        self.results_by_request.pop(result.request_id, None)
            return True
        finally:
            with self.lock:
                result.cleaning = False

    def _schedule_cleanup(self):
        # No idle timer in normal operation. After a sharing violation only,
        # coalesce at most three delayed retries for explicitly released data.
        with self.lock:
            pending=bool(self.cleanup_pending) or any(r.released for r in self.results.values())
            if self.closed or self._cleanup_timer is not None or not pending or self._cleanup_retries>=3:
                return
            delay=(.5,2,10)[self._cleanup_retries]
            self._cleanup_retries+=1
            def retry():
                with self.lock:
                    self._cleanup_timer=None
                    if self.closed:return
                self.retry_cleanup()
                self._schedule_cleanup()
            timer=threading.Timer(delay,retry)
            timer.daemon=True
            self._cleanup_timer=timer
            timer.start()

    def retry_cleanup(self):
        """One bounded best-effort reap of previously released leftovers."""
        with self.lock:
            released = [ticket for ticket, value in self.results.items()
                        if value.released and not value.leases]
            pending = tuple(self.cleanup_pending)
        for ticket in released:
            self.release(ticket)
        for temporary in pending:
            try:
                temporary.cleanup()
            except OSError:
                continue
            with self.lock:
                if temporary in self.cleanup_pending:
                    self.cleanup_pending.remove(temporary)

    def storage_stats(self):
        with self.lock:
            return {'active': len(self.active), 'retainedResults':
                    sum(not result.released for result in self.results.values()),
                    'cleanupPending': len(self.cleanup_pending) +
                    sum(result.released for result in self.results.values()),
                    'leasedResults': sum(bool(result.leases) for result in self.results.values())}

    def close(self):
        with self.lock:
            self.closed = True
            if self._cleanup_timer is not None:
                self._cleanup_timer.cancel()
                self._cleanup_timer = None
            active = list(self.active.values())
            tickets = list(self.results)
        for request in active:
            request.cancel.set()
            request.packaging_cancel.set()
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
