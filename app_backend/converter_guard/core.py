"""Disk-owned, opt-in native jobs. One worker; independent file/target failures."""
from __future__ import annotations

from collections import deque, OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
import copy
import hashlib
from dataclasses import dataclass, field
from html.parser import HTMLParser
import json
import math
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
from .result_store import ResultStore

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
    diagnostic: str = ''
    started_ms: int | None = None
    finished_ms: int | None = None
    execution_mode: str | None = None
    started_mono: float | None = None
    duration_ms: int | None = None


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
    content_digest: str | None = None
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
                'ticket': self.ticket,
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
    preparation: bool = False
    preflight_checks: int = 0
    preflight_cache_hits: int = 0
    bundle_cancel: threading.Event = field(default_factory=threading.Event)
    pause_wake: threading.Event = field(default_factory=threading.Event)
    paused: bool = False
    current: object = None
    file_timeout: float | None = None
    changes: object = field(default_factory=lambda: deque(maxlen=4096))
    office_starts: int = 0
    store: object = None
    recovery_error: str | None = None
    recovered: bool = False
    detached: bool = False
    strict_native: bool = False
    save_cancel: threading.Event = field(default_factory=threading.Event)
    timings: dict = field(default_factory=dict)
    preflight_timeout_seconds: float | None = None
    diagnostic_bytes: int = 0
    preflight_identity: object = None


def _source_name(name):
    normalized = ((os.name != 'nt' and len(os.fsencode(name)) > 255) or
        (os.name == 'nt' and (bool(re.search(r'[<>:"|?*]', name)) or
         name.endswith((' ', '.')) or bool(re.fullmatch(
             r'CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]', name.split('.')[0], re.I)))))
    suffix = Path(name).suffix
    safe_suffix = suffix if re.fullmatch(r'\.[a-zA-Z0-9_-]{1,32}', suffix) else '.bin'
    return ('source' + safe_suffix if normalized else name), normalized



class _LocalHtmlResources(HTMLParser):
    """Detect references which cannot survive isolated single-file upload."""
    _tags = {'img', 'link', 'script', 'source', 'video', 'audio', 'iframe', 'object'}
    _attrs = {'src', 'srcset', 'href', 'poster', 'data'}
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.found = False

    @staticmethod
    def _local(value):
        value = value.strip().strip(chr(34)).strip(chr(39))
        if not value or value.startswith(('#', '//', '/', 'data:')):
            return False
        if re.match(r'(?i)^[a-z][a-z0-9+.-]*:', value):
            return False
        return True

    def handle_starttag(self, tag, attrs):
        if tag not in self._tags:
            return
        for key, value in attrs:
            if key in self._attrs and isinstance(value, str):
                # Commas are legal inside data: URLs (including base64).
                # Only srcset is a list; src/href/poster/data are single URIs.
                # Srcset descriptors are separated by commas, typically followed
                # by whitespace; uncertain candidates remain fail-closed.
                candidates = (re.split(r',\s+(?=\S)', value) if key == 'srcset'
                              else [value])
                urls = (part.strip().split()[0] for part in candidates if part.strip())
                if any(self._local(url) for url in urls):
                    self.found = True


def _missing_html_resources(source):
    if source.suffix.lower() not in {'.html', '.htm', '.xhtml'}:
        return False
    inspector = _LocalHtmlResources()
    # Streaming parser: never load an entire user document just to look for
    # relative paths. No external file or network resource is opened.
    with source.open('rb') as stream:
        while not inspector.found:
            block = stream.read(128 * 1024)
            if not block:
                break
            inspector.feed(block.decode('utf-8', errors='replace'))
    return inspector.found

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
    def __init__(self, *, adapter=None, limits=None, spool_parent=None, recovery_parent=None):
        self.adapter = adapter if adapter is not None else LibreOfficeAdapter()
        self.limits = limits if limits is not None else GuardLimits()
        self._recovery = None if recovery_parent is None else Path(recovery_parent).resolve()
        self._parent = self._recovery or (None if spool_parent is None else Path(spool_parent).resolve())
        self._lock = threading.RLock()
        self._io_drained = threading.Condition(self._lock)
        self._jobs, self._tickets = {}, {}
        self._reserved = 0
        # Construction and metadata inspection start no worker. Native preflight
        # is explicit; the export executor starts its thread only at start().
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='aio-guard')
        self._closed = False
        self._preflight_gate = threading.BoundedSemaphore(1)
        self._proof_cache = OrderedDict()
        self._recovery_issues = []
        self._recovery_loaded = not bool(self._recovery)
        self._recovery_lock = threading.Lock()

    def _load_recovery(self):
        # Excel/startup does no recovery scan. The first explicit recovery or
        # retained-job lookup runs on its HTTP caller, never the Qt UI thread.
        with self._recovery_lock:
            if not self._recovery_loaded and not self._closed:
                self._recover_results()
                self._recovery_loaded = True

    def _batch(self, job, name):
        method = getattr(self.adapter, 'batch', None)
        return (method(job.root / name, max_tasks=self.limits.session_max_tasks)
                if method else nullcontext())

    def _journal(self, job, item, task):
        if job.store:
            try:
                job.store.task(item, task)
            except OSError:
                job.recovery_error = 'RECOVERY_WRITE_FAILED'

    def _initialize_store(self, job):
        if self._recovery:
            if job.store is None:
                job.store = ResultStore(job.root)
                try:
                    if not job.store.acquire():
                        raise OSError('Recovery store is busy')
                except OSError:
                    job.recovery_error = 'RECOVERY_WRITE_FAILED'
                    return
            # Persist ownership during preparation; rewrite plans at start.
            try:
                job.store.initialize(job)
            except OSError:
                job.recovery_error = 'RECOVERY_WRITE_FAILED'

    def _recover_results(self):
        for root in sorted(self._recovery.glob('aio-guard-*')):
            if root.is_symlink() or not root.is_dir() or not (root/'job.json').is_file():
                continue
            store = ResultStore(root)
            try:
                if not store.acquire():
                    continue  # Another running A.I.O instance owns this job.
                header, records, saved, integrity = store.read()
                identity = header.get('jobId')
                if not isinstance(identity, str) or not re.fullmatch(r'[0-9a-f]{32}', identity):
                    raise ValueError('Invalid recovery job')
                rows = validate_files(header.get('files'), self.limits)
                files, indices = [], set()
                for row, original in zip(rows, header['files']):
                    tasks = []
                    for target in original.get('targets', []):
                        index, format_id = target['index'], target['format']
                        if (type(index) is not int or index < 0 or index in indices or
                                not isinstance(format_id, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', format_id)):
                            raise ValueError('Invalid recovery task')
                        indices.add(index)
                        tasks.append(_Task(format_id, index, state='cancelled',
                            code='INTERRUPTED_RESTART', error='Задание было прервано. Исходник можно выбрать повторно.'))
                    files.append(_File(**row, source=root/'input'/'unavailable', targets=tasks, state='done',
                                       native_route=original.get('nativeRoute')))
                job = _Job(identity, root, files, 0, store=store, recovered=True)
                if integrity != 'complete':
                    job.recovery_error = 'RECOVERY_LEDGER_INCOMPLETE'
                    self._recovery_issues.append({'folder':root.name,
                        'code':'RECOVERY_LEDGER_INCOMPLETE', 'integrity':integrity,
                        'jobId':identity})
                summary=header.get('summary',{})
                job.timings=summary.get('timings',{})
                job.office_starts=summary.get('officeStarts',0)
                job.file_timeout=summary.get('fileTimeoutSeconds')
                job.files_by_id = {item.id:item for item in files}
                for item in files:
                    for task in item.targets:
                        record = records.get(task.index, {})
                        if record.get('fileId') != item.id or record.get('state') not in DONE:
                            continue
                        task.state, task.code, task.error = record['state'], record.get('code'), record.get('error')
                        task.stage, task.diagnostic = record.get('stage'), str(record.get('diagnostic', ''))[-8192:]
                        task.lo_started = record.get('loStarted') is True
                        task.started_ms, task.finished_ms = record.get('startedMs'), record.get('finishedMs')
                        if any(v is not None and (type(v) is not int or v < 0)
                               for v in (task.started_ms,task.finished_ms,record.get('durationMs'))):
                            raise ValueError('Invalid recovery timing')
                        task.duration_ms=record.get('durationMs')
                        task.execution_mode=record.get('executionMode')
                        if task.state not in SUCCESS:
                            continue
                        for output in record.get('outputs', []):
                            try:
                                relative = output['path']
                                if Path(relative).parts[:3] != ('work', str(task.index), 'output'):
                                    raise ValueError('Unexpected output location')
                                path = store.output_path(relative, output['size'])
                                archive_name = output['archiveName']
                                if (not isinstance(archive_name, str) or '\\' in archive_name or
                                        Path(archive_name).is_absolute() or '..' in Path(archive_name).parts):
                                    raise ValueError('Invalid archive name')
                                artifact = _Artifact(identity, path, path.name, output['size'],
                                    secrets.token_urlsafe(32), item.id, task.format, archive_name,
                                    saved=relative in saved)
                                task.artifacts.append(artifact)
                                job.artifacts.append(artifact)
                            except (OSError, ValueError, KeyError, TypeError):
                                task.state, task.code = 'error', 'RECOVERY_OUTPUT_MISSING'
                                task.error = 'Один из результатов отсутствует или изменён. Доступные файлы сохранены.'
                        if task.artifacts and task.state in SUCCESS:
                            task.state = 'saved' if all(a.saved for a in task.artifacts) else 'ready'
                if not job.artifacts:
                    self._recovery_issues.append({'folder':root.name,
                        'code':'RECOVERY_UNFINISHED_PREPARATION', 'jobId':identity,
                        'bytes':sum(p.stat().st_size for p in root.rglob('*')
                                    if p.is_file() and not p.is_symlink())})
                    store.close()
                    continue
                indexed={a.path.resolve() for a in job.artifacts}
                untracked=any(p.resolve() not in indexed for p in root.glob('work/*/output/**/*')
                              if p.is_file() and not p.is_symlink())
                if untracked and not job.recovery_error:
                    job.recovery_error='RECOVERY_UNTRACKED_OUTPUTS'
                    self._recovery_issues.append({'folder':root.name,
                        'code':'RECOVERY_UNTRACKED_OUTPUTS', 'jobId':identity})
                job.reserved = sum(p.stat().st_size for p in root.rglob('*') if p.is_file() and not p.is_symlink())
                job.result = {**job.artifacts[0].metadata(), 'jobId':identity}
                job.packaging_error = 'RECOVERED_RESULTS'
                job.state = 'partial'
                job.diagnostic_bytes=sum(len(t.diagnostic.encode('utf-8')) for f in files for t in f.targets)
                with self._lock:
                    if self._closed or identity in self._jobs:
                        store.close()
                        continue
                    self._jobs[identity] = job
                    self._reserved += job.reserved
                    for artifact in job.artifacts:
                        self._tickets[artifact.ticket] = artifact
                    self._record(job, 'recovered')
            except (OSError, ValueError, TypeError, KeyError, GuardError):
                store.close()  # Never delete an unreadable recovery directory.
                self._recovery_issues.append({'folder':root.name,'code':'RECOVERY_METADATA_INVALID'})

    def recoverable(self):
        self._load_recovery()
        with self._lock:
            return {'warnings':list(self._recovery_issues),
                    'jobs': [{'jobId': j.id, 'recovered': j.recovered,
                              'label':j.files[0].name if j.files else 'Результаты',
                              'outputs': len(j.artifacts), 'bytes': sum(a.size for a in j.artifacts),
                              'state': j.state}
                             for j in self._jobs.values() if not j.running and not j.released
                             and any(not a.saved for a in j.artifacts)]}

    def _cached_proof(self, key):
        # Metadata only: no source bytes, processes, timers or disk copies.
        now = time.monotonic()
        for old, (stamp, _) in list(self._proof_cache.items()):
            if now - stamp > self.limits.retention_seconds:
                del self._proof_cache[old]
        if key is None or key not in self._proof_cache:
            return None
        stamp, proof = self._proof_cache[key]
        self._proof_cache.move_to_end(key)
        return copy.deepcopy(proof)

    def _remember_proof(self, key, proof):
        if (key is None or proof.get('state') != 'detected'
                or proof.get('registryFingerprint') != key[0][0]):
            return
        self._proof_cache[key] = (time.monotonic(), copy.deepcopy(proof))
        self._proof_cache.move_to_end(key)
        while len(self._proof_cache) > 256:
            self._proof_cache.popitem(last=False)

    def capabilities(self, files):
        return self.adapter.capabilities([] if files == [] else validate_files(files, self.limits))

    @staticmethod
    def _record(job, stage, **details):
        job.trace.add(stage, **details)
        job.revision += 1
        changed = details.get('file_id')
        if stage in {'job_queued', 'save_confirmed', 'job_finished', 'recovered'} and changed is None:
            changed = '*'
        job.changes.append((job.revision, changed))
        if stage in ('preflight_started','preflight_done','job_queued','job_started',
                     'engine_started','packaging_started','bundle_ready','job_finished'):
            job.timings.setdefault(stage+'Ms',round((time.monotonic()-job.trace.started)*1000))

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

    def create(self, files, formats=None, *, plan=None, prepare_only=False):
        rows = validate_files(files, self.limits)
        planned = ({row['id']: [] for row in rows} if prepare_only else
                   validate_plan(rows, formats, plan))
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
                if self._parent:
                    self._parent.mkdir(parents=True, exist_ok=True, mode=0o700)
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
            job = _Job(identity, root, files, size, preparation=prepare_only)
            job.files_by_id = {item.id: item for item in files}
            self._initialize_store(job)
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
        digest = hashlib.sha256()
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
                    digest.update(block)
                    remaining -= len(block)
            with self._lock:
                if job.cancel.is_set() or item.cancel.is_set():
                    raise GuardError('CANCELLED', 'Загрузка отменена.', stage='upload')
                part.replace(item.source)
                item.state = 'queued'
                item.content_digest = digest.hexdigest()
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

    def update_prepared(self, job_id, files):
        """Reuse immutable stored files/proofs; upload and inspect only additions."""
        rows = validate_files(files, self.limits)
        with self._lock:
            job = self._get(job_id)
            if not job.preparation or job.state != 'prepared' or job.running:
                raise GuardError('JOB_STATE', 'Предыдущая проверка ещё не завершена.', status=409)
            for row in rows:
                item = job.files_by_id.get(row['id'])
                if item and (item.name != row['name'] or item.size != row['size']):
                    raise GuardError('INPUT_ID', 'Изменённый файл требует нового задания.')
            ids = {row['id'] for row in rows}
            removed = [f for f in job.files if f.id not in ids]
            additions = [row for row in rows if row['id'] not in job.files_by_id]
            # Check the conservative quota before changing ownership. Failed
            # cleanup remains accounted rather than granting phantom space.
            maximum = self.limits.max_spool_bytes
            if maximum is not None and self._reserved + sum(r['size'] for r in additions) > maximum:
                raise GuardError('SPOOL_QUOTA', 'Недостаточно настроенной квоты Guard.', status=413)
            self._reserve(job, sum(r['size'] for r in additions))
            for item in removed:
                self._drop_source(job, item)
            for row in additions:
                name, normalized = _source_name(row['name'])
                item = _File(**row, source=job.root/'input'/secrets.token_hex(8)/name,
                             targets=[], name_normalized=normalized)
                job.files_by_id[item.id] = item
            job.files = [job.files_by_id[row['id']] for row in rows]
            job.files_by_id = {f.id:f for f in job.files}
            job.state = 'uploading'
            self._record(job, 'prepared_selection_updated')
            return self._snapshot(job)

    def preflight(self, job_id, *, recheck=False, timeout_seconds=None, only_undetermined=False):
        """Inspect stored originals once, before any requested export."""
        if type(recheck) is not bool or type(only_undetermined) is not bool:
            raise GuardError('PREFLIGHT_REQUEST', 'Некорректный режим проверки.')
        if timeout_seconds is not None and (isinstance(timeout_seconds,bool) or
                not isinstance(timeout_seconds,(int,float)) or not math.isfinite(timeout_seconds) or timeout_seconds<=0):
            raise GuardError('PREFLIGHT_REQUEST','Укажите положительное время проверки.')
        native_timeout=self.limits.preflight_timeout if timeout_seconds is None else timeout_seconds
        if not self._preflight_gate.acquire(blocking=False):
            raise GuardError('PREFLIGHT_BUSY', 'Завершается предыдущая проверка.', stage='preflight', status=409)
        job = None
        try:
            with self._lock:
                job = self._get(job_id)
                if (not job.preparation or job.state not in {'uploading', 'prepared'} or job.running or
                        any(f.uploading or f.state not in {'queued','prepared'} for f in job.files)):
                    job = None
                    raise GuardError('JOB_STATE', 'Дождитесь загрузки исходников.', status=409)
                job.state, job.running = 'checking', True
                job.preflight_timeout_seconds = native_timeout
                job.io += 1
                self._record(job, 'preflight_started')
            with self._batch(job, 'preflight-office') as office:
                identity = getattr(self.adapter, 'preflight_fingerprint', lambda: None)()
                engine_changed=identity!=job.preflight_identity
                job.preflight_identity=identity
                for position, item in enumerate(job.files):
                    if job.cancel.is_set():
                        raise GuardError('CANCELLED', 'Предварительная проверка отменена.', stage='preflight')
                    if item.state == 'prepared' and not recheck and not engine_changed:
                        continue
                    if (only_undetermined and item.state == 'prepared' and item.native_route
                            and item.native_route.get('state') == 'detected' and identity
                            and item.native_route.get('registryFingerprint') == identity[0]):
                        continue
                    if recheck:
                        item.cancel.clear()
                    key = (identity, item.content_digest, item.source.name) if identity and item.content_digest else None
                    with self._lock:
                        proof = None if recheck else self._cached_proof(key)
                        if proof is not None:
                            item.native_route, item.state = proof, 'prepared'
                            job.preflight_cache_hits += 1
                            self._record(job, 'preflight_cache_hit', file_id=item.id)
                    if proof is not None:
                        continue
                    work = job.root / 'preflight' / item.source.parent.name
                    with self._lock:
                        job.preflight_checks += 1
                        job.current = (item, _Task('', -position - 1, state='converting',
                            stage='preflight', started_ms=round(time.time()*1000)))
                        self._record(job, 'preflight_file_started', file_id=item.id)
                    try:
                        if _missing_html_resources(item.source):
                            proof = {'state':'undetermined',
                                     'code':'HTML_LOCAL_RESOURCES_NOT_UPLOADED',
                                     'exported':False}
                        else:
                            proof = self.adapter.preflight(item.source, work, cancel=_Cancellation(job.cancel,item.cancel),
                                                           timeout=native_timeout)
                    except GuardError as error:
                        if error.code == 'CANCELLED' and job.cancel.is_set():
                            raise
                        proof = {'state': 'undetermined', 'code': 'PREFLIGHT_SKIPPED'
                                 if error.code == 'CANCELLED' else error.code}
                    except Exception:
                        proof = {'state': 'undetermined', 'code': 'PREFLIGHT_API_UNAVAILABLE'}
                    finally:
                        self._remove_work(job, work)
                    with self._lock:
                        if job.cancel.is_set():
                            raise GuardError('CANCELLED', 'Предварительная проверка отменена.', stage='preflight')
                        self._remember_proof(key, proof)
                        item.native_route = proof
                        item.state = 'prepared'
                        job.current = None
                        self._record(job, 'preflight_finished', file_id=item.id,
                                     code=proof.get('code', 'NATIVE_KIND_DETECTED'))
            with self._lock:
                if job.cancel.is_set():
                    raise GuardError('CANCELLED', 'Предварительная проверка отменена.', stage='preflight')
                job.state = 'prepared'
                report = self.adapter.capabilities(
                    [{'id': f.id, 'name': f.name, 'size': f.size} for f in job.files],
                    proofs={f.id: f.native_route for f in job.files})
                report['preflightChecks'] = job.preflight_checks
                report['preflightCacheHits'] = job.preflight_cache_hits
                return report
        finally:
            if job is not None:
                with self._lock:
                    job.running = False
                    job.current = None
                    if job.cancel.is_set():
                        job.state = 'cancelled'
                    job.io -= 1
                    self._record(job,'preflight_done')
                    self._io_drained.notify_all()
                self._cleanup(job)
            self._preflight_gate.release()

    def start(self, job_id, *, formats=None, file_ids=None, only_verified=False, plan=None,
              timeout_seconds=None):
        planning_started = time.monotonic()
        if timeout_seconds is not None and (isinstance(timeout_seconds, bool) or
                not isinstance(timeout_seconds, (int, float)) or
                not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
            raise GuardError('ENGINE_TIMEOUT_POLICY', 'Укажите положительное время обработки.')
        # Inventory/stat work happens once, before entering the global lock.
        decider = getattr(self.adapter, 'route_decider', None)
        routing = (decider() if decider else
                   getattr(self.adapter, 'route_decision', lambda target, proof: 'undetermined'))
        with self._lock:
            job = self._get(job_id)
            if type(only_verified) is not bool:
                raise GuardError('CONVERSION_PLAN', 'Некорректный режим предварительного плана.', stage='plan')
            if job.preparation:
                job.strict_native = True
                if job.state != 'prepared' or job.running:
                    raise GuardError('JOB_STATE', 'Сначала завершите предварительную проверку.', status=409)
                if (not isinstance(file_ids, list) or not file_ids or
                        any(not isinstance(i, str) or i not in job.files_by_id for i in file_ids)
                        or len(set(file_ids)) != len(file_ids)):
                    raise GuardError('CONVERSION_PLAN', 'Выберите подготовленные исходники.', stage='plan')
                selected = [job.files_by_id[i] for i in file_ids]
                planned = validate_plan([{'id': f.id, 'name': f.name, 'size': f.size} for f in selected],
                                        formats, plan)
                selected_ids = set(file_ids)
                for item in job.files:
                    if item.id not in selected_ids:
                        self._drop_source(job, item)
                job.files = selected
                job.files_by_id = {f.id: f for f in selected}
                task_index = 0
                for item in job.files:
                    item.state = 'queued'
                    item.targets = [_Task(target, task_index + i) for i, target in enumerate(planned[item.id])]
                    task_index += len(item.targets)
                    for task in item.targets:
                        decision = routing(task.format, item.native_route)
                        if decision != 'registered':
                            task.state, task.stage = 'skipped', 'preflight'
                            missing_html = (item.native_route or {}).get('code') == 'HTML_LOCAL_RESOURCES_NOT_UPLOADED'
                            task.code = ('HTML_LOCAL_RESOURCES_NOT_UPLOADED' if missing_html else
                                         'NATIVE_ROUTE_UNAVAILABLE' if decision == 'unavailable' else
                                         'PREFLIGHT_ROUTE_UNDETERMINED')
                            task.error = ('HTML ссылается на локальные файлы, но AIO получил только основной HTML. '
                                          'Чтобы не потерять изображения/стили, экспорт не запускается.'
                                          if missing_html else 'У определённого LibreOffice типа нет этого экспорта.'
                                          if decision == 'unavailable' else
                                          'Направление не подтверждено; экспорт не запускается.')
                            task.route = copy.deepcopy(item.native_route)
                            self._record(job, 'preflight_route_excluded', file_id=item.id,
                                         target=task.format, code=task.code)
                job.state = 'uploading'
                job.preparation = False
            elif formats is not None or file_ids is not None or only_verified or plan is not None:
                raise GuardError('JOB_STATE', 'Это задание создано без предварительной подготовки.', status=409)
            if job.state != 'uploading' or any(
                    f.uploading or f.state not in {'queued', 'cancelled'} for f in job.files):
                raise GuardError('JOB_STATE', 'Дождитесь загрузки всех файлов.', status=409)
            job.state, job.running = 'queued', True
            job.timings['planMs'] = round((time.monotonic()-planning_started)*1000)
            job.file_timeout = self.limits.file_timeout if timeout_seconds is None else timeout_seconds
            self._initialize_store(job)
            self._record(job, 'job_queued')
            job.future = self._executor.submit(self._run, job)
            return self._snapshot(job)

    def _fail(self, job, item, task, error):
        task.state = 'cancelled' if error.code == 'CANCELLED' else 'error'
        task.code, task.error, task.stage = error.code, error.message, error.stage
        diagnostic=getattr(error,'diagnostic','').encode('utf-8',errors='replace')
        remaining=max(0,2*1024*1024-job.diagnostic_bytes)
        task.diagnostic=diagnostic[-min(8192,remaining):].decode('utf-8',errors='ignore') if remaining else ''
        job.diagnostic_bytes+=len(task.diagnostic.encode('utf-8'))
        task.finished_ms = round(time.time() * 1000)
        task.duration_ms = round((time.monotonic()-task.started_mono)*1000) if task.started_mono else None
        self._journal(job, item, task)
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
            task.finished_ms = round(time.time() * 1000)
            task.duration_ms = round((time.monotonic()-task.started_mono)*1000) if task.started_mono else None
            self._journal(job, item, task)
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
        if item.state in {'awaiting_upload', 'queued', 'converting', 'prepared'}:
            return item.state
        states = [task.state for task in item.targets]
        if states and all(s == 'skipped' for s in states):
            return 'skipped'
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
            with self._batch(job, 'export-office') as office:
                for item in job.files:
                    source_failure = None
                    prepare_source = getattr(self.adapter, 'prepare_source', None)
                    if prepare_source:
                        prepare_source(item.native_route)
                    for task in item.targets:
                        self._wait_unpaused(job)
                        workspace = job.root / 'work' / str(task.index)
                        with self._lock:
                            if task.state in DONE:
                                continue
                            if job.cancel.is_set() or item.cancel.is_set():
                                if task.state not in DONE:
                                    self._fail(job, item, task, GuardError('CANCELLED', 'Конвертация отменена.'))
                                continue
                            if source_failure:
                                self._fail(job, item, task, GuardError(
                                    'SOURCE_DEFERRED_AFTER_TIMEOUT' if source_failure == 'ENGINE_TIMEOUT' else
                                    'PREFLIGHT_ROUTE_UNDETERMINED' if source_failure == 'NATIVE_MODEL_CHANGED' else 'SOURCE_IMPORT_FAILED',
                                    'Оставшиеся направления этого файла отложены после тайм-аута. Можно повторить с большим временем.'
                                    if source_failure == 'ENGINE_TIMEOUT' else
                                    'Нативная модель изменилась. Повторите проверку исходника.' if source_failure == 'NATIVE_MODEL_CHANGED' else
                                    'LibreOffice не смог открыть этот исходник; повторные экспорты не запускались.',
                                    stage='queue' if source_failure == 'ENGINE_TIMEOUT' else 'import'))
                                continue
                            item.state, task.state = 'converting', 'converting'
                            task.started_ms = round(time.time() * 1000)
                            task.started_mono = time.monotonic()
                            task.stage = 'engine'
                            job.current = (item, task)
                            self._record(job, 'route_selected', file_id=item.id, target=task.format)
                        try:
                            def started():
                                with self._lock:
                                    task.lo_started = True
                                    task.execution_mode = getattr(self.adapter,'execution_mode',lambda:None)()
                                    self._record(job, 'engine_started', file_id=item.id,
                                                  target=task.format, lo_started=True)

                            routing = getattr(self.adapter, 'route_decision', None)
                            decision = routing(task.format, item.native_route) if routing else None
                            if routing and (decision == 'unavailable' or job.strict_native and decision != 'registered'):
                                with self._lock:
                                    task.state, task.code, task.stage = 'skipped', 'NATIVE_ROUTE_UNAVAILABLE', 'route'
                                    task.error = ('Для определённого LibreOffice типа документа нет экспорта в '
                                                  + task.format.upper() + '.')
                                    task.route = dict(item.native_route)
                                    if decision != 'unavailable':
                                        task.code, task.error = 'PREFLIGHT_ROUTE_UNDETERMINED', 'Подтверждение направления устарело. Повторите проверку.'
                                    task.finished_ms = round(time.time() * 1000)
                                    self._journal(job, item, task)
                                    self._record(job, 'route_filtered', file_id=item.id,
                                                 target=task.format, code=task.code)
                                continue

                            def detected(proof):
                                with self._lock:
                                    item.native_route = dict(proof)
                                    self._record(job, 'native_document_identified', file_id=item.id,
                                                 target=task.format, lo_started=task.lo_started)

                            needs_observation = getattr(self.adapter, 'observation_needed', lambda proof: True)
                            def stage_changed(stage):
                                if stage in ('import','export'):
                                    with self._lock:
                                        task.stage=stage
                                        self._record(job,'native_stage',file_id=item.id,target=task.format)
                            stages = ({'on_stage':stage_changed} if getattr(self.adapter.convert,'__func__',None)
                                      is LibreOfficeAdapter.convert else {})
                            paths = self.adapter.convert(item.source, task.format, workspace,
                                cancel=_Cancellation(job.cancel, item.cancel),
                                timeout=job.file_timeout, on_started=started,
                                output_limit=(None if self.limits.max_spool_bytes is None else self._output_budget),
                                **stages, **({'on_document_service': detected if needs_observation(item.native_route) else None}
                                   if routing else {}))
                            self._retain_outputs(job, item, task, workspace, paths)
                        except GuardError as error:
                            if error.code in ('ENGINE_TIMEOUT', 'SOURCE_IMPORT_FAILED','NATIVE_MODEL_CHANGED'):
                                source_failure = error.code
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
                            with self._lock:
                                job.current = None
                                if office is not None:
                                    job.office_starts = office.starts
                                self._record(job, 'task_finished', file_id=item.id, target=task.format, code=task.code)
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
                if job.artifacts and job.bundle is None:
                    job.packaging_error = 'BUNDLE_INTERRUPTED'
                self._record(job, 'job_failed', code='GUARD_INTERNAL')
        finally:
            with self._lock:
                job.running = False
                job.touched = time.monotonic()
                self._record(job, 'worker_finished', code=job.state)
                if job.store:
                    try:job.store.finish(job)
                    except OSError:job.recovery_error='RECOVERY_WRITE_FAILED'
            self._cleanup(job)

    def _wait_unpaused(self, job):
        while True:
            with self._lock:
                if not job.paused or job.cancel.is_set():
                    if job.state == 'paused':
                        job.state = 'running'
                        self._record(job, 'queue_resumed')
                    return
                if job.state != 'paused':
                    job.state = 'paused'
                    self._record(job, 'queue_paused')
                job.pause_wake.clear()
            job.pause_wake.wait(.1)

    def pause(self, job_id, paused=True):
        if type(paused) is not bool:
            raise GuardError('QUEUE_PAUSE', 'Некорректное состояние паузы.')
        with self._lock:
            job = self._get(job_id)
            if not job.running or job.state == 'packaging':
                raise GuardError('JOB_STATE', 'Очередь сейчас не выполняется.', status=409)
            job.paused = paused
            job.pause_wake.set()
            self._record(job, 'pause_requested' if paused else 'resume_requested')
            return self._snapshot(job)

    @staticmethod
    def _counts(job):
        # One pass, shared by live status and the downloadable report. Route
        # skips are neither successes nor process errors; attempts may fail.
        counts = dict.fromkeys(('total', 'completed', 'attempted', 'succeeded',
                                'failed', 'cancelled', 'filtered', 'pending'), 0)
        for item in job.files:
            for task in item.targets:
                counts['total'] += 1
                counts['attempted'] += bool(task.lo_started)
                counts['completed'] += task.state in DONE
                if task.state in SUCCESS:
                    counts['succeeded'] += 1
                elif task.state == 'error':
                    counts['failed'] += 1
                elif task.state == 'cancelled':
                    counts['cancelled'] += 1
                elif task.state == 'skipped':
                    counts['filtered'] += 1
                else:
                    counts['pending'] += 1
        return counts

    def _report(self, job):
        return {'schemaVersion': 3, **self._counts(job), 'preflightChecks':job.preflight_checks,
                'recoveryError':job.recovery_error, 'files': [
            {'fileId': f.id, 'name': f.name, 'nativeRoute': f.native_route, 'warnings': (
                ['SOURCE_NAME_NORMALIZED'] if f.name_normalized else []),
             'targets': [{'taskId': job.id + ':' + str(t.index), 'format': t.format, 'state': t.state, 'code': t.code,
                          'error': t.error, 'stage': t.stage,
                          'diagnostic': t.diagnostic,
                          'executionMode': t.execution_mode,
                          'durationMs': t.duration_ms,
                          'outputs': [{'path': a.archive_name, 'size': a.size} for a in t.artifacts],
                          'route': t.route,
                          'libreOfficeStarted': t.lo_started} for t in f.targets]}
            for f in job.files], 'timings':dict(job.timings),'qualityCompared': False}

    def report(self, job_id):
        if job_id not in self._jobs:
            self._load_recovery()
        with self._lock:
            job = self._get(job_id)
            return {**self._report(job), 'jobId': job.id, 'state': job.state,
                    'officeStarts':job.office_starts,'trace':job.trace.snapshot(),
                    'fileTimeoutSeconds':job.file_timeout,'recoveryError':job.recovery_error}

    def _make_result(self, job):
        with self._lock:
            if not job.artifacts:
                return
            if job.bundle is not None:
                return
            # Until an archive is actually ready, keep a real, downloadable
            # artifact as fallback. A ZIP name without a ticket loses the save
            # path when packaging is cancelled, out of quota or out of space.
            job.result = {**job.artifacts[0].metadata()}
            job.result['jobId'] = job.id
            self._record(job, 'results_available')
            # A single successful artifact needs no duplicate archive at all.
            if len(job.artifacts) == 1 and all(t.state in SUCCESS for f in job.files for t in f.targets):
                return
            report = json.dumps(self._report(job), ensure_ascii=False, indent=2).encode('utf-8')
            budget = (sum(a.size + 512 + 2 * len(a.archive_name.encode('utf-8')) for a in job.artifacts)
                      + len(report) + 4096)
            if job.bundle_cancel.is_set():
                job.packaging_error = 'BUNDLE_CANCELLED'
                self._record(job, 'bundle', code=job.packaging_error)
                return
            try:
                self._reserve(job, budget)
            except GuardError:
                job.packaging_error = 'BUNDLE_QUOTA'
                self._record(job, 'bundle', code=job.packaging_error)
                return
        bundle, retained = job.root / ('results-' + secrets.token_hex(6) + '.zip'), False
        try:
            with zipfile.ZipFile(bundle, 'x', compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
                for artifact in job.artifacts:
                    info = zipfile.ZipInfo.from_file(artifact.path, arcname=artifact.archive_name)
                    # Force ZIP64 even when the stream length was not given to
                    # ZipFile.open: no hidden 2 GiB failure half-way through.
                    with artifact.path.open('rb') as source, archive.open(
                            info, 'w', force_zip64=True) as output:
                        while block := source.read(CHUNK):
                            if job.bundle_cancel.is_set():
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
                name = ('AIO_Results_Partial.zip' if any(t.state not in SUCCESS for f in job.files for t in f.targets)
                        else 'AIO_Results.zip')
                artifact = _Artifact(job.id, bundle, name, actual, secrets.token_urlsafe(32))
                job.packaging_error = None
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

    def rebuild_bundle(self, job_id):
        with self._lock:
            job = self._get(job_id)
            if job.running or not job.artifacts:
                raise GuardError('JOB_STATE', 'Дождитесь готовых результатов.', status=409)
            if job.bundle is not None:
                return self._snapshot(job)
            job.bundle_cancel.clear()
            job.state, job.running = 'packaging', True
            self._record(job, 'packaging_started')
            job.future = self._executor.submit(self._rebuild_bundle, job)
            return self._snapshot(job)

    def _rebuild_bundle(self, job):
        try:
            self._make_result(job)
        except Exception:
            with self._lock:
                job.packaging_error='BUNDLE_INTERRUPTED'
                self._record(job,'bundle',code=job.packaging_error)
        finally:
            with self._lock:
                job.state, job.running = 'partial', False
                self._record(job, 'job_finished')
            self._cleanup(job)

    def save_folder(self, ticket, destination, *, cancel=None, on_progress=lambda current, total: None):
        """Desktop-only, one directory choice; no paths accepted by HTTP API."""
        cancel = cancel or threading.Event()
        with self._lock:
            anchor = self._tickets.get(ticket)
            if anchor is None:
                raise GuardError('RESULT_NOT_FOUND', 'Результат больше недоступен.', status=404)
            job = self._get(anchor.job_id)
            cancel = _Cancellation(cancel,job.save_cancel)
            if job.running:
                raise GuardError('JOB_STATE', 'Дождитесь остановки очереди.', status=409)
            copies = list(job.artifacts)
            report = self._report(job)
            job.io += 1
        folder = None
        try:
            destination = Path(destination).resolve(strict=True)
            if self.owns_path(destination):
                raise GuardError('SAVE_OWNED_STORAGE','Выберите папку вне служебного хранилища AIO.',stage='save')
            required = sum(a.size for a in copies)
            if not destination.is_dir() or shutil.disk_usage(destination).free < required + 1024 * 1024:
                raise GuardError('OUTPUT_DISK_SPACE', 'Недостаточно места в выбранной папке.', stage='save', status=507)
            # One fresh root preserves every native relative sidecar path and
            # avoids both overwrites and thousands of collision dialogs.
            for attempt in range(100):
                name = 'AIO_Results_' + job.id[:8] + ('' if attempt == 0 else '_' + str(attempt))
                folder = destination / name
                try:
                    folder.mkdir()
                    break
                except FileExistsError:
                    continue
            else:
                raise GuardError('OUTPUT_COLLISION', 'Не удалось создать отдельную папку результатов.', stage='save')
            manifest = []
            for number, artifact in enumerate(copies, 1):
                if cancel.is_set():
                    raise GuardError('SAVE_CANCELLED', 'Сохранение отменено. Исходные результаты остаются в AIO.', stage='save')
                relative = Path(artifact.archive_name)
                if relative.is_absolute() or '..' in relative.parts:
                    raise GuardError('ENGINE_OUTPUT_PATH', 'Недопустимое имя результата.', stage='save')
                target = folder / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.parent.resolve().is_relative_to(folder.resolve()):
                    raise GuardError('ENGINE_OUTPUT_PATH', 'Недопустимый путь результата.', stage='save')
                part = target.with_name(target.name + '.aio-part-' + secrets.token_hex(6))
                digest, written = hashlib.sha256(), 0
                try:
                    with artifact.path.open('rb') as source, part.open('xb') as output:
                        while block := source.read(CHUNK):
                            if cancel.is_set():
                                raise GuardError('SAVE_CANCELLED', 'Сохранение отменено. Результаты остаются в AIO.', stage='save')
                            output.write(block)
                            digest.update(block)
                            written += len(block)
                        output.flush()
                        os.fsync(output.fileno())
                    if written != artifact.size or target.exists():
                        raise GuardError('SAVE_CHANGED', 'Результат изменён или имя уже занято.', stage='save')
                    # Destination is a newly created private result tree.
                    part.replace(target)
                finally:
                    part.unlink(missing_ok=True)
                manifest.append({'path': artifact.archive_name, 'size': written,
                                 'sha256': digest.hexdigest(), 'fileId':artifact.file_id,
                                 'format':artifact.format})
                on_progress(number, len(copies))
            from .result_store import atomic_json
            atomic_json(folder / 'AIO-conversion-report.json', report)
            atomic_json(folder / 'AIO-results-manifest.json', {'schema':1,'outputs':manifest})
            if cancel.is_set():
                raise GuardError('SAVE_CANCELLED', 'Сохранение отменено. Результаты остаются в AIO.', stage='save')
            with self._lock:
                for artifact in copies:
                    artifact.saved = True
                for item in job.files:
                    for task in item.targets:
                        if task.state in SUCCESS and task.artifacts and all(a.saved for a in task.artifacts):
                            task.state = 'saved'
                if job.store:
                    try:
                        job.store.saved(copies)
                    except OSError:
                        job.recovery_error = 'RECOVERY_WRITE_FAILED'
                self._record(job, 'save_confirmed')
            return {'saved':True, 'savedAll':job.recovery_error is None,
                    'incompleteRecovery':bool(job.recovery_error),
                    'name':folder.name, 'outputs':len(copies)}
        except OSError as error:
            raise GuardError('SAVE_STORAGE_FAILED', 'Не удалось сохранить все файлы. Результаты остаются в AIO.',
                             stage='save', status=507) from error
        finally:
            with self._lock:
                job.io -= 1
                self._io_drained.notify_all()
            self._cleanup(job)

    def owns_path(self, path):
        """Do not confirm a save into a directory our cleanup will remove."""
        resolved=Path(path).resolve()
        with self._lock:
            roots=[j.root for j in self._jobs.values()]
            if self._parent:
                roots.append(self._parent)
        return any(resolved.is_relative_to(root.resolve()) for root in roots)

    def _snapshot(self, job, include_trace=False, only_files=None):
        rows = []
        for item in job.files:
            if only_files is not None and item.id not in only_files:
                continue
            targets = [{'taskId': job.id + ':' + str(t.index), 'format': t.format, 'state': t.state, 'code': t.code, 'error': t.error,
                        'stage': t.stage, 'libreOfficeStarted': t.lo_started,
                        'executionMode':t.execution_mode,
                        'startedMs': t.started_ms, 'finishedMs': t.finished_ms,
                        'route': copy.deepcopy(t.route),
                        'percent': 100 if t.state in SUCCESS else None,
                        'outputs': [a.metadata() for a in t.artifacts]} for t in item.targets]
            rows.append({'fileId': item.id, 'name': item.name, 'state': self._file_state(item),
                         'cancellationRequested': job.cancel.is_set() or item.cancel.is_set(),
                         'nativeRoute': copy.deepcopy(item.native_route),
                         'targets': targets, 'warnings': (
                             ['SOURCE_NAME_NORMALIZED'] if item.name_normalized else [])})
        counts = self._counts(job)
        result = copy.deepcopy(job.result)
        if result is not None:
            result['outputs'] = [a.metadata() for a in job.artifacts]
        finished = not job.running and job.state in TERMINAL
        current = None
        if job.current:
            item, task = job.current
            current = {'taskId': job.id + ':' + str(task.index), 'fileId': item.id,
                       'name': item.name, 'format': task.format, 'stage': task.stage,
                       'startedMs': task.started_ms, 'position': task.index + 1}
            if task.stage == 'preflight':
                current['taskId']=job.id+':preflight:'+item.id
                current['position']=-task.index
        return {'schemaVersion': 3, 'jobId': job.id, 'revision': job.revision,
                'state': job.state, 'finished': finished,
                'diagnosticBytes':job.diagnostic_bytes,
                'currentTask': current, 'pauseRequested': job.paused,
                'fileTimeoutSeconds': job.file_timeout, 'officeStarts': job.office_starts,
                'recovered': job.recovered, 'recoveryError': job.recovery_error,
                'timings':dict(job.timings),
                'files': rows, **counts, 'preflightChecks':job.preflight_checks,
                'preflightTotal':len(job.files),
                'preflightTimeoutSeconds':job.preflight_timeout_seconds,
                'preflightCompleted':sum(f.state == 'prepared' for f in job.files),
                'preparation':job.preparation,
                'preflightUndetermined':sum(not f.native_route or f.native_route.get('state')!='detected' for f in job.files),
                'percent': counts['completed'] * 100 / counts['total'] if counts['total'] else None, 'result': result,
                'trace': job.trace.snapshot() if include_trace else None,
                'packagingError': job.packaging_error}

    def status(self, job_id, *, include_trace=False, since_revision=None, delta=False):
        if job_id not in self._jobs:
            self._load_recovery()
        if since_revision is not None and (type(since_revision) is not int or since_revision < 0):
            raise GuardError('STATUS_REVISION', 'Некорректная версия состояния.')
        self._prune()
        with self._lock:
            job = self._get(job_id)
            if not include_trace and since_revision == job.revision:
                return {'jobId': job.id, 'revision': job.revision, 'unchanged': True}
            if (delta and not include_trace and since_revision is not None and job.changes
                    and job.changes[0][0] <= since_revision + 1 <= job.revision):
                changes = {file_id for revision, file_id in job.changes if revision > since_revision}
                if '*' not in changes:
                    return {**self._snapshot(job, only_files=changes), 'delta': True,
                            'baseRevision': since_revision}
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
                    if task.state not in DONE:
                        self._fail(job, item, task, GuardError('CANCELLED', 'Конвертация отменена.'))
                item.state = 'cancelled'
            job.state, job.running = 'cancelled', False

    def _stop(self, job):
        job.cancel.set()
        job.pause_wake.set()
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
            if job.state == 'packaging':
                job.bundle_cancel.set()
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
            if not job.preparation and all(t.state in DONE for t in item.targets):
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
            if job.store:
                try:
                    job.store.saved(job.artifacts if artifact is job.bundle else [artifact])
                except OSError:
                    job.recovery_error = 'RECOVERY_WRITE_FAILED'
            for item in job.files:
                for task in item.targets:
                    if task.state in SUCCESS and task.artifacts and all(a.saved for a in task.artifacts):
                        task.state = 'saved'
                if item.state in SUCCESS | {'partial'}:
                    item.state = self._file_state(item)
            self._record(job, 'save_confirmed', file_id=artifact.file_id, target=artifact.format)
            return self._snapshot(job)

    def release(self, job_id, *, discard=False):
        if type(discard) is not bool:
            raise GuardError('DISCARD_REQUEST', 'Некорректное подтверждение удаления.')
        with self._lock:
            job = self._get(job_id)
            if self._recovery and not discard and (job.recovery_error or
                    (job.running and not job.preparation) or
                    any(not a.saved for a in job.artifacts)):
                job.detached = True
                callbacks = self._stop(job)
                self._record(job, 'results_retained')
                preserve = True
            else:
                preserve = False
                job.bundle_cancel.set()
                job.save_cancel.set()
                job.released = True
                callbacks = self._stop(job)
                for artifact in job.artifacts + ([job.bundle] if job.bundle else []):
                    self._tickets.pop(artifact.ticket, None)
        self._interrupt(callbacks)
        if not preserve:
            self._cleanup(job)

    def _cleanup(self, job):
        with self._lock:
            if not job.released or job.running or job.io or job.cleaning:
                return
            job.cleaning = True
        # Potentially slow filesystem deletion never holds the global job lock.
        try:
            if job.store:
                job.store.close()
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
                job.bundle_cancel.set()
                job.save_cancel.set()
                job.released = not bool(self._recovery)
                callbacks.extend(self._stop(job))
            self._tickets.clear()
            self._proof_cache.clear()
        self._interrupt(callbacks)
        self._executor.shutdown(wait=True)
        with self._io_drained:
            self._io_drained.wait_for(lambda: all(j.io == 0 for j in self._jobs.values()), timeout=5)
        self._prune()
        if self._recovery:
            with self._lock:
                jobs = list(self._jobs.values())
            for job in jobs:
                if job.recovery_error or (job.artifacts and any(not a.saved for a in job.artifacts)):
                    if job.store:
                        job.store.close()
                else:
                    job.released = True
                    self._cleanup(job)
            with self._lock:
                self._jobs.clear()
                self._reserved = 0

    def storage_stats(self):
        with self._lock:
            return {'jobs': len(self._jobs), 'reservedBytes': self._reserved,
                    'tickets': len(self._tickets),
                    'running': sum(j.state in {'checking', 'running', 'packaging'} for j in self._jobs.values()),
                    'queued': sum(j.state == 'queued' for j in self._jobs.values())}
