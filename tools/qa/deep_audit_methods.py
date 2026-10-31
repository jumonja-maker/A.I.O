"""Temporary, explicit source fragments for audit 101. Removed before QA merge."""
ASSETS = {}
ASSETS['verify_saved_download.txt'] = '''    def verify_saved_download(self, ticket, destination, *, cancel=None):
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
'''
ASSETS['transport_scan.txt'] = '''    def _refresh_result_availability(self, tickets=None):
        """Metadata-only explicit reconciliation; inventory polls never touch disk."""
        with self.lock:
            if self.closed:
                return
            selected = [(ticket, result) for ticket, result in self.results.items()
                        if not result.released and (tickets is None or ticket in tickets)]
        observed = [(ticket, result, owned_file_identity(result.path, result.temporary.name))
                    for ticket, result in selected]
        with self.lock:
            if self.closed:
                return
            for ticket, result, identity in observed:
                if self.results.get(ticket) is not result or result.released:
                    continue
                result.availability_error = (None if identity is not None and
                    identity == result.file_identity else 'LEGACY_RESULT_UNAVAILABLE')

    def scan_recovery(self):
        """Explicit rescan with cancellation and a post-I/O publication barrier."""
        if self.recovery_parent is None:
            return {'state': 'disabled', 'warnings': []}
        with self._recovery_scan_lock:
            with self.lock:
                if self.closed:
                    raise LibreOfficeError('Программа закрывается.', 'CANCELLED')
            self._refresh_result_availability()
            with self.lock:
                claimed = {Path(result.temporary.name).name for result in self.results.values()
                           if isinstance(result.temporary, LegacyWorkspace)}
                claimed.update(Path(request.temporary.name).name for request in self.active.values()
                               if isinstance(request.temporary, LegacyWorkspace))
                claimed.update(Path(value.name).name for value in self.cleanup_pending
                               if isinstance(value, LegacyWorkspace))
            try:
                recovered, issues = recover_workspaces(self.recovery_parent,
                    skip_roots=claimed, cancel=self._shutdown)
            except InterruptedError:
                return {'state': 'cancelled', 'warnings': []}
            except OSError:
                with self.lock:
                    if self.closed:
                        return {'state': 'cancelled', 'warnings': []}
                    self.recovery_issues = [{'workspaceId': 'legacy', 'code': 'LEGACY_RECOVERY_UNAVAILABLE'}]
                    self.recovery_available = False
                    self._recovery_loaded = True
                    return {'state': 'unavailable', 'warnings': list(self.recovery_issues)}
            pending, duplicates = {}, set()
            with self.lock:
                # Shutdown may have overtaken slow hashing. Close every late owner.
                if self.closed:
                    for workspace, _value in recovered:
                        workspace.close()
                    return {'state': 'cancelled', 'warnings': []}
                for workspace, value in recovered:
                    identity = value['requestId']
                    if identity in self.active or identity in self.results_by_request or identity in duplicates:
                        workspace.close()
                        issues.append({'workspaceId': Path(workspace.name).name, 'code': 'LEGACY_RECOVERY_DUPLICATE_ID'})
                    elif identity in pending:
                        previous = pending.pop(identity)[0]
                        previous.close()
                        issues.extend(({'workspaceId': Path(previous.name).name, 'code': 'LEGACY_RECOVERY_DUPLICATE_ID'},
                                       {'workspaceId': Path(workspace.name).name, 'code': 'LEGACY_RECOVERY_DUPLICATE_ID'}))
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
                unavailable = sum(bool(result.availability_error) for result in self.results.values() if not result.released)
                return {'state': 'completed', 'restored': len(pending), 'unavailableResults': unavailable,
                        'warnings': [dict(item) for item in issues]}
'''
ASSETS['transport_salvage.txt'] = '''    def salvage_workspace(self, workspace_id):
        """Reserve before copying; only one outstanding derivative per source."""
        if self.recovery_parent is None:
            raise LibreOfficeError('Хранилище восстановления выключено.', 'LEGACY_SALVAGE_UNAVAILABLE')
        if not self._recovery_loaded:
            self.scan_recovery()
        if not isinstance(workspace_id, str) or not ROOT_NAME.fullmatch(workspace_id):
            raise LibreOfficeError('Неверный идентификатор каталога.', 'LEGACY_SALVAGE_INVALID')
        with self._recovery_scan_lock:
            with self._admission_lock:
                disk_roots = self._disk_roots()
                with self.lock:
                    if self.closed:
                        raise LibreOfficeError('Программа закрывается.', 'CANCELLED')
                    if not self.recovery_available:
                        raise storage_error('LEGACY_STORAGE_MEASUREMENT_FAILED')
                    if not any(item.get('workspaceId') == workspace_id and item.get('code') == 'LEGACY_RECOVERY_UNVERIFIED'
                               for item in self.recovery_issues):
                        raise LibreOfficeError('Каталог не помечен как непроверенный.', 'LEGACY_SALVAGE_INVALID')
                    if any(result.source_workspace_id == workspace_id for result in self.results.values()):
                        raise LibreOfficeError('Извлечённая копия уже существует. Сохраните и закройте её перед повторным извлечением.',
                                               'LEGACY_SALVAGE_EXISTS')
                    if self._owned_root_count(disk_roots) >= self.max_owned_roots:
                        raise LibreOfficeError('Нет свободного места для временного задания. Закройте прежние результаты.',
                                               'LEGACY_STORAGE_LIMIT')
                    self._salvage_roots[workspace_id] = None
            temporary = None
            published = False
            try:
                temporary = tempfile.TemporaryDirectory(prefix='aio-legacy-salvage-')
                with self.lock:
                    self._salvage_roots[workspace_id] = temporary
                target = Path(temporary.name) / 'Непроверенные_результаты.zip'
                count = salvage_unverified_workspace(self.recovery_parent, workspace_id, target,
                    check_budget=self.check_storage, cancel=self._shutdown)
                request_id = secrets.token_hex(16)
                ticket = secrets.token_urlsafe(32)
                report = {'state': 'error', 'code': 'RECOVERED_UNVERIFIED',
                          'succeeded': 0, 'failed': count, 'total': count,
                          'qualityCompared': False, 'completenessVerified': False}
                result = Result(temporary, target, target.name, 'application/zip',
                    request_id=request_id, size=target.stat().st_size,
                    target_report=report, salvaged=True, source_workspace_id=workspace_id)
                with self.lock:
                    if self.closed:
                        raise LibreOfficeError('Программа закрывается.', 'CANCELLED')
                    self.results[ticket] = result
                    self.results_by_request[request_id] = ticket
                    self._salvage_roots.pop(workspace_id, None)
                    published = True
                return {'state': 'ready', 'result': {
                    'name': result.name, 'mime': result.mime, 'size': result.size,
                    'ticket': ticket, 'downloadUrl': '/api/converter/result?ticket=' + ticket,
                    'targetReport': report, 'formatIds': [], 'salvagedUnverified': True},
                    'sourceKept': True, 'conversionSuccess': False}
            except InterruptedError as error:
                raise LibreOfficeError('Извлечение отменено.', 'CANCELLED') from error
            finally:
                if not published:
                    try:
                        if temporary is not None:
                            temporary.cleanup()
                    except OSError:
                        with self.lock:
                            self.cleanup_pending.append(temporary)
                        self._schedule_cleanup()
                    finally:
                        with self.lock:
                            self._salvage_roots.pop(workspace_id, None)

    def _disk_roots(self):
        if self.recovery_parent is None:
            return ()
        try:
            return tuple(str(item.absolute()) for item in self.recovery_parent.iterdir()
                         if ROOT_NAME.fullmatch(item.name))
        except FileNotFoundError:
            return ()
        except OSError as error:
            raise storage_error('LEGACY_STORAGE_MEASUREMENT_FAILED') from error
'''
ASSETS['transport_root_count.txt'] = '''    def _owned_root_count(self, disk_roots=()):
        # Caller holds lock. De-duplicate physical roots across owners/warnings.
        owners = ([item.temporary for item in self.active.values()] +
                  [item.temporary for item in self.results.values()] + list(self.cleanup_pending) +
                  [item for item in self._salvage_roots.values() if item is not None])
        roots = {str(Path(item.name).absolute()) for item in owners}
        roots.update(disk_roots)
        if self.recovery_parent is not None:
            roots.update(str((self.recovery_parent / item['workspaceId']).absolute())
                         for item in self.recovery_issues if isinstance(item.get('workspaceId'), str) and
                         ROOT_NAME.fullmatch(item['workspaceId']))
        return len(roots) + sum(item is None for item in self._salvage_roots.values())
'''
ASSETS['recover_workspaces.txt'] = '''def recover_workspaces(parent, *, skip_roots=(), cancel=None):
    """Claim complete journals, closing every untransferred owner on abort."""
    parent = Path(parent)
    _check_cancel(cancel)
    parent.mkdir(parents=True, exist_ok=True)
    if path_is_link(parent):
        raise OSError('Legacy recovery parent is linked')
    recovered, issues = [], []
    try:
        for root in sorted(parent.iterdir()):
            _check_cancel(cancel)
            if not ROOT_NAME.fullmatch(root.name) or root.name in skip_roots:
                continue
            workspace = None
            try:
                if path_is_link(root) or not root.is_dir():
                    raise ValueError('Invalid legacy workspace')
                if ResultStore(root).retirement_pending():
                    try:
                        workspace = LegacyWorkspace(root)
                        try:
                            workspace.store.cleanup_retired()
                        finally:
                            workspace.close()
                    except (OSError, ValueError, TypeError, RecursionError):
                        pass
                    continue
                workspace = LegacyWorkspace(root)
                value = workspace.load(cancel=cancel)
                _check_cancel(cancel)
                recovered.append((workspace, value))
                workspace = None
            except InterruptedError:
                raise
            except (OSError, ValueError, TypeError, RecursionError) as error:
                issues.append({'workspaceId': root.name, 'code': (
                    'LEGACY_RECOVERY_BUSY' if isinstance(error, BlockingIOError) else 'LEGACY_RECOVERY_UNVERIFIED')})
            finally:
                if workspace is not None:
                    workspace.close()
        _check_cancel(cancel)
        return recovered, issues
    except BaseException:
        for workspace, _value in recovered:
            workspace.close()
        raise
'''
ASSETS['salvage_unverified_workspace.txt'] = '''def salvage_unverified_workspace(parent, workspace_id, destination, *, check_budget=None, cancel=None):
    """Copy an unverified snapshot. One independent CRC/SHA read per member."""
    _check_cancel(cancel)
    if not isinstance(workspace_id, str) or not ROOT_NAME.fullmatch(workspace_id):
        raise ValueError('Invalid legacy workspace identity')
    root = Path(parent) / workspace_id
    if path_is_link(root) or not root.is_dir():
        raise ValueError('Legacy workspace is not an ordinary directory')
    workspace = LegacyWorkspace(root)
    try:
        if workspace.store.retirement_pending():
            raise ValueError('Previously retired workspace must not be salvaged')
        try:
            workspace.load(cancel=cancel)
        except InterruptedError:
            raise
        except (OSError, ValueError, TypeError, RecursionError):
            pass
        else:
            raise ValueError('A verified legacy result does not need salvage')
        native = root / 'native'
        if not native.is_dir() or path_is_link(native):
            raise ValueError('No safe native outputs in legacy workspace')
        files, total, visited = [], 0, 0
        for candidate in native.glob('*/*/output/**/*'):
            visited += 1
            if visited > 100_000:
                raise ValueError('Legacy salvage inspection limit exceeded')
            _check_cancel(cancel)
            relative = candidate.relative_to(root).as_posix()
            parts = PurePosixPath(relative).parts
            if (len(parts) < 5 or parts[0] != 'native' or
                    not parts[1].isdecimal() or not parts[2].isdecimal() or parts[3] != 'output'):
                continue
            if path_is_link(candidate):
                raise ValueError('Linked native output cannot be salvaged')
            if candidate.is_dir():
                continue
            output = workspace._path(relative)
            size = output.stat().st_size
            total += size
            files.append((relative, output, size))
            if len(files) > 4096 or total > 8 * 1024 ** 3:
                raise ValueError('Legacy salvage inspection limit exceeded')
        files.sort(key=lambda item: item[0])
        if not files:
            raise ValueError('No surviving native outputs were found')
        if check_budget is not None:
            check_budget(total + 1024 * 1024 + 512 * len(files))
        _check_cancel(cancel)
        destination = Path(destination)
        entries = []
        with ZipFile(destination, 'x', compression=ZIP_STORED, allowZip64=True) as archive:
            for relative, output, size in files:
                _check_cancel(cancel)
                digest = hashlib.sha256()
                copied = 0
                with output.open('rb') as source, archive.open(relative, 'w', force_zip64=True) as target:
                    while block := source.read(128 * 1024):
                        _check_cancel(cancel)
                        if copied + len(block) > size:
                            raise OSError('Native output grew during salvage')
                        target.write(block)
                        digest.update(block)
                        copied += len(block)
                if copied != size or output.stat().st_size != size:
                    raise OSError('Native output changed during salvage')
                entries.append({'path': relative, 'size': size, 'sha256': digest.hexdigest()})
            report = json.dumps({'schema': 1, 'status': 'RECOVERED_UNVERIFIED',
                'conversionSuccess': False, 'qualityCompared': False,
                'completenessVerified': False, 'sourceWorkspaceId': workspace_id,
                'files': entries}, ensure_ascii=False).encode('utf-8')
            archive.writestr('AIO-salvage-report.json', report)
        _check_cancel(cancel)
        with ZipFile(destination) as archive:
            names = [entry['path'] for entry in entries] + ['AIO-salvage-report.json']
            if sorted(archive.namelist()) != sorted(names):
                raise OSError('Salvaged ZIP inventory changed')
            if archive.read('AIO-salvage-report.json') != report:
                raise OSError('Salvaged ZIP report changed')
            for entry in entries:
                _check_cancel(cancel)
                digest, size = hashlib.sha256(), 0
                with archive.open(entry['path']) as stream:
                    while block := stream.read(128 * 1024):
                        _check_cancel(cancel)
                        digest.update(block)
                        size += len(block)
                if size != entry['size'] or digest.hexdigest() != entry['sha256']:
                    raise OSError('Salvaged ZIP bytes changed')
        _check_cancel(cancel)
        if check_budget is not None:
            check_budget()
        return len(entries)
    finally:
        workspace.close()
'''
ASSETS['measured_bytes.txt'] = '''def measured_bytes(roots):
    """Full bounded scans, reusing scandir metadata instead of Path per file.

    No stale byte cache can hide active writers or foreign-owned files.
    """
    total, visited = 0, 0
    pending = [(os.fspath(root), 0, None) for root in roots]
    while pending:
        path, depth, entry = pending.pop()
        visited += 1
        if visited > MAX_SCAN_ENTRIES or depth > MAX_SCAN_DEPTH:
            raise OSError('Legacy storage inspection budget exceeded')
        try:
            info = (os.lstat(path) if entry is None else entry.stat(follow_symlinks=False))
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise OSError('Linked legacy storage cannot be measured safely')
        if stat.S_ISREG(info.st_mode):
            total += info.st_size
        elif stat.S_ISDIR(info.st_mode):
            try:
                with os.scandir(path) as entries:
                    for item in entries:
                        pending.append((item.path, depth + 1, item))
                        if visited + len(pending) > MAX_SCAN_ENTRIES:
                            raise OSError('Legacy storage inspection budget exceeded')
            except FileNotFoundError:
                continue
        else:
            raise OSError('Unsupported legacy storage entry')
    return total
'''
ASSETS['guard_reconcile.txt'] = '''    def _reconcile_results(self, job_id):
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
'''
ASSETS['owned_artifact_path.txt'] = '''    def _owned_artifact_path(self, job, artifact, *, stage):
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
'''
