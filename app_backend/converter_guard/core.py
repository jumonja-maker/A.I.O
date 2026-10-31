"""Disk-owned, opt-in native jobs. One worker; independent file/target failures."""
from __future__ import annotations

from collections import deque, OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
import copy
import errno
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
from types import MappingProxyType
import zipfile

from ..libreoffice_catalog import mime_for
from .adapter import LibreOfficeAdapter
from .errors import GuardError
from .policy import GuardLimits, validate_files, validate_plan
from .result_store import ResultStore, path_is_link
from .native_metrics import clean_timings
from .export_recipe import warning_details, WARNING_TEXT
from .build_info import report_build_identity

CHUNK = 64 * 1024
SUCCESS = {'ready', 'saved'}
DONE = SUCCESS | {'error', 'cancelled', 'skipped'}
TERMINAL = {'ready', 'partial', 'failed', 'cancelled'}
COUNT_KEYS = ('total', 'completed', 'attempted', 'succeeded',
              'failed', 'cancelled', 'filtered', 'pending')


def _publish_saved_copy(part, target, cancel):
    """Create a final filename without overwriting an unrelated writer."""
    if os.name == 'nt':
        os.rename(part, target)  # Windows rename fails if target exists.
        return
    try:
        os.link(part, target, follow_symlinks=False)
        return
    except OSError as error:
        if error.errno not in {errno.EPERM, errno.EXDEV, errno.ENOSYS,
                               errno.ENOTSUP, errno.EOPNOTSUPP}:
            raise
    # FAT/exFAT and some mounted/FUSE destinations do not support hard links.
    # O_EXCL retains the same no-overwrite guarantee there. The ordinary
    # save path independently hashes this final file before acknowledging it.
    if cancel.is_set():
        raise GuardError('SAVE_CANCELLED', 'Сохранение отменено. Результаты остаются в AIO.', stage='save')
    with part.open('rb') as source:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                             getattr(os, 'O_BINARY', 0), 0o600)
        try:
            output = os.fdopen(descriptor, 'wb')
            descriptor = None
            with output:
                while block := source.read(CHUNK):
                    if cancel.is_set():
                        raise GuardError('SAVE_CANCELLED', 'Сохранение отменено. Результаты остаются в AIO.',
                                         stage='save')
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
        finally:
            if descriptor is not None:
                os.close(descriptor)
    # On failure leave any partial final file in the failed save folder. Do
    # not perform a stat-then-unlink that could delete an intervening writer.


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
    native_timings: dict | None = None
    export_filter: str | None = None
    warnings: tuple = ()
    quality: dict | None = None
    result_checks: list = field(default_factory=list)
    execution_reason: str | None = None
    automatic_recipe: str | None = None
    automatic_parameters: dict | None = None


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
    counts: dict | None = None
    source_released: bool = False
    resources: dict = field(default_factory=dict)
    resources_uploading: set = field(default_factory=set)
    resource_interrupts: dict = field(default_factory=dict)
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
    sha256: str | None = None
    readback_verified: bool = False
    source_sheet: dict | None = None
    bundle_outputs: tuple | None = None
    archive_verification: dict | None = None
    archive_expected: tuple | None = None
    save_receipt: dict | None = None
    availability_error: str | None = None
    file_identity: tuple | None = field(default=None, init=False)

    def __post_init__(self):
        from ..file_identity import file_identity
        self.file_identity = file_identity(self.path)

    @property
    def companion(self):
        # Generated whole-workbook manifest is a necessary sidecar, not a
        # fifth converted spreadsheet. It must still be durably saved.
        return self.name == 'AIO-sheets.json' and self.source_sheet is None and self.format in ('csv','tab','tsv')

    def metadata(self):
        return {'available': self.availability_error is None, 'availabilityCode': self.availability_error,
                'supportingFile': self.companion, 'fileId': self.file_id, 'format': self.format, 'name': self.name,
                'archiveName': self.archive_name, 'size': self.size, 'saved': self.saved,
                'sha256': self.sha256, 'readbackVerified':self.readback_verified,
                'sourceSheet':copy.deepcopy(self.source_sheet),
                'ticket': None if self.availability_error else self.ticket,
                'mime': mime_for(self.path.suffix.lower().lstrip('.')),
                'downloadUrl': None if self.availability_error else '/api/converter-guard/result?ticket=' + self.ticket,
                **({'archiveVerification':copy.deepcopy(self.archive_verification)}
                   if self.archive_verification is not None else {})}


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
    cleanup_background: bool = False
    cleanup_retry_at: float = 0.0
    io: int = 0
    future: object = None
    artifacts: list = field(default_factory=list)
    diagnostic_artifacts: list = field(default_factory=list)
    diagnostic_building: bool = False
    bundle: object = None
    archive_mode: str = 'auto'
    bundle_deferred: bool = False
    result: dict | None = None
    packaging_error: str | None = None
    preparation: bool = False
    preflight_checks: int = 0
    preflight_cache_hits: int = 0
    preflight_phase: str = 'files'
    preflight_matrix_completed: int = 0
    preflight_matrix_total: int = 0
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
    history_session: str = field(default_factory=lambda: secrets.token_hex(16))
    detached: bool = False
    strict_native: bool = False
    save_cancel: threading.Event = field(default_factory=threading.Event)
    timings: dict = field(default_factory=dict)
    preflight_timeout_seconds: float | None = None
    diagnostic_bytes: int = 0
    diagnostic_quota_reserved: int = 0
    preflight_identity: object = None
    saving: bool = False
    counts: dict | None = None
    pending_journal: dict = field(default_factory=dict)
    journal_lock: object = field(default_factory=threading.Lock)
    salvage: bool = False
    quality_check: bool = False
    quality_compared: bool = False
    input_accounting: list = field(default_factory=list)
    retain_sources: bool = False
    retain_results: bool = False  # This process/UI lifetime, never a source lease.
    comparison_parent_job_id: str | None = None
    comparison_recipe: str | None = None
    comparison_parent_tasks: tuple = ()


def _source_name(name):
    normalized = ((os.name != 'nt' and len(os.fsencode(name)) > 255) or
        (os.name == 'nt' and (bool(re.search(r'[<>:"|?*]', name)) or
         name.endswith((' ', '.')) or bool(re.fullmatch(
             r'CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]', name.split('.')[0], re.I)))))
    suffix = Path(name).suffix
    safe_suffix = suffix if re.fullmatch(r'\.[a-zA-Z0-9_-]{1,32}', suffix) else '.bin'
    return ('source' + safe_suffix if normalized else name), normalized



class _LocalHtmlResources(HTMLParser):
    """Fail closed when isolated HTML contains local assets (including CSS)."""
    _tags = {'img', 'link', 'script', 'source', 'video', 'audio', 'iframe', 'object',
             'embed', 'image', 'use', 'input', 'body', 'table', 'td', 'th', 'base'}
    _attrs = {'src', 'srcset', 'href', 'xlink:href', 'poster', 'data', 'background'}
    _css_refs = re.compile(
        r"""(?is)\burl\(\s*(?:"([^"]*)"|'([^']*)'|([^'"\s)]+))\s*\)"""
        r"""|@import\s+(?:"([^"]*)"|'([^']*)')""")

    def __init__(self, resolver=None):
        super().__init__(convert_charrefs=True)
        self.resolver = resolver
        self.found = False
        self._in_style = False
        self._css_tail = ''
        self.undetermined = False

    def _notice(self, value):
        if self._local(value) and (self.resolver is None or not self.resolver(value)):
            self.found = True

    @staticmethod
    def _local(value):
        value = value.strip().strip(chr(34)).strip(chr(39))
        if not value or value.startswith(('#', '//', 'data:')):
            return False
        # /assets/logo.svg is a local resource, not a remote URL.
        if value.lower().startswith('file:') or re.match(r'^[a-zA-Z]:[\\/]', value):
            return True
        if re.match(r'(?i)^[a-z][a-z0-9+.-]*:', value):
            return False
        return True

    def _scan_css(self, value):
        # CSS escapes can hide both url() and its path. Decode CSS escapes,
        # not Python/URL escapes; keep the streaming overlap bounded below.
        def unescape(match):
            digits, literal = match.groups()
            number = int(digits, 16) if digits else None
            character = chr(number) if number and number <= 0x10ffff else literal or '\ufffd'
            # Keep an escaped whitespace character within a valid unquoted
            # url() token. The dependency resolver decodes URL escapes later;
            # raw whitespace here would make the tokenizer drop the resource.
            return ('%' + format(ord(character), '02X')
                    if character in ' \t\r\n\f' else character)
        value = re.sub(r'\\(?:([0-9a-fA-F]{1,6})[ \t\r\n\f]?|([^\r\n]))', unescape, value)
        value = re.sub(r'/\*.*?\*/', ' ', value, flags=re.S)
        unfinished = re.search(r'(?is)(?:\burl\([^)]*|@import\b[^;]*)\Z', value)
        if unfinished and len(unfinished.group())>4096:
            self.undetermined = True
        for candidates in self._css_refs.findall(value):
            self._notice(next((candidate for candidate in candidates if candidate), ''))

    def handle_starttag(self, tag, attrs):
        if tag == 'style':
            self._in_style = True
            self._css_tail = ''
        for key, value in attrs:
            if not isinstance(value, str):
                continue
            if key == 'style':
                self._scan_css(value)
            if tag not in self._tags or key not in self._attrs:
                continue
            # Only srcset is a list; a data URL can contain commas.
            candidates = (re.findall(r'(?:data:[^\s]+|[^,\s]+)(?:\s+[^,]+)?', value)
                          if key == 'srcset' else [value])
            # Descriptors follow whitespace only in srcset. Whitespace within
            # an ordinary quoted src/href is part of the resource filename.
            urls = ((part.strip().split()[0] if key == 'srcset' else part.strip())
                    for part in candidates if part.strip())
            for url in urls:
                self._notice(url)

    def handle_data(self, data):
        if self._in_style:
            self._scan_css(self._css_tail + data)
            # A bounded overlap covers a url() split between feed() calls.
            self._css_tail = (self._css_tail + data)[-4096:]

    def handle_endtag(self, tag):
        if tag == 'style':
            self._in_style = False
            self._css_tail = ''


def _html_resource_warning(source, *, cancel=None, missing=None, seconds=2):
    from .html_resources import inspect_dependencies
    return inspect_dependencies(source, _LocalHtmlResources, cancel=cancel, missing=missing, seconds=seconds)


def _missing_html_resources(source):
    """Legacy boolean helper; callers needing an explanation use the code."""
    return _html_resource_warning(source) is not None


class _Cancellation:
    """Combine job/file cancellation without another timer or watcher thread."""
    def __init__(self, job, item):
        self.job, self.item = job, item

    def is_set(self):
        return self.job.is_set() or self.item.is_set()

    def wait(self, seconds):
        self.job.wait(seconds)
        return self.is_set()




from .recovery_control import RecoveryStopped, check_recovery_stop


class ConverterGuard:
    def __init__(self, *, adapter=None, limits=None, spool_parent=None, recovery_parent=None, history_parent=None):
        self.adapter = adapter if adapter is not None else LibreOfficeAdapter()
        self.build_identity = report_build_identity()
        self.limits = limits if limits is not None else GuardLimits()
        self._recovery = None if recovery_parent is None else Path(recovery_parent).resolve()
        self._parent = self._recovery or (None if spool_parent is None else Path(spool_parent).resolve())
        from .conversion_history import ConversionHistory
        self._history = None if history_parent is None else ConversionHistory(history_parent)
        self._history_write_error = None
        if self._history is not None and self._parent is not None:
            history_root = self._history.root.resolve()
            if history_root.is_relative_to(self._parent) or self._parent.is_relative_to(history_root):
                raise ValueError('History must be outside result storage')
        self._lock = threading.RLock()
        self._io_drained = threading.Condition(self._lock)
        self._jobs, self._tickets = {}, {}
        self._reserved = 0
        # Construction and metadata inspection start no worker. Native preflight
        # is explicit; the export executor starts its thread only at start().
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='aio-guard')
        # Lazy, separate disk cleanup: never queue deletion behind native conversion.
        self._cleanup_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='aio-cleanup')
        self._closed = False
        self._preflight_gate = threading.BoundedSemaphore(1)
        self._proof_cache = OrderedDict()
        self._recovery_issues = []
        self._recovery_pending = []
        self._recovery_retry = set()
        self._recovery_loaded = not bool(self._recovery)
        self._recovery_lock = threading.Lock()
        # Constructed lazily like the conversion executor: no thread or disk
        # search starts until the user explicitly requests old results.
        self._recovery_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='aio-recovery')
        self._recovery_stop = threading.Event()
        self._recovery_future = None
        self._recovery_scan_state = 'idle'
        self._recovery_scan_id = 0
        self._recovery_scan_error = None

    def _remember_history(self, job):
        """Persist metadata on worker/save boundaries, outside the state lock.

        No outputs or sources are opened. An unavailable history must never
        change conversion success, save acknowledgements or result ownership.
        """
        if self._history is None:
            return
        try:
            with self._lock:
                if job.preparation or job.running or job.state not in TERMINAL:
                    return
                revision = job.revision
                report = copy.deepcopy({**self._report(job), 'jobId':job.id,
                    'state':job.state, 'officeStarts':job.office_starts,
                    'packagingError':job.packaging_error})
            self._history.record(job.id, revision, report, session=job.history_session)
        except Exception:
            # Sticky for this process: a later successful write does not prove
            # that a previously missed report was recovered.
            self._history_write_error = 'HISTORY_WRITE_FAILED'

    def history_list(self, *, before=None, limit=25):
        if self._history is None:
            return {'schema':1, 'enabled':False, 'items':[], 'nextCursor':None}
        return {**self._history.list(before=before, limit=limit),
                'writeWarning':self._history_write_error}

    def history_report(self, identity):
        if self._history is None:
            raise GuardError('HISTORY_DISABLED', 'Постоянная история недоступна.',
                             stage='history', status=503)
        return self._history.report(identity)

    def delete_history(self, identity):
        if self._history is None:
            raise GuardError('HISTORY_DISABLED', 'Постоянная история недоступна.',
                             stage='history', status=503)
        return self._history.delete(identity)

    def _load_recovery(self):
        # Legacy direct lookups retain synchronous semantics. The desktop UI
        # reads snapshots and submits this operation only on explicit request.
        try:
            with self._recovery_lock:
                with self._lock:
                    if self._closed:
                        return
                    initial = not self._recovery_loaded
                    pending = []
                    if not initial and len(self._jobs) < self.limits.max_jobs:
                        pending, self._recovery_pending = self._recovery_pending, []
                if initial:
                    self._recover_results()
                    with self._lock:
                        self._recovery_loaded = True
                elif pending:
                    self._recover_results(pending)
        except RecoveryStopped as error:
            raise GuardError('GUARD_CLOSED', 'Guard завершает работу.', status=503) from error
        except Exception:
            with self._lock:
                self._recovery_loaded = False  # A later explicit retry must rescan.
            raise

    def start_recovery_scan(self):
        with self._lock:
            if self._closed:
                raise GuardError('GUARD_CLOSED', 'Guard завершает работу.', status=503)
            if (self._recovery or self._jobs) and self._recovery_scan_state != 'scanning':
                # Every explicit request also reconciles known output files;
                # loaded old-job discovery alone cannot refresh their availability.
                self._recovery_scan_state = 'scanning'
                self._recovery_scan_error = None
                self._recovery_scan_id += 1
                # Claim and submit under one lock: duplicate clicks while the
                # worker runs still share one request and never perform inline I/O.
                try:
                    self._recovery_future = self._recovery_executor.submit(self._run_recovery_scan)
                except RuntimeError as error:
                    self._recovery_scan_state = 'failed'
                    self._recovery_scan_error = 'RECOVERY_SCAN_FAILED'
                    raise GuardError('RECOVERY_SCAN_FAILED', 'Не удалось начать поиск результатов.', status=503) from error
        return self.recoverable(scan=False)

    def _run_recovery_scan(self):
        failed = False
        try:
            self.recoverable(scan=True)
        except Exception:
            failed = True  # Paths and exception text never enter the UI status.
        finally:
            with self._lock:
                self._recovery_scan_state = ('closed' if self._closed else 'failed' if failed
                    else 'retry_available' if self._recovery_retry
                    else 'waiting_for_slot' if self._recovery_pending else 'completed')
                self._recovery_scan_error = 'RECOVERY_SCAN_FAILED' if failed and not self._closed else None

    def _recovery_warning(self, value):
        with self._lock:
            if value not in self._recovery_issues:
                self._recovery_issues.append(value)

    def _retry_recovery(self, root, code):
        # A busy/unreadable tree is not a completed search. Only another
        # explicit request retries it; snapshots and startup never touch disk.
        with self._lock:
            if root not in self._recovery_pending:
                self._recovery_pending.append(root)
            self._recovery_retry.add(root)
        self._recovery_warning({'folder': root.name, 'code': code, 'retryable': True})

    @staticmethod
    def _recovery_bytes(root):
        total = 0
        def failed(error):
            raise error
        for base, dirs, names in os.walk(root, followlinks=False, onerror=failed):
            dirs[:] = [name for name in dirs if not path_is_link(Path(base)/name)]
            for name in names:
                path = Path(base)/name
                if not path_is_link(path):
                    total += path.stat().st_size
        return total

    def _publish_recovered(self, job):
        if self._history is not None:
            try:
                # Only explicit recovery reaches here, with ownership of the
                # result journal. Never open SQLite under the Guard state lock.
                self._history.adopt(job.id, job.history_session)
            except Exception:
                self._history_write_error = 'HISTORY_WRITE_FAILED'
        # Validation/hashing happened without holding the state lock. A new
        # conversion can consume the final slot meanwhile; recheck atomically.
        with self._lock:
            if self._closed or job.id in self._jobs or any(j.root == job.root for j in self._jobs.values()):
                job.store.close()
                return
            if len(self._jobs) >= self.limits.max_jobs:
                if job.root not in self._recovery_pending:
                    self._recovery_pending.append(job.root)
                job.store.close()
                return
            self._jobs[job.id] = job
            self._reserved += job.reserved
            for artifact in job.artifacts:
                self._tickets[artifact.ticket] = artifact
            self._record(job, 'recovered')

    def _batch(self, job, name):
        method = getattr(self.adapter, 'batch', None)
        extra = ({'import_timeout':self.limits.native_import_timeout,
                  'close_timeout':self.limits.native_close_timeout,
                  'chart_timeout':self.limits.native_chart_timeout}
                 if type(self.adapter) is LibreOfficeAdapter else {})
        if extra and job.file_timeout and job.file_timeout != self.limits.file_timeout:
            extra['import_timeout'] = max(extra['import_timeout'], job.file_timeout)
            extra['chart_timeout'] = max(extra['chart_timeout'], job.file_timeout)
        return (method(job.root / name, max_tasks=self.limits.session_max_tasks, **extra)
                if method else nullcontext())

    def _journal(self, job, item, task):
        # This method is called under the state lock. Freeze small terminal
        # metadata, not disk I/O; output objects remain owned by this job.
        if job.store:
            job.pending_journal[task.index] = (item, copy.copy(task))

    def _store_write(self, job, operation, *args):
        """Synchronous durability on the existing caller, outside state lock."""
        tick = time.monotonic()
        with self._lock:
            job.io += 1
        try:
            operation(*args)
        except OSError:
            with self._lock:
                job.recovery_error = 'RECOVERY_WRITE_FAILED'
                self._record(job, 'recovery_write_failed', code='RECOVERY_WRITE_FAILED')
        finally:
            with self._lock:
                job.io -= 1
                job.timings['journalMs'] = job.timings.get('journalMs', 0) + round(
                    (time.monotonic() - tick) * 1000)
                self._io_drained.notify_all()

    def _flush_journal(self, job):
        # Pending failures are bounded by the already admitted task plan and
        # retain no document bytes. Bulk cancellation uses one durable batch.
        with job.journal_lock:
            while True:
                with self._lock:
                    entries = tuple(job.pending_journal.values())
                    job.pending_journal.clear()
                    if entries:
                        job.io += 1
                if not entries:
                    return
                try:
                    self._store_write(job, job.store.tasks, entries)
                finally:
                    with self._lock:
                        job.io -= 1
                        self._io_drained.notify_all()

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

    def _recover_results(self, roots=None):
        check_recovery_stop(self._recovery_stop)
        ResultStore.reap_finished_retirements(self._recovery, cancel=self._recovery_stop)
        if roots is None:
            # scandir reports access failures; glob may silently return an
            # empty list and falsely declare an unreadable store searched.
            try:
                with os.scandir(self._recovery) as entries:
                    roots = sorted(Path(entry.path) for entry in entries if entry.name.startswith('aio-guard-'))
            except FileNotFoundError:
                roots = []  # A first-run store has no old results yet.
        for root in roots:
            check_recovery_stop(self._recovery_stop)
            with self._lock:
                self._recovery_retry.discard(root)
                if any(job.root == root for job in self._jobs.values()):
                    # A deleting job may already have closed owner.lock. It is
                    # still ours until physical cleanup releases its quota.
                    continue
            with self._lock:
                if len(self._jobs) >= self.limits.max_jobs:
                    if root not in self._recovery_pending:
                        self._recovery_pending.append(root)
                    continue
            store = ResultStore(root)
            try:
                if path_is_link(root) or not root.is_dir():
                    continue
                if not store.acquire():
                    self._retry_recovery(root, 'RECOVERY_BUSY')
                    continue  # Another instance/Windows lock; never delete it.
                with self._lock:
                    self._recovery_retry.discard(root)
                    self._recovery_issues[:] = [w for w in self._recovery_issues
                        if w.get('folder') != root.name or not w.get('retryable')]
                try:
                    retired = store.retirement_pending()
                except (OSError, ValueError, TypeError):
                    store.close()
                    self._recovery_warning({'folder': root.name,
                        'code': 'RECOVERY_RETIREMENT_INVALID', 'manualRecovery': True})
                    continue  # An unreadable marker never authorizes deletion.
                if retired:
                    try:
                        store.cleanup_retired()
                    except (OSError, ValueError, TypeError):
                        self._recovery_warning({'folder': root.name,
                            'code': 'RECOVERY_CLEANUP_RETRY', 'manualRecovery': True})
                    finally:
                        store.close()
                    continue  # Never adopt a partially removed result tree.
                if not (root/'job.json').is_file():
                    # Crash during initial metadata write: preserve and surface
                    # the otherwise invisible directory for manual inspection.
                    self._recovery_warning({'folder': root.name,
                        'code': 'RECOVERY_MANIFEST_MISSING', 'manualRecovery': True})
                    self._salvage_orphan(root, store=store)
                    continue
                header, records, saved, integrity = store.read(cancel=self._recovery_stop)
                identity = header.get('jobId')
                if not isinstance(identity, str) or not re.fullmatch(r'[0-9a-f]{32}', identity):
                    raise ValueError('Invalid recovery job')
                rows = validate_files(header.get('files'), self.limits)
                files, indices = [], set()
                for row, original in zip(rows, header['files']):
                    check_recovery_stop(self._recovery_stop)
                    tasks = []
                    for target in original.get('targets', []):
                        index, format_id = target['index'], target['format']
                        if (type(index) is not int or index < 0 or index in indices or
                                not isinstance(format_id, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', format_id)):
                            raise ValueError('Invalid recovery task')
                        indices.add(index)
                        tasks.append(_Task(format_id, index, state='cancelled',
                            code='INTERRUPTED_RESTART', error='Задание было прервано. Исходник можно выбрать повторно.'))
                    item = _File(**row, source=root/'input'/'unavailable', targets=tasks, state='done',
                                 native_route=original.get('nativeRoute'),
                                 content_digest=original.get('sourceSha256'), source_released=True)
                    if header.get('retainSources') is True:
                        try:
                            item.source = store.input_path(original.get('sourcePath'), row['size'],
                                                           item.content_digest, cancel=self._recovery_stop)
                            from .html_resources import resource_manifest
                            metadata = original.get('resources', [])
                            resources = resource_manifest(metadata, item.source.name) if metadata else {}
                            for resource in metadata:
                                name = resource['path']
                                relative = (item.source.parent / name).relative_to(root).as_posix()
                                store.input_path(relative, resource['size'], resource.get('digest'), cancel=self._recovery_stop)
                                resources[name].update(ready=True, digest=resource['digest'])
                            item.resources = resources
                            item.source_released = False
                        except (OSError, ValueError, TypeError, KeyError, GuardError):
                            # The outputs are still useful if a retained original
                            # is absent/altered. Comparison reports unavailable,
                            # never imports guessed paths or deletes these bytes.
                            item.source = root/'input'/'unavailable'
                    files.append(item)
                job = _Job(identity, root, files, 0, store=store, recovered=True,
                           retain_sources=header.get('retainSources') is True)
                parent_id = header.get('comparisonParentJobId')
                recipe = header.get('comparisonRecipe')
                parent_tasks = header.get('comparisonParentTasks', [])
                if (parent_id is None or isinstance(parent_id, str)
                        and re.fullmatch(r'[0-9a-f]{32}', parent_id)):
                    job.comparison_parent_job_id = parent_id
                if recipe is None or isinstance(recipe, str) and re.fullmatch(r'[a-z0-9_]{1,64}', recipe):
                    job.comparison_recipe = recipe
                if isinstance(parent_tasks, list) and all(isinstance(t, str) and
                        re.fullmatch(r'[0-9a-f]{32}:[0-9]{1,20}', t) for t in parent_tasks):
                    job.comparison_parent_tasks = tuple(parent_tasks)
                job.input_accounting = header.get('inputAccounting', [])
                if integrity != 'complete':
                    job.recovery_error = 'RECOVERY_LEDGER_INCOMPLETE'
                    self._recovery_warning({'folder':root.name,
                        'code':'RECOVERY_LEDGER_INCOMPLETE', 'integrity':integrity,
                        'jobId':identity})
                summary=header.get('summary',{})
                job.timings=summary.get('timings',{})
                job.office_starts=summary.get('officeStarts',0)
                job.file_timeout=summary.get('fileTimeoutSeconds')
                job.files_by_id = {item.id:item for item in files}
                for item in files:
                    check_recovery_stop(self._recovery_stop)
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
                        task.execution_reason=record.get('executionReason')
                        automatic_recipe = record.get('automaticRecipe')
                        if isinstance(automatic_recipe, str) and re.fullmatch(r'[a-z0-9_]{1,64}', automatic_recipe):
                            task.automatic_recipe = automatic_recipe
                        task.automatic_parameters = self._automatic_parameters(record.get('automaticParameters'))
                        task.result_checks=record.get('resultChecks', [])[:8]
                        task.native_timings=clean_timings(record.get('nativeTimings'))
                        task.quality = record.get('quality') if isinstance(record.get('quality'),dict) else None
                        if task.quality and task.quality.get('qualityCompared'):
                            job.quality_compared = True
                        task.export_filter=record.get('exportFilter')
                        if task.export_filter is not None and (not isinstance(task.export_filter,str) or
                                                               len(task.export_filter)>256):
                            raise ValueError('Invalid recovery export filter')
                        task.warnings=tuple(code for code in record.get('warnings', [])
                                            if isinstance(code,str) and code in WARNING_TEXT)
                        if task.state not in SUCCESS:
                            continue
                        for output in record.get('outputs', []):
                            check_recovery_stop(self._recovery_stop)
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
                                    saved=relative in saved and relative in header.get('readbackVerifiedPaths', []),
                                    sha256=output.get('sha256'),
                                    readback_verified=relative in header.get('readbackVerifiedPaths', []),
                                    source_sheet=output.get('sourceSheet'))
                                if artifact.sha256 is not None:
                                    if not isinstance(artifact.sha256, str) or not re.fullmatch('[0-9a-f]{64}', artifact.sha256):
                                        raise ValueError('Invalid output fingerprint')
                                    from .save_verification import sha256_stream
                                    with path.open('rb') as restored:
                                        try:
                                            actual = sha256_stream(restored, cancel=self._recovery_stop)
                                        except GuardError:
                                            check_recovery_stop(self._recovery_stop)
                                            raise
                                    if actual != (artifact.size, artifact.sha256):
                                        raise ValueError('Recovery output fingerprint changed')
                                task.artifacts.append(artifact)
                                job.artifacts.append(artifact)
                                if relative in saved and not artifact.readback_verified:
                                    self._recovery_warning({'folder':root.name, 'jobId':identity,
                                        'code':'RECOVERY_SAVE_UNVERIFIED'})
                            except (OSError, ValueError, KeyError, TypeError) as error:
                                job.recovery_error = job.recovery_error or 'RECOVERY_OUTPUT_MISSING'
                                task.state, task.code = 'error', 'RECOVERY_OUTPUT_MISSING'
                                task.error = 'Один из результатов отсутствует или изменён. Доступные файлы сохранены.'
                                if isinstance(error, PermissionError):
                                    self._recovery_warning({'folder':root.name, 'jobId':identity,
                                        'code':'RECOVERY_OUTPUT_UNREADABLE'})
                        if task.state in SUCCESS and not task.artifacts:
                            # Legacy journals have no output count. An empty
                            # success cannot mean the file was converted or
                            # saved; keep surviving results and originals.
                            job.recovery_error = job.recovery_error or 'RECOVERY_OUTPUT_MISSING'
                            task.state, task.code = 'error', 'RECOVERY_OUTPUT_MISSING'
                            task.error = 'Результат отсутствует. Доступные файлы и исходники сохранены.'
                        elif task.artifacts and task.state in SUCCESS:
                            task.state = 'saved' if all(a.saved for a in task.artifacts) else 'ready'
                if not job.artifacts:
                    self._recovery_warning({'folder':root.name,
                        'code':'RECOVERY_UNFINISHED_PREPARATION', 'jobId':identity,
                        'manualRecovery': True,
                        'bytes':sum(p.stat().st_size for p in root.rglob('*')
                                    if p.is_file() and not p.is_symlink())})
                    self._salvage_orphan(root, store=store)
                    continue
                indexed={a.path.resolve() for a in job.artifacts}
                untracked=any(p.resolve() not in indexed for p in root.glob('work/*/output/**/*')
                              if p.is_file() and not p.is_symlink())
                if untracked and not job.recovery_error:
                    job.recovery_error='RECOVERY_UNTRACKED_OUTPUTS'
                    self._recovery_warning({'folder':root.name,
                        'code':'RECOVERY_UNTRACKED_OUTPUTS', 'jobId':identity})
                if untracked:
                    self._salvage_outputs(job, indexed)
                job.reserved = self._recovery_bytes(root)
                job.result = {**job.artifacts[0].metadata(), 'jobId':identity}
                job.packaging_error = 'RECOVERED_RESULTS'
                job.state = 'partial'
                job.diagnostic_bytes=sum(len(t.diagnostic.encode('utf-8')) for f in files for t in f.targets)
                check_recovery_stop(self._recovery_stop)
                self._publish_recovered(job)
            except RecoveryStopped:
                store.close()
                raise  # Shutdown is not invalid metadata and never triggers salvage.
            except OSError as error:
                store.close()
                if isinstance(error, FileNotFoundError) and not root.exists():
                    with self._lock:
                        self._recovery_retry.discard(root)
                    continue
                self._retry_recovery(root, 'RECOVERY_ACCESS_DENIED' if isinstance(error, PermissionError)
                                     else 'RECOVERY_READ_FAILED')
            except (ValueError, TypeError, KeyError, GuardError):
                store.close()  # Never delete an unreadable recovery directory.
                self._salvage_orphan(root)
                self._recovery_warning({'folder':root.name,
                    'code':'RECOVERY_METADATA_INVALID', 'manualRecovery': True})

    def _salvage_outputs(self, job, indexed=()):
        """Rescue only files from the owned native output contract, never inputs.

        No ledger association, native success or quality is inferred. Keep the
        original directory until an explicit discard, including after export.
        """
        indexed = set(indexed)
        next_index = max((t.index for f in job.files for t in f.targets), default=-1) + 1
        for output in sorted(job.root.glob('work/*/output')):
            if output.is_symlink() or path_is_link(output) or not output.is_dir():
                continue
            artifacts = []
            for base, dirs, names in os.walk(output, followlinks=False):
                dirs[:] = sorted(d for d in dirs if not (Path(base)/d).is_symlink()
                                 and not path_is_link(Path(base)/d))
                for name in sorted(names):
                    check_recovery_stop(self._recovery_stop)
                    candidate = Path(base)/name
                    try:
                        if candidate.is_symlink() or candidate.stat().st_nlink != 1:
                            continue
                        size = candidate.stat().st_size
                        relative = candidate.relative_to(job.root).as_posix()
                        path = job.store.output_path(relative, size)
                        if path.resolve() in indexed:
                            continue
                        archive_name = 'recovered/' + output.parent.name + '/' + path.relative_to(output).as_posix()
                        artifacts.append(_Artifact(job.id, path, path.name, size,
                            secrets.token_urlsafe(32), 'rescued-' + str(next_index), 'bin', archive_name))
                        indexed.add(path.resolve())
                    except (OSError, ValueError):
                        continue
            if artifacts:
                while 'rescued-' + str(next_index) in job.files_by_id:
                    next_index += 1
                task = _Task('bin', next_index, state='error', stage='recovery',
                             code='RECOVERED_UNVERIFIED', artifacts=artifacts)
                task.error = 'Файлы спасены с диска. Исходник, формат и полнота результата не подтверждены.'
                item = _File('rescued-' + str(next_index), 'Спасённые файлы · ' + output.parent.name,
                             0, job.root/'input'/'unavailable', [task], state='done')
                job.files.append(item)
                job.files_by_id[item.id] = item
                job.artifacts.extend(artifacts)
                next_index += 1
                job.salvage = True
                job.recovery_error = 'RECOVERY_SALVAGED_UNVERIFIED'
        return bool(job.artifacts)

    def _salvage_orphan(self, root, store=None):
        store = store or ResultStore(root)
        try:
            if store.owner is None and not store.acquire():
                return
            identity = hashlib.sha256(root.name.encode('utf-8')).hexdigest()[:32]
            job = _Job(identity, root, [], 0, store=store, recovered=True,
                       state='partial', recovery_error='RECOVERY_SALVAGED_UNVERIFIED')
            if not self._salvage_outputs(job):
                store.close()
                return
            job.reserved = self._recovery_bytes(root)
            job.result = {**job.artifacts[0].metadata(), 'jobId':identity}
            job.packaging_error = 'RECOVERED_RESULTS'
            check_recovery_stop(self._recovery_stop)
            self._publish_recovered(job)
        except RecoveryStopped:
            store.close()
            raise
        except OSError as error:
            store.close()
            self._retry_recovery(root, 'RECOVERY_ACCESS_DENIED' if isinstance(error, PermissionError)
                                 else 'RECOVERY_READ_FAILED')
        except ValueError:
            store.close()

    def recoverable(self, *, scan=True):
        if type(scan) is not bool:
            raise GuardError('RECOVERY_REQUEST', 'Неверный режим получения результатов.')
        # Lightweight GUI polls do not enumerate directories, hash documents,
        # wait for an existing scan, or synchronously remove a large tree.
        self._prune(reap_saved=True, defer_cleanup=not scan)
        if scan:
            self._load_recovery()
            with self._lock:
                identities = [job.id for job in self._jobs.values() if not job.released]
            for identity in identities:
                try:
                    self._reconcile_results(identity)
                except GuardError as error:
                    if error.code not in ('JOB_NOT_FOUND', 'GUARD_CLOSED'):
                        raise
        with self._lock:
            return {'storage': self.storage_stats(), 'pendingRecoveryFolders':len(self._recovery_pending),
                    'recoveryScan': {'state': self._recovery_scan_state,
                        'available': bool(self._recovery), 'loaded': self._recovery_loaded,
                        'requestId': self._recovery_scan_id,
                        'pendingFolders': len(self._recovery_pending),
                        'retryFolders': len(self._recovery_retry),
                        'errorCode': self._recovery_scan_error},
                    'warnings':list(self._recovery_issues),
                    'jobs': [{'jobId': j.id, 'recovered': j.recovered,
                              'label':j.files[0].name if j.files else 'Результаты',
                              'outputs': sum(not a.availability_error for a in j.artifacts),
                              'knownOutputs': len(j.artifacts),
                              'unavailableOutputs': sum(bool(a.availability_error) for a in j.artifacts),
                              'bytes': sum(a.size for a in j.artifacts if not a.availability_error),
                              'state': self._availability_state(j), 'conversionState': j.state, 'running': j.running, 'saveInProgress': j.saving,
                              'unsavedOutputs': sum(not a.saved and not a.availability_error for a in j.artifacts),
                              'unsavedCompanionFiles': sum(not a.saved and not a.availability_error and a.companion for a in j.artifacts),
                              'unsavedDocuments': sum(not a.saved and not a.availability_error and not a.companion for a in j.artifacts),
                              'unsavedBytes': sum(a.size for a in j.artifacts if not a.saved and not a.availability_error),
                              'recoveryDurable': bool(j.store and not j.recovery_error),
                              'retainSources': j.retain_sources,
                              'comparisonAvailable': False,
                              'comparisonParentJobId': j.comparison_parent_job_id,
                              'comparisonRecipe': j.comparison_recipe,
                              'comparisonParentTasks': list(j.comparison_parent_tasks),
                              'salvaged': j.salvage, 'qualityCompared': False}
                             for j in self._jobs.values() if not j.released
                             and (j.recovery_error or any(not a.saved for a in j.artifacts) or
                                  j.retain_sources and not j.preparation and (j.artifacts or
                                  any(t.state in DONE for f in j.files for t in f.targets)))]}

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

    def _record(self, job, stage, **details):
        changed_file = details.get('file_id')
        if job.counts is None or stage in ('job_queued', 'recovered', 'save_confirmed'):
            job.counts = dict.fromkeys(COUNT_KEYS, 0)
            for item in job.files:
                item.counts = self._scan_counts((item,))
                for key in COUNT_KEYS:
                    job.counts[key] += item.counts[key]
        elif changed_file in job.files_by_id:
            item = job.files_by_id[changed_file]
            previous = item.counts or dict.fromkeys(COUNT_KEYS, 0)
            item.counts = self._scan_counts((item,))
            for key in COUNT_KEYS:
                job.counts[key] += item.counts[key] - previous[key]
        job.trace.add(stage, **details)
        job.revision += 1
        changed = details.get('file_id')
        if stage in {'job_queued', 'save_confirmed', 'job_finished', 'recovered',
                     'results_available', 'job_failed'} and changed is None:
            changed = '*'
        job.changes.append((job.revision, changed))
        if stage in ('preflight_started','preflight_done','job_queued','job_started',
                     'engine_started','packaging_started','bundle_ready','job_finished'):
            job.timings.setdefault(stage+'Ms',round((time.monotonic()-job.trace.started)*1000))
    def _get(self, identity):
        if self._closed:
            raise GuardError('GUARD_CLOSED', 'Guard завершает работу.', status=503)
        job = self._jobs.get(identity)
        if job is None or job.released:
            raise GuardError('JOB_NOT_FOUND', 'Задание больше недоступно.', status=404)
        job.touched = time.monotonic()
        return job

    def _prune(self, *, reap_saved=False, defer_cleanup=False):
        with self._lock:
            now = time.monotonic()
            expired = [j for j in self._jobs.values() if j.released or (
                not j.running and not j.io and not j.pending_journal and (
                    reap_saved and self._saved_all(j) and not (j.retain_sources or j.retain_results) or not j.artifacts and not j.recovery_error
                    and now - j.touched > self.limits.retention_seconds))]
            for job in expired:
                job.released = True
                job.cleanup_background = job.cleanup_background or defer_cleanup
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

    def create(self, files, formats=None, *, plan=None, prepare_only=False, retain_sources=False, retain_results=False):
        if type(retain_results) is not bool:
            raise GuardError('RETENTION_POLICY', 'Некорректный режим хранения результатов.')
        if type(retain_sources) is not bool:
            raise GuardError('COMPARISON_POLICY', 'Некорректный режим хранения исходников.')
        rows = validate_files(files, self.limits)
        planned = ({row['id']: [] for row in rows} if prepare_only else
                   validate_plan(rows, formats, plan))
        self._prune(reap_saved=True)
        size = sum(row['size'] for row in rows)
        with self._lock:
            if self._closed:
                raise GuardError('GUARD_CLOSED', 'Guard завершает работу.', status=503)
            if len(self._jobs) >= self.limits.max_jobs:
                raise GuardError('JOB_LIMIT', 'Заняты все слоты Guard (' + str(self.limits.max_jobs) + '). '
                    'Откройте «Состояние»: сохраните или явно удалите завершённые результаты. '
                    'Несохранённые файлы не удаляются автоматически.', status=409)
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
            job = _Job(identity, root, files, size, preparation=prepare_only,
                       retain_sources=retain_sources, retain_results=retain_results)
            job.files_by_id = {item.id: item for item in files}
            self._record(job, 'job_created')
            self._jobs[identity] = job
            self._reserved += size
            job.io += 1
        try:
            self._store_write(job, self._initialize_store, job)
            with self._lock:
                if self._closed:
                    raise GuardError('GUARD_CLOSED', 'Guard завершает работу.', status=503)
                return self._snapshot(job)
        finally:
            with self._lock:
                job.io -= 1
                self._io_drained.notify_all()
            self._cleanup(job)

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

    def configure_resources(self, job_id, file_id, resources):
        from .html_resources import resource_manifest
        with self._lock:
            job = self._get(job_id)
            item = job.files_by_id.get(file_id) if isinstance(file_id, str) else None
            if item is None:
                raise GuardError('FILE_NOT_FOUND', 'Исходник не относится к заданию.', status=404)
            if (not job.preparation or job.running or job.io or job.state not in ('uploading', 'prepared')
                    or item.source.suffix.lower() not in ('.html', '.htm', '.xhtml')):
                raise GuardError('RESOURCE_STATE', 'Дождитесь проверки HTML.', status=409)
            manifest = resource_manifest(resources, item.source.name)
            if item.resources:
                # Immutable ownership: retrying the same manifest is allowed;
                # replacing companions requires an explicit new preparation.
                if {p:r['size'] for p,r in manifest.items()} != {p:r['size'] for p,r in item.resources.items()}:
                    raise GuardError('RESOURCE_STATE', 'Папка ресурсов уже назначена. Выберите исходник заново.', status=409)
                return self._snapshot(job)
            self._reserve(job, sum(r['size'] for r in manifest.values()))
            item.resources = manifest
            item.native_route, item.state, job.state = None, 'queued', 'uploading'
            self._record(job, 'resources_configured', file_id=item.id)
            return self._snapshot(job)

    def upload_resource(self, job_id, file_id, path, stream, length, *, interrupt=None):
        from .html_resources import resource_path
        path = resource_path(path)
        with self._lock:
            job = self._get(job_id)
            item = job.files_by_id.get(file_id) if isinstance(file_id, str) else None
            resource = item.resources.get(path) if item else None
            if resource is None:
                raise GuardError('RESOURCE_NOT_FOUND', 'Ресурс не объявлен в задании.', status=404)
            if (not job.preparation or job.running or job.state != 'uploading' or resource['ready']
                    or path in item.resources_uploading):
                raise GuardError('RESOURCE_STATE', 'Этот ресурс уже загружен или используется.', status=409)
            if type(length) is not int or length != resource['size']:
                raise GuardError('UPLOAD_LENGTH', 'Размер ресурса не совпадает.')
            item.resources_uploading.add(path)
            if interrupt:
                item.resource_interrupts[path] = interrupt
            job.io += 1
        target = item.source.parent / path
        part = item.source.parent / ('.aio-resource-' + secrets.token_hex(12))
        digest = hashlib.sha256()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with part.open('xb') as output:
                remaining = length
                while remaining:
                    if job.cancel.is_set() or item.cancel.is_set():
                        raise GuardError('CANCELLED', 'Загрузка ресурсов отменена.', stage='upload')
                    block = stream.read(min(CHUNK, remaining))
                    if not block or len(block) > remaining:
                        raise GuardError('UPLOAD_TRUNCATED', 'Загрузка ресурса прервана.', stage='upload')
                    output.write(block)
                    digest.update(block)
                    remaining -= len(block)
            with self._lock:
                if job.cancel.is_set() or item.cancel.is_set():
                    raise GuardError('CANCELLED', 'Загрузка ресурсов отменена.', stage='upload')
                if target.exists():
                    raise GuardError('RESOURCE_COLLISION', 'Ресурс перезаписывает другой файл.')
                part.replace(target)
                resource.update(ready=True, digest=digest.hexdigest())
                self._record(job, 'resource_stored', file_id=item.id)
        except OSError as error:
            raise GuardError('INPUT_STORAGE_FAILED', 'Не удалось записать ресурс HTML.',
                             stage='upload', status=507) from error
        finally:
            try:
                part.unlink(missing_ok=True)
            finally:
                with self._lock:
                    item.resources_uploading.discard(path)
                    item.resource_interrupts.pop(path, None)
                    job.io -= 1
                    self._io_drained.notify_all()
                self._cleanup(job)

    def update_prepared(self, job_id, files):
        """Reuse immutable stored files/proofs; upload and inspect only additions."""
        rows = validate_files(files, self.limits)
        with self._lock:
            job = self._get(job_id)
            if not job.preparation or job.state != 'prepared' or job.running or job.io:
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
            for row in additions:
                name, normalized = _source_name(row['name'])
                item = _File(**row, source=job.root/'input'/secrets.token_hex(8)/name,
                             targets=[], name_normalized=normalized)
                job.files_by_id[item.id] = item
            job.files = [job.files_by_id[row['id']] for row in rows]
            job.files_by_id = {f.id:f for f in job.files}
            job.state = 'uploading'
            job.io += 1
            self._record(job, 'prepared_selection_updated')
        try:
            # Antivirus/network filesystems may stall deletion. The I/O lease
            # keeps this tree owned while status, cancel and other jobs remain
            # responsive; removed sources are no longer visible to preflight.
            for item in removed:
                self._drop_source(job, item)
            with self._lock:
                return self._snapshot(self._get(job_id))
        finally:
            with self._lock:
                job.io -= 1
                self._io_drained.notify_all()
            self._cleanup(job)

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
                        any(f.uploading or f.resources_uploading or any(not r['ready'] for r in f.resources.values())
                            or f.state not in {'queued','prepared'} for f in job.files)):
                    job = None
                    raise GuardError('JOB_STATE', 'Дождитесь загрузки исходников.', status=409)
                # A 510-file matrix must not spend the normal two-minute
                # preflight budget on every malformed/native-wedged sample.
                # Unverified files remain unavailable, with a coded reason;
                # the explicit "recheck longer" action retains full budgets.
                if timeout_seconds is None and len(job.files) >= 32:
                    native_timeout = min(native_timeout, 15.0)
                job.state, job.running = 'checking', True
                job.preflight_phase = 'files'
                job.preflight_matrix_completed = job.preflight_matrix_total = 0
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
                    resources_digest = hashlib.sha256(json.dumps(sorted(
                        (p, r['size'], r.get('digest')) for p, r in item.resources.items())).encode()).hexdigest()
                    key = (identity, item.content_digest, item.source.name, resources_digest) if identity and item.content_digest else None
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
                        missing = []
                        html_warning = _html_resource_warning(item.source,
                            cancel=_Cancellation(job.cancel, item.cancel), missing=missing,
                            seconds=max(2, native_timeout / 60))
                        if html_warning:
                            proof = {'state':'undetermined', 'code':html_warning,
                                     'missingResources':missing, 'exported':False}
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
                files = [{'id': f.id, 'name': f.name, 'size': f.size} for f in job.files]
                proofs = {f.id: f.native_route for f in job.files}
            # All source imports may finish while the native route matrix is
            # still running. Make this phase visible and independently
            # cancellable instead of holding the UI at "510 / 510".
            with self._lock:
                if job.cancel.is_set():
                    raise GuardError('CANCELLED', 'Предварительная проверка отменена.', stage='preflight')
                job.preflight_phase = 'routes'
                self._record(job, 'preflight_matrix_started')

            def matrix_progress(completed, total):
                with self._lock:
                    if job.cancel.is_set():
                        raise GuardError('CANCELLED', 'Проверка направлений отменена.', stage='preflight')
                    job.preflight_matrix_completed = completed
                    job.preflight_matrix_total = total
                    self._record(job, 'preflight_matrix_progress')

            capability = self.adapter.capabilities
            # Preserve older external/test adapters which implement only
            # capabilities(files, proofs=...). Native default has an explicit
            # cooperative cancel/progress protocol; overridden methods retain
            # their original signature and result semantics.
            if getattr(capability, '__func__', None) is LibreOfficeAdapter.capabilities:
                report = capability(files, proofs=proofs, cancel=job.cancel,
                                    on_progress=matrix_progress)
            else:
                report = capability(files, proofs=proofs)
            with self._lock:
                if job.cancel.is_set():
                    raise GuardError('CANCELLED', 'Предварительная проверка отменена.', stage='preflight')
                job.state = 'prepared'
                job.preflight_phase = 'done'
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
                        job.preflight_phase = 'cancelled'
                    job.io -= 1
                    self._record(job,'preflight_done')
                    self._io_drained.notify_all()
                self._cleanup(job)
            self._preflight_gate.release()

    def start(self, job_id, *, formats=None, file_ids=None, only_verified=False, plan=None,
              timeout_seconds=None, archive_mode='auto', quality_check=False):
        if type(quality_check) is not bool:
            raise GuardError('QUALITY_POLICY', 'Некорректный режим проверки структуры.')
        planning_started = time.monotonic()
        unused = []
        if not isinstance(archive_mode, str) or archive_mode not in ('auto', 'on_demand'):
            raise GuardError('ARCHIVE_POLICY', 'Некорректный режим создания архива.', stage='plan')
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
                selected_ids = set(file_ids)
                job.input_accounting = [
                    {'fileId':f.id, 'name':f.name, 'planned':f.id in selected_ids,
                     'preflightState':(f.native_route or {}).get('state'),
                     'code':(f.native_route or {}).get('code')}
                    for f in job.files]
                planned = validate_plan([{'id': f.id, 'name': f.name, 'size': f.size} for f in selected],
                                        formats, plan)
                selected_ids = set(file_ids)
                for item in job.files:
                    if item.id not in selected_ids:
                        unused.append(item)
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
                            html_issue = (item.native_route or {}).get('code')
                            html_issue = html_issue if html_issue in (
                                'HTML_LOCAL_RESOURCES_NOT_UPLOADED', 'HTML_PREFLIGHT_UNDETERMINED') else None
                            task.code = (html_issue if html_issue else
                                         'NATIVE_ROUTE_UNAVAILABLE' if decision == 'unavailable' else
                                         'PREFLIGHT_ROUTE_UNDETERMINED')
                            task.error = ('HTML ссылается на локальные файлы, но AIO получил только основной HTML. '
                                          'Чтобы не потерять изображения/стили, экспорт не запускается.'
                                          if html_issue == 'HTML_LOCAL_RESOURCES_NOT_UPLOADED' else
                                          'Проверка зависимостей HTML не завершена из-за синтаксиса или времени анализа. Можно повторить проверку дольше.'
                                          if html_issue else 'У определённого LibreOffice типа нет этого экспорта.'
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
            job.archive_mode = archive_mode
            job.quality_check = quality_check
            job.timings['planMs'] = round((time.monotonic()-planning_started)*1000)
            job.file_timeout = self.limits.file_timeout if timeout_seconds is None else timeout_seconds
            self._record(job, 'job_queued')
            job.io += 1
        try:
            for item in unused:
                self._drop_source(job, item)
            self._store_write(job, self._initialize_store, job)
            with self._lock:
                if self._closed:
                    job.running = False
                    raise GuardError('GUARD_CLOSED', 'Guard завершает работу.', status=503)
                job.future = self._executor.submit(self._run, job)
                return self._snapshot(job)
        finally:
            with self._lock:
                job.io -= 1
                self._io_drained.notify_all()
            self._cleanup(job)

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

    def _capture_rejected_output(self, job, item, task, workspace, error):
        # Opt-in diagnostics have a separate small budget, covered by the
        # configured spool quota. Never let a capture replace the native error.
        limit = self.limits.rejected_output_bytes
        if not limit or error.code == 'CANCELLED':
            return
        try:
            with self._lock:
                if not job.diagnostic_quota_reserved:
                    self._reserve(job, limit)
                    job.diagnostic_quota_reserved = limit
            from .diagnostic_quarantine import capture_failed_output
            receipt = capture_failed_output(job.root, workspace,
                task_index=task.index, error_code=error.code,
                source_sha256=item.content_digest, diagnostic=error.diagnostic,
                max_bytes=limit, cancel=_Cancellation(job.cancel, item.cancel))
            with self._lock:
                task.result_checks = list(task.result_checks[:7]) + [{
                    'check': 'rejected_output_diagnostic', 'status': receipt['status'],
                    'qualityCompared': False, 'ready': False, 'receipt': receipt}]
        except Exception:
            pass

    def _output_budget(self):
        with self._lock:
            return max(0, self.limits.max_spool_bytes - self._reserved)

    def _capture_native_metrics(self, job, item, task):
        try:
            getter = getattr(self.adapter, 'native_timings', None)
            value = clean_timings(getter()) if getter else None
            export = getattr(self.adapter, 'export_info', lambda: {})()
            export = export if isinstance(export,dict) else {}
            selected = export.get('filter')
            selected = selected if isinstance(selected,str) and len(selected)<=256 else None
            codes = export.get('warnings')
            codes = codes if isinstance(codes,(list,tuple)) else ()
            warnings = tuple(code for code in WARNING_TEXT if code in codes)
        except Exception:
            return  # Diagnostic availability cannot change an export outcome.
        with self._lock:
            task.native_timings = value
            task.execution_mode = getattr(self.adapter, 'execution_mode', lambda: task.execution_mode)()
            task.export_filter = selected
            task.warnings = warnings
            task.result_checks = copy.deepcopy(export.get('resultChecks', []))[:8]
            reason = export.get('executionReason')
            task.execution_reason = reason if isinstance(reason, str) and len(reason) <= 64 else None
            automatic = export.get('automaticRecipe')
            task.automatic_recipe = (automatic if isinstance(automatic, str) and
                re.fullmatch(r'[a-z0-9_]{1,64}', automatic) else None)
            task.automatic_parameters = self._automatic_parameters(export.get('automaticParameters'))

    @staticmethod
    def _automatic_parameters(value):
        if (not isinstance(value, dict) or len(value) > 16 or any(
                not isinstance(key, str) or len(key) > 64 or
                not (type(item) in (bool, int) or isinstance(item, str) and len(item) <= 1024)
                for key, item in value.items())):
            return None
        return dict(value)

    def _retain_outputs(self, job, item, task, workspace, paths):
        root = (workspace / 'output').resolve()
        if not root.is_relative_to(job.root.resolve()):
            raise GuardError('ENGINE_OUTPUT_PATH', 'Недопустимое расположение результата.',
                             stage='result')
        artifacts, seen = [], set()
        # Optional adapter diagnostics must not turn verified files into a loss.
        # The native adapter validates its mandatory all-sheet manifest before
        # returning paths; this layer only retains the validated provenance.
        try:
            export = getattr(self.adapter, 'export_info', lambda: {})()
            entries = export.get('sheetOutputs', []) if isinstance(export, dict) else []
            sheet_outputs = {entry['file']:entry for entry in entries
                if isinstance(entry, dict) and isinstance(entry.get('file'), str)
                and type(entry.get('sourceSheetIndex')) is int and entry['sourceSheetIndex'] >= 0
                and isinstance(entry.get('sourceSheetName'), str)} if isinstance(entries, list) else {}
        except Exception:
            sheet_outputs = {}
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
            from .save_verification import sha256_stream
            try:
                with resolved.open('rb') as output:
                    retained_size, retained_sha256 = sha256_stream(output,
                        cancel=_Cancellation(job.cancel, item.cancel))
            except GuardError as error:
                if error.code == 'SAVE_CANCELLED':
                    raise GuardError('CANCELLED', 'Проверка результата отменена.', stage='result') from error
                raise
            artifacts.append(_Artifact(job.id, resolved, path.name, retained_size,
                secrets.token_urlsafe(32), item.id, task.format,
                f'{task.index + 1:04d}-{task.format}/' + relative, sha256=retained_sha256,
                source_sheet=({'index':sheet_outputs[path.name]['sourceSheetIndex'],
                               'name':sheet_outputs[path.name]['sourceSheetName'], 'indexBase':0}
                              if path.name in sheet_outputs else None)))
        if not artifacts:
            raise GuardError('ENGINE_OUTPUT_MISSING', 'LibreOffice не создал результат.',
                             stage='result')
        with self._lock:
            self._reserve(job, sum(a.size for a in artifacts))
            task.stage = 'result'
            self._record(job, 'result_retaining', file_id=item.id, target=task.format,
                         lo_started=task.lo_started)
            completed = copy.copy(task)
            completed.artifacts = artifacts
            completed.state = 'ready'
            completed.finished_ms = round(time.time() * 1000)
            completed.duration_ms = round((time.monotonic()-task.started_mono)*1000) if task.started_mono else None
        # Publish ready outputs only after the same durable transaction as
        # before. A slow fsync cannot lock status/cancel/other converter jobs.
        if job.store:
            with job.journal_lock:
                self._store_write(job, job.store.task, item, completed)
        with self._lock:
            for artifact in artifacts:
                self._tickets[artifact.ticket] = artifact
            job.artifacts.extend(artifacts)
            task.artifacts = artifacts
            task.state = 'ready'
            if task.quality and task.quality.get('qualityCompared'):
                job.quality_compared = True
            task.finished_ms = completed.finished_ms
            task.duration_ms = completed.duration_ms
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
        if item.source_released:
            return
        try:
            shutil.rmtree(item.source.parent)
        except FileNotFoundError:
            pass
        except OSError:
            with self._lock:
                self._record(job, 'cleanup_deferred', file_id=item.id, code='INPUT_CLEANUP_RETRY')
            return
        with self._lock:
            self._unreserve(job, item.size + sum(r['size'] for r in item.resources.values()))
            item.source_released = True

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
                    before_quality = None
                    if job.quality_check and not job.cancel.is_set() and not item.cancel.is_set():
                        from .quality import inspect_isolated
                        try:
                            before_quality = inspect_isolated(item.source, job.root/'quality-source'/item.id,
                                cancel=_Cancellation(job.cancel, item.cancel))
                        except Exception:
                            before_quality = {'complete':False,'status':'not_checked','reason':'inspection_failed'}
                    prepare_source = getattr(self.adapter, 'prepare_source', None)
                    if prepare_source:
                        prepare_source(item.native_route)
                    for task in item.targets:
                        self._flush_journal(job)
                        self._wait_unpaused(job)
                        workspace = job.root / 'work' / str(task.index)
                        with self._lock:
                            if task.state in DONE:
                                continue
                            if job.cancel.is_set() or item.cancel.is_set():
                                if task.state not in DONE:
                                    self._fail(job, item, task, GuardError('CANCELLED', 'Конвертация отменена.'))
                                continue
                            if source_failure == 'DIF_NUMERIC_IMPORT_MISMATCH':
                                self._fail(job, item, task, GuardError(
                                    source_failure, 'Импорт чисел DIF изменяет данные. Повторные направления этого исходника не запускались.',
                                    stage='import'))
                                continue
                            if source_failure:
                                self._fail(job, item, task, GuardError(
                                    'SOURCE_DEFERRED_AFTER_TIMEOUT' if source_failure == 'ENGINE_TIMEOUT' else
                                    'PREFLIGHT_ROUTE_UNDETERMINED' if source_failure in (
                                        'NATIVE_MODEL_CHANGED', 'SOURCE_MODEL_MISMATCH', 'NATIVE_IMPORT_FILTER_CHANGED')
                                    else 'SOURCE_IMPORT_FAILED',
                                    'Оставшиеся направления этого файла отложены после тайм-аута. Можно повторить с большим временем.'
                                    if source_failure == 'ENGINE_TIMEOUT' else
                                    'Нативная модель или фильтр импорта изменились. Повторите проверку исходника.'
                                    if source_failure in ('NATIVE_MODEL_CHANGED', 'SOURCE_MODEL_MISMATCH', 'NATIVE_IMPORT_FILTER_CHANGED') else
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
                                if stage in ('import','export','close'):
                                    with self._lock:
                                        task.stage=stage
                                        self._record(job,'native_stage',file_id=item.id,target=task.format)
                            stages = ({'on_stage':stage_changed} if getattr(self.adapter.convert,'__func__',None)
                                      is LibreOfficeAdapter.convert else {})
                            try:
                                prepare_recipe = getattr(self.adapter, 'prepare_retry_recipe', None)
                                if prepare_recipe:
                                    prepare_recipe(None)
                                paths = self.adapter.convert(item.source, task.format, workspace,
                                    cancel=_Cancellation(job.cancel, item.cancel),
                                    timeout=job.file_timeout, on_started=started,
                                    output_limit=(None if self.limits.max_spool_bytes is None else self._output_budget),
                                    **stages, **({'on_document_service': detected if needs_observation(item.native_route) else None}
                                       if routing else {}))
                            finally:
                                self._capture_native_metrics(job, item, task)
                            if job.quality_check:
                                try:
                                    from .quality import inspect_isolated, compare_documents
                                    primary = next((p for p in paths if p.suffix.lower()=='.'+task.format), None)
                                    after_quality = (inspect_isolated(primary, workspace/'quality',
                                        cancel=_Cancellation(job.cancel,item.cancel)) if primary else
                                        {'complete':False,'status':'not_checked','reason':'primary_undetermined'})
                                    task.quality = compare_documents(before_quality or {'complete':False}, after_quality)
                                except Exception:
                                    # Diagnostics cannot turn a successful native
                                    # export into failure or destroy its outputs.
                                    task.quality = {'schema':1,'status':'not_checked','qualityCompared':False,
                                                    'reason':'inspection_failed','risks':[]}
                            self._retain_outputs(job, item, task, workspace, paths)
                        except GuardError as error:
                            self._capture_rejected_output(job, item, task, workspace, error)
                            if (error.code in ('DIF_NUMERIC_IMPORT_MISMATCH', 'SOURCE_IMPORT_FAILED', 'NATIVE_MODEL_CHANGED',
                                               'SOURCE_MODEL_MISMATCH', 'NATIVE_IMPORT_FILTER_CHANGED',
                                               'PREFLIGHT_INTERACTION_REQUIRED')
                                    or error.code == 'ENGINE_TIMEOUT' and error.stage not in ('export', 'close')):
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
                            self._flush_journal(job)
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
                    # Ordinary API jobs retain their existing cleanup policy.
                    # Legacy retained jobs keep disk-owned originals until their
                    # owning result job is explicitly released/saved/discarded.
                    if not job.retain_sources:
                        self._drop_source(job, item)
            self._flush_journal(job)
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
            prepare_recipe = getattr(self.adapter, 'prepare_retry_recipe', None)
            if prepare_recipe:
                try:
                    prepare_recipe(None)
                except Exception:
                    pass  # A diagnostic/reset failure must not skip durable cleanup.
            self._flush_journal(job)
            if job.store:
                self._store_write(job, job.store.finish, job)
            with self._lock:
                job.running = False
                job.touched = time.monotonic()
                self._record(job, 'worker_finished', code=job.state)
            self._remember_history(job)
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
    def _scan_counts(files):
        counts = dict.fromkeys(COUNT_KEYS, 0)
        for item in files:
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

    @staticmethod
    def _counts(job):
        return dict(job.counts) if job.counts is not None else ConverterGuard._scan_counts(job.files)

    def _report(self, job):
        from .evidence_status import task_evidence
        return {'schemaVersion': 3, **self._counts(job), 'preflightChecks':job.preflight_checks,
                'buildIdentity':dict(self.build_identity),
                'nativeRegistryFingerprints': sorted({f.native_route['registryFingerprint'] for f in job.files
                    if isinstance(f.native_route, dict) and isinstance(f.native_route.get('registryFingerprint'), str)
                    and re.fullmatch(r'[0-9a-f]{64}', f.native_route['registryFingerprint'])}),
                'inputsUploaded':len(job.input_accounting or job.files),
                'inputsPlanned':len(job.files),
                'inputsWithoutTasks':[row for row in job.input_accounting if not row['planned']],
                'recoveryError':job.recovery_error, 'files': [
            {'fileId': f.id, 'name': f.name, 'sourceSha256':f.content_digest,
             'nativeRoute': f.native_route, 'warnings': (
                ['SOURCE_NAME_NORMALIZED'] if f.name_normalized else []),
             'targets': [{'taskId': job.id + ':' + str(t.index), 'format': t.format, 'state': t.state, 'code': t.code,
                          'error': t.error, 'stage': t.stage, **task_evidence(t, quality_requested=job.quality_check),
                          'diagnostic': t.diagnostic,
                          'executionMode': t.execution_mode,
                          'executionReason': t.execution_reason, 'resultChecks':copy.deepcopy(t.result_checks),
                          'automaticRecipe':t.automatic_recipe,
                          'automaticParameters':copy.deepcopy(t.automatic_parameters),
                          'durationMs': t.duration_ms,
                          'nativeTimings': clean_timings(t.native_timings),
                          'exportFilter': t.export_filter, 'warnings': warning_details(t.warnings),
                          'quality':copy.deepcopy(t.quality) if t.quality else None,
                          'outputs': [{'path': a.archive_name, 'size': a.size, 'sha256':a.sha256,
                                       'saved':a.saved, 'available':not a.availability_error,
                                       'availabilityCode':a.availability_error, 'readbackVerified':a.readback_verified,
                                       'sourceSheet':copy.deepcopy(a.source_sheet)} for a in t.artifacts],
                          'route': t.route,
                          'libreOfficeStarted': t.lo_started} for t in f.targets]}
            for f in job.files], 'timings':dict(job.timings),'qualityCheckRequested':job.quality_check,
            'qualityCompared':job.quality_compared,
            'retainSources':job.retain_sources,
            'comparisonParentJobId':job.comparison_parent_job_id,
            'comparisonRecipe':job.comparison_recipe,
            'comparisonParentTasks':list(job.comparison_parent_tasks)}

    def report(self, job_id, *, recover=True):
        if type(recover) is not bool:
            raise GuardError('RECOVERY_REQUEST', 'Неверный режим поиска задания.')
        if recover and job_id not in self._jobs:
            self._load_recovery()
        with self._lock:
            job = self._get(job_id)
            return {**self._report(job), 'jobId': job.id, 'state': job.state,
                    'officeStarts':job.office_starts,'trace':job.trace.snapshot(),
                    'fileTimeoutSeconds':job.file_timeout,'recoveryError':job.recovery_error}

    def _make_result(self, job, *, force_archive=False):
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
            if not force_archive and len(job.artifacts) == 1 and all(t.state in SUCCESS for f in job.files for t in f.targets):
                return
            if job.archive_mode == 'on_demand' and not force_archive:
                # Folder-save consumes the retained artifacts directly. Do not
                # copy gigabytes into an unused ZIP or reserve duplicate disk
                # space before the user explicitly asks for an archive.
                job.bundle_deferred = True
                job.result['folderOnly'] = True
                self._record(job, 'bundle_deferred')
                return
            job.bundle_deferred = False
            copies = tuple(job.artifacts)
            archive_snapshot = {'schema': 1, 'snapshotId': secrets.token_hex(16),
                'snapshotRevision': job.revision, 'state': 'packaged',
                'jobStateAtStart': job.state, 'externalSave': 'not_observed',
                'saveStateScope': 'at_archive_start_not_this_download',
                'completionAuthority': 'adjacent_zip_AIO-save-status.json'}
            report_value = copy.deepcopy(self._report(job))
            report_value['reportScope'] = 'conversion_snapshot_at_archive_start'
            report_value['archiveSnapshot'] = archive_snapshot
        # Encoding a large report is CPU work, not a shared-state operation.
        report = json.dumps(report_value, ensure_ascii=False, indent=2).encode('utf-8')
        manifest_budget = len(json.dumps({'schema':2, 'jobId':job.id, 'outputs':[
            {'path':a.archive_name, 'size':a.size, 'sha256':'0'*64,
             'fileId':a.file_id, 'format':a.format, 'sourceSheet':a.source_sheet} for a in copies]},
            ensure_ascii=False).encode('utf-8'))
        with self._lock:
            budget = (sum(a.size + 512 + 2 * len(a.archive_name.encode('utf-8')) for a in copies)
                      + len(report) + manifest_budget + 4096)
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
        archive_tick = time.monotonic()
        try:
            with zipfile.ZipFile(bundle, 'x', compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
                manifest = []
                for artifact in copies:
                    digest, written = hashlib.sha256(), 0
                    source_path = self._owned_artifact_path(job, artifact, stage='bundle')
                    info = zipfile.ZipInfo.from_file(source_path, arcname=artifact.archive_name)
                    # Force ZIP64 even when the stream length was not given to
                    # ZipFile.open: no hidden 2 GiB failure half-way through.
                    with source_path.open('rb') as source, archive.open(
                            info, 'w', force_zip64=True) as output:
                        while block := source.read(CHUNK):
                            if job.bundle_cancel.is_set():
                                raise GuardError('BUNDLE_CANCELLED', 'Упаковка отменена.')
                            output.write(block)
                            digest.update(block)
                            written += len(block)
                    if written != artifact.size or (artifact.sha256 and digest.hexdigest() != artifact.sha256):
                        raise GuardError('SAVE_CHANGED', 'Байты результата изменились после проверки, до упаковки.')
                    manifest.append({'path':artifact.archive_name, 'size':written,
                                     'sha256':digest.hexdigest(), 'fileId':artifact.file_id,
                                     'format':artifact.format, 'exportSha256':artifact.sha256,
                                     'copySha256':digest.hexdigest(), 'sourceSheet':copy.deepcopy(artifact.source_sheet)})
                metadata = {'AIO-conversion-report.json': report,
                    'AIO-save-status.json': json.dumps({'jobId':job.id,
                        **archive_snapshot, 'readbackVerified':False}, ensure_ascii=False).encode('utf-8'),
                    'AIO-results-manifest.json': json.dumps(
                        {'schema':2, 'jobId':job.id, 'readbackVerified':False,
                         'archiveSnapshot':archive_snapshot,
                         'verificationScope':'hashes_during_zip', 'outputs':manifest},
                        ensure_ascii=False).encode('utf-8')}
                for name, content in metadata.items():
                    with archive.open(name, 'w', force_zip64=True) as output:
                        output.write(content)
            from .archive_verification import verify_built_archive
            actual, bundle_sha256, archive_check = verify_built_archive(
                bundle, manifest, metadata, archive_snapshot, cancel=job.bundle_cancel)
            with self._lock:
                if job.bundle_cancel.is_set():
                    raise GuardError('BUNDLE_CANCELLED', 'Упаковка отменена.', stage='bundle')
                delta = actual - budget
                if delta > 0:
                    self._reserve(job, delta)
                else:
                    self._unreserve(job, -delta)
                name = ('AIO_Results_Partial.zip' if any(t.state not in SUCCESS for f in job.files for t in f.targets)
                        else 'AIO_Results.zip')
                artifact = _Artifact(job.id, bundle, name, actual, secrets.token_urlsafe(32),
                                     sha256=bundle_sha256, bundle_outputs=copies,
                                     archive_verification=archive_check,
                                     archive_expected=(copy.deepcopy(manifest), dict(metadata), dict(archive_snapshot)))
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
            with self._lock:
                job.timings['archiveMs'] = job.timings.get('archiveMs', 0) + round(
                    (time.monotonic() - archive_tick) * 1000)
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
            terminal_state = job.state
            job.state, job.running = 'packaging', True
            self._record(job, 'packaging_started')
            job.future = self._executor.submit(self._rebuild_bundle, job, terminal_state)
            return self._snapshot(job)

    def _rebuild_bundle(self, job, terminal_state):
        try:
            self._make_result(job, force_archive=True)
        except Exception:
            with self._lock:
                job.packaging_error='BUNDLE_INTERRUPTED'
                self._record(job,'bundle',code=job.packaging_error)
        finally:
            with self._lock:
                # Creating a ZIP does not change conversion success/failure.
                job.state, job.running = terminal_state, False
                self._record(job, 'job_finished')
            self._remember_history(job)
            self._cleanup(job)

    @staticmethod
    def _saved_all(job):
        return bool(job.artifacts) and not job.running and not job.recovery_error and all(
            artifact.saved for artifact in job.artifacts)

    def _acknowledge_outputs(self, job, copies, *, readback_verified=False):
        # Hold an I/O lease, not the global state lock, across durable journal I/O.
        # ResultStore serializes its own records with conversion transactions.
        journal_failed = False
        if job.store:
            try:
                job.store.saved(copies, readback_verified=readback_verified)
            except OSError:
                journal_failed = True
        with self._lock:
            if journal_failed:
                job.recovery_error = 'RECOVERY_WRITE_FAILED'
            for artifact in copies:
                artifact.saved = True
                artifact.readback_verified = artifact.readback_verified or readback_verified
            for item in job.files:
                for task in item.targets:
                    if task.state in SUCCESS and task.artifacts and all(a.saved for a in task.artifacts):
                        task.state = 'saved'
                if item.state in SUCCESS | {'partial'}:
                    item.state = self._file_state(item)
            self._record(job, 'save_confirmed')
            return {'jobId': job.id, 'saved': True, 'savedAll': self._saved_all(job),
                    'incompleteRecovery': bool(job.recovery_error),
                    'jobRunning': job.running,
                    'remainingOutputs': sum(not a.saved for a in job.artifacts),
                    'remainingCompanionFiles': sum(not a.saved and a.companion for a in job.artifacts),
                    'remainingDocuments': sum(not a.saved and not a.companion for a in job.artifacts),
                    'outputs': len(copies)}

    def save_folder(self, ticket, destination, *, cancel=None, on_progress=lambda current, total: None,
                    verify=True, on_verifying=lambda: None):
        """Copy one immutable snapshot of completed outputs, including a live job.

        New outputs stay unsaved; saving never cancels or releases a queue.
        Destination paths are accepted only from the native desktop host.
        """
        from .result_store import atomic_json
        from .save_verification import verify_folder
        from .save_snapshot import snapshot_report
        if type(verify) is not bool:
            raise GuardError('SAVE_VERIFY_POLICY', 'Неверный режим проверки сохранения.', stage='save')
        # Retain the API parameter for older hosts. A legacy false value cannot
        # bypass independent on-disk verification before acknowledging a save.
        cancel = cancel or threading.Event()
        with self._lock:
            anchor = self._tickets.get(ticket)
            if anchor is None:
                raise GuardError('RESULT_NOT_FOUND', 'Результат больше недоступен.', status=404)
            job = self._get(anchor.job_id)
            if job.saving:
                raise GuardError('SAVE_BUSY', 'Результаты этого задания уже сохраняются.', stage='save', status=409)
            copies = tuple(job.artifacts)
            if not copies:
                raise GuardError('RESULT_NOT_FOUND', 'Пока нет готовых результатов.', status=404)
            report = self._report(job)
            snapshot_revision, state_at_start = job.revision, job.state
            cancel = _Cancellation(cancel, job.save_cancel)
            job.io += 1
            job.saving = True
            self._record(job, 'save_started')
        save_id = secrets.token_hex(16)
        folder, manifest = None, []
        bytes_written = 0
        verification = {'readbackVerified':False, 'verificationScope':'hashes_during_copy',
                        'outputsVerified':0}
        def save_status(state, code=None):
            return {'schema': 1, 'jobId': job.id, 'saveId': save_id,
                    'snapshotRevision': snapshot_revision, 'state': state, 'code': code,
                    'outputsExpected': len(copies), 'outputsWritten': len(manifest),
                    'bytesWritten': bytes_written, **verification}
        try:
            # A saved ticket and matching digest do not grant access to a path
            # redirected outside this job after conversion. Validate the whole
            # snapshot before creating destination files, then again per open.
            for artifact in copies:
                self._owned_artifact_path(job, artifact, stage='save')
            destination = Path(destination).resolve(strict=True)
            if self.owns_path(destination):
                raise GuardError('SAVE_OWNED_STORAGE','Выберите папку вне служебного хранилища AIO.',stage='save')
            required = sum(a.size for a in copies)
            if not destination.is_dir() or shutil.disk_usage(destination).free < required + 1024 * 1024:
                raise GuardError('OUTPUT_DISK_SPACE', 'Недостаточно места в выбранной папке.', stage='save', status=507)
            for attempt in range(100):
                name = 'AIO_Results_' + job.id[:8] + ('' if attempt == 0 else '_' + str(attempt))
                folder = destination / name
                try:
                    folder.mkdir()
                    break
                except FileExistsError:
                    continue
            else:
                folder = None
                raise GuardError('OUTPUT_COLLISION', 'Не удалось создать отдельную папку результатов.', stage='save')
            atomic_json(folder / 'AIO-save-status.json', save_status('writing'))
            for number, artifact in enumerate(copies, 1):
                if cancel.is_set():
                    raise GuardError('SAVE_CANCELLED', 'Сохранение отменено. Исходные результаты остаются в AIO.', stage='save')
                source_path = self._owned_artifact_path(job, artifact, stage='save')
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
                    with source_path.open('rb') as source, part.open('xb') as output:
                        while block := source.read(CHUNK):
                            if cancel.is_set():
                                raise GuardError('SAVE_CANCELLED', 'Сохранение отменено. Результаты остаются в AIO.', stage='save')
                            output.write(block)
                            digest.update(block)
                            written += len(block)
                        output.flush()
                        os.fsync(output.fileno())
                    if (written != artifact.size or target.exists() or
                            artifact.sha256 and digest.hexdigest() != artifact.sha256):
                        raise GuardError('SAVE_CHANGED', 'Результат изменён или имя уже занято.', stage='save')
                    try:
                        # The earlier exists() check is advisory: another
                        # process can create this name before publication.
                        # Never replace such a file. Both operations below
                        # fail atomically when the final name already exists.
                        _publish_saved_copy(part, target, cancel)
                    except FileExistsError as error:
                        raise GuardError('SAVE_CHANGED', 'Имя результата уже занято. Файл не заменён.',
                                         stage='save') from error
                finally:
                    part.unlink(missing_ok=True)
                bytes_written += written
                manifest.append({'path': artifact.archive_name, 'size': written,
                                 'sha256': digest.hexdigest(), 'fileId': artifact.file_id,
                                 'format': artifact.format, 'exportSha256':artifact.sha256,
                                 'copySha256':digest.hexdigest(), 'sourceSheet':copy.deepcopy(artifact.source_sheet)})
                try:
                    on_progress(number, len(copies))
                except Exception:
                    pass  # A detached GUI observer cannot interrupt a disk save.
            atomic_json(folder / 'AIO-conversion-report.json', snapshot_report(
                report, manifest, save_status('verifying'), job_state_at_start=state_at_start))
            atomic_json(folder / 'AIO-results-manifest.json', {
                'schema':2, 'jobId':job.id, 'saveId':save_id,
                'snapshotRevision':snapshot_revision, 'jobStateAtStart':state_at_start,
                **verification, 'outputs':manifest})
            atomic_json(folder / 'AIO-save-status.json', save_status('verifying'))
            try:
                on_verifying()
            except Exception:
                pass
            verification = verify_folder(folder, manifest, cancel=cancel, on_progress=on_progress)
            atomic_json(folder / 'AIO-results-manifest.json', {
                'schema': 2, 'jobId': job.id, 'saveId': save_id,
                'snapshotRevision': snapshot_revision, 'jobStateAtStart': state_at_start,
                **verification, 'outputs': manifest})
            if cancel.is_set():
                raise GuardError('SAVE_CANCELLED', 'Сохранение отменено. Результаты остаются в AIO.', stage='save')
            # Complete the external snapshot before acknowledging or releasing
            # any source. Every output has been independently read back from
            # its final disk path. This verifies bytes, not document fidelity.
            atomic_json(folder / 'AIO-conversion-report.json', snapshot_report(
                report, manifest, save_status('completed'), job_state_at_start=state_at_start))
            atomic_json(folder / 'AIO-save-status.json', save_status('completed'))
            outcome = self._acknowledge_outputs(job, copies, readback_verified=True)
            return {**outcome, 'saveId': save_id, 'snapshotComplete': True,
                    'snapshotRevision': snapshot_revision, 'bytesWritten': bytes_written,
                    **verification, 'name': folder.name}
        except Exception as error:
            code = error.code if isinstance(error, GuardError) else 'SAVE_STORAGE_FAILED'
            if folder is not None:
                try:
                    if any('readbackVerified' in entry for entry in manifest):
                        verified = [entry for entry in manifest if entry.get('readbackVerified') is True]
                        verification = {'readbackVerified':False, 'verificationScope':'manifest_outputs',
                            'verificationMethod':'independent_read_sha256', 'outputsVerified':len(verified),
                            'bytesVerified':sum(entry['readbackSize'] for entry in verified)}
                    atomic_json(folder / 'AIO-results-manifest.json', {
                        'schema':2, 'jobId':job.id, 'saveId':save_id,
                        'snapshotRevision':snapshot_revision, 'jobStateAtStart':state_at_start,
                        **verification, 'outputs':manifest})
                    failed_status = save_status('cancelled' if code == 'SAVE_CANCELLED' else 'failed', code)
                    atomic_json(folder / 'AIO-conversion-report.json', snapshot_report(
                        report, manifest, failed_status, job_state_at_start=state_at_start))
                    atomic_json(folder / 'AIO-save-status.json', failed_status)
                except OSError:
                    pass
            with self._lock:
                self._record(job, 'save_failed', code=code)
            if isinstance(error, OSError):
                raise GuardError('SAVE_STORAGE_FAILED',
                    'Не удалось сохранить все файлы. Результаты остаются в AIO.',
                    stage='save', status=507) from error
            raise
        finally:
            # Keep the existing save I/O lease until its metadata snapshot is
            # persisted; close() must not race this final history write.
            self._remember_history(job)
            with self._lock:
                job.saving = False
                job.io -= 1
                self._record(job, 'save_idle')
                self._io_drained.notify_all()
            self._cleanup(job)

    def verify_saved_download(self, ticket, destination, *, cancel=None):
        """Verify final bytes and publish an immutable, no-clobber receipt."""
        from .save_verification import sha256_stream, readback_fingerprint
        from .result_store import atomic_json
        cancel = cancel or threading.Event()
        destination = Path(destination)
        if self.owns_path(destination):
            raise GuardError('SAVE_OWNED_STORAGE', 'Выберите папку вне хранилища AIO.', stage='save')
        with self._lock:
            retained = self._tickets.get(ticket) if isinstance(ticket, str) else None
            if retained is None:
                raise GuardError('RESULT_NOT_FOUND', 'Результат больше недоступен.', status=404)
            job = self._get(retained.job_id)
            if job.saving:
                raise GuardError('SAVE_BUSY', 'Результаты этого задания уже сохраняются.', stage='save', status=409)
            job.saving = True
            job.io += 1
            retained.save_receipt = None
            retained.readback_verified = False
            cancel = _Cancellation(cancel, job.save_cancel)
            self._record(job, 'save_started')
        save_id = secrets.token_hex(16)
        receipt = {'schema': 1, 'jobId': job.id, 'saveId': save_id, 'state': 'verifying',
                   'readbackVerified': False, 'scope': 'final_destination_copy', 'qualityCompared': False}
        is_bundle = retained is job.bundle

        def publish(value):
            # Never replace an earlier receipt, arbitrary file or concurrent writer.
            for attempt in range(32):
                suffix = ('.AIO-save-status.json' if attempt == 0 else
                          '.AIO-save-status-' + save_id + '-' + str(attempt) + '.json')
                name = destination.name + suffix
                if len(name.encode('utf-8')) > 240:
                    name = 'AIO-save-status-' + save_id + '-' + str(attempt) + '.json'
                path = destination.with_name(name)
                value['receiptFile'] = name
                try:
                    atomic_json(path, value, overwrite=False)
                    return path
                except FileExistsError:
                    continue
            raise OSError('Could not reserve a new save receipt name')

        try:
            if is_bundle:
                if retained.archive_expected is None:
                    raise GuardError('SAVE_VERIFY_MISSING', 'Нет описания состава ZIP. Результаты остаются в AIO.', stage='save')
                manifest, metadata, snapshot = retained.archive_expected
                receipt.update(archiveSnapshot=copy.deepcopy(snapshot), outputsExpected=len(manifest),
                               outputsWritten=len(manifest), bytesWritten=retained.size)
            with self.open_result(ticket) as (stream, _name, _size):
                expected = sha256_stream(stream, cancel=cancel)
                if retained.sha256 and expected != (retained.size, retained.sha256):
                    raise GuardError('SAVE_CHANGED', 'Байты результата изменились до скачивания.', stage='save')
                if is_bundle:
                    from .archive_verification import verify_built_archive
                    try:
                        size, digest, check = verify_built_archive(destination, manifest, metadata, snapshot, cancel=cancel)
                    except GuardError as error:
                        raise GuardError('SAVE_CANCELLED' if error.code == 'BUNDLE_CANCELLED' else 'SAVE_VERIFY_FAILED',
                            'Проверка конечного ZIP не завершена. Результаты остаются в AIO.', stage='save') from error
                    verification = {**check, 'verificationScope': 'final_destination_zip_members', 'externalSaveVerified': True}
                else:
                    size, digest, unchanged = readback_fingerprint(destination, cancel=cancel)
                    if not unchanged:
                        raise GuardError('SAVE_VERIFY_FAILED', 'Сохранённый файл изменился при проверке.', stage='save')
                    verification = {'verificationScope': 'download_bytes',
                                    'verificationMethod': 'independent_read_sha256', 'bytesVerified': size}
                if expected != (size, digest):
                    raise GuardError('SAVE_VERIFY_FAILED', 'Сохранённые байты отличаются от результата Guard.', stage='save')
                if cancel.is_set():
                    raise GuardError('SAVE_CANCELLED', 'Проверка сохранения отменена.', stage='save')
                receipt.update(verification, state='completed', readbackVerified=True,
                               destinationSha256=digest, destinationSize=size)
                if is_bundle:
                    receipt_path = publish(receipt)
                    raw = json.dumps(receipt, ensure_ascii=True, separators=(',', ':')).encode('utf-8')
                    seen_size, seen_digest, stable = readback_fingerprint(receipt_path, cancel=cancel)
                    if not stable or (seen_size, seen_digest) != (len(raw), hashlib.sha256(raw).hexdigest()):
                        raise GuardError('SAVE_VERIFY_FAILED', 'Не удалось проверить квитанцию сохранения ZIP.', stage='save')
                if cancel.is_set():
                    raise GuardError('SAVE_CANCELLED', 'Проверка сохранения отменена.', stage='save')
                with self._lock:
                    retained.readback_verified = True
                    retained.save_receipt = receipt
                return receipt
        except Exception as error:
            if is_bundle:
                try:
                    publish({**receipt, 'state': 'cancelled' if
                        isinstance(error, GuardError) and error.code == 'SAVE_CANCELLED' else 'failed',
                        'readbackVerified': False, 'externalSaveVerified': False,
                        'code': error.code if isinstance(error, GuardError) else 'SAVE_STORAGE_FAILED'})
                except OSError:
                    pass
            if isinstance(error, OSError):
                raise GuardError('SAVE_STORAGE_FAILED', 'Не удалось записать квитанцию. Результаты остаются в AIO.', stage='save') from error
            raise
        finally:
            with self._lock:
                job.saving = False
                job.io -= 1
                self._record(job, 'save_idle')
                self._io_drained.notify_all()
            self._cleanup(job)

    def owns_path(self, path):
        """Do not confirm a save into a directory our cleanup will remove."""
        resolved=Path(path).resolve()
        # Production has a fixed private recovery parent. This fast path does
        # no state-lock acquisition on Qt's GUI thread during live saves.
        if self._parent is not None:
            return resolved.is_relative_to(self._parent)
        with self._lock:
            roots=[j.root for j in self._jobs.values()]
            if self._parent:
                roots.append(self._parent)
        return any(resolved.is_relative_to(root.resolve()) for root in roots)

    def _snapshot(self, job, include_trace=False, only_files=None, result_delta=False):
        from .evidence_status import task_evidence
        rows = []
        # Delta readers merge by immutable file ID. Do not walk all admitted
        # inputs to find one changed row in a long-running batch.
        selected = (job.files if only_files is None else
                    [job.files_by_id[identity] for identity in only_files
                     if identity in job.files_by_id])
        for item in selected:
            targets = [{'taskId': job.id + ':' + str(t.index), 'format': t.format, 'state': t.state, 'code': t.code, 'error': t.error,
                        'stage': t.stage, 'libreOfficeStarted': t.lo_started,
                        **task_evidence(t, quality_requested=job.quality_check),
                        'executionMode':t.execution_mode,
                        'executionReason':t.execution_reason, 'resultChecks':copy.deepcopy(t.result_checks),
                        'automaticRecipe':t.automatic_recipe,
                        'automaticParameters':copy.deepcopy(t.automatic_parameters),
                        'nativeTimings':clean_timings(t.native_timings),
                        'exportFilter':t.export_filter, 'warnings':warning_details(t.warnings),
                        'quality':copy.deepcopy(t.quality) if t.quality else None,
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
        available = [a for a in job.artifacts if not a.availability_error]
        result = copy.deepcopy(job.result)
        if result is not None:
            anchor = self._tickets.get(result.get('ticket'))
            if anchor is not None and anchor.availability_error:
                result = ({**available[0].metadata(), 'jobId': job.id, 'folderOnly': True}
                          if available else None)
        outputs_delta = bool(result_delta and only_files is not None and result is not None)
        if result is not None:
            # This negotiated wire representation avoids repeatedly allocating
            # and serializing every retained output during packaging/status
            # changes. Legacy clients and full/resync snapshots still receive
            # the complete inventory. Artifact tickets survive ready -> saved.
            artifacts = (job.artifacts if not outputs_delta else
                         (a for item in selected for task in item.targets for a in task.artifacts))
            result['outputs'] = [a.metadata() for a in artifacts if not a.availability_error]
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
        snapshot = {'schemaVersion': 3, 'jobId': job.id, 'revision': job.revision,
                'state': self._availability_state(job), 'conversionState': job.state, 'finished': finished,
                'unavailableOutputs': sum(bool(a.availability_error) for a in job.artifacts),
                'diagnosticBytes':job.diagnostic_bytes,
                'unsavedCompanionFiles':sum(not a.saved and not a.availability_error and a.companion for a in job.artifacts),
                'unsavedDocuments':sum(not a.saved and not a.availability_error and not a.companion for a in job.artifacts),
                'currentTask': current, 'pauseRequested': job.paused,
                'fileTimeoutSeconds': job.file_timeout, 'officeStarts': job.office_starts,
                'recovered': job.recovered, 'salvaged': job.salvage, 'recoveryError': job.recovery_error,
                'recoveryDurable': bool(job.store and not job.recovery_error),
                'resultAvailability': ('unavailable' if job.artifacts and not available and
                                        (job.bundle is None or job.bundle.availability_error) else
                                       'partial_unavailable' if any(a.availability_error for a in job.artifacts) else
                                       'recovered_unverified' if job.salvage else
                                       'ready_volatile' if job.artifacts and job.recovery_error else
                                       'ready_retained' if job.artifacts else 'none'),
                'qualityCheckRequested':job.quality_check,
                'qualityCompared': job.quality_compared,
                'retainSources':job.retain_sources,
                'comparisonAvailable':False,
                'comparisonParentJobId':job.comparison_parent_job_id,
                'comparisonRecipe':job.comparison_recipe,
                'timings':dict(job.timings),
                'files': rows, **counts, 'preflightChecks':job.preflight_checks,
                'preflightTotal':len(job.files),
                'preflightTimeoutSeconds':job.preflight_timeout_seconds,
                'preflightCompleted':sum(f.state == 'prepared' for f in job.files),
                'preflightPhase': job.preflight_phase,
                'preflightMatrixCompleted': job.preflight_matrix_completed,
                'preflightMatrixTotal': job.preflight_matrix_total,
                'cancellationRequested': job.cancel.is_set(),
                'preparation':job.preparation,
                'preflightUndetermined':sum(not f.native_route or f.native_route.get('state')!='detected' for f in job.files),
                'percent': counts['completed'] * 100 / counts['total'] if counts['total'] else None, 'result': result,
                'trace': job.trace.snapshot() if include_trace else None,
                'archiveMode': job.archive_mode, 'bundleDeferred': job.bundle_deferred,
                'packagingError': job.packaging_error, 'saveInProgress': job.saving,
                'partialResult': ({**available[0].metadata(), 'jobId': job.id,
                    'outputsCount': len(available), 'folderOnly': True}
                    if available else None)}
        if outputs_delta:
            snapshot['resultOutputsDelta'] = True
        return snapshot

    def _reconcile_results(self, job_id):
        """Explicit metadata reconciliation; never ordinary delta polling."""
        from ..file_identity import file_identity
        with self._lock:
            job = self._get(job_id)
            if job.running or job.saving:
                return
            artifacts = list(job.artifacts)
            if job.bundle is not None and all(job.bundle is not a for a in artifacts):
                artifacts.append(job.bundle)
            job.io += 1
        try:
            observations = []
            for artifact in artifacts:
                path = self._owned_file_path(job, artifact.path, artifact.size)
                identity = file_identity(path) if path is not None else None
                code = (None if identity is not None and identity == artifact.file_identity else 'RESULT_UNAVAILABLE')
                observations.append((artifact, code))
            with self._lock:
                current = {id(a) for a in job.artifacts}
                if job.bundle is not None:
                    current.add(id(job.bundle))
                changed = False
                for artifact, code in observations:
                    if id(artifact) in current and artifact.availability_error != code:
                        artifact.availability_error = code
                        changed = True
                if changed and not job.released:
                    self._record(job, 'results_reconciled', file_id='*')
        finally:
            with self._lock:
                job.io -= 1
                self._io_drained.notify_all()
            self._cleanup(job)

    @staticmethod
    def _availability_state(job):
        if job.running or not any(a.availability_error and not a.saved for a in job.artifacts):
            return job.state
        return ('partial' if any(not a.availability_error or a.saved for a in job.artifacts)
                or job.bundle is not None and not job.bundle.availability_error else 'failed')

    def status(self, job_id, *, include_trace=False, since_revision=None, delta=False,
               result_delta=False, recover=True):
        if type(recover) is not bool:
            raise GuardError('RECOVERY_REQUEST', 'Неверный режим поиска задания.')
        if recover and job_id not in self._jobs:
            self._load_recovery()
        if since_revision is not None and (type(since_revision) is not int or since_revision < 0):
            raise GuardError('STATUS_REVISION', 'Некорректная версия состояния.')
        if type(result_delta) is not bool:
            raise GuardError('STATUS_RESULT_DELTA', 'Некорректный режим состояния результатов.')
        self._prune(defer_cleanup=not recover)
        # lookup=current suppresses old-job discovery, not verification of
        # a known job on an explicit full refresh. ETag/delta polling stays cheap.
        if since_revision is None:
            self._reconcile_results(job_id)
        with self._lock:
            job = self._get(job_id)
            if not include_trace and since_revision == job.revision:
                return {'jobId': job.id, 'revision': job.revision, 'unchanged': True}
            if (delta and not include_trace and since_revision is not None and job.changes
                    and job.changes[0][0] <= since_revision + 1 <= job.revision):
                changes = {file_id for revision, file_id in job.changes if revision > since_revision}
                if '*' not in changes:
                    return {**self._snapshot(job, only_files=changes, result_delta=result_delta), 'delta': True,
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

    @staticmethod
    def _owned_file_path(job, path, size):
        """Resolve only a regular, existing file under this job's owned tree."""
        try:
            path = Path(path)
            if not path.resolve(strict=True).is_relative_to(job.root.resolve()):
                return None
            for part in (path, *path.parents):
                if part == job.root.parent:
                    break
                if path_is_link(part):
                    return None
            return path if path.is_file() and path.stat().st_size == size else None
        except (OSError, ValueError):
            return None

    def _owned_artifact_path(self, job, artifact, *, stage):
        """Validate identity at every read and invalidate metadata on failure."""
        from ..file_identity import file_identity
        path = self._owned_file_path(job, artifact.path, artifact.size)
        identity = file_identity(path) if path is not None else None
        if path is None or (artifact.file_identity is not None and identity != artifact.file_identity):
            with self._lock:
                if not artifact.availability_error:
                    artifact.availability_error = 'RESULT_UNAVAILABLE'
                    self._record(job, 'results_reconciled', file_id='*')
            raise GuardError('SAVE_CHANGED',
                'Результат отсутствует, изменён или находится вне служебного хранилища AIO.', stage=stage, status=409)
        return path

    @contextmanager
    def retained_inputs(self, job_id):
        """Lease completed originals/results for an explicit analysis only.

        Paths are generated from owned records, never accepted from clients.
        The lease prevents discard/reaping from deleting an active inspection;
        a missing old original remains a reportable unavailable item.
        """
        if job_id not in self._jobs:
            self._load_recovery()
        with self._lock:
            job = self._get(job_id)
            if job.running or job.state not in TERMINAL:
                raise GuardError('COMPARISON_BUSY', 'Дождитесь завершения конвертации.', status=409)
            job.io += 1
            items = tuple(job.files)
        try:
            records = []
            for item in items:
                source = (self._owned_file_path(job, item.source, item.size)
                          if not item.source_released else None)
                resources = []
                for name, metadata in item.resources.items():
                    path = self._owned_file_path(job, item.source.parent/name, metadata['size'])
                    if path is None or not metadata.get('ready'):
                        source = None
                    resources.append(MappingProxyType({'path':name, 'size':metadata['size'],
                        'source':path, 'sha256':metadata.get('digest')}))
                for task in item.targets:
                    # Artifact paths were owned when the export was retained,
                    # but a parent directory can since become a symlink or a
                    # Windows junction. Revalidate outputs just like originals
                    # before an analyzer or preview receives filesystem paths.
                    output_paths = tuple(self._owned_file_path(job, artifact.path, artifact.size)
                                         for artifact in task.artifacts)
                    paths = tuple(path for path in output_paths if path is not None)
                    records.append(MappingProxyType({
                        'taskId':job.id+':'+str(task.index), 'fileId':item.id,
                        'name':item.name, 'format':task.format, 'state':task.state,
                        'source':source, 'sourceAvailable':source is not None,
                        'sourceSha256':item.content_digest, 'size':item.size,
                        'paths':paths, 'outputPaths':output_paths,
                        'tickets':tuple(a.ticket for a in task.artifacts),
                        'outputs':tuple(a.metadata() for a in task.artifacts),
                        'warnings':tuple(task.warnings), 'code':task.code,
                        'error':task.error, 'exportFilter':task.export_filter,
                        'automaticRecipe':task.automatic_recipe,
                        'automaticParameters':copy.deepcopy(task.automatic_parameters),
                        'nativeRoute':copy.deepcopy(item.native_route),
                        'resultChecks':copy.deepcopy(task.result_checks),
                        'quality':copy.deepcopy(task.quality), 'resources':tuple(resources)}))
            yield tuple(records)
        finally:
            with self._lock:
                job.io -= 1
                self._io_drained.notify_all()
            self._cleanup(job)


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
        # Queued targets have no running native operation to interrupt.
        # The current worker can persist their cancellation in one batch.
        if job.state != 'uploading':
            for item in job.files:
                for task in item.targets:
                    if task.state == 'queued':
                        self._fail(job, item, task, GuardError('CANCELLED', 'Конвертация отменена.'))
        if job.state == 'uploading':
            for item in job.files:
                for task in item.targets:
                    if task.state not in DONE:
                        self._fail(job, item, task, GuardError('CANCELLED', 'Загрузка отменена.', stage='upload'))
                item.state = 'cancelled'
            job.state = 'cancelled'
        return ([f.interrupt for f in job.files if f.interrupt is not None] +
                [callback for f in job.files for callback in f.resource_interrupts.values()])

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
            if not job.running and job.state in TERMINAL:
                # A delayed UI/analysis cancellation must not change the
                # durable outcome or poison a completed candidate's event.
                return self._snapshot(job)
            if job.state == 'packaging':
                job.bundle_cancel.set()
            callbacks = self._stop(job)
            self._record(job, 'cancel_requested')
            snapshot = self._snapshot(job)
        self._interrupt(callbacks)
        if not job.running:
            self._flush_journal(job)
        return snapshot

    def cancel_file(self, job_id, file_id, *, expected_task_id=None):
        """Atomically skip only the task the user actually saw."""
        with self._lock:
            job = self._get(job_id)
            item = job.files_by_id.get(file_id) if isinstance(file_id, str) else None
            if item is None:
                raise GuardError('FILE_NOT_FOUND', 'Файл не относится к заданию.', status=404)
            if expected_task_id is not None:
                current = job.current
                task_id = None
                if current:
                    current_item, current_task = current
                    task_id = (job.id + ':preflight:' + current_item.id
                               if current_task.stage == 'preflight'
                               else job.id + ':' + str(current_task.index))
                if (not isinstance(expected_task_id, str) or
                        not current or current[0] is not item or task_id != expected_task_id):
                    raise GuardError('STALE_CURRENT_TASK',
                                     'Очередь уже перешла к другой операции. Обновите состояние.',
                                     stage='queue', status=409)
            if not job.preparation and all(t.state in DONE for t in item.targets):
                return self._snapshot(job)
            item.cancel.set()
            for task in item.targets:
                if task.state == 'queued':
                    self._fail(job, item, task, GuardError('CANCELLED', 'Файл пропущен.'))
            if item.state in {'awaiting_upload', 'queued'}:
                item.state = 'cancelled'
            callbacks = [item.interrupt] if item.interrupt is not None else []
            callbacks.extend(item.resource_interrupts.values())
            self._record(job, 'file_cancel_requested', file_id=item.id)
            snapshot = self._snapshot(job)
        self._interrupt(callbacks)
        if not job.running:
            self._flush_journal(job)
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
            path = self._owned_artifact_path(job, artifact, stage='download')
            with path.open('rb') as stream:
                yield stream, artifact.name, artifact.size
        finally:
            with self._lock:
                job.io -= 1
                self._io_drained.notify_all()
            self._cleanup(job)

    def confirm_saved(self, job_id, ticket, *, compact=False, save_id=None):
        """Host-confirmed save, with an optional bounded acknowledgement."""
        if type(compact) is not bool:
            raise GuardError('SAVE_REQUEST', 'Некорректный режим подтверждения сохранения.')
        with self._lock:
            job = self._get(job_id)
            artifact = self._tickets.get(ticket) if isinstance(ticket, str) else None
            if artifact is None or artifact.job_id != job.id:
                raise GuardError('RESULT_NOT_FOUND', 'Результат не относится к заданию.', status=404)
            if job.saving:
                raise GuardError('SAVE_BUSY', 'Результаты этого задания уже сохраняются.', stage='save', status=409)
            receipt = artifact.save_receipt or {}
            if (receipt.get('state') != 'completed' or receipt.get('readbackVerified') is not True or
                    save_id is not None and save_id != receipt.get('saveId')):
                raise GuardError('SAVE_VERIFY_MISSING',
                    'Нет подтверждения проверки конечной копии. Результаты остаются в AIO.', stage='save', status=409)
            if artifact is job.bundle:
                # Acknowledgement belongs to the immutable ZIP inventory,
                # never to outputs that appeared after it was packaged.
                copies = artifact.bundle_outputs
                check = artifact.archive_verification or {}
                if (not copies or check.get('status') != 'byte_verified' or
                        check.get('outputsVerified') != len(copies)):
                    raise GuardError('BUNDLE_VERIFY_FAILED',
                        'Нет подтверждения состава архива. Результаты остаются в AIO.',
                        stage='save')
            else:
                copies = (artifact,)
            job.saving = True
            job.io += 1
            self._record(job, 'save_started')
        try:
            outcome = self._acknowledge_outputs(job, copies, readback_verified=True)
            with self._lock:
                artifact.saved = True
                return {**outcome, 'saveId':receipt['saveId'], 'readbackVerified':True} if compact else self._snapshot(job)
        finally:
            # Keep the existing save I/O lease until its metadata snapshot is
            # persisted; close() must not race this final history write.
            self._remember_history(job)
            with self._lock:
                job.saving = False
                job.io -= 1
                self._record(job, 'save_idle')
                self._io_drained.notify_all()
            self._cleanup(job)

    def close_conversion(self, job_id, *, discard=False, defer_cleanup=False):
        if type(discard) is not bool or type(defer_cleanup) is not bool:
            raise GuardError('DISCARD_REQUEST','Некорректное подтверждение закрытия.')
        with self._lock:
            job=self._get(job_id)
            if job.saving or job.diagnostic_building:
                raise GuardError('SAVE_BUSY','Дождитесь завершения сохранения.',status=409)
            unsaved=sum(not artifact.saved for artifact in job.artifacts)
            active=bool(job.running or job.state not in TERMINAL)
            recovery_warning=bool(job.recovery_error)
            # A zero-output running queue still has valuable unfinished work.
            # An incomplete recovery ledger may have unindexed files, even
            # when every currently indexed artifact was saved.
            if (unsaved or active or recovery_warning) and not discard:
                return {'jobId':job.id,'closed':False,'confirmationRequired':True,
                        'unsavedOutputs':unsaved,'activeConversion':active,
                        'recoveryWarning':recovery_warning}
            # Commit ownership/cancellation atomically with the save check.
            # Slow worker interruption and filesystem cleanup run after unlock.
            job.bundle_cancel.set();job.save_cancel.set();job.released=True
            job.cleanup_background = job.cleanup_background or defer_cleanup
            callbacks=self._stop(job)
            for artifact in job.artifacts + job.diagnostic_artifacts + ([job.bundle] if job.bundle else []):
                self._tickets.pop(artifact.ticket,None)
            outcome={'jobId':job.id,'closed':True,'confirmationRequired':False,
                     'cancellationRequested':job.cancel.is_set(),'cleanupPending':job.running or job.io>0}
        self._interrupt(callbacks)
        if not job.running:self._flush_journal(job)
        self._cleanup(job)
        with self._lock:
            outcome['cleanupPending'] = job.id in self._jobs
        return outcome

    def release(self, job_id, *, discard=False, defer_cleanup=False):
        if type(discard) is not bool or type(defer_cleanup) is not bool:
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
                job.cleanup_background = job.cleanup_background or defer_cleanup
                callbacks = self._stop(job)
                for artifact in job.artifacts + job.diagnostic_artifacts + ([job.bundle] if job.bundle else []):
                    self._tickets.pop(artifact.ticket, None)
        self._interrupt(callbacks)
        if not job.running:
            self._flush_journal(job)
        if not preserve:
            self._cleanup(job)
        with self._lock:
            return {'jobId': job.id, 'released': not preserve, 'retained': preserve,
                    'cleanupPending': not preserve and job.id in self._jobs}

    def _cleanup(self, job):
        with self._lock:
            if job.running or job.io or job.pending_journal or job.cleaning:
                return
            if job.cleanup_background and not self._closed and time.monotonic() < job.cleanup_retry_at:
                return
            # Shutdown may time out while an HTTP/diagnostic reader still
            # holds an I/O lease. Keep both the OS ownership lock and quota
            # accounting until that last lease calls cleanup. Unsaved bytes
            # then detach for recovery; explicit discard still means delete.
            preserve = bool(self._closed and self._recovery and not job.released
                and (job.recovery_error or any(not a.saved for a in job.artifacts)))
            if not job.released:
                if not (self._closed and self._recovery):
                    return
                if not preserve:
                    job.released = True
            job.cleaning = True
            if job.cleanup_background and not self._closed:
                try:
                    # Submit under the state lock so close() cannot shut this
                    # executor down between claiming and submitting the job.
                    self._cleanup_executor.submit(self._cleanup_owned, job, preserve)
                except RuntimeError:
                    self._cleanup_deferred(job)
                return
        self._cleanup_owned(job, preserve)

    def _cleanup_deferred(self, job):
        with self._lock:
            job.cleaning = False
            job.cleanup_retry_at = time.monotonic() + 2.0
            self._record(job, 'cleanup_deferred', code='CLEANUP_RETRY')

    def _cleanup_owned(self, job, preserve):
        # Potentially slow filesystem deletion never holds the global job lock.
        try:
            if job.store and not preserve:
                # Commit the admission barrier while ownership is still held.
                # The parent-side marker survives every partial rmtree order.
                job.store.retire()
                job.store.cleanup_retired()
            elif job.store:
                job.store.close()
            elif not preserve:
                shutil.rmtree(job.root)
        except FileNotFoundError:
            # A disappearing CHILD is not proof that the owned tree is gone.
            # Keep the retirement barrier, ownership and quota until retried.
            if os.path.lexists(job.root):
                self._cleanup_deferred(job)
                return
        except Exception:
            self._cleanup_deferred(job)
            return
        with self._lock:
            for artifact in job.artifacts + job.diagnostic_artifacts + ([job.bundle] if job.bundle else []):
                self._tickets.pop(artifact.ticket, None)
            if self._jobs.pop(job.id, None) is not None:
                self._reserved -= job.reserved
                self._recovery_issues[:] = [w for w in self._recovery_issues if w.get('folder')!=job.root.name]

    def close(self):
        with self._lock:
            self._closed = True
            self._recovery_stop.set()
            callbacks = []
            for job in self._jobs.values():
                job.bundle_cancel.set()
                job.save_cancel.set()
                job.released = job.released or not bool(self._recovery)
                callbacks.extend(self._stop(job))
            self._tickets.clear()
            self._proof_cache.clear()
        self._interrupt(callbacks)
        self._recovery_executor.shutdown(wait=True)
        self._executor.shutdown(wait=True)
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            self._flush_journal(job)
        with self._io_drained:
            self._io_drained.wait_for(lambda: all(j.io == 0 for j in self._jobs.values()), timeout=5)
        self._prune()
        if self._recovery:
            with self._lock:
                jobs = list(self._jobs.values())
            for job in jobs:
                self._cleanup(job)
            # Leased jobs deliberately remain registered until their final
            # caller exits. Clearing them here would unlock a live recovery
            # tree and lose accounting after the bounded shutdown wait.
        self._cleanup_executor.shutdown(wait=True)

    def storage_stats(self):
        with self._lock:
            jobs = [job for job in self._jobs.values() if not job.released]
            return {'jobs': len(self._jobs), 'maxJobs': self.limits.max_jobs,
                    'reservedBytes': self._reserved, 'tickets': len(self._tickets),
                    'availableSlots': max(0, self.limits.max_jobs-len(self._jobs)),
                    'pendingRecoveryFolders': len(self._recovery_pending),
                    'reclaimableSavedJobs': sum(self._saved_all(j) and not j.io for j in jobs),
                    'cleanupPending': sum(j.released for j in self._jobs.values()),
                    'cleanupRetryJobs': sum(j.released and j.cleanup_retry_at > 0 and not j.cleaning
                                            for j in self._jobs.values()),
                    'running': sum(j.state in {'checking', 'running', 'packaging'} for j in jobs),
                    'queued': sum(j.state == 'queued' for j in jobs),
                    'retainedJobs': sum(bool(j.artifacts) for j in jobs),
                    'unsavedJobs': sum(any(not a.saved for a in j.artifacts) for j in jobs),
                    'unsavedOutputs': sum(not a.saved for j in jobs for a in j.artifacts),
                'unsavedCompanionFiles': sum(not a.saved and a.companion for j in jobs for a in j.artifacts),
                'unsavedDocuments': sum(not a.saved and not a.companion for j in jobs for a in j.artifacts),
                    'unsavedBytes': sum(a.size for j in jobs for a in j.artifacts if not a.saved),
                    'recoveryWriteFailures': sum(bool(j.recovery_error) for j in jobs)}
