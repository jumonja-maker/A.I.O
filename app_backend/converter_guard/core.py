"""Bounded, disk-backed job ownership for the A.I.O → Guard → LibreOffice port.

One worker processes files independently. Excel, Qt and the old converter are
not imported. Inspection uses metadata; conversion hands original bytes to LO.
"""
from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
import math
from pathlib import Path
import re
import secrets
import shutil
import tempfile
import threading
import time
import zipfile

from .catalog import family_for, inspect_files
from .errors import GuardError
from .libreoffice import LibreOfficeAdapter

IDENTIFIER = re.compile(r'[A-Za-z0-9_-]{1,64}\Z')
TERMINAL = {'ready', 'partial', 'failed', 'cancelled'}
CHUNK = 64 * 1024


@dataclass(frozen=True)
class GuardLimits:
    max_jobs: int = 16
    max_files: int = 100
    max_file_bytes: int = 512 * 1024 * 1024
    max_spool_bytes: int = 2 * 1024 * 1024 * 1024
    file_timeout: float = 1800
    retention_seconds: float = 1800

    def __post_init__(self):
        for value in (self.max_jobs, self.max_files, self.max_file_bytes, self.max_spool_bytes):
            if type(value) is not int or value <= 0:
                raise ValueError('Guard byte/count limits must be positive integers')
        for value in (self.file_timeout, self.retention_seconds):
            if not math.isfinite(value) or value <= 0:
                raise ValueError('Guard time limits must be finite and positive')


class GuardTrace:
    def __init__(self):
        self.started = time.monotonic()
        self.events = deque(maxlen=512)
        self.count = 0

    def add(self, stage, *, file_id=None, code=None, lo_started=False):
        self.count += 1
        self.events.append({'sequence': self.count,
            'elapsedMs': round((time.monotonic() - self.started) * 1000),
            'stage': stage, 'fileId': file_id, 'code': code,
            'libreOfficeStarted': bool(lo_started)})

    def snapshot(self):
        return {'events': list(self.events), 'droppedEvents': self.count - len(self.events)}


@dataclass
class _File:
    id: str
    name: str
    size: int
    source: Path
    state: str = 'awaiting_upload'
    code: str | None = None
    error: str | None = None
    lo_started: bool = False
    uploading: bool = False
    output: Path | None = None
    ticket: str | None = None


@dataclass
class _Job:
    id: str
    root: Path
    files: list[_File]
    reserved: int
    state: str = 'uploading'
    cancel: threading.Event = field(default_factory=threading.Event)
    trace: GuardTrace = field(default_factory=GuardTrace)
    touched: float = field(default_factory=time.monotonic)
    released: bool = False
    running: bool = False
    io: int = 0
    result: dict | None = None
    bundle: Path | None = None
    bundle_ticket: str | None = None
    packaging_error: str | None = None
    future: object = None


def validate_files(files, limits: GuardLimits) -> list[dict]:
    if not isinstance(files, list) or not 1 <= len(files) <= limits.max_files:
        raise GuardError('FILE_COUNT', 'Выберите от 1 до ' + str(limits.max_files) + ' файлов.')
    result, ids = [], set()
    for file in files:
        if not isinstance(file, dict):
            raise GuardError('INPUT_METADATA', 'Неверное описание файла.')
        id, name, size = file.get('id'), file.get('name'), file.get('size')
        if not isinstance(id, str) or not IDENTIFIER.fullmatch(id) or id in ids:
            raise GuardError('INPUT_ID', 'Идентификаторы файлов должны быть уникальными.')
        if (not isinstance(name, str) or not name.strip() or len(name.encode('utf-8')) > 240
                or any(ord(c) < 32 for c in name) or '/' in name or '\\' in name
                or name in ('.', '..')):
            raise GuardError('INPUT_NAME', 'Недопустимое имя файла.')
        if type(size) is not int or not 0 <= size <= limits.max_file_bytes:
            raise GuardError('INPUT_SIZE', 'Размер файла превышает настроенный лимит Guard.')
        ids.add(id)
        result.append({'id': id, 'name': name, 'size': size})
    return result


class ConverterGuard:
    def __init__(self, *, adapter=None, limits=None, spool_parent=None):
        self.adapter = adapter if adapter is not None else LibreOfficeAdapter()
        self.limits = limits or GuardLimits()
        self._parent = spool_parent
        self._lock = threading.RLock()
        self._io_drained = threading.Condition(self._lock)
        self._jobs: dict[str, _Job] = {}
        self._reserved = 0
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='aio-guard')
        self._closed = False

    def capabilities(self, files):
        return inspect_files(validate_files(files, self.limits), self.adapter.available)

    def _get(self, job_id) -> _Job:
        job = self._jobs.get(job_id)
        if job is None or job.released:
            raise GuardError('JOB_NOT_FOUND', 'Задание больше недоступно.', status=404)
        job.touched = time.monotonic()
        return job

    def _prune(self):
        with self._lock:
            now = time.monotonic()
            expired = [j for j in self._jobs.values() if j.released or (
                not j.result and not j.running and not j.io and
                now - j.touched > self.limits.retention_seconds)]
            for job in expired:
                job.released = True
                job.cancel.set()
                self._cleanup(job)

    def create(self, files) -> dict:
        rows = validate_files(files, self.limits)
        self._prune()
        size = sum(row['size'] for row in rows)
        with self._lock:
            if self._closed:
                raise GuardError('GUARD_CLOSED', 'Guard завершает работу.', status=503)
            if len(self._jobs) >= self.limits.max_jobs:
                raise GuardError('JOB_LIMIT', 'Освободите предыдущие результаты перед новым заданием.', status=409)
            if self._reserved + size > self.limits.max_spool_bytes:
                raise GuardError('SPOOL_QUOTA', 'Недостаточно квоты временного хранилища Guard.', status=413)
            try:
                root = Path(tempfile.mkdtemp(prefix='aio-guard-', dir=self._parent))
            except OSError as exc:
                raise GuardError('STORAGE_UNAVAILABLE', 'Не удалось создать временное хранилище.', status=507) from exc
            id = secrets.token_hex(16)
            items = []
            for index, row in enumerate(rows):
                # Names and client IDs never choose a physical path.
                suffix = Path(row['name']).suffix.lower()
                suffix = suffix if re.fullmatch(r'\.[a-z0-9]{1,12}', suffix) else '.bin'
                items.append(_File(**row, source=root / 'input' / str(index) / ('source' + suffix)))
            job = _Job(id, root, items, size)
            job.trace.add('job_created', lo_started=False)
            self._jobs[id] = job
            self._reserved += size
            return self._snapshot(job)

    def upload(self, job_id, file_id, stream, length):
        with self._lock:
            job = self._get(job_id)
            item = next((f for f in job.files if f.id == file_id), None)
            if item is None:
                raise GuardError('FILE_NOT_FOUND', 'Файл не относится к заданию.', status=404)
            if job.state != 'uploading' or item.state != 'awaiting_upload' or item.uploading:
                raise GuardError('UPLOAD_STATE', 'Этот файл уже загружен или обрабатывается.', status=409)
            if type(length) is not int or length != item.size:
                raise GuardError('UPLOAD_LENGTH', 'Размер загрузки не совпадает с размером файла.')
            job.io += 1
            item.uploading = True
        part = item.source.with_suffix(item.source.suffix + '.part')
        try:
            part.parent.mkdir(parents=True, exist_ok=True)
            with part.open('xb') as output:
                remaining = length
                while remaining:
                    if job.cancel.is_set():
                        raise GuardError('CANCELLED', 'Загрузка отменена.', stage='upload')
                    block = stream.read(min(CHUNK, remaining))
                    if not block or len(block) > remaining:
                        raise GuardError('UPLOAD_TRUNCATED', 'Загрузка файла прервана.', stage='upload')
                    output.write(block)
                    remaining -= len(block)
            with self._lock:
                if job.cancel.is_set():
                    raise GuardError('CANCELLED', 'Загрузка отменена.', stage='upload')
                part.replace(item.source)
                item.state = 'queued'
                job.trace.add('input_stored', file_id=item.id, lo_started=False)
        except OSError as exc:
            raise GuardError('INPUT_STORAGE_FAILED', 'Не удалось сохранить загружаемый файл.', stage='upload', status=507) from exc
        finally:
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass  # Ownership is retained; release/prune retries directory cleanup.
            with self._lock:
                item.uploading = False
                job.io -= 1
                self._io_drained.notify_all()
                self._cleanup(job)

    def start(self, job_id):
        with self._lock:
            job = self._get(job_id)
            if job.state != 'uploading' or any(f.state != 'queued' for f in job.files):
                raise GuardError('JOB_STATE', 'Дождитесь загрузки всех файлов.', status=409)
            job.state, job.running = 'queued', True
            job.trace.add('job_queued')
            job.future = self._executor.submit(self._run, job)
            return self._snapshot(job)

    def _run(self, job):
        try:
            with self._lock:
                job.state = 'running'
            for index, item in enumerate(job.files):
                with self._lock:
                    if job.cancel.is_set():
                        self._fail(job, item, GuardError('CANCELLED', 'Конвертация отменена.'))
                        continue
                    item.state = 'converting'
                    job.trace.add('route_selected', file_id=item.id)
                try:
                    if family_for(item.name) is None:
                        raise GuardError('INPUT_NOT_CONNECTED', 'Этот тип файла пока не подключён к Guard.', stage='route')
                    if item.size == 0:
                        raise GuardError('INPUT_EMPTY', 'Исходный файл пуст.', stage='input')

                    def started():
                        with self._lock:
                            item.lo_started = True
                            job.trace.add('engine_started', file_id=item.id, lo_started=True)

                    output = self.adapter.export_pdf(item.source, job.root / 'work' / str(index) / 'out',
                        job.root / 'work' / str(index) / 'profile', cancel=job.cancel,
                        timeout=self.limits.file_timeout, on_started=started,
                        output_limit=self._output_budget)
                    with self._lock:
                        size = output.stat().st_size
                        if self._reserved + size > self.limits.max_spool_bytes:
                            output.unlink()
                            raise GuardError('SPOOL_QUOTA', 'Результат превышает квоту временного хранилища.', stage='result', status=507)
                        self._reserved += size
                        job.reserved += size
                        item.output, item.ticket, item.state = output, secrets.token_urlsafe(32), 'ready'
                        job.trace.add('result_ready', file_id=item.id, lo_started=item.lo_started)
                except GuardError as error:
                    with self._lock:
                        self._fail(job, item, error)
                except Exception:
                    with self._lock:
                        self._fail(job, item, GuardError('ENGINE_INTERNAL', 'Внутренняя ошибка обработки файла.', stage='engine'))
                finally:
                    # Profiles and inputs are no longer needed once this task ends.
                    shutil.rmtree(job.root / 'work' / str(index) / 'profile', ignore_errors=True)
                    if item.output is None:
                        shutil.rmtree(job.root / 'work' / str(index) / 'out', ignore_errors=True)
            with self._lock:
                succeeded = sum(f.state == 'ready' for f in job.files)
                job.state = ('cancelled' if job.cancel.is_set() else
                             'ready' if succeeded == len(job.files) else 'partial' if succeeded else 'failed')
            self._make_result(job)
            with self._lock:
                if job.cancel.is_set():
                    job.state = 'cancelled'
                job.trace.add('job_finished', code=job.state,
                              lo_started=any(f.lo_started for f in job.files))
        except Exception:
            with self._lock:
                for item in job.files:
                    if item.state in {'queued', 'converting'}:
                        self._fail(job, item, GuardError('GUARD_INTERNAL', 'Не удалось завершить задание.'))
                job.state = 'failed'
                job.trace.add('job_failed', code='GUARD_INTERNAL',
                              lo_started=any(f.lo_started for f in job.files))
        finally:
            with self._lock:
                job.running = False
                job.touched = time.monotonic()
                self._cleanup(job)

    def _fail(self, job, item, error):
        item.state = 'cancelled' if error.code == 'CANCELLED' else 'error'
        item.code, item.error = error.code, error.message
        job.trace.add(error.stage, file_id=item.id, code=error.code, lo_started=item.lo_started)

    def _output_budget(self):
        with self._lock:
            return max(0, self.limits.max_spool_bytes - self._reserved)

    @staticmethod
    def _download(item):
        return {'fileId': item.id, 'name': Path(item.name).stem + '.pdf',
                'size': item.output.stat().st_size,
                'downloadUrl': '/api/converter/result?ticket=' + item.ticket}

    def _make_result(self, job):
        ready = [f for f in job.files if f.output is not None]
        if not ready:
            return
        outputs = [self._download(f) for f in ready]
        result = dict(outputs[0]) if len(outputs) == 1 else {'name': 'AIO_Results.zip'}
        if len(outputs) > 1:
            bundle = job.root / 'results.zip'
            # ZIP_STORED avoids wasting CPU recompressing already-compressed PDFs.
            budget = sum(o['size'] for o in outputs) + 8192 + 1024 * len(outputs)
            with self._lock:
                allowed = self._reserved + budget <= self.limits.max_spool_bytes
                if allowed:
                    self._reserved += budget
                    job.reserved += budget
            if allowed:
                try:
                    with zipfile.ZipFile(bundle, 'x', compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
                        for index, item in enumerate(ready):
                            with item.output.open('rb') as source, archive.open(
                                    f'{index+1:03d}-' + Path(item.name).stem + '.pdf', 'w') as target:
                                while block := source.read(CHUNK):
                                    if job.cancel.is_set():
                                        raise GuardError('BUNDLE_CANCELLED', 'Упаковка отменена.')
                                    target.write(block)
                    with self._lock:
                        actual = bundle.stat().st_size
                        self._reserved += actual - budget
                        job.reserved += actual - budget
                        job.bundle, job.bundle_ticket = bundle, secrets.token_urlsafe(32)
                    result['downloadUrl'] = '/api/converter/result?ticket=' + job.bundle_ticket
                    result['size'] = actual
                except (OSError, GuardError) as error:
                    bundle.unlink(missing_ok=True)
                    with self._lock:
                        self._reserved -= budget
                        job.reserved -= budget
                    job.packaging_error = error.code if isinstance(error, GuardError) else 'BUNDLE_STORAGE_FAILED'
            else:
                job.packaging_error = 'BUNDLE_QUOTA'
            if job.packaging_error:
                job.trace.add('bundle', code=job.packaging_error,
                              lo_started=any(f.lo_started for f in job.files))
        result.update({'jobId': job.id, 'outputs': outputs})
        with self._lock:
            job.result = result

    def _snapshot(self, job, include_trace=True):
        rows = [{'fileId': f.id, 'name': f.name, 'state': f.state,
                 'code': f.code, 'error': f.error, 'libreOfficeStarted': f.lo_started,
                 'format': 'pdf', 'percent': 100 if f.state == 'ready' else None}
                for f in job.files]
        completed = sum(f.state in {'ready', 'error', 'cancelled'} for f in job.files)
        finished = not job.running and job.state in TERMINAL
        return {'jobId': job.id, 'state': job.state, 'finished': finished,
                'files': rows, 'completed': completed, 'total': len(rows),
                'percent': completed * 100 / len(rows), 'result': job.result,
                'trace': job.trace.snapshot() if include_trace or finished else None,
                'packagingError': job.packaging_error}

    def status(self, job_id, *, include_trace=True):
        self._prune()
        with self._lock:
            return self._snapshot(self._get(job_id), include_trace)

    def cancel(self, job_id):
        with self._lock:
            job = self._get(job_id)
            job.cancel.set()
            self._cancel_queued(job)
            if job.state == 'uploading':
                for item in job.files:
                    self._fail(job, item, GuardError('CANCELLED', 'Загрузка отменена.', stage='upload'))
                job.state = 'cancelled'
            job.trace.add('cancel_requested', lo_started=any(f.lo_started for f in job.files))
            return self._snapshot(job)

    @contextmanager
    def open_result(self, ticket):
        self._prune()
        with self._lock:
            found = None
            for job in self._jobs.values():
                if job.released:
                    continue
                if ticket == job.bundle_ticket and job.bundle:
                    found = (job, job.bundle, 'AIO_Results.zip')
                    break
                for item in job.files:
                    if ticket == item.ticket and item.output:
                        found = (job, item.output, Path(item.name).stem + '.pdf')
                        break
                if found:
                    break
            if not found:
                raise GuardError('RESULT_NOT_FOUND', 'Результат больше недоступен.', status=404)
            job, path, name = found
            job.io += 1
            job.touched = time.monotonic()
        try:
            with path.open('rb') as stream:
                yield stream, name, path.stat().st_size
        finally:
            with self._lock:
                job.io -= 1
                self._io_drained.notify_all()
                self._cleanup(job)

    def release(self, job_id):
        with self._lock:
            job = self._get(job_id)
            job.released = True
            job.cancel.set()
            self._cancel_queued(job)
            self._cleanup(job)

    def _cancel_queued(self, job):
        if job.state == 'queued' and job.future is not None and job.future.cancel():
            for item in job.files:
                self._fail(job, item, GuardError('CANCELLED', 'Конвертация отменена.'))
            job.state, job.running = 'cancelled', False

    def _cleanup(self, job):
        if not job.released or job.running or job.io:
            return
        try:
            shutil.rmtree(job.root)
        except FileNotFoundError:
            pass
        except OSError:
            job.trace.add('cleanup_deferred', code='CLEANUP_RETRY')
            return  # Keep ownership/quota until a subsequent prune can retry.
        if self._jobs.pop(job.id, None) is not None:
            self._reserved -= job.reserved

    def close(self):
        with self._lock:
            self._closed = True
            for job in self._jobs.values():
                job.released = True
                job.cancel.set()
                self._cancel_queued(job)
        self._executor.shutdown(wait=True)
        with self._io_drained:
            self._io_drained.wait_for(lambda: all(job.io == 0 for job in self._jobs.values()), timeout=5)
        self._prune()

    def storage_stats(self):
        with self._lock:
            return {'jobs': len(self._jobs), 'reservedBytes': self._reserved,
                    'running': sum(j.running for j in self._jobs.values())}
