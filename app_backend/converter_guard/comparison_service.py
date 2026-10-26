"""Optional disk-backed before/after jobs; no analysis on ordinary conversion."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
import copy
import hashlib
import io
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import sys
import threading
import time
import unicodedata
from urllib.parse import urlencode

from .errors import GuardError
from ..native_process import ProcessTree

STATUSES = ('confirmed_loss', 'format_limitation', 'uncertain', 'not_checked', 'checked')
FINAL = {'completed', 'cancelled', 'failed', 'interrupted'}
MAX_PREVIEW = 64 * 1024 * 1024  # Preview only; never conversion admission.
MAX_WORKER_MEMORY = 768 * 1024 * 1024  # Disposable analysis/preview only.
MAX_SESSIONS = 32
MAX_RETRY_RUNS = 128
MAX_RETRY_EVENTS = 8192
MAX_RETRY_TASKS = 100000
MAX_REPORT_BYTES = 128 * 1024 * 1024
IDENTITY = re.compile(r'[a-f0-9]{32}\Z')


@contextmanager
def _database(path, *, create=False):
    mode = 'rwc' if create else 'rw'
    try:
        connection = sqlite3.connect(Path(path).resolve().as_uri()+'?mode='+mode, uri=True)
    except sqlite3.Error as error:
        raise GuardError('COMPARISON_REPORT_UNAVAILABLE', 'Отчёт анализа недоступен. Результаты конвертации остаются в AIO.', status=404) from error
    try:
        with connection:
            yield connection
    except sqlite3.Error as error:
        raise GuardError('COMPARISON_REPORT_UNAVAILABLE', 'Не удалось прочитать отчёт анализа. Результаты остаются в AIO.', status=404) from error
    finally:
        connection.close()


@dataclass
class _Analysis:
    job_id: str
    identity: str = field(default_factory=lambda: secrets.token_hex(16))
    state: str = 'queued'
    total: int = 0
    processed: int = 0
    summary: dict = field(default_factory=lambda: dict.fromkeys(STATUSES, 0))
    cancel: threading.Event = field(default_factory=threading.Event)
    pause: threading.Event = field(default_factory=threading.Event)
    root: object = None
    db: object = None
    error: str | None = None
    retry: object = None
    retry_cancel: threading.Event = field(default_factory=threading.Event)
    retry_jobs: list = field(default_factory=list)
    pin: object = None
    records: object = None
    future: object = None
    catalog: object = None
    created_at: int = field(default_factory=lambda: round(time.time() * 1000))
    updated_at: int = field(default_factory=lambda: round(time.time() * 1000))
    recovered: bool = False
    persistence_error: str | None = None
    report_bytes: int = 0
    retry_future: object = None
    retry_pin: object = None
    expected_tasks: dict = field(default_factory=dict)
    validated_stamp: object = None


class ComparisonService:
    def __init__(self, guard):
        self.guard = guard
        self.lock = threading.RLock()
        self.jobs = {}
        self._retry_reservation = None
        self.closed = False
        self.preview_gate = threading.BoundedSemaphore(1)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='aio-comparison')
        self.retry_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='aio-auto-retry')

    @staticmethod
    def _decode(payload, *, maximum=1024 * 1024):
        try:
            if not isinstance(payload, str) or len(payload.encode('utf-8')) > maximum:
                raise ValueError('payload budget')
            value = json.loads(payload)
            if not isinstance(value, dict): raise ValueError('expected object')
            return value
        except (ValueError, TypeError) as error:
            raise GuardError('COMPARISON_REPORT_UNAVAILABLE', 'Данные отчёта повреждены. Конвертированные файлы сохранены.', status=404) from error

    def _expected(self, item, identity, *, known_identity=None):
        if identity is not None and identity != (item.identity if item is not None else known_identity):
            raise GuardError('COMPARISON_STALE', 'Окно относится к предыдущему анализу. Откройте текущий отчёт.', status=409)

    @staticmethod
    def _report_stamp(path):
        value = Path(path).stat()
        return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns

    @staticmethod
    def _task_map(job):
        return {job.id + ':' + str(task.index): (source.id, task.format, source.name)
                for source in job.files for task in source.targets}

    def _row_payload(self, item, row):
        task, name, target, status, raw = row
        value = self._decode(raw, maximum=128 * 1024)
        expected = item.expected_tasks.get(task)
        if (expected is None or status not in STATUSES or
                value.get('taskId') != task or value.get('status') != status or
                value.get('fileId') != expected[0] or value.get('format') != target or
                target != expected[1] or value.get('name') != name or name != expected[2]):
            raise GuardError('COMPARISON_REPORT_UNAVAILABLE',
                'Строка отчёта не соответствует этому заданию. Выполните анализ заново; конвертированные файлы сохранены.', status=404)
        return value

    def _validate_report(self, item, *, force=False):
        stamp = self._report_stamp(item.db)
        if not force and stamp == item.validated_stamp:
            return
        total_bytes = 0
        counts = dict.fromkeys(STATUSES, 0)
        with self._item_database(item) as db:
            for row in db.execute('SELECT task,name,format,status,payload FROM results'):
                self._row_payload(item, row)
                total_bytes += len(row[4].encode('utf-8'))
                counts[row[3]] += 1
        count = sum(counts.values())
        if count > item.total:
            raise GuardError('COMPARISON_REPORT_UNAVAILABLE', 'Количество строк отчёта не соответствует заданию.', status=404)
        if item.state == 'completed' and count != item.total:
            item.state, item.processed, item.summary = 'interrupted', count, counts
            item.error = 'В сохранённом отчёте отсутствует часть строк. Продолжите анализ явно.'
            self._persist(item)
        item.report_bytes = total_bytes
        item.validated_stamp = self._report_stamp(item.db)

    @contextmanager
    def _lease(self, job_id):
        if not isinstance(job_id, str) or not IDENTITY.fullmatch(job_id):
            raise GuardError('JOB_NOT_FOUND', 'Задание больше недоступно.', status=404)
        if job_id not in self.guard._jobs:
            self.guard._load_recovery()
        with self.guard._lock:
            job = self.guard._get(job_id)
            job.io += 1
        try:
            yield job
        finally:
            with self.guard._lock:
                job.io -= 1
                self.guard._io_drained.notify_all()
            self.guard._cleanup(job)

    def _alive(self, job_id):
        with self.guard._lock:
            job = self.guard._jobs.get(job_id)
            return bool(job and not job.released and not self.guard._closed)

    def _safe_file(self, job, path, *, maximum=None):
        with self.lock:
            try:
                # A writer already in SQLite may commit a page between these
                # stats, but cannot start its next row until its metadata lock
                # is released. One revalidation handles that bounded race.
                for attempt in range(2):
                    size = Path(path).stat().st_size
                    if maximum is not None and size > maximum: return False
                    if self.guard._comparison_path(job, path, size) is not None: return True
                    if attempt == 0 and Path(path).stat().st_size != size: continue
                    return False
                return False
            except (OSError, ValueError, RuntimeError):
                return False

    @contextmanager
    def _item_database(self, item, *, catalog=False):
        path = item.catalog if catalog else item.db
        with self.guard._lock:
            job = self.guard._jobs.get(item.job_id)
        maximum = 32 * 1024 * 1024 if catalog else MAX_REPORT_BYTES + 16 * 1024 * 1024
        if job is None or not self._safe_file(job, path, maximum=maximum):
            raise GuardError('COMPARISON_REPORT_UNAVAILABLE', 'Файл отчёта изменён или недоступен. Результаты конвертации сохранены.', status=404)
        with _database(path) as db:
            yield db

    def _catalog(self, job, *, create=False):
        base = job.root / 'comparison'
        if create:
            # Never follow a replaced service directory when creating state.
            from .result_store import path_is_link
            for path in (job.root, base):
                if path.exists() and path_is_link(path):
                    raise GuardError('COMPARISON_STORAGE_UNSAFE', 'Хранилище анализа перенаправлено.', status=409)
            base.mkdir(exist_ok=True)
        path = base / 'sessions.sqlite'
        # exists() is false for dangling links: opening SQLite with mode=rwc
        # must never create a target outside the owned job through such a link.
        from .result_store import path_is_link
        try:
            if path_is_link(path):
                raise GuardError('COMPARISON_STORAGE_UNSAFE', 'Журнал анализа перенаправлен.', status=409)
        except FileNotFoundError:
            pass
        if path.exists() and not self._safe_file(job, path, maximum=32 * 1024 * 1024):
            raise GuardError('COMPARISON_REPORT_UNAVAILABLE', 'Журнал анализа повреждён или недоступен.', status=404)
        if create:
            with _database(path, create=True) as db:
                db.execute('PRAGMA max_page_count=8192')
                db.execute('CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
                db.execute('CREATE TABLE IF NOT EXISTS current (singleton INTEGER PRIMARY KEY CHECK(singleton=1), id TEXT NOT NULL)')
                db.execute('CREATE TABLE IF NOT EXISTS retry_events (seq INTEGER PRIMARY KEY AUTOINCREMENT, run TEXT NOT NULL, analysis TEXT NOT NULL, payload TEXT NOT NULL)')
                db.execute('CREATE INDEX IF NOT EXISTS retry_run ON retry_events(run,seq)')
                db.execute('CREATE TABLE IF NOT EXISTS retry_tasks (run TEXT NOT NULL, task TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(run,task))')
        return path

    def _restart_catalog(self, job, *, current, expected_analysis_id):
        # Both recovery and cached-report restart enter here. Serialize the
        # complete quarantine/publication, including the former error path.
        with self.lock:
            if self.closed:
                raise GuardError('GUARD_CLOSED', 'Приложение завершает работу.', status=503)
            return self._restart_catalog_locked(job, current=current,
                expected_analysis_id=expected_analysis_id)

    def _restart_catalog_locked(self, job, *, current, expected_analysis_id):
        """Quarantine only an unusable owned catalogue after an explicit restart.

        Old result databases and converted documents are deliberately untouched.
        A caller with a stale session token cannot authorize replacing an index
        whose durable current pointer is unreadable after process recovery.
        """
        catalog = self._catalog(job)
        if not catalog.is_file():
            return None
        try:
            with _database(catalog) as db:
                integrity = db.execute('PRAGMA quick_check(1)').fetchone()
                tables = {row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                required = {'sessions': {'id', 'payload'},
                            'current': {'singleton', 'id'},
                            'retry_events': {'seq', 'run', 'analysis', 'payload'},
                            'retry_tasks': {'run', 'task', 'payload'}}
                schema_valid = required.keys() <= tables and all(
                    columns <= {row[1] for row in db.execute('PRAGMA table_info(' + name + ')')}
                    for name, columns in required.items())
                if integrity == ('ok',) and schema_valid:
                    pointer = db.execute('SELECT id FROM current WHERE singleton=1').fetchone()
                    return pointer[0] if pointer else None
                damaged = True
        except GuardError as error:
            cause = error.__cause__
            code = getattr(cause, 'sqlite_errorcode', None)
            # Busy/locked/read-only/IO failures are not proof of corruption.
            damaged = code is not None and (code & 255) in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB)
            if not damaged:
                raise
        if not damaged:
            return None
        if current is not None and (current.state not in FINAL or not self._settled(current)
                or not self._retry_settled(current)):
            raise GuardError('COMPARISON_BUSY', 'Сначала остановите текущий анализ или повтор.', status=409)
        if expected_analysis_id is not None and current is None:
            raise GuardError('COMPARISON_STALE',
                'Повреждённый журнал не позволяет подтвердить прежний сеанс. Откройте анализ заново и явно повторите проверку.', status=409)
        self._expected(current, expected_analysis_id)
        # At most four broken catalogues per owning job, each subject to the
        # catalogue size budget. Never evict old evidence to make room.
        if len(list(catalog.parent.glob('sessions-corrupt-*.sqlite'))) >= 4:
            raise GuardError('COMPARISON_RECOVERY_BUDGET',
                'Достигнут лимит сохранённых повреждённых журналов. Сохраните результаты и начните новое задание.', status=409)
        companions = [catalog]
        for suffix in ('-wal', '-shm', '-journal'):
            sidecar = Path(str(catalog) + suffix)
            if sidecar.exists() or sidecar.is_symlink():
                if not self._safe_file(job, sidecar, maximum=32 * 1024 * 1024):
                    raise GuardError('COMPARISON_STORAGE_UNSAFE', 'Служебный журнал перенаправлен или превышает бюджет.', status=409)
                companions.append(sidecar)
        quarantine = catalog.with_name('sessions-corrupt-' + secrets.token_hex(12) + '.sqlite')
        moved, created_identity = [], None
        try:
            for original in companions:
                # Recheck immediately before rename; no foreign/reparse path
                # is eligible even when a previous check had accepted it.
                if not self._safe_file(job, original, maximum=32 * 1024 * 1024):
                    raise GuardError('COMPARISON_STORAGE_UNSAFE', 'Служебный журнал изменён.', status=409)
                target = Path(str(quarantine) + str(original)[len(str(catalog)):])
                original.rename(target)
                moved.append((original, target))
            # Exclusive reservation identifies this call's replacement. A
            # concurrently introduced index is never eligible for rollback.
            with catalog.open('xb') as reserved:
                stamp = os.fstat(reserved.fileno())
                created_identity = (stamp.st_dev, stamp.st_ino)
            self._catalog(job, create=True)
        except Exception as error:
            # No partial quarantine may silently replace the original index.
            # A newly created index can be removed only if this method owns it.
            if created_identity is not None:
                try:
                    stamp = catalog.lstat()
                    if (stamp.st_dev, stamp.st_ino) == created_identity:
                        catalog.unlink()
                except OSError:
                    pass
            for original, target in reversed(moved):
                if target.exists() and not original.exists():
                    try:
                        target.rename(original)
                    except OSError:
                        pass
            if isinstance(error, OSError):
                raise GuardError('COMPARISON_PERSISTENCE_FAILED',
                    'Не удалось восстановить журнал анализа. Конвертированные файлы сохранены.', status=507) from error
            raise
        return current.identity if current is not None else None

    def _metadata(self, item):
        return {'jobId': item.job_id, 'analysisId': item.identity, 'state': item.state,
                'total': item.total, 'processed': item.processed, 'summary': item.summary,
                'error': item.error, 'createdAt': item.created_at, 'updatedAt': item.updated_at,
                'reportBytes': item.report_bytes}

    def _persist(self, item, *, strict=False):
        try:
            if not item.catalog or not Path(item.catalog).is_file():
                raise OSError('analysis catalogue disappeared')
            item.updated_at = round(time.time() * 1000)
            payload = json.dumps(self._metadata(item), ensure_ascii=False, allow_nan=False)
            with self._item_database(item, catalog=True) as db:
                db.execute('INSERT OR REPLACE INTO sessions VALUES (?,?)', (item.identity, payload))
        except (OSError, ValueError, GuardError) as error:
            item.persistence_error = 'COMPARISON_PERSISTENCE_FAILED'
            if strict:
                raise GuardError('COMPARISON_PERSISTENCE_FAILED',
                    'Не удалось сохранить состояние анализа. Конвертированные файлы остаются в AIO.', status=507) from error

    def _recover(self, job):
        catalog = self._catalog(job)
        if not catalog.is_file():
            return None
        with _database(catalog) as db:
            pointer = db.execute('SELECT id FROM current WHERE singleton=1').fetchone()
            row = db.execute('SELECT payload FROM sessions WHERE id=?', pointer).fetchone() if pointer else None
        if row is None:
            return None
        try:
            value = self._decode(row[0])
            identity = value['analysisId']
            if (not IDENTITY.fullmatch(identity) or value['jobId'] != job.id or
                    type(value['total']) is not int or value['total'] < 0):
                raise ValueError('invalid session metadata')
            item = _Analysis(job.id, identity=identity, catalog=catalog,
                root=job.root / 'comparison' / identity, total=value['total'],
                state=value['state'] if value['state'] in FINAL else 'interrupted',
                error=value.get('error'), recovered=True)
            item.created_at = int(value.get('createdAt', item.created_at))
            item.report_bytes = int(value.get('reportBytes', 0))
            item.db = item.root / 'results.sqlite'
            item.expected_tasks = self._task_map(job)
            if not self._safe_file(job, item.db, maximum=MAX_REPORT_BYTES + 16 * 1024 * 1024):
                raise GuardError('COMPARISON_REPORT_UNAVAILABLE', 'Сохранённый отчёт недоступен. Выполните новый анализ.', status=404)
            self._validate_report(item, force=True)
            with self._item_database(item) as db:
                counts = dict(db.execute('SELECT status,COUNT(*) FROM results GROUP BY status'))
            if any(name not in STATUSES for name in counts):
                raise ValueError('invalid report status')
            item.summary = {name: counts.get(name, 0) for name in STATUSES}
            item.processed = sum(item.summary.values())
            if item.processed > item.total: raise ValueError('invalid report row count')
            if item.state == 'completed' and item.processed != item.total:
                item.state = 'interrupted'
                item.error = 'В сохранённом отчёте отсутствует часть строк. Продолжите анализ явно.'
            with _database(catalog) as db:
                latest = db.execute('SELECT run,analysis,payload FROM retry_events WHERE analysis=? ORDER BY seq DESC LIMIT 1',
                                    (item.identity,)).fetchone()
            if latest:
                item.retry = self._retry_payload(latest)
                if (not isinstance(item.retry, dict) or
                        item.retry.get('analysisId') != item.identity or
                        not isinstance(item.retry.get('runId'), str) or
                        not IDENTITY.fullmatch(item.retry['runId']) or
                        not isinstance(item.retry.get('children'), list)):
                    raise ValueError('invalid retry history metadata')
                if item.retry.get('state') not in FINAL:
                    item.retry['state'] = 'interrupted'
                    item.retry['error'] = 'Повтор был прерван завершением приложения. Уже созданные результаты сохранены.'
                    for child in item.retry['children']:
                        if not isinstance(child, dict): raise ValueError('invalid retry child')
                        if child.get('state') not in FINAL:
                            child['state'] = 'interrupted'
                    self._finish_verifications(item)
                    self._retry_event(item)
            self._persist(item)
            return item
        except (ValueError, TypeError, KeyError) as error:
            raise GuardError('COMPARISON_REPORT_UNAVAILABLE', 'Метаданные анализа повреждены. Конвертированные файлы сохранены.', status=404) from error

    def _get(self, job_id):
        with self._lease(job_id) as job:
            with self.lock:
                item = self.jobs.get(job_id)
                if item is None:
                    item = self._recover(job)
                    if item is not None:
                        self._cache(item)
                if item is None:
                    raise GuardError('COMPARISON_NOT_STARTED', 'Сначала запустите анализ до/после.', status=404)
                if not self._safe_file(job, item.db, maximum=MAX_REPORT_BYTES + 16 * 1024 * 1024):
                    self.jobs.pop(job_id, None)
                    raise GuardError('COMPARISON_REPORT_UNAVAILABLE', 'Отчёт анализа недоступен. Конвертированные файлы сохранены.', status=404)
                self._validate_report(item)
                return item

    def _cache(self, item):
        for key in list(self.jobs):
            if len(self.jobs) < 32:
                break
            old = self.jobs[key]
            if old.state in FINAL and self._settled(old) and self._retry_settled(old) and not (
                    old.retry and old.retry.get('state') not in FINAL):
                self.jobs.pop(key)
        if len(self.jobs) >= 32 and item.job_id not in self.jobs:
            raise GuardError('COMPARISON_BUSY', 'Дождитесь завершения текущих сеансов анализа и повторов.', status=409)
        self.jobs[item.job_id] = item

    def _submit(self, item, pin, records):
        item.pin, item.records = pin, records
        item.cancel.clear(); item.pause.clear()
        item.state = 'queued'
        try:
            self._persist(item, strict=True)
            item.future = self.executor.submit(self._run, item)
            item.future.add_done_callback(lambda future: self._cancelled(item) if future.cancelled() else None)
        except Exception:
            item.state = 'interrupted'
            self._persist(item)
            item.pin, item.records = None, None
            raise

    def _cancelled(self, item):
        with self.lock:
            item.state = 'cancelled'; item.cancel.set()
            self._persist(item)
        self._unpin(item)

    def start(self, job_id, restart=False, *, expected_analysis_id=None):
        if type(restart) is not bool:
            raise GuardError('COMPARISON_RESTART', 'Неверный режим повторного анализа.')
        # Merely reopening an existing report never launches background work.
        known_identity = None
        try:
            current = self._get(job_id)
        except GuardError as error:
            if error.code != 'COMPARISON_NOT_STARTED' and not (
                    restart and error.code == 'COMPARISON_REPORT_UNAVAILABLE'):
                raise
            with self.lock:
                current = self.jobs.get(job_id)
            if restart and error.code == 'COMPARISON_REPORT_UNAVAILABLE':
                # A bad report is replaceable, but a delayed restart must still
                # match the current durable session pointer after recovery.
                with self._lease(job_id) as job:
                    known_identity = self._restart_catalog(job, current=current,
                        expected_analysis_id=expected_analysis_id)
        with self.lock:
            self._expected(current, expected_analysis_id, known_identity=known_identity)
        if current is not None and not restart:
            return self.snapshot(job_id)
        pin = self.guard.comparison_inputs(job_id)
        records = pin.__enter__()
        transferred = False
        try:
            with self.lock:
                if self.closed:
                    raise GuardError('GUARD_CLOSED', 'Приложение завершает работу.', status=503)
                current = self.jobs.get(job_id)
                self._expected(current, expected_analysis_id, known_identity=known_identity)
                if current and (not self._settled(current) or current.state not in FINAL or
                        current.retry and (not self._retry_settled(current) or current.retry.get('state') not in FINAL)):
                    raise GuardError('COMPARISON_BUSY', 'Сначала остановите текущий анализ или повтор.', status=409)
                if any(j.state not in FINAL for j in self.jobs.values()):
                    raise GuardError('COMPARISON_BUSY', 'Другой анализ ещё выполняется.', status=409)
                with self.guard._lock:
                    job = self.guard._get(job_id)
                if restart:
                    durable_identity = self._restart_catalog(job, current=current,
                        expected_analysis_id=expected_analysis_id)
                    self._expected(current, expected_analysis_id, known_identity=durable_identity)
                catalog = self._catalog(job, create=True)
                with _database(catalog) as db:
                    if db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] >= MAX_SESSIONS:
                        raise GuardError('COMPARISON_SESSION_LIMIT', 'Достигнут лимит сохранённых сеансов анализа этого задания. Сохраните результаты и начните новое задание.', status=409)
                item = _Analysis(job_id, catalog=catalog, total=len(records))
                item.expected_tasks = {record['taskId']: (record['fileId'], record['format'], record['name'])
                                       for record in records}
                item.root = job.root / 'comparison' / item.identity
                item.root.mkdir()
                item.db = item.root / 'results.sqlite'
                with _database(item.db, create=True) as db:
                    db.execute('PRAGMA max_page_count=36864')
                    db.execute('CREATE TABLE results (task TEXT PRIMARY KEY, name TEXT, format TEXT, status TEXT, eligible INTEGER, payload TEXT, search TEXT)')
                    db.execute('CREATE INDEX results_status ON results(status)')
                self._persist(item, strict=True)
                with _database(catalog) as db:
                    db.execute('INSERT OR REPLACE INTO current VALUES (1,?)', (item.identity,))
                self._cache(item)
                self._submit(item, pin, records)
                transferred = True
        finally:
            if not transferred: pin.__exit__(None, None, None)
        return self.snapshot(job_id)

    def _store(self, item, record, outcome):
        # Public report never includes filesystem paths or a whole document.
        payload = {'taskId': record['taskId'], 'fileId': record['fileId'],
                   'name': record['name'], 'format': record['format'],
                   'sourceAvailable': record['sourceAvailable'], **outcome}
        payload.pop('source', None)
        status = payload.get('status')
        if status not in STATUSES:
            payload.update(status='not_checked', findings=[], unknown=['invalid_analysis_response'])
            status = 'not_checked'
        candidates = []
        validate = getattr(self.guard.adapter, 'validate_retry_recipe', None)
        historical_outputs = bool(record.get('outputs')) and all(
            isinstance(meta.get('sha256'), str) and re.fullmatch(r'[a-f0-9]{64}', meta['sha256'])
            for meta in record['outputs'])
        if record['sourceAvailable'] and record['state'] in ('ready', 'saved') and historical_outputs and validate:
            for recipe in payload.get('recipeCandidates', ()):
                try:
                    validate(recipe, record['format'], record.get('nativeRoute'))
                    candidates.append(recipe)
                except (GuardError, ValueError, TypeError):
                    pass
        payload['recipeCandidates'] = candidates
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        if len(data.encode('utf-8')) > 128 * 1024:
            payload = {k: payload[k] for k in ('taskId', 'fileId', 'name', 'format', 'sourceAvailable')}
            payload.update(status='not_checked', findings=[], recipeCandidates=[], unknown=['report_budget'])
            status, data = 'not_checked', json.dumps(payload, ensure_ascii=False)
        encoded = len(data.encode('utf-8'))
        self._row_payload(item, (record['taskId'], record['name'], record['format'], status, data))
        if item.report_bytes + encoded > MAX_REPORT_BYTES:
            raise GuardError('COMPARISON_REPORT_BUDGET',
                'Достигнут бюджет отчёта анализа. Проверенная часть сохранена; конвертация и сохранение файлов доступны.', status=507)
        with self._item_database(item) as db:
            inserted = db.execute('INSERT OR IGNORE INTO results VALUES (?,?,?,?,?,?,?)',
                       (record['taskId'], record['name'], record['format'], status,
                        bool(candidates), data, unicodedata.normalize('NFC', record['name']+' '+record['format']).casefold())).rowcount
        with self.lock:
            if inserted:
                item.processed += 1
                item.summary[status] += 1
                item.report_bytes += encoded
                item.validated_stamp = self._report_stamp(item.db)
                if item.processed % 25 == 0:
                    self._persist(item)

    @staticmethod
    def _unknown(reason, message):
        return {'status': 'not_checked', 'findings': [{'code': reason, 'category': 'not_checked',
                'message': message, 'certainty': 'unknown'}], 'checkedFeatures': [],
                'unknown': [reason], 'recipeCandidates': []}

    def _run(self, item):
        try:
            with nullcontext(item.records) as records:
                root = item.root
                self._validate_report(item, force=True)
                with self._item_database(item) as db:
                    done = {row[0] for row in db.execute('SELECT task FROM results')}
                with self.lock:
                    item.total, item.state = len(records), 'running'
                    self._persist(item)
                grouped = {}
                for record in records:
                    if record['taskId'] not in done:
                        grouped.setdefault(record['fileId'], []).append(record)
                for index, group in enumerate(grouped.values()):
                    while item.pause.is_set() and not item.cancel.wait(.1):
                        if not self._alive(item.job_id): item.cancel.set()
                    if item.cancel.is_set() or not self._alive(item.job_id):
                        item.cancel.set()
                        break
                    valid = []
                    for record in group:
                        if not record['sourceAvailable']:
                            self._store(item, record, self._unknown('SOURCE_UNAVAILABLE',
                                'Служебная копия исходника недоступна. Сравнение не выполнено.'))
                        elif not record['paths']:
                            self._store(item, record, self._unknown('RESULT_NOT_CREATED',
                                'Результат конвертации не создан. Сравнивать содержимое пока нельзя.'))
                        else:
                            valid.append(record)
                    if valid:
                        self._worker(item, valid, root / ('source-' + secrets.token_hex(8)))
                with self.lock:
                    item.state = 'cancelled' if item.cancel.is_set() else 'completed'
        except Exception as error:
            with self.lock:
                item.state = 'cancelled' if item.cancel.is_set() else 'failed'
                item.error = error.message if isinstance(error, GuardError) else 'Не удалось завершить анализ. Результаты конвертации сохранены в AIO.'
        finally:
            # Persist while the Guard lease still owns the directory. Never
            # recreate a released job's paths after the final unpin.
            with self.lock:
                self._persist(item)
            self._unpin(item)

    def _unpin(self, item):
        with self.lock:
            pin, item.pin = item.pin, None
            item.records = None
        if pin is not None:
            pin.__exit__(None, None, None)

    def _worker(self, item, records, root):
        root.mkdir()
        request = {'source': str(records[0]['source']), 'sourceSha256': records[0].get('sourceSha256'), 'pairs': [
            {'taskId': r['taskId'], 'target': r['format'], 'paths': [str(p) for p in r['paths']],
             'sha256': {str(path): metadata.get('sha256') for path, metadata in
                        zip(self._output_paths(r), r.get('outputs', ())) if path is not None}}
            for r in records]}
        request_path = root / 'request.json'
        request_path.write_text(json.dumps(request), encoding='utf-8')
        command = ([sys.executable, '--aio-guard-comparison', str(request_path)]
                   if getattr(sys, 'frozen', False) else
                   [sys.executable, '-m', 'app_backend.converter_guard.comparison_worker', str(request_path)])
        output = root / 'outcomes.jsonl'
        done, by_id = set(), {record['taskId']: record for record in records}
        offset, skipping_long_line = 0, False
        def drain(*, final=False):
            nonlocal offset, skipping_long_line
            if not output.is_file(): return
            with output.open('rb') as stream:
                stream.seek(offset)
                while True:
                    line = stream.readline(256 * 1024 + 1)
                    if not line: break
                    if not line.endswith(b'\n'):
                        if len(line) > 256 * 1024:
                            skipping_long_line = True
                            offset = stream.tell()
                            continue
                        if final: offset = stream.tell()
                        break  # Retry only this bounded unfinished final line.
                    offset = stream.tell()
                    if skipping_long_line or len(line) > 256 * 1024:
                        skipping_long_line = False
                        continue
                    value = json.loads(line)
                    tid = value.get('taskId')
                    record = by_id.get(tid)
                    if record is None or tid in done: continue
                    self._store(item, record, value['outcome'])
                    done.add(tid)
        elapsed, previous = 0., time.monotonic()
        try:
            with ProcessTree(command, cwd=Path(__file__).resolve().parents[2], memory_limit_bytes=MAX_WORKER_MEMORY) as tree:
                while True:
                    now = time.monotonic()
                    if not item.pause.is_set():
                        elapsed += now - previous
                    previous = now
                    paused = root / 'pause'
                    if item.pause.is_set(): paused.touch(exist_ok=True)
                    else: paused.unlink(missing_ok=True)
                    if not self._alive(item.job_id): item.cancel.set()
                    if item.cancel.is_set() or elapsed > 20 + 12 * len(records):
                        break
                    drain()
                    if tree.process.poll() is not None: break
                    time.sleep(.08)
                # The process can exit between a read and poll(). Its final
                # flushed outcome must be consumed before missing-pair rows.
                drain(final=True)
        except (OSError, ValueError, KeyError):
            pass
        finally:
            shutil.rmtree(root, ignore_errors=True)
        if not item.cancel.is_set():
            for record in records:
                if record['taskId'] not in done:
                    self._store(item, record, self._unknown('ANALYSIS_INCOMPLETE',
                        'Проверка не завершилась в отведённых ресурсах. Потеря содержимого не установлена.'))

    def snapshot(self, job_id, *, offset=0, limit=50, filter='all', q=''):
        with self._lease(job_id):
            item = self._get(job_id)
            return self._snapshot_item(item, offset=offset, limit=limit, filter=filter, q=q)

    def _snapshot_item(self, item, *, offset=0, limit=50, filter='all', q='', include_report=True):
        job_id = item.job_id
        if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 100:
            raise GuardError('COMPARISON_PAGE', 'Неверная страница анализа.')
        if filter not in ('all', *STATUSES) or not isinstance(q, str) or len(q) > 200:
            raise GuardError('COMPARISON_FILTER', 'Неверный фильтр анализа.')
        with self.lock:
            active_retry_job = self._active_retry_job()
            result = {'jobId': job_id, 'analysisId': item.identity, 'state': item.state,
                'processed': item.processed, 'total': item.total, 'summary': dict(item.summary),
                'error': item.error, 'offset': offset, 'limit': limit, 'items': [],
                'totalFiltered': item.processed, 'retry': self._decorate_retry(item.retry),
                'recovered': item.recovered, 'canRestart': item.state in FINAL and self._settled(item) and self._retry_settled(item),
                'canResume': item.state in ('cancelled', 'interrupted', 'failed') and self._settled(item),
                'finalizing': item.state in FINAL and not self._settled(item),
                'retryFinalizing': bool(item.retry) and item.retry.get('state') in FINAL and not self._retry_settled(item),
                'canRetry': item.state == 'completed' and self._settled(item) and self._retry_settled(item) and
                    (not item.retry or item.retry.get('state') in FINAL) and active_retry_job is None,
                'activeRetryJobId': active_retry_job,
                'reportAvailable': bool(item.db and Path(item.db).is_file()),
                'persistenceError': item.persistence_error, 'stale': not self._alive(job_id),
                'historyRetention': 'owned_job_lifetime'}
        if include_report and item.db and Path(item.db).exists():
            clauses, params = [], []
            if filter != 'all': clauses.append('status=?'); params.append(filter)
            if q:
                clauses.append("search LIKE ? ESCAPE '\\'")
                q = unicodedata.normalize('NFC', q).casefold()
                pattern = '%' + q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
                params.append(pattern)
            where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
            with self._item_database(item) as db:
                result['totalFiltered'] = db.execute('SELECT COUNT(*) FROM results' + where, params).fetchone()[0]
                result['items'] = [self._row_payload(item, row) for row in db.execute(
                    'SELECT task,name,format,status,payload FROM results' + where + ' ORDER BY rowid LIMIT ? OFFSET ?', [*params, limit, offset])]
        if include_report:
            history = self._history(item, offset=0, limit=20)
            for run in history['items']:
                run['isCurrentAnalysis'] = run.get('analysisId') == item.identity
            result.update(retryHistory=history['items'], historyOffset=0,
                          historyLimit=20, historyTotal=history['total'])
            result['navigation'] = self._navigation(item, history['items'])
        return result

    def _available(self, job_id):
        if not isinstance(job_id, str): return False
        with self.guard._lock:
            job = self.guard._jobs.get(job_id)
            return bool(job and not job.released)

    @staticmethod
    def _settled(item):
        return item.pin is None and (item.future is None or item.future.done())

    @staticmethod
    def _retry_settled(item):
        return item.retry_pin is None and (item.retry_future is None or item.retry_future.done())

    def _active_retry_job(self):
        if self._retry_reservation is not None:
            return self._retry_reservation[1]
        for candidate in self.jobs.values():
            if candidate.retry and (candidate.retry.get('state') not in FINAL or not self._retry_settled(candidate)):
                return candidate.job_id
        return None

    def _retry_payload(self, row):
        run, analysis, payload = row
        value = self._decode(payload)
        if value.get('runId') != run or value.get('analysisId') != analysis:
            raise GuardError('COMPARISON_REPORT_UNAVAILABLE',
                'Запись истории не соответствует сеансу анализа. Конвертированные файлы сохранены.', status=404)
        # Validation does not decorate/copy: paginated history performs that
        # work once after its SQL identity has been checked.
        self._validate_retry(value)
        return value

    @staticmethod
    def _validate_retry(value):
        if (not isinstance(value, dict) or not isinstance(value.get('analysisId'), str) or
                not IDENTITY.fullmatch(value['analysisId']) or not isinstance(value.get('runId'), str) or
                not IDENTITY.fullmatch(value['runId']) or not isinstance(value.get('children'), list) or
                len(value['children']) > 10000 or any(not isinstance(child, dict) or
                    not isinstance(child.get('jobId'), str) or not IDENTITY.fullmatch(child['jobId']) or
                    child.get('verification') is not None and not isinstance(child.get('verification'), dict)
                    for child in value['children'])):
            raise GuardError('COMPARISON_REPORT_UNAVAILABLE', 'История повторов повреждена. Конвертированные файлы сохранены.', status=404)

    def _decorate_retry(self, value):
        if value is None:
            return None
        self._validate_retry(value)
        result = copy.deepcopy(value)
        result.pop('selectedTaskIds', None)
        for child in result.get('children', ()):
            child.pop('parentTaskIds', None)
            child['available'] = self._available(child.get('jobId'))
            child['stale'] = not child['available']
        if result.get('children') and not all(c['available'] for c in result['children']):
            result['stale'] = True
            verification = result.get('verification')
            if verification:
                result['verification'] = {**verification, 'historicalAssessment': verification.get('assessment'),
                                          'assessment': 'results_unavailable'}
        return result

    def _navigation(self, item, history):
        with self.guard._lock:
            job = self.guard._jobs.get(item.job_id)
            parent_id = getattr(job, 'comparison_parent_job_id', None)
            parent_tasks = list(getattr(job, 'comparison_parent_tasks', ()))
            ancestors, seen, current = [], {item.job_id}, parent_id
            while current and current not in seen and len(ancestors) < 16:
                seen.add(current)
                ancestor = self.guard._jobs.get(current)
                available = bool(ancestor and not ancestor.released)
                ancestors.append({'jobId': current, 'label': self._job_label(ancestor) if available else
                                  'Предыдущий результат недоступен', 'available': available})
                current = getattr(ancestor, 'comparison_parent_job_id', None) if available else None
        children, seen = [], set()
        for run in history:
            for child in run.get('children', ()):
                if child.get('jobId') and child['jobId'] not in seen:
                    seen.add(child['jobId'])
                    with self.guard._lock:
                        label = self._job_label(self.guard._jobs.get(child['jobId']))
                    children.append({'runId': run['runId'], **child, 'label': label})
        return {'parentJobId': parent_id, 'parentTaskIds': parent_tasks,
                'ancestors': list(reversed(ancestors)), 'children': children}

    @staticmethod
    def _job_label(job):
        if job is None:
            return 'Результат повтора недоступен'
        name = job.files[0].name if len(job.files) == 1 else 'Файлы: ' + str(len(job.files))
        recipe = {'pdf_preserve_images': 'PDF без уменьшения изображений',
                  'csv_preserve_values': 'CSV с точными значениями'}.get(job.comparison_recipe)
        return name + (' · ' + recipe if recipe else ' · исходный результат')

    def _history(self, item, *, offset=0, limit=20, run_id=None):
        if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 100:
            raise GuardError('COMPARISON_PAGE', 'Неверная страница истории.')
        if run_id is not None and (not isinstance(run_id, str) or not IDENTITY.fullmatch(run_id)):
            raise GuardError('COMPARISON_RUN', 'Неверный идентификатор повтора.')
        with self._item_database(item, catalog=True) as db:
            if run_id is not None:
                if db.execute('SELECT 1 FROM retry_events WHERE run=? LIMIT 1', (run_id,)).fetchone() is None:
                    raise GuardError('COMPARISON_RUN', 'Повтор не найден в этом задании.', status=404)
                total = db.execute('SELECT COUNT(*) FROM retry_tasks WHERE run=?', (run_id,)).fetchone()[0]
                rows = db.execute('SELECT payload FROM retry_tasks WHERE run=? ORDER BY rowid LIMIT ? OFFSET ?',
                                  (run_id, limit, offset)).fetchall()
                values = [self._decode(row[0]) for row in rows]
                for value in values:
                    value['available'] = self._available(value.get('jobId'))
                    value['stale'] = not value['available']
                return {'runId': run_id, 'items': values, 'offset': offset, 'limit': limit, 'total': total}
            total = db.execute('SELECT COUNT(DISTINCT run) FROM retry_events').fetchone()[0]
            rows = db.execute('SELECT run,analysis,payload FROM retry_events WHERE seq IN '
                '(SELECT MAX(seq) FROM retry_events GROUP BY run) ORDER BY seq DESC LIMIT ? OFFSET ?',
                (limit, offset)).fetchall()
        return {'items': [self._decorate_retry(self._retry_payload(row)) for row in rows],
                'offset': offset, 'limit': limit, 'total': total}

    def retry_history(self, job_id, *, offset=0, limit=20, run_id=None):
        with self._lease(job_id):
            item = self._get(job_id)
            return {'jobId': job_id, 'analysisId': item.identity,
                    **self._history(item, offset=offset, limit=limit, run_id=run_id)}

    def detail(self, job_id, task_id, *, expected_analysis_id=None):
        with self._lease(job_id):
            return self._detail(job_id, task_id, expected_analysis_id=expected_analysis_id)

    def _detail(self, job_id, task_id, *, expected_analysis_id=None):
        item = self._get(job_id)
        self._expected(item, expected_analysis_id)
        if not isinstance(task_id, str) or not item.db or not Path(item.db).is_file():
            raise GuardError('COMPARISON_TASK', 'Результат анализа ещё недоступен.', status=404)
        with self._item_database(item) as db:
            row = db.execute('SELECT task,name,format,status,payload FROM results WHERE task=?', (task_id,)).fetchone()
        if row is None: raise GuardError('COMPARISON_TASK', 'Преобразование не найдено.', status=404)
        result = self._row_payload(item, row)
        result['analysisId'] = item.identity
        result['jobId'] = job_id
        result['historyRetention'] = 'owned_job_lifetime'
        with self.guard.comparison_inputs(job_id) as records:
            record = next((r for r in records if r['taskId'] == task_id), None)
            if record is None: raise GuardError('COMPARISON_TASK', 'Преобразование недоступно.', status=404)
            result['previews'] = {side: self._preview_meta(job_id, task_id, side, path)
                for side, path in [('source', record['source']), ('result', self._primary(record))]}
            result['outputPreviews'] = []
            for index, (path, metadata) in enumerate(zip(self._output_paths(record), record['outputs'])):
                preview = self._preview_meta(job_id, task_id, 'result', path, output_index=index)
                result['outputPreviews'].append({**preview, 'outputIndex': index,
                    'name': metadata.get('name', preview.get('name')), 'size': metadata.get('size'),
                    'available': path is not None and preview['kind'] != 'unavailable'})
            result['outputsTotal'] = len(record['outputs'])
        return result

    @staticmethod
    def _primary(record):
        paths = record['paths']
        return next((p for p in paths if p.suffix.lower() == '.' + record['format']), paths[0] if paths else None)

    @staticmethod
    def _output_paths(record):
        if 'outputPaths' in record:
            return record['outputPaths']
        paths, outputs = record['paths'], record.get('outputs', ())
        if len(paths) == len(outputs): return paths
        # Compatibility for earlier in-memory fixture records only. Production
        # core supplies aligned slots including None for unavailable artifacts.
        return tuple(next((path for path in paths if path.name == meta.get('name')), None)
                     for meta in outputs)

    @staticmethod
    def _preview_meta(job_id, task_id, side, path, *, output_index=None):
        if path is None: return {'kind': 'unavailable', 'note': 'Файл недоступен для просмотра.'}
        suffix = path.suffix.lower()
        kind = 'pdf' if suffix == '.pdf' else 'image' if suffix in ('.png','.jpg','.jpeg','.gif','.webp','.bmp') else 'text'
        if suffix != '.pdf' and path.stat().st_size > MAX_PREVIEW:
            return {'kind': 'unavailable', 'note': 'Встроенный просмотр ограничен 64 МиБ. Конвертация и сохранение доступны.'}
        query = {'job': job_id, 'task': task_id, 'side': side}
        if output_index is not None: query['outputIndex'] = output_index
        return {'kind': kind, 'name': path.name, 'size': path.stat().st_size,
                'url': '/api/converter-guard/compare/preview?' + urlencode(query),
                'note': ('Текстовое представление; оформление не показано.' if kind == 'text' else
                         'Уменьшенный просмотр изображения; для анимации показан первый кадр.' if kind == 'image' else '')}

    @contextmanager
    def preview(self, job_id, task_id, side, *, page=0, output_index=None):
        if side not in ('source', 'result'): raise GuardError('COMPARISON_SIDE', 'Неверная сторона сравнения.')
        if output_index is not None and (type(output_index) is not int or output_index < 0):
            raise GuardError('COMPARISON_OUTPUT', 'Неверный номер результата.')
        with self.guard.comparison_inputs(job_id) as records:
            record = next((r for r in records if r['taskId'] == task_id), None)
            if record is None: raise GuardError('COMPARISON_TASK', 'Преобразование недоступно.', status=404)
            if side == 'source':
                path = record['source']
                expected_sha = record.get('sourceSha256')
            elif output_index is None:
                path = self._primary(record)
                slots = self._output_paths(record)
                selected = next((index for index, candidate in enumerate(slots) if candidate == path), None)
                expected_sha = record['outputs'][selected].get('sha256') if selected is not None else None
            elif output_index >= len(self._output_paths(record)):
                raise GuardError('COMPARISON_OUTPUT', 'Результат с этим номером недоступен.')
            else:
                path = self._output_paths(record)[output_index]
                expected_sha = record['outputs'][output_index].get('sha256')
            meta = self._preview_meta(job_id, task_id, side, path)
            if meta['kind'] == 'unavailable':
                yield {'json': meta}; return
            if meta['kind'] == 'text':
                yield {'json': self._text_preview(job_id, path, expected_sha=expected_sha, side=side)}; return
            if meta['kind'] in ('pdf','image'):
                data = self._text_preview(job_id, path,
                    mode='pdf_preview' if meta['kind'] == 'pdf' else 'image_preview', page=page,
                    expected_sha=expected_sha, side=side)
                if 'png' in data:
                    raw = data.pop('png')
                    with io.BytesIO(raw) as stream:
                        yield {'stream':stream,'size':len(raw),'mime':'image/png',**data}
                else: yield {'json':data}
                return
            with path.open('rb') as stream:
                yield {'stream': stream, 'size': path.stat().st_size,
                       'mime': 'application/pdf' if meta['kind'] == 'pdf' else mimetypes.guess_type(path.name)[0] or 'application/octet-stream'}

    def _text_preview(self, job_id, path, *, mode='preview', page=0, expected_sha=None, side='result'):
        if not self.preview_gate.acquire(blocking=False):
            raise GuardError('COMPARISON_PREVIEW_BUSY', 'Предыдущий предпросмотр ещё готовится. Повторите через несколько секунд.', status=409)
        try:
            with self.guard._lock:
                parent = self.guard._get(job_id)
                root = parent.root / 'comparison' / ('preview-' + secrets.token_hex(12))
            root.mkdir(parents=True)
            request = root / 'request.json'
            request.write_text(json.dumps({'mode':mode, 'source':str(path), 'page':page,
                'expectedSha256':expected_sha, 'previewSide':side}), encoding='utf-8')
            command = ([sys.executable, '--aio-guard-comparison', str(request)]
                       if getattr(sys, 'frozen', False) else
                       [sys.executable, '-m', 'app_backend.converter_guard.comparison_worker', str(request)])
            until = time.monotonic() + 12
            with ProcessTree(command, cwd=Path(__file__).resolve().parents[2], memory_limit_bytes=MAX_WORKER_MEMORY) as tree:
                while tree.process.poll() is None:
                    if self.closed or time.monotonic() >= until:
                        return {'kind':'unavailable','note':'Предпросмотр не завершился в отведённых ресурсах. Результат можно сохранить.'}
                    time.sleep(.06)
            output = root / 'preview.json'
            if output.is_file() and output.stat().st_size <= 1024 * 1024:
                result = json.loads(output.read_text(encoding='utf-8'))
                image = root/'preview.png'
                if mode in ('pdf_preview','image_preview') and result.get('kind') == 'image' and image.is_file() and image.stat().st_size <= 16*1024*1024:
                    result['png'] = image.read_bytes()
                return result
            return {'kind':'unavailable','note':'Встроенный просмотр этого документа недоступен.'}
        finally:
            # Only this newly allocated private directory is eligible.
            if 'root' in locals():
                import shutil
                shutil.rmtree(root, ignore_errors=True)
            self.preview_gate.release()

    def control(self, job_id, action, *, expected_analysis_id=None):
        # Cancellation remains usable while an explicit discard is draining
        # the analysis lease; it cannot read or resurrect a removed report.
        with self.lock:
            cached = self.jobs.get(job_id)
            if cached is not None: self._expected(cached, expected_analysis_id)
        if action == 'cancel' and cached is not None and not self._alive(job_id):
            cached.cancel.set(); cached.retry_cancel.set(); cached.pause.clear()
            if cached.future is not None: cached.future.cancel()
            if cached.retry_future is not None: cached.retry_future.cancel()
            return self._snapshot_item(cached, include_report=False)
        item = self._get(job_id)
        if action == 'resume' and item.state in ('cancelled', 'interrupted', 'failed'):
            pin = self.guard.comparison_inputs(job_id)
            records = pin.__enter__()
            transferred = False
            try:
                with self.lock:
                    self._expected(self.jobs.get(job_id), expected_analysis_id)
                    if self.closed:
                        raise GuardError('GUARD_CLOSED', 'Приложение завершает работу.', status=503)
                    if not self._settled(item):
                        raise GuardError('COMPARISON_BUSY', 'Предыдущий анализ ещё освобождает ресурсы.', status=409)
                    if any(j.state not in FINAL for j in self.jobs.values()):
                        raise GuardError('COMPARISON_BUSY', 'Другой анализ ещё выполняется.', status=409)
                    if item.retry and item.retry.get('state') not in FINAL:
                        raise GuardError('COMPARISON_RETRY_BUSY', 'Сначала остановите повтор.', status=409)
                    item.error = None
                    self._submit(item, pin, records)
                    transferred = True
            finally:
                if not transferred: pin.__exit__(None, None, None)
            return self.snapshot(job_id)
        with self.lock:
            self._expected(self.jobs.get(job_id), expected_analysis_id)
            if action == 'cancel':
                item.cancel.set(); item.retry_cancel.set(); item.pause.clear()
                if item.future is not None: item.future.cancel()
                if item.retry_future is not None: item.retry_future.cancel()
            elif action in ('pause', 'resume') and item.state not in FINAL:
                if action == 'pause': item.pause.set(); item.state = 'paused'
                else: item.pause.clear(); item.state = 'running'
            elif action not in ('pause', 'resume'):
                raise GuardError('COMPARISON_ACTION', 'Неизвестная команда анализа.')
            self._persist(item)
        return self.snapshot(job_id)

    def retry(self, job_id, analysis_id, task_ids):
        if (not isinstance(task_ids, list) or not task_ids or
                len(task_ids) > 10000 or
                any(not isinstance(t,str) or len(t)>100 for t in task_ids) or
                len(task_ids) != len(set(task_ids))):
            raise GuardError('COMPARISON_TASKS', 'Выберите преобразования без повторений.')
        reservation = object()
        with self.lock:
            if self.closed:
                raise GuardError('GUARD_CLOSED', 'Приложение завершает работу.', status=503)
            if self._active_retry_job() is not None:
                raise GuardError('COMPARISON_RETRY_BUSY',
                    'Сейчас выполняется повтор другого пакета. Дождитесь завершения или отмените его.', status=409)
            self._retry_reservation = (reservation, job_id)
        pin = self._lease(job_id)
        transferred = entered = False
        try:
            pin.__enter__()
            entered = True
            item = self._get(job_id)
            if analysis_id != item.identity or item.state != 'completed':
                raise GuardError('COMPARISON_STALE', 'Дождитесь завершения актуального анализа.', status=409)
            groups = {}
            with self._item_database(item) as db:
                for tid in task_ids:
                    row = db.execute('SELECT task,name,format,status,payload FROM results WHERE task=?', (tid,)).fetchone()
                    value = self._row_payload(item, row) if row else {}
                    candidates = value.get('recipeCandidates', [])
                    if not candidates:
                        raise GuardError('AUTOMATIC_RECIPE_UNAVAILABLE', 'Для выбранного результата нет проверенной автонастройки.')
                    groups.setdefault(candidates[0], []).append(tid)
            with self.lock:
                if self.closed:
                    raise GuardError('GUARD_CLOSED', 'Приложение завершает работу.', status=503)
                if self.jobs.get(job_id) is not item or analysis_id != item.identity or item.state != 'completed':
                    raise GuardError('COMPARISON_STALE', 'Дождитесь завершения актуального анализа.', status=409)
                if item.retry and item.retry.get('state') not in FINAL:
                    raise GuardError('COMPARISON_RETRY_BUSY', 'Повторная конвертация уже выполняется.', status=409)
                if not self._retry_settled(item):
                    raise GuardError('COMPARISON_RETRY_BUSY', 'Предыдущий повтор ещё освобождает ресурсы.', status=409)
                with self._item_database(item, catalog=True) as db:
                    if (db.execute('SELECT COUNT(DISTINCT run) FROM retry_events').fetchone()[0] >= MAX_RETRY_RUNS or
                            db.execute('SELECT COUNT(*) FROM retry_tasks').fetchone()[0] + len(task_ids) > MAX_RETRY_TASKS):
                        raise GuardError('COMPARISON_HISTORY_BUDGET', 'Достигнут бюджет истории повторов. Сохраните результаты и начните новое задание.', status=409)
                item.retry_cancel = threading.Event()
                item.retry_jobs = []
                now = round(time.time() * 1000)
                item.retry = {'runId': secrets.token_hex(16), 'analysisId': item.identity,
                    'state': 'queued', 'createdAt': now, 'updatedAt': now,
                    'jobId': None, 'jobIds': [], 'children': [], 'error': None,
                    'selectedTaskIds': list(task_ids), 'selectedCount': len(task_ids),
                    'verification': {'assessment': 'not_comparable', 'scope': 'checked_subset'}}
                try:
                    self._retry_event(item, strict=True)
                except GuardError:
                    item.retry['state'] = 'failed'
                    item.retry['error'] = 'Не удалось записать историю; повтор не запускался.'
                    raise
                item.retry_pin = pin
                try:
                    item.retry_future = self.retry_executor.submit(self._retry, item, groups)
                    item.retry_future.add_done_callback(lambda future: self._cancelled_retry(item) if future.cancelled() else None)
                except Exception:
                    item.retry_pin = None
                    item.retry['state'] = 'interrupted'
                    self._retry_event(item)
                    raise
                transferred = True
        finally:
            if entered and not transferred: pin.__exit__(None, None, None)
            with self.lock:
                if self._retry_reservation is not None and self._retry_reservation[0] is reservation:
                    self._retry_reservation = None
        return self.snapshot(job_id)

    def _retry_event(self, item, *, strict=False):
        try:
            item.retry['updatedAt'] = round(time.time() * 1000)
            item.retry['durable'] = True
            payload = json.dumps(item.retry, ensure_ascii=False, allow_nan=False)
            if len(payload.encode('utf-8')) > 1024 * 1024:
                raise ValueError('retry event budget')
            with self._item_database(item, catalog=True) as db:
                if db.execute('SELECT COUNT(*) FROM retry_events').fetchone()[0] >= MAX_RETRY_EVENTS:
                    raise ValueError('retry event budget')
                db.execute('INSERT INTO retry_events(run,analysis,payload) VALUES (?,?,?)',
                           (item.retry['runId'], item.retry['analysisId'], payload))
        except (OSError, ValueError, GuardError) as error:
            item.persistence_error = 'COMPARISON_PERSISTENCE_FAILED'
            item.retry['durable'] = False
            if strict:
                raise GuardError('COMPARISON_PERSISTENCE_FAILED',
                    'Не удалось сохранить историю повтора. Первый результат остаётся в AIO.', status=507) from error

    def _unpin_retry(self, item):
        with self.lock:
            pin, item.retry_pin = item.retry_pin, None
        if pin is not None:
            pin.__exit__(None, None, None)

    def _cancelled_retry(self, item):
        with self.lock:
            item.retry_cancel.set(); item.retry['state'] = 'cancelled'
            try:
                self._finish_verifications(item)
            except (OSError, ValueError, GuardError):
                item.persistence_error = 'COMPARISON_PERSISTENCE_FAILED'
            self._retry_event(item)
        self._unpin_retry(item)

    def _record_verifications(self, item, child, verification):
        tasks = verification.pop('taskVerifications', [])
        with self._item_database(item, catalog=True) as db:
            for task in tasks:
                task.update(jobId=child['jobId'], recipe=child['recipe'])
                db.execute('INSERT OR IGNORE INTO retry_tasks VALUES (?,?,?)',
                    (item.retry['runId'], task['parentTaskId'], json.dumps(task, ensure_ascii=False)))
        child['verification'] = verification

    def _check_parent_outputs(self, item, task_ids):
        """Recheck selected evidence only, outside the GUI/status paths."""
        selected = set(task_ids)
        with self.guard.comparison_inputs(item.job_id) as records:
            seen = set()
            for record in records:
                if record['taskId'] not in selected: continue
                seen.add(record['taskId'])
                slots = self._output_paths(record)
                if not slots or any(path is None for path in slots):
                    raise GuardError('COMPARISON_RESULT_CHANGED',
                        'Первый результат изменён или недоступен. Запустите анализ заново перед повтором.', status=409)
                for path, metadata in zip(slots, record['outputs']):
                    expected = metadata.get('sha256')
                    digest = hashlib.sha256()
                    with path.open('rb') as stream:
                        while block := stream.read(64 * 1024):
                            if item.retry_cancel.is_set() or not self._alive(item.job_id):
                                item.retry_cancel.set()
                                raise GuardError('CANCELLED', 'Проверка исходного результата отменена.')
                            digest.update(block)
                    if not expected or digest.hexdigest() != expected:
                        raise GuardError('COMPARISON_RESULT_CHANGED',
                            'Первый результат изменился после анализа. Новый кандидат не считается улучшением; запустите анализ заново.', status=409)
            if seen != selected:
                raise GuardError('COMPARISON_STALE', 'Выбранные результаты больше недоступны.', status=409)

    def _finish_verifications(self, item):
        """Every selected pair gets a verdict, even if a later group failed."""
        with self._item_database(item, catalog=True) as db:
            done = {row[0] for row in db.execute('SELECT task FROM retry_tasks WHERE run=?', (item.retry['runId'],))}
        missing = set(item.retry['selectedTaskIds']) - done
        with self._item_database(item) as db:
            values = []
            for tid in missing:
                row = db.execute('SELECT task,name,format,status,payload FROM results WHERE task=?', (tid,)).fetchone()
                before = self._row_payload(item, row) if row else {}
                child = next((c for c in item.retry['children'] if tid in c.get('parentTaskIds', ())), {})
                values.append({'parentTaskId': tid, 'taskId': None, 'jobId': child.get('jobId'),
                    'name': before.get('name'), 'fileId': before.get('fileId'), 'format': before.get('format'),
                    'assessment': 'not_comparable', 'scope': 'checked_subset',
                    'reason': item.retry.get('error') or 'Проверка этого кандидата не завершена.'})
        with self._item_database(item, catalog=True) as db:
            for value in values:
                db.execute('INSERT OR IGNORE INTO retry_tasks VALUES (?,?,?)',
                    (item.retry['runId'], value['parentTaskId'], json.dumps(value, ensure_ascii=False)))
            rows = [json.loads(row[0]) for row in db.execute('SELECT payload FROM retry_tasks WHERE run=?', (item.retry['runId'],))]
        comparable = (len(rows) == item.retry['selectedCount'] and
                      all(r.get('assessment') in ('checked_subset_improved', 'no_confirmed_improvement') for r in rows))
        improved = comparable and all(r['assessment'] == 'checked_subset_improved' for r in rows)
        item.retry['verification'] = {'assessment': 'checked_subset_improved' if improved else
            'no_confirmed_improvement' if comparable else 'not_comparable',
            'scope': 'checked_subset', 'pairsSelected': item.retry['selectedCount'],
            'pairsVerified': len(rows), 'beforeConfirmed': sum(r.get('beforeConfirmed', 0) for r in rows),
            'afterConfirmed': sum(r.get('afterConfirmed', 0) for r in rows),
            'note': 'Оценены только указанные свойства каждой выбранной пары. Первый вариант сохранён.'}

    def _retry(self, item, groups):
        try:
            for recipe, tasks in groups.items():
                if item.retry_cancel.is_set() or not self._alive(item.job_id):
                    item.retry_cancel.set()
                    raise GuardError('CANCELLED', 'Повтор отменён. Первый результат сохранён.')
                self._check_parent_outputs(item, tasks)
                with self.lock:
                    item.retry['state'] = 'preparing'
                    self._retry_event(item, strict=True)
                def on_created(identity):
                    with self.lock:
                        item.retry_jobs.append(identity)
                        item.retry['children'].append({'jobId': identity, 'recipe': recipe,
                            'parentTaskIds': list(tasks), 'selectedCount': len(tasks),
                            'state': 'preparing', 'error': None,
                            'verification': {'assessment': 'not_comparable', 'scope': 'checked_subset'}})
                        item.retry.update(jobId=identity, jobIds=list(item.retry_jobs))
                        self._retry_event(item, strict=True)
                created = self.guard.retry_comparison(item.job_id, tasks, recipe,
                    cancel=item.retry_cancel, on_created=on_created)
                identity = created['jobId']
                with self.lock:
                    candidate = next(c for c in item.retry['children'] if c['jobId'] == identity)
                    candidate['state'] = 'running'
                    item.retry['state'] = 'running'
                    self._retry_event(item, strict=True)
                while not item.retry_cancel.is_set():
                    if not self._alive(item.job_id): item.retry_cancel.set(); break
                    state = self.guard.status(identity)
                    with self.lock:
                        candidate.update(completed=state['completed'], total=state['total'],
                            succeeded=state['succeeded'], failed=state['failed'], conversionState=state['state'])
                        item.retry.update(completed=state['completed'], total=state['total'])
                    if state['finished']: break
                    time.sleep(.15)
                if item.retry_cancel.is_set():
                    self.guard.cancel(identity)
                    raise GuardError('CANCELLED', 'Повтор отменён. Первый результат сохранён.')
                with self.lock:
                    candidate['state'] = 'analyzing'
                    item.retry['state'] = 'analyzing'
                    self._retry_event(item, strict=True)
                while True:
                    if item.retry_cancel.is_set() or self.closed or not self._alive(item.job_id):
                        item.retry_cancel.set()
                        raise GuardError('CANCELLED', 'Повторная проверка отменена.')
                    try:
                        self.start(identity)
                        break
                    except GuardError as error:
                        if error.code != 'COMPARISON_BUSY': raise
                        time.sleep(.15)
                child = self._get(identity)
                while child.state not in FINAL and not item.retry_cancel.wait(.1):
                    if not self._alive(item.job_id): item.retry_cancel.set()
                if item.retry_cancel.is_set():
                    self.control(identity, 'cancel', expected_analysis_id=child.identity)
                    raise GuardError('CANCELLED', 'Повторная проверка отменена.')
                self._check_parent_outputs(item, tasks)
                with self._lease(identity):
                    verification = self._verify_retry(item, child, tasks, opener=self._item_database,
                                                      decoder=self._row_payload)
                with self.lock:
                    self._record_verifications(item, candidate, verification)
                    candidate['state'] = 'completed' if child.state == 'completed' else child.state
                    self._retry_event(item, strict=True)
            with self.lock:
                item.retry['state'] = 'completed'
        except Exception as error:
            with self.lock:
                item.retry.update(state='cancelled' if item.retry_cancel.is_set() else 'failed',
                    error=error.message if isinstance(error,GuardError) else 'Повтор не завершён. Первый результат остаётся в AIO.')
                for child in item.retry['children']:
                    if child['state'] not in FINAL:
                        child['state'] = item.retry['state']
                        child['error'] = item.retry['error']
        finally:
            try:
                with self.lock:
                    self._finish_verifications(item)
                    self._retry_event(item)
            except (OSError, ValueError, GuardError):
                item.persistence_error = 'COMPARISON_PERSISTENCE_FAILED'
            self._unpin_retry(item)

    @staticmethod
    def _verify_retry(parent, child, task_ids, *, opener=None, decoder=None):
        columns = 'task,name,format,status,payload' if decoder else 'payload'
        with (opener(parent) if opener else _database(parent.db)) as db:
            before = []
            for tid in task_ids:
                row = db.execute('SELECT ' + columns + ' FROM results WHERE task=?', (tid,)).fetchone()
                if row: before.append(decoder(parent, row) if decoder else ComparisonService._decode(row[0]))
        after = []
        if child.db and Path(child.db).is_file():
            with (opener(child) if opener else _database(child.db)) as db:
                after = [decoder(child, row) if decoder else ComparisonService._decode(row[0])
                         for row in db.execute('SELECT ' + columns + ' FROM results')]
        following = {(r['fileId'], r['format']): r for r in after}
        verdicts = []
        for old in before:
            row = following.get((old['fileId'], old['format']), {})
            comparable = bool(row) and child.state == 'completed'
            scope = set(row.get('checkedFeatures', ()))
            if row.get('status') not in ('checked', 'format_limitation'): comparable = False
            if not scope or not set(old.get('checkedFeatures', ())).issubset(scope): comparable = False
            proof = {'cell_values': {'cell_values_single_grid', 'all_sheet_values_unordered'},
                     'frames': {'frames'}, 'pages': {'pages'},
                     'image_dimensions': {'image_dimensions'}, 'alpha_channel': {'alpha_channel'}}
            for finding in old.get('findings', ()):
                if finding.get('category') == 'confirmed_loss' and 'identical_file_bytes' not in scope:
                    required = proof.get(finding.get('feature'), set())
                    if not required.intersection(scope): comparable = False
            old_losses = sum(f.get('category') == 'confirmed_loss' for f in old.get('findings', ()))
            new_losses = sum(f.get('category') == 'confirmed_loss' for f in row.get('findings', ()))
            verdicts.append({'parentTaskId': old.get('taskId'), 'taskId': row.get('taskId'),
                'fileId': old['fileId'], 'format': old['format'], 'name': old.get('name'),
                'assessment': 'checked_subset_improved' if comparable and old_losses > 0 and new_losses == 0 else
                    'no_confirmed_improvement' if comparable else 'not_comparable',
                'scope': 'checked_subset', 'beforeConfirmed': old_losses, 'afterConfirmed': new_losses})
        comparable = bool(verdicts) and len(verdicts) == len(task_ids) and all(v['assessment'] != 'not_comparable' for v in verdicts)
        improved = comparable and all(v['assessment'] == 'checked_subset_improved' for v in verdicts)
        return {'assessment': 'checked_subset_improved' if improved else 'no_confirmed_improvement' if comparable else 'not_comparable',
                'scope':'checked_subset', 'beforeConfirmed':sum(v['beforeConfirmed'] for v in verdicts),
                'afterConfirmed':sum(v['afterConfirmed'] for v in verdicts), 'taskVerifications': verdicts,
                'note':'Полная визуальная идентичность не проверяется. Первый вариант сохранён.'}

    def close(self):
        with self.lock:
            self.closed = True
            for item in self.jobs.values(): item.cancel.set(); item.retry_cancel.set(); item.pause.clear()
        self.executor.shutdown(wait=True, cancel_futures=True)
        self.retry_executor.shutdown(wait=True, cancel_futures=True)
