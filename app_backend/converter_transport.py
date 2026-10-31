"""Disk ownership and transport for direct LO requests, without conversion policy."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import re
from pathlib import Path
import secrets
import tempfile
import threading
import time
from zipfile import ZipFile, ZipInfo, ZIP_STORED

from .libreoffice_catalog import mime_for
from .libreoffice_engine import LibreOfficeError, convert_path
from .legacy_result_journal import LegacyWorkspace, ROOT_NAME, recover_workspaces, salvage_unverified_workspace
from .converter_guard.result_store import path_is_link
from .legacy_storage_budget import LegacyRunControl, measured_bytes, storage_error

ARCHIVE_CHUNK = 128 * 1024
MAX_OWNED_ROOTS = 32


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
    restored: bool = False
    salvaged: bool = False


class ConverterTransport:
    def __init__(self, *, max_owned_roots=MAX_OWNED_ROOTS, recovery_parent=None,
                 max_owned_bytes=None, lazy_recovery=False):
        if type(max_owned_roots) is not int or not 1 <= max_owned_roots <= 256:
            raise ValueError('max_owned_roots must be an integer from 1 to 256')
        if max_owned_bytes is not None and (type(max_owned_bytes) is not int or max_owned_bytes <= 0):
            raise ValueError('max_owned_bytes must be None or a positive integer')
        self.lock = threading.RLock()
        self.active = {}
        self.results = {}
        self.results_by_request = {}
        self.cleanup_pending = []
        self._pending_cleaning = set()
        self.max_owned_roots = max_owned_roots
        self.max_owned_bytes = max_owned_bytes
        self.used_bytes = None
        self.storage_measurement_ok = None
        self.closed = False
        self._cleanup_timer = None
        self._cleanup_retries = 0
        self.recovery_parent = Path(recovery_parent) if recovery_parent is not None else None
        self.recovery_issues = []
        self.recovery_available = True
        self._recovery_scan_lock = threading.Lock()
        self._recovery_loaded = self.recovery_parent is None
        # The desktop opens immediately, without hashing historical files.
        # The explicit Legacy Recovery panel performs the first disk scan.
        # Direct transport compatibility tests may still request eager recovery.
        if self.recovery_parent is not None and not lazy_recovery:
            self.scan_recovery()

    def scan_recovery(self):
        """Explicit idempotent rescan; never release or overwrite existing bytes."""
        if self.recovery_parent is None:
            return {'state': 'disabled', 'warnings': []}
        with self._recovery_scan_lock:
            with self.lock:
                if self.closed:
                    raise LibreOfficeError('Программа закрывается.', 'CANCELLED')
                claimed = {Path(result.temporary.name).name for result in self.results.values()
                           if isinstance(result.temporary, LegacyWorkspace)}
                claimed.update(Path(request.temporary.name).name for request in self.active.values()
                               if isinstance(request.temporary, LegacyWorkspace))
                claimed.update(Path(value.name).name for value in self.cleanup_pending
                               if isinstance(value, LegacyWorkspace))
            try:
                recovered, issues = recover_workspaces(self.recovery_parent, skip_roots=claimed)
            except OSError:
                with self.lock:
                    self.recovery_issues = [{'workspaceId': 'legacy',
                                             'code': 'LEGACY_RECOVERY_UNAVAILABLE'}]
                    self.recovery_available = False
                    self._recovery_loaded = True
                return {'state': 'unavailable', 'warnings': list(self.recovery_issues)}
            pending, duplicates = {}, set()
            with self.lock:
                for workspace, value in recovered:
                    identity = value['requestId']
                    if identity in self.active or identity in self.results_by_request or identity in duplicates:
                        workspace.close()
                        issues.append({'workspaceId': Path(workspace.name).name,
                                       'code': 'LEGACY_RECOVERY_DUPLICATE_ID'})
                    elif identity in pending:
                        previous = pending.pop(identity)[0]
                        previous.close()
                        issues.extend(({'workspaceId': Path(previous.name).name,
                                        'code': 'LEGACY_RECOVERY_DUPLICATE_ID'},
                                       {'workspaceId': Path(workspace.name).name,
                                        'code': 'LEGACY_RECOVERY_DUPLICATE_ID'}))
                        workspace.close()
                        duplicates.add(identity)
                    else:
                        pending[identity] = (workspace, value)
                for identity, (workspace, value) in pending.items():
                    ticket = secrets.token_urlsafe(32)
                    self.results[ticket] = Result(workspace, value['path'], value['name'], value['mime'],
                        request_id=identity, size=value['size'], target_report=value['targetReport'],
                        format_ids=tuple(value['formatIds']), restored=True)
                    self.results_by_request[identity] = ticket
                self.recovery_issues = issues
                self.recovery_available = True
                self._recovery_loaded = True
                return {'state': 'completed', 'restored': len(pending),
                        'warnings': [dict(item) for item in issues]}

    def decline_recovery(self, *, request_id=None, workspace_id=None):
        """Explicit confirmed refusal: retire only one previous legacy workspace.

        A durable retirement barrier is written before a refusal is acknowledged.
        If Windows cannot delete files yet, that barrier prevents the same
        results being offered again after a restart. M04 history is untouched.
        This method is never called automatically or for current conversions.
        """
        if (request_id is None) == (workspace_id is None):
            raise LibreOfficeError('Укажите ровно один прежний результат.',
                                   'LEGACY_DECLINE_INVALID')
        if self.recovery_parent is None:
            raise LibreOfficeError('Восстановление прежних результатов отключено.',
                                   'LEGACY_DECLINE_INVALID')
        if not self._recovery_loaded:
            self.scan_recovery()
        with self._recovery_scan_lock:
            with self.lock:
                if self.closed:
                    raise LibreOfficeError('Программа закрывается.', 'CANCELLED')
                if request_id is not None:
                    if not isinstance(request_id, str) or not re.fullmatch(r'[a-f0-9]{32}', request_id):
                        raise LibreOfficeError('Некорректный идентификатор задания.',
                                               'LEGACY_DECLINE_INVALID')
                    ticket = self.results_by_request.get(request_id)
                    result = self.results.get(ticket)
                    if (result is None or result.released or not result.restored or
                            not isinstance(result.temporary, LegacyWorkspace)):
                        raise LibreOfficeError('Прежний результат не найден.',
                                               'LEGACY_DECLINE_INVALID')
                    if result.leases or result.cleaning:
                        raise LibreOfficeError('Результат сейчас сохраняется. Повторите отказ позже.',
                                               'LEGACY_DECLINE_BUSY')
                    workspace = result.temporary
                else:
                    if not isinstance(workspace_id, str) or not ROOT_NAME.fullmatch(workspace_id):
                        raise LibreOfficeError('Некорректный идентификатор каталога.',
                                               'LEGACY_DECLINE_INVALID')
                    # Only an explicit warning from a completed manual scan is
                    # eligible. Never retire an unrelated user-created directory.
                    if not any(item.get('workspaceId') == workspace_id for item in self.recovery_issues):
                        raise LibreOfficeError('Каталог не найден в списке прежних результатов.',
                                               'LEGACY_DECLINE_INVALID')
                    if any(Path(item.temporary.name).name == workspace_id
                           for item in self.results.values() if isinstance(item.temporary, LegacyWorkspace)):
                        raise LibreOfficeError('Каталог уже доступен как результат.',
                                               'LEGACY_DECLINE_INVALID')
                    if any(Path(item.temporary.name).name == workspace_id
                           for item in self.active.values() if isinstance(item.temporary, LegacyWorkspace)):
                        raise LibreOfficeError('Каталог используется конвертацией.',
                                               'LEGACY_DECLINE_BUSY')
                    root = self.recovery_parent / workspace_id
                    workspace = None
            if workspace_id is not None:
                try:
                    if path_is_link(root) or not root.is_dir():
                        raise ValueError('Invalid legacy storage root')
                    workspace = LegacyWorkspace(root)
                except (BlockingIOError, OSError):
                    raise LibreOfficeError('Каталог занят другим экземпляром AIO. '
                                           'Закройте его и повторите отказ.',
                                           'LEGACY_DECLINE_BUSY') from None
                except ValueError:
                    raise LibreOfficeError('Каталог изменился. Ничего не удалено.',
                                           'LEGACY_DECLINE_INVALID') from None
            try:
                # The marker is fsynced before returning "declined". Failure
                # to write it means refusal is NOT confirmed.
                try:
                    workspace.store.retire()
                except (OSError, ValueError) as error:
                    raise LibreOfficeError('Не удалось записать отказ. Результаты не удалены.',
                                           'LEGACY_DECLINE_WRITE_FAILED') from error
                if request_id is not None:
                    deleted = self.release(ticket)
                else:
                    try:
                        workspace.store.cleanup_retired()
                        deleted = True
                    except OSError:
                        deleted = False
                with self.lock:
                    if workspace_id is not None:
                        self.recovery_issues = [item for item in self.recovery_issues
                                                if item.get('workspaceId') != workspace_id]
                return {'declined': True, 'cleanupPending': not deleted,
                        'willReappear': False, 'historyUntouched': True}
            finally:
                if workspace_id is not None and workspace is not None:
                    workspace.close()

    def decline_all_recovery(self):
        """One explicit opt-out for all previous A06 results; never live work.

        Individual failures are reported instead of silently claiming that a
        locked workspace was deleted. Successful refusals have durable markers
        and cannot reappear in the recovery panel.
        """
        if self.recovery_parent is None:
            return {'declined': 0, 'notDeclined': 0, 'cleanupPending': 0, 'errors': []}
        if not self._recovery_loaded:
            self.scan_recovery()
        with self.lock:
            identities = [result.request_id for result in self.results.values()
                          if result.restored and not result.released and
                          isinstance(result.temporary, LegacyWorkspace)]
            workspaces = sorted({item.get('workspaceId') for item in self.recovery_issues
                                 if isinstance(item.get('workspaceId'), str) and
                                 ROOT_NAME.fullmatch(item['workspaceId'])})
        completed, pending, errors = 0, 0, []
        for kwargs in ([{'request_id': identity} for identity in identities] +
                       [{'workspace_id': identity} for identity in workspaces]):
            try:
                result = self.decline_recovery(**kwargs)
                completed += 1
                pending += bool(result['cleanupPending'])
            except LibreOfficeError as error:
                errors.append({'code': error.code, 'workspaceId': kwargs.get('workspace_id')})
        return {'declined': completed, 'notDeclined': len(errors),
                'cleanupPending': pending, 'errors': errors,
                'historyUntouched': True}

    def salvage_workspace(self, workspace_id):
        """Manually copy intact output bytes from an unverified workspace.

        It is NOT a conversion success. The source workspace is never deleted.
        """
        if self.recovery_parent is None:
            raise LibreOfficeError('Хранилище восстановления выключено.', 'LEGACY_SALVAGE_UNAVAILABLE')
        if not self._recovery_loaded:
            self.scan_recovery()
        if not isinstance(workspace_id, str) or not ROOT_NAME.fullmatch(workspace_id):
            raise LibreOfficeError('Неверный идентификатор каталога.', 'LEGACY_SALVAGE_INVALID')
        with self._recovery_scan_lock:
            with self.lock:
                if self.closed:
                    raise LibreOfficeError('Программа закрывается.', 'CANCELLED')
                if not any(item.get('workspaceId') == workspace_id and
                           item.get('code') == 'LEGACY_RECOVERY_UNVERIFIED'
                           for item in self.recovery_issues):
                    raise LibreOfficeError('Каталог не помечен как непроверенный.',
                                          'LEGACY_SALVAGE_INVALID')
            temporary = tempfile.TemporaryDirectory(prefix='aio-legacy-salvage-')
            target = Path(temporary.name) / 'Непроверенные_результаты.zip'
            try:
                count = salvage_unverified_workspace(self.recovery_parent, workspace_id, target,
                                                      check_budget=self.check_storage)
                request_id = secrets.token_hex(16)
                ticket = secrets.token_urlsafe(32)
                report = {'state': 'error', 'code': 'RECOVERED_UNVERIFIED',
                          'succeeded': 0, 'failed': count, 'total': count,
                          'qualityCompared': False, 'completenessVerified': False}
                result = Result(temporary, target, target.name, 'application/zip',
                                request_id=request_id, size=target.stat().st_size,
                                target_report=report, salvaged=True)
                with self.lock:
                    if self.closed:
                        raise LibreOfficeError('Программа закрывается.', 'CANCELLED')
                    self.results[ticket] = result
                    self.results_by_request[request_id] = ticket
                return {'state': 'ready', 'result': {
                    'name': result.name, 'mime': result.mime, 'size': result.size,
                    'ticket': ticket, 'downloadUrl': '/api/converter/result?ticket=' + ticket,
                    'targetReport': report, 'formatIds': [], 'salvagedUnverified': True},
                    'sourceKept': True, 'conversionSuccess': False}
            except BaseException:
                temporary.cleanup()
                raise

    def _owned_root_count(self):
        # Caller holds lock. Published-but-not-finished work shares one owner;
        # leased/released/in-flight cleanup remains counted until deletion ends.
        return len({id(item.temporary) for item in self.active.values()} |
                   {id(item.temporary) for item in self.results.values()} |
                   {id(item) for item in self.cleanup_pending}) + len(self.recovery_issues)

    def check_storage(self, additional=0):
        """Measure outside the transport lock; status/cancel never wait on I/O."""
        if type(additional) is not int or additional < 0:
            raise ValueError('additional must be a non-negative integer')
        if self.max_owned_bytes is None:
            return
        with self.lock:
            # Durable storage includes foreign-owned, incomplete and unknown
            # children, not just byte-verified published result payloads.
            roots = ([self.recovery_parent] + [result.temporary.name
                      for result in self.results.values() if result.salvaged]
                     if self.recovery_parent is not None else
                     list({item.temporary.name for item in self.active.values()} |
                          {item.temporary.name for item in self.results.values()} |
                          {item.name for item in self.cleanup_pending}))
        try:
            used = measured_bytes(roots)
        except OSError as error:
            with self.lock:
                self.storage_measurement_ok = False
            raise storage_error('LEGACY_STORAGE_MEASUREMENT_FAILED') from error
        with self.lock:
            self.used_bytes = used
            self.storage_measurement_ok = True
        if used + additional > self.max_owned_bytes:
            raise storage_error('LEGACY_STORAGE_BYTES')

    def run_control(self, request):
        return (request.cancel if self.max_owned_bytes is None else
                LegacyRunControl(request.cancel, self.check_storage))

    def begin(self, identity, connection, *, expected_upload_bytes=0):
        # Retry only already-released storage. Unsaved tickets are never
        # eligible for automatic cleanup.
        self.retry_cleanup()
        with self.lock:
            if not self.recovery_available:
                raise LibreOfficeError('Хранилище прежних результатов недоступно. '
                    'Проверьте права и диск; конвертация не начата.', 'LEGACY_STORAGE_UNAVAILABLE')
        self.check_storage(expected_upload_bytes)
        # Slot counting includes unverified roots even before manual recovery
        # has been opened. A shallow name-only scan never hashes old documents.
        disk_roots = 0
        if self.recovery_parent is not None:
            try:
                disk_roots = sum(ROOT_NAME.fullmatch(item.name) is not None
                                 for item in self.recovery_parent.iterdir())
            except FileNotFoundError:
                pass
            except OSError as error:
                raise storage_error('LEGACY_STORAGE_MEASUREMENT_FAILED') from error
        with self.lock:
            if self.closed or identity in self.active or identity in self.results_by_request:
                raise LibreOfficeError('Задание недоступно.', 'REQUEST_BUSY')
            if not self.recovery_available:
                raise LibreOfficeError('Хранилище прежних результатов недоступно. '
                    'Проверьте права и диск; конвертация не начата.', 'LEGACY_STORAGE_UNAVAILABLE')
            if max(self._owned_root_count(), disk_roots) >= self.max_owned_roots:
                raise LibreOfficeError(
                    'Достигнут предел временных заданий старого конвертера. '
                    'Сохраните и закройте прежние результаты или повторите очистку. '
                    'Несохранённые результаты не удалены.', 'LEGACY_STORAGE_LIMIT')
            temporary = (tempfile.TemporaryDirectory(prefix='aio-lo-') if self.recovery_parent is None
                         else LegacyWorkspace.create(self.recovery_parent))
            request = Request(identity, connection, temporary)
            self.active[identity] = request
            return request

    def inventory(self, *, offset=0, limit=20):
        """Bounded metadata-only discovery for an authenticated current session.

        Never scan the disk or return filesystem paths/full per-target reports.
        A caller selects a requestId and uses recover(); no result is selected
        or released implicitly. Durable workspaces were verified at startup.
        """
        if (type(offset) is not int or offset < 0 or type(limit) is not int or
                not 1 <= limit <= 50):
            raise ValueError('Invalid recovery page')
        if not self._recovery_loaded:
            self.scan_recovery()
        with self.lock:
            available = [result for result in self.results.values() if not result.released]
            items = [{'state': 'salvaged_unverified' if result.salvaged else 'ready',
                      'salvagedUnverified': result.salvaged, 'requestId': result.request_id,
                      'name': result.name, 'mime': result.mime, 'size': result.size,
                      'formatIds': list(result.format_ids),
                      'partial': bool((result.target_report or {}).get('failed')),
                      'recoverUrl': '/api/converter/recover?request=' + result.request_id}
                     for result in available[offset:offset + limit]]
            durable = self.recovery_parent is not None
            return {'state': 'inventory', 'scope': 'durable_published_results' if durable else 'current_process',
                    'restartRecovery': durable and self.recovery_available,
                    'scanLoaded': self._recovery_loaded,
                    'storageAvailable': self.recovery_available,
                    'storage': self.storage_stats(),
                    'recoveryWarnings': [dict(item) for item in self.recovery_issues],
                    'retention': 'until_explicit_release' if durable else 'until_release_or_process_shutdown',
                    'total': len(available), 'offset': offset, 'limit': limit,
                    'more': offset + len(items) < len(available), 'items': items}

    def recover(self, identity):
        """Recover a retained ticket after a lost HTTP response.

        HTTP callers need the session token and an exact 32-hex request ID,
        either retained by the client or selected from inventory().
        """
        if not self._recovery_loaded:
            self.scan_recovery()
        with self.lock:
            ticket = self.results_by_request.get(identity)
            result = self.results.get(ticket)
            if result is not None and not result.released:
                return {'state': 'ready', 'requestId': identity,
                    'result': {'name': result.name, 'mime': result.mime,
                        'size': result.size, 'ticket': ticket,
                        'downloadUrl': '/api/converter/result?ticket=' + ticket,
                        'targetReport': result.target_report,
                        'restoredFromJournal': result.restored,
                        'salvagedUnverified': result.salvaged,
                        'conversionQualityCompared': False,
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
                if isinstance(request.temporary, LegacyWorkspace) and request.temporary.preserve_outputs:
                    # Failed publication/packing never deletes the only copy
                    # of native outputs. An incomplete journal stays unverified.
                    request.temporary.close()
                    with self.lock:
                        self.recovery_issues.append({'workspaceId': request.root.name,
                                                     'code': 'LEGACY_RECOVERY_UNVERIFIED'})
                else:
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
        control = self.run_control(request)
        if isinstance(request.temporary, LegacyWorkspace):
            request.temporary.preserve_outputs = True
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
                    self.check_storage()
                    if request.cancel.is_set():
                        raise LibreOfficeError('Конвертация отменена.', 'CANCELLED')

                    def started():
                        with self.lock:
                            row['libreOfficeStarted'] = True

                    outputs = convert_path(source['path'], target,
                        request.root / 'native' / str(i) / str(j),
                        cancel=control, on_started=started)
                    self.check_storage()
                    output_root = request.root / 'native' / str(i) / str(j) / 'output'
                    for path in outputs:
                        archive_path = f'{i + 1:03d}_{target}/' + path.relative_to(output_root).as_posix()
                        artifacts.append((archive_path, path))
                    row['state'] = 'ready'
                    row['warnings'] = source['warnings']
                except LibreOfficeError as exc:
                    row.update(state='error', code=exc.code, error=str(exc))
                    if exc.code.startswith('LEGACY_STORAGE_'):
                        # A full/unknown disk must stop scheduling new native
                        # writers, without changing the user's cancel intent.
                        with self.lock:
                            for later_file in request.files:
                                for later in later_file['targets']:
                                    if later['state'] == 'queued':
                                        later.update(state='error', code=exc.code, error=str(exc))
                            request.completed = request.total - 1
                        exc.batch_report = self.progress(request.identity)
                        exc.batch_report['completed'] = request.total
                        exc.batch_report['percent'] = 100
                        exc.batch_report['succeeded'] = sum(t['state'] == 'ready'
                            for f in exc.batch_report['files'] for t in f['targets'])
                        exc.batch_report['failed'] = request.total - exc.batch_report['succeeded']
                        raise
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
            # ZIP_STORED duplicates native bytes. Check a conservative full
            # archive before opening it; include UTF-8 names and the report.
            if self.max_owned_bytes is not None:
                archive_budget = 512 + len(json.dumps(report, ensure_ascii=False, indent=2).encode('utf-8'))
                archive_budget += sum(output.stat().st_size + 256 + 2 * len(name.encode('utf-8'))
                                      for name, output in artifacts)
                self.check_storage(archive_budget)
            storage_control = LegacyRunControl(threading.Event(), self.check_storage)
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
                                storage_control.is_set()
                                target.write(chunk)
                    self._check_packaging_cancel(request)
                    if report['failed']:
                        archive.writestr('AIO-conversion-report.json',
                                         json.dumps(report, ensure_ascii=False, indent=2).encode())
                    self._check_packaging_cancel(request)
                    self.check_storage()
            finally:
                with self.lock:
                    request.packaging = False
            name, mime = path.name, 'application/zip'
        size = path.stat().st_size
        ticket = secrets.token_urlsafe(32)
        report['cancelledRequested'] = bool(request.cancel.is_set())
        result = Result(request.temporary, path, name, mime,
            request_id=request.identity, size=size, target_report=report, format_ids=tuple(formats))
        if isinstance(request.temporary, LegacyWorkspace):
            request.temporary.record(result)
        self.check_storage()
        with self.lock:
            self._check_packaging_cancel(request)
            if self.closed:
                raise LibreOfficeError('Программа закрывается.', 'CANCELLED')
            self.results[ticket] = result
            self.results_by_request[request.identity] = ticket
            request.published = True
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
            if result is None or result.released or self.closed:
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
            elif self.closed and not result.leases and isinstance(result.temporary, LegacyWorkspace):
                result.temporary.close()

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

    def stream_result(self, ticket, on_headers, write):
        """Release the file lease before the HTTP response can be complete.

        Keep only the final chunk in memory. A client may issue release as
        soon as Content-Length bytes arrive; it must not race our lease's
        finally block. Empty responses similarly defer their headers.
        """
        pending = b''
        with self.lease(ticket) as result:
            size = result.path.stat().st_size
            read = 0
            with result.path.open('rb') as stream:
                pending = stream.read(64 * 1024)
                read += len(pending)
                if pending:
                    on_headers(result, size)
                while block := stream.read(64 * 1024):
                    write(pending)
                    pending = block
                    read += len(block)
            if read != size:
                raise OSError('Legacy result size changed while streaming')
        if pending:
            write(pending)
        else:
            on_headers(result, size)

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
            pending = [temporary for temporary in self.cleanup_pending
                       if id(temporary) not in self._pending_cleaning]
            self._pending_cleaning.update(id(temporary) for temporary in pending)
        for ticket in released:
            self.release(ticket)
        for temporary in pending:
            try:
                temporary.cleanup()
            except OSError:
                pass
            else:
                with self.lock:
                    if temporary in self.cleanup_pending:
                        self.cleanup_pending.remove(temporary)
            finally:
                with self.lock:
                    self._pending_cleaning.discard(id(temporary))

    def storage_stats(self):
        with self.lock:
            owned = self._owned_root_count()
            return {'active': len(self.active), 'retainedResults':
                    sum(not result.released for result in self.results.values()),
                    'cleanupPending': len(self.cleanup_pending) +
                    sum(result.released for result in self.results.values()),
                    'leasedResults': sum(bool(result.leases) for result in self.results.values()),
                    'ownedRoots': owned, 'maxOwnedRoots': self.max_owned_roots,
                    'availableSlots': max(0, self.max_owned_roots - owned),
                    'usedBytes': self.used_bytes, 'maxOwnedBytes': self.max_owned_bytes,
                    'storageMeasurementOk': self.storage_measurement_ok,
                    'availableBytes': (max(0, self.max_owned_bytes - self.used_bytes)
                        if self.max_owned_bytes is not None and self.used_bytes is not None and
                        self.storage_measurement_ok else None)}

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
            with self.lock:
                result = self.results.get(ticket)
                keep = (result is not None and not result.released and
                        isinstance(result.temporary, LegacyWorkspace))
            if keep:
                if not result.leases:
                    result.temporary.close()
            else:
                self.release(ticket)
        # Share cleanup claims with timer/begin retries instead of concurrently
        # deleting the same pending directory during shutdown.
        self.retry_cleanup()
