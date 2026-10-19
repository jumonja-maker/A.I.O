"""Disk-owned, opt-in native jobs. One worker; independent file/target failures."""
from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import copy
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import tempfile
import threading
import time
import zipfile

from ..libreoffice_catalog import mime_for
from .adapter import LibreOfficeAdapter
from .errors import GuardError
from .policy import GuardLimits, validate_files, validate_plan

CHUNK = 64 * 1024
SUCCESS = {'ready', 'saved'}
DONE = SUCCESS | {'error', 'cancelled', 'skipped'}
TERMINAL = {'ready', 'partial', 'failed', 'cancelled'}


class GuardTrace:
    def __init__(self):
        self.started = time.monotonic()
        self.events = deque(maxlen=512)
        self.count = 0

    def add(self, stage, *, file_id=None, target=None, code=None, lo_started=False):
        self.count += 1
        self.events.append({'sequence': self.count,
            'elapsedMs': round((time.monotonic() - self.started) * 1000),
            'stage': stage, 'fileId': file_id, 'format': target, 'code': code,
            'libreOfficeStarted': bool(lo_started)})

    def snapshot(self):
        return {'events': list(self.events), 'droppedEvents': self.count - len(self.events)}


@dataclass
class _Task:
    format: str
    index: int
    state: str = 'queued'
    code: str | None = None
    error: str | None = None
    stage: str | None = None
    lo_started: bool = False
    artifacts: list = field(default_factory=list)
    route: dict | None = None


@dataclass
class _File:
    id: str
    name: str
    size: int
    source: Path
    targets: list
    state: str = 'awaiting_upload'
    uploading: bool = False
    interrupt: object = None
    name_normalized: bool = False
    native_route: dict | None = None
    cancel: threading.Event = field(default_factory=threading.Event)


@dataclass
class _Artifact:
    job_id: str
    path: Path
    name: str
    size: int
    ticket: str
    file_id: str | None = None
    format: str | None = None
    archive_name: str = ''
    saved: bool = False

    def metadata(self):
        return {'fileId': self.file_id, 'format': self.format, 'name': self.name,
                'archiveName': self.archive_name, 'size': self.size, 'saved': self.saved,
                'mime': mime_for(self.path.suffix.lower().lstrip('.')),
                'downloadUrl': '/api/converter-guard/result?ticket=' + self.ticket}


@dataclass
class _Job:
    id: str
    root: Path
    files: list
    reserved: int
    revision: int = 0
    files_by_id: dict = field(default_factory=dict)
    state: str = 'uploading'
    cancel: threading.Event = field(default_factory=threading.Event)
    trace: GuardTrace = field(default_factory=GuardTrace)
    touched: float = field(default_factory=time.monotonic)
    running: bool = False
    released: bool = False
    cleaning: bool = False
    io: int = 0
    future: object = None
    artifacts: list = field(default_factory=list)
    bundle: object = None
    result: dict | None = None
    packaging_error: str | None = None


def _source_name(name):
    normalized = ((os.name != 'nt' and len(os.fsencode(name)) > 255) or
        (os.name == 'nt' and (bool(re.search(r'[<>:"|?*]', name)) or
         name.endswith((' ', '.')) or bool(re.fullmatch(
             r'CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]', name.split('.')[0], re.I)))))
    suffix = Path(name).suffix
    safe_suffix = suffix if re.fullmatch(r'\.[a-zA-Z0-9_-]{1,32}', suffix) else '.bin'
    return ('source' + safe_suffix if normalized else name), normalized


class _Cancellation:
    """Combine job/file cancellation without another timer or watcher thread."""
    def __init__(self, job, item):
        self.job, self.item = job, item

    def is_set(self):
        return self.job.is_set() or self.item.is_set()

    def wait(self, seconds):
        self.job.wait(seconds)
        return self.is_set()


class ConverterGuard:
    def __init__(self, *, adapter=None, limits=None, spool_parent=None):
        self.adapter = adapter if adapter is not None else LibreOfficeAdapter()
        self.limits = limits if limits is not None else GuardLimits()
        self._parent = None if spool_parent is None else Path(spool_parent).resolve()
        self._lock = threading.RLock()
        self._io_drained = threading.Condition(self._lock)
        self._jobs, self._tickets = {}, {}
        self._reserved = 0
        # The executor starts no thread until start(). Import/inspection is idle.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='aio-guard')
        self._closed = False

    def capabilities(self, files):
        return self.adapter.capabilities([] if files == [] else validate_files(files, self.limits))

    @staticmethod
    def _record(job, stage, **details):
        job.trace.add(stage, **details)
        job.revision += 1

    def _get(self, identity):
        job = self._jobs.get(identity)
        if job is None or job.released:
            raise GuardError('JOB_NOT_FOUND', 'Задание больше недоступно.', status=404)
        job.touched = time.monotonic()
        return job

    def _prune(self):
        with self._lock:
            now = time.monotonic()
            expired = [j for j in self._jobs.values() if j.released or (
                not j.artifacts and not j.running and not j.io
                and now - j.touched > self.limits.retention_seconds)]
            for job in expired:
                job.released = True
                job.cancel.set()
        for job in expired:
            self._cleanup(job)

    def _reserve(self, job, amount):
        maximum = self.limits.max_spool_bytes
        if maximum is not None and self._reserved + amount > maximum:
            raise GuardError('SPOOL_QUOTA', 'Превышена настроенная квота временных файлов.',
                             stage='storage', status=507)
        self._reserved += amount
        job.reserved += amount

    def _unreserve(self, job, amount):
        self._reserved -= amount
        job.reserved -= amount

    def create(self, files, formats=None, *, plan=None):
        rows = validate_files(files, self.limits)
        planned = validate_plan(rows, formats, plan)
        self._prune()
        size = sum(row['size'] for row in rows)
        with self._lock:
            if self._closed:
                raise GuardError('GUARD_CLOSED', 'Guard завершает работу.', status=503)
            if len(self._jobs) >= self.limits.max_jobs:
                raise GuardError('JOB_LIMIT', 'Освободите предыдущие задания.', status=409)
            maximum = self.limits.max_spool_bytes
            if maximum is not None and self._reserved + size > maximum:
                raise GuardError('SPOOL_QUOTA', 'Недостаточно настроенной квоты Guard.', status=413)
            try:
                root = Path(tempfile.mkdtemp(prefix='aio-guard-', dir=self._parent)).resolve()
            except OSError as error:
                raise GuardError('STORAGE_UNAVAILABLE', 'Не удалось создать временное хранилище.',
                                 stage='storage', status=507) from error
            files = []
            task_index = 0
            for index, row in enumerate(rows):
                name, normalized = _source_name(row['name'])
                tasks = [_Task(target, task_index + offset)
                         for offset, target in enumerate(planned[row['id']])]
                task_index += len(tasks)
                files.append(_File(**row, source=root / 'input' / str(index) / name,
                                   targets=tasks, name_normalized=normalized))
            identity = secrets.token_hex(16)
            job = _Job(identity, root, files, size)
            job.files_by_id = {item.id: item for item in files}
            self._record(job, 'job_created')
            self._jobs[identity] = job
            self._reserved += size
            return self._snapshot(job)

    def upload(self, job_id, file_id, stream, length, *, interrupt=None):
        with self._lock:
            job = self._get(job_id)
            item = job.files_by_id.get(file_id) if isinstance(file_id, str) else None
            if item is None:
                raise GuardError('FILE_NOT_FOUND', 'Файл не относится к заданию.', status=404)
            if job.state != 'uploading' or item.state != 'awaiting_upload' or item.uploading:
                raise GuardError('UPLOAD_STATE', 'Этот файл уже загружен или обрабатывается.', status=409)
            if type(length) is not int or length != item.size:
                raise GuardError('UPLOAD_LENGTH', 'Размер загрузки не совпадает с размером файла.')
            item.uploading, item.interrupt = True, interrupt
            job.io += 1
        part = item.source.parent.parent / (item.source.parent.name + '.part')
        try:
            item.source.parent.mkdir(parents=True, exist_ok=True)
            with part.open('xb') as output:
                remaining = length
                while remaining:
                    if job.cancel.is_set() or item.cancel.is_set():
                        raise GuardError('CANCELLED', 'Загрузка отменена.', stage='upload')
                    block = stream.read(min(CHUNK, remaining))
                    if job.cancel.is_set() or item.cancel.is_set():
                        raise GuardError('CANCELLED', 'Загрузка отменена.', stage='upload')
                    if not block or len(block) > remaining:
                        raise GuardError('UPLOAD_TRUNCATED', 'Загрузка файла прервана.', stage='upload')
                    output.write(block)
                    remaining -= len(block)
            with self._lock:
                if job.cancel.is_set() or item.cancel.is_set():
                    raise GuardError('CANCELLED', 'Загрузка отменена.', stage='upload')
                part.replace(item.source)
                item.state = 'queued'
                self._record(job, 'input_stored', file_id=item.id)
                if item.name_normalized:
                    self._record(job, 'input_name_adjusted', file_id=item.id, code='SOURCE_NAME_NORMALIZED')
        except OSError as error:
            if job.cancel.is_set() or item.cancel.is_set():
                raise GuardError('CANCELLED', 'Загрузка отменена.', stage='upload') from error
            raise GuardError('INPUT_STORAGE_FAILED', 'Не удалось записать исходный файл.',
                             stage='upload', status=507) from error
        finally:
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass
            with self._lock:
                item.uploading, item.interrupt = False, None
                job.io -= 1
                self._io_drained.notify_all()
            self._cleanup(job)

    def start(self, job_id):
        with self._lock:
            job = self._get(job_id)
            if job.state != 'uploading' or any(
                    f.uploading or f.state not in {'queued', 'cancelled'} for f in job.files):
                raise GuardError('JOB_STATE', 'Дождитесь загрузки всех файлов.', status=409)
            job.state, job.running = 'queued', True
            self._record(job, 'job_queued')
            job.future = self._executor.submit(self._run, job)
            return self._snapshot(job)

    def _fail(self, job, item, task, error):
        task.state = 'cancelled' if error.code == 'CANCELLED' else 'error'
        task.code, task.error, task.stage = error.code, error.message, error.stage
        self._record(job, error.stage, file_id=item.id, target=task.format,
                      code=error.code, lo_started=task.lo_started)

    def _output_budget(self):
        with self._lock:
            return max(0, self.limits.max_spool_bytes - self._reserved)

    def _retain_outputs(self, job, item, task, workspace, paths):
        root = (workspace / 'output').resolve()
        if not root.is_relative_to(job.root.resolve()):
            raise GuardError('ENGINE_OUTPUT_PATH', 'Недопустимое расположение результата.',
                             stage='result')
        artifacts, seen = [], set()
        for candidate in paths:
            path = Path(candidate)
            resolved = path.resolve(strict=True)
            if path.is_symlink() or not path.is_file() or not resolved.is_relative_to(root):
                raise GuardError('ENGINE_OUTPUT_PATH', 'Недопустимое расположение результата.',
                                 stage='result')
            if resolved in seen:
                continue
            seen.add(resolved)
            relative = resolved.relative_to(root).as_posix()
            artifacts.append(_Artifact(job.id, resolved, path.name, path.stat().st_size,
                secrets.token_urlsafe(32), item.id, task.format,
                f'{task.index + 1:04d}-{task.format}/' + relative))
        if not artifacts:
            raise GuardError('ENGINE_OUTPUT_MISSING', 'LibreOffice не создал результат.',
                             stage='result')
        with self._lock:
            self._reserve(job, sum(a.size for a in artifacts))
            for artifact in artifacts:
                self._tickets[artifact.ticket] = artifact
            job.artifacts.extend(artifacts)
            task.artifacts = artifacts
            task.state = 'ready'
            self._record(job, 'result_ready', file_id=item.id, target=task.format,
                          lo_started=task.lo_started)

    def _remove_work(self, job, path):
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            return
        except OSError:
            # Rare deferred deletion stays owned/accounted, not orphaned.
            try:
                residue = sum(p.stat().st_size for p in path.rglob('*')
                              if p.is_file() and not p.is_symlink())
            except OSError:
                residue = 0
            with self._lock:
                self._reserved += residue
                job.reserved += residue
                self._record(job, 'cleanup_deferred', code='WORK_CLEANUP_RETRY')

    def _drop_source(self, job, item):
        try:
            item.source.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            with self._lock:
                self._record(job, 'cleanup_deferred', file_id=item.id, code='INPUT_CLEANUP_RETRY')
            return
        with self._lock:
            self._unreserve(job, item.size)
        try:
            item.source.parent.rmdir()
        except OSError:
            pass

    @staticmethod
    def _file_state(item):
        if item.state in {'awaiting_upload', 'queued', 'converting'}:
            return item.state
        states = [task.state for task in item.targets]
        if any(s == 'saved' for s in states) and all(s in {'saved', 'skipped'} for s in states):
            return 'saved'
        if any(s in SUCCESS for s in states) and all(s in SUCCESS | {'skipped'} for s in states):
            return 'ready'
        if any(s in SUCCESS for s in states):
            return 'partial'
        return 'cancelled' if all(s == 'cancelled' for s in states) else 'error'

    def _run(self, job):
        try:
            with self._lock:
                job.state = 'running'
                self._record(job, 'job_started')
            for item in job.files:
                for task in item.targets:
                    workspace = job.root / 'work' / str(task.index)
                    with self._lock:
                        if job.cancel.is_set() or item.cancel.is_set():
                            if task.state not in DONE:
                                self._fail(job, item, task, GuardError('CANCELLED', 'Конвертация отменена.'))
                            continue
                        item.state, task.state = 'converting', 'converting'
                        self._record(job, 'route_selected', file_id=item.id, target=task.format)
                    try:
                        def started():
                            with self._lock:
                                task.lo_started = True
                                self._record(job, 'engine_started', file_id=item.id,
                                              target=task.format, lo_started=True)

                        routing = getattr(self.adapter, 'route_decision', None)
                        if routing and routing(task.format, item.native_route) == 'unavailable':
                            with self._lock:
                                task.state, task.code, task.stage = 'skipped', 'NATIVE_ROUTE_UNAVAILABLE', 'route'
                                task.error = ('Для определённого LibreOffice типа документа нет экспорта в '
                                              + task.format.upper() + '.')
                                task.route = dict(item.native_route)
                                self._record(job, 'route_filtered', file_id=item.id,
                                             target=task.format, code=task.code)
                            continue

                        def detected(proof):
                            with self._lock:
                                item.native_route = dict(proof)
                                self._record(job, 'native_document_identified', file_id=item.id,
                                             target=task.format, lo_started=task.lo_started)

                        needs_observation = getattr(self.adapter, 'observation_needed', lambda proof: True)
                        paths = self.adapter.convert(item.source, task.format, workspace,
                            cancel=_Cancellation(job.cancel, item.cancel),
                            timeout=self.limits.file_timeout, on_started=started,
                            output_limit=(None if self.limits.max_spool_bytes is None else self._output_budget),
                            **({'on_document_service': detected if needs_observation(item.native_route) else None}
                               if routing else {}))
                        self._retain_outputs(job, item, task, workspace, paths)
                    except GuardError as error:
                        with self._lock:
                            self._fail(job, item, task, error)
                    except OSError:
                        with self._lock:
                            self._fail(job, item, task, GuardError('STORAGE_ERROR',
                                'Ошибка временного хранилища.', stage='storage', status=507))
                    except Exception:
                        with self._lock:
                            self._fail(job, item, task, GuardError('ENGINE_INTERNAL',
                                'Внутренняя ошибка обработки файла.', stage='engine'))
                    finally:
                        self._remove_work(job, workspace / 'profile' if task.state in SUCCESS else workspace)
                with self._lock:
                    item.state = 'done'
                    item.state = self._file_state(item)
                    self._record(job, 'input_finished', file_id=item.id)
                # Keep original bytes across all targets; drop only the temp
                # input after its last target. Original user files are untouched.
                self._drop_source(job, item)
            with self._lock:
                job.state = 'packaging'
                self._record(job, 'packaging_started')
            self._make_result(job)
            with self._lock:
                tasks = [t for f in job.files for t in f.targets]
                successes = sum(t.state in SUCCESS for t in tasks)
                job.state = ('cancelled' if job.cancel.is_set() or all(t.state == 'cancelled' for t in tasks) else
                    'ready' if successes and all(t.state in SUCCESS | {'skipped'} for t in tasks)
                    else 'partial' if successes else 'failed')
                self._record(job, 'job_finished', code=job.state,
                              lo_started=any(t.lo_started for t in tasks))
        except Exception:
            with self._lock:
                for item in job.files:
                    for task in item.targets:
                        if task.state not in DONE:
                            self._fail(job, item, task, GuardError('GUARD_INTERNAL',
                                'Не удалось завершить задание.'))
                job.state = 'failed'
                if job.artifacts and job.result is None:
                    job.result = {**job.artifacts[0].metadata(), 'jobId': job.id}
                self._record(job, 'job_failed', code='GUARD_INTERNAL')
        finally:
            with self._lock:
                job.running = False
                job.touched = time.monotonic()
                self._record(job, 'worker_finished', code=job.state)
            self._cleanup(job)

    def _report(self, job):
        return {'schemaVersion': 3, 'files': [
            {'fileId': f.id, 'name': f.name, 'warnings': (
                ['SOURCE_NAME_NORMALIZED'] if f.name_normalized else []),
             'targets': [{'format': t.format, 'state': t.state, 'code': t.code,
                          'error': t.error, 'stage': t.stage,
                          'route': t.route,
                          'libreOfficeStarted': t.lo_started} for t in f.targets]}
            for f in job.files], 'qualityCompared': False}

    def _make_result(self, job):
        with self._lock:
            if not job.artifacts:
                return
            job.result = ({**job.artifacts[0].metadata()} if len(job.artifacts) == 1 else
                          {'name': 'AIO_Results.zip', 'mime': 'application/zip'})
            job.result['jobId'] = job.id
            self._record(job, 'results_available')
            # A single successful artifact needs no duplicate archive at all.
            if len(job.artifacts) == 1 and all(t.state in SUCCESS for f in job.files for t in f.targets):
                return
            report = json.dumps(self._report(job), ensure_ascii=False, indent=2).encode('utf-8')
            budget = (sum(a.size + 512 + 2 * len(a.archive_name.encode('utf-8')) for a in job.artifacts)
                      + len(report) + 4096)
            if job.cancel.is_set():
                job.packaging_error = 'BUNDLE_CANCELLED'
                self._record(job, 'bundle', code=job.packaging_error)
                return
            try:
                self._reserve(job, budget)
            except GuardError:
                job.packaging_error = 'BUNDLE_QUOTA'
                self._record(job, 'bundle', code=job.packaging_error)
                return
        bundle, retained = job.root / 'results.zip', False
        try:
            with zipfile.ZipFile(bundle, 'x', compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
                for artifact in job.artifacts:
                    info = zipfile.ZipInfo.from_file(artifact.path, arcname=artifact.archive_name)
                    # Force ZIP64 even when the stream length was not given to
                    # ZipFile.open: no hidden 2 GiB failure half-way through.
                    with artifact.path.open('rb') as source, archive.open(
                            info, 'w', force_zip64=True) as output:
                        while block := source.read(CHUNK):
                            if job.cancel.is_set():
                                raise GuardError('BUNDLE_CANCELLED', 'Упаковка отменена.')
                            output.write(block)
                archive.writestr('AIO-conversion-report.json', report)
            with self._lock:
                actual = bundle.stat().st_size
                delta = actual - budget
                if delta > 0:
                    self._reserve(job, delta)
                else:
                    self._unreserve(job, -delta)
                artifact = _Artifact(job.id, bundle, 'AIO_Results.zip', actual, secrets.token_urlsafe(32))
                job.bundle = artifact
                self._tickets[artifact.ticket] = artifact
                job.result = {**artifact.metadata(), 'jobId': job.id}
                self._record(job, 'bundle_ready')
                retained = True
        except (OSError, GuardError, ValueError, RuntimeError) as error:
            with self._lock:
                job.packaging_error = error.code if isinstance(error, GuardError) else 'BUNDLE_STORAGE_FAILED'
                self._record(job, 'bundle', code=job.packaging_error)
        finally:
            if not retained:
                try:
                    bundle.unlink(missing_ok=True)
                except OSError:
                    with self._lock:
                        self._record(job, 'cleanup_deferred', code='BUNDLE_CLEANUP_RETRY')
                else:
                    with self._lock:
                        self._unreserve(job, budget)

    def _snapshot(self, job, include_trace=False):
        rows = []
        for item in job.files:
            targets = [{'format': t.format, 'state': t.state, 'code': t.code, 'error': t.error,
                        'stage': t.stage, 'libreOfficeStarted': t.lo_started,
                        'route': copy.deepcopy(t.route),
                        'percent': 100 if t.state in SUCCESS else None,
                        'outputs': [a.metadata() for a in t.artifacts]} for t in item.targets]
            rows.append({'fileId': item.id, 'name': item.name, 'state': self._file_state(item),
                         'cancellationRequested': job.cancel.is_set() or item.cancel.is_set(),
                         'nativeRoute': copy.deepcopy(item.native_route),
                         'targets': targets, 'warnings': (
                             ['SOURCE_NAME_NORMALIZED'] if item.name_normalized else [])})
        total = sum(len(f.targets) for f in job.files)
        completed = sum(t.state in DONE for f in job.files for t in f.targets)
        result = copy.deepcopy(job.result)
        if result is not None:
            result['outputs'] = [a.metadata() for a in job.artifacts]
        finished = not job.running and job.state in TERMINAL
        return {'schemaVersion': 3, 'jobId': job.id, 'revision': job.revision,
                'state': job.state, 'finished': finished,
                'files': rows, 'completed': completed, 'total': total,
                'attempted': sum(t.lo_started for f in job.files for t in f.targets),
                'filtered': sum(t.state == 'skipped' for f in job.files for t in f.targets),
                'percent': completed * 100 / total, 'result': result,
                'trace': job.trace.snapshot() if include_trace else None,
                'packagingError': job.packaging_error}

    def status(self, job_id, *, include_trace=False, since_revision=None):
        if since_revision is not None and (type(since_revision) is not int or since_revision < 0):
            raise GuardError('STATUS_REVISION', 'Некорректная версия состояния.')
        self._prune()
        with self._lock:
            job = self._get(job_id)
            if not include_trace and since_revision == job.revision:
                return {'jobId': job.id, 'revision': job.revision, 'unchanged': True}
            return self._snapshot(job, include_trace)

    def retry_plan(self, job_id, *, include_cancelled=False):
        """Metadata only. Reupload failed sources explicitly; keep prior outputs."""
        if type(include_cancelled) is not bool:
            raise GuardError('RETRY_PLAN', 'Некорректный режим повторной попытки.')
        with self._lock:
            job = self._get(job_id)
            if job.running or job.state not in TERMINAL:
                raise GuardError('JOB_STATE', 'Дождитесь завершения задания.', status=409)
            retry_states = {'error', 'cancelled'} if include_cancelled else {'error'}
            files, plan = [], []
            for item in job.files:
                formats = [t.format for t in item.targets if t.state in retry_states]
                if formats:
                    files.append({'id': item.id, 'name': item.name, 'size': item.size})
                    plan.append({'fileId': item.id, 'formats': formats})
            return {'sourceJobId': job.id, 'files': files, 'plan': plan,
                    'requiresUpload': bool(files), 'retainedOutputs': len(job.artifacts)}

    def _cancel_queued(self, job):
        if job.state == 'queued' and job.future is not None and job.future.cancel():
            for item in job.files:
                for task in item.targets:
                    self._fail(job, item, task, GuardError('CANCELLED', 'Конвертация отменена.'))
                item.state = 'cancelled'
            job.state, job.running = 'cancelled', False

    def _stop(self, job):
        job.cancel.set()
        self._cancel_queued(job)
        if job.state == 'uploading':
            for item in job.files:
                for task in item.targets:
                    self._fail(job, item, task, GuardError('CANCELLED', 'Загрузка отменена.', stage='upload'))
                item.state = 'cancelled'
            job.state = 'cancelled'
        return [f.interrupt for f in job.files if f.interrupt is not None]

    @staticmethod
    def _interrupt(callbacks):
        for callback in callbacks:
            try:
                callback()
            except OSError:
                pass

    def cancel(self, job_id):
        with self._lock:
            job = self._get(job_id)
            callbacks = self._stop(job)
            self._record(job, 'cancel_requested')
            snapshot = self._snapshot(job)
        self._interrupt(callbacks)
        return snapshot

    def cancel_file(self, job_id, file_id):
        """Skip one troublesome source and continue the other files."""
        with self._lock:
            job = self._get(job_id)
            item = job.files_by_id.get(file_id) if isinstance(file_id, str) else None
            if item is None:
                raise GuardError('FILE_NOT_FOUND', 'Файл не относится к заданию.', status=404)
            if all(t.state in DONE for t in item.targets):
                return self._snapshot(job)
            item.cancel.set()
            for task in item.targets:
                if task.state == 'queued':
                    self._fail(job, item, task, GuardError('CANCELLED', 'Файл пропущен.'))
            if item.state in {'awaiting_upload', 'queued'}:
                item.state = 'cancelled'
            callbacks = [item.interrupt] if item.interrupt is not None else []
            self._record(job, 'file_cancel_requested', file_id=item.id)
            snapshot = self._snapshot(job)
        self._interrupt(callbacks)
        return snapshot

    @contextmanager
    def open_result(self, ticket):
        self._prune()
        with self._lock:
            artifact = self._tickets.get(ticket) if isinstance(ticket, str) else None
            if artifact is None:
                raise GuardError('RESULT_NOT_FOUND', 'Результат больше недоступен.', status=404)
            job = self._get(artifact.job_id)
            job.io += 1
        try:
            with artifact.path.open('rb') as stream:
                yield stream, artifact.name, artifact.size
        finally:
            with self._lock:
                job.io -= 1
                self._io_drained.notify_all()
            self._cleanup(job)

    def confirm_saved(self, job_id, ticket):
        """Call only after host-confirmed save completion, never on GET start."""
        with self._lock:
            job = self._get(job_id)
            artifact = self._tickets.get(ticket) if isinstance(ticket, str) else None
            if artifact is None or artifact.job_id != job.id:
                raise GuardError('RESULT_NOT_FOUND', 'Результат не относится к заданию.', status=404)
            for target in job.artifacts if artifact is job.bundle else [artifact]:
                target.saved = True
            artifact.saved = True
            for item in job.files:
                for task in item.targets:
                    if task.state in SUCCESS and task.artifacts and all(a.saved for a in task.artifacts):
                        task.state = 'saved'
                if item.state in SUCCESS | {'partial'}:
                    item.state = self._file_state(item)
            self._record(job, 'save_confirmed', file_id=artifact.file_id, target=artifact.format)
            return self._snapshot(job)

    def release(self, job_id):
        with self._lock:
            job = self._get(job_id)
            job.released = True
            callbacks = self._stop(job)
            for artifact in job.artifacts + ([job.bundle] if job.bundle else []):
                self._tickets.pop(artifact.ticket, None)
        self._interrupt(callbacks)
        self._cleanup(job)

    def _cleanup(self, job):
        with self._lock:
            if not job.released or job.running or job.io or job.cleaning:
                return
            job.cleaning = True
        # Potentially slow filesystem deletion never holds the global job lock.
        try:
            shutil.rmtree(job.root)
        except FileNotFoundError:
            pass
        except OSError:
            with self._lock:
                job.cleaning = False
                self._record(job, 'cleanup_deferred', code='CLEANUP_RETRY')
            return
        with self._lock:
            for artifact in job.artifacts + ([job.bundle] if job.bundle else []):
                self._tickets.pop(artifact.ticket, None)
            if self._jobs.pop(job.id, None) is not None:
                self._reserved -= job.reserved

    def close(self):
        with self._lock:
            self._closed = True
            callbacks = []
            for job in self._jobs.values():
                job.released = True
                callbacks.extend(self._stop(job))
            self._tickets.clear()
        self._interrupt(callbacks)
        self._executor.shutdown(wait=True)
        with self._io_drained:
            self._io_drained.wait_for(lambda: all(j.io == 0 for j in self._jobs.values()), timeout=5)
        self._prune()

    def storage_stats(self):
        with self._lock:
            return {'jobs': len(self._jobs), 'reservedBytes': self._reserved,
                    'tickets': len(self._tickets),
                    'running': sum(j.state in {'running', 'packaging'} for j in self._jobs.values()),
                    'queued': sum(j.state == 'queued' for j in self._jobs.values())}
