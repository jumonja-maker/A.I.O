"""Optional disk-backed before/after jobs; no analysis on ordinary conversion."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
import copy
import io
import json
import mimetypes
from pathlib import Path
import secrets
import sqlite3
import sys
import threading
import time
import unicodedata
from urllib.parse import urlencode

from .errors import GuardError
from ..native_process import ProcessTree

STATUSES = ('confirmed_loss', 'format_limitation', 'uncertain', 'not_checked', 'checked')
FINAL = {'completed', 'cancelled', 'failed'}
MAX_PREVIEW = 64 * 1024 * 1024  # Preview only; never conversion admission.


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


class ComparisonService:
    def __init__(self, guard):
        self.guard = guard
        self.lock = threading.RLock()
        self.jobs = {}
        self.closed = False
        self.preview_gate = threading.BoundedSemaphore(1)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='aio-comparison')
        self.retry_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='aio-auto-retry')

    def _get(self, job_id):
        with self.lock:
            item = self.jobs.get(job_id)
            if item is not None and item.state in FINAL and item.db and not Path(item.db).is_file():
                self.jobs.pop(job_id, None)
                item = None
            if item is None:
                raise GuardError('COMPARISON_NOT_STARTED', 'Сначала запустите анализ до/после.', status=404)
            return item

    def start(self, job_id):
        pin = self.guard.comparison_inputs(job_id)
        records = pin.__enter__()
        transferred = False
        try:
            with self.lock:
                if self.closed:
                    raise GuardError('GUARD_CLOSED', 'Приложение завершает работу.', status=503)
                current = self.jobs.get(job_id)
                if current and current.state not in ('failed', 'cancelled'):
                    return self.snapshot(job_id)
                if any(j.state not in FINAL for j in self.jobs.values()):
                    raise GuardError('COMPARISON_BUSY', 'Другой анализ ещё выполняется.', status=409)
                # Finalized metadata is small, but must not accumulate across
                # unlimited user sessions. Disk reports belong to Guard jobs.
                for key in list(self.jobs):
                    if len(self.jobs) < 32: break
                    if self.jobs[key].state in FINAL: self.jobs.pop(key)
                item = _Analysis(job_id, pin=pin, records=records)
                self.jobs[job_id] = item
                item.future = self.executor.submit(self._run, item)
                item.future.add_done_callback(lambda future: self._unpin(item) if future.cancelled() else None)
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
        if record['sourceAvailable'] and record['state'] in ('ready', 'saved') and validate:
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
        with _database(item.db) as db:
            db.execute('INSERT INTO results VALUES (?,?,?,?,?,?,?)',
                       (record['taskId'], record['name'], record['format'], status,
                        bool(candidates), data, unicodedata.normalize('NFC', record['name']+' '+record['format']).casefold()))
        with self.lock:
            item.processed += 1
            item.summary[status] += 1

    @staticmethod
    def _unknown(reason, message):
        return {'status': 'not_checked', 'findings': [{'code': reason, 'category': 'not_checked',
                'message': message, 'certainty': 'unknown'}], 'checkedFeatures': [],
                'unknown': [reason], 'recipeCandidates': []}

    def _run(self, item):
        try:
            with nullcontext(item.records) as records:
                with self.guard._lock:
                    parent = self.guard._get(item.job_id)
                    root = parent.root / 'comparison' / item.identity
                root.mkdir(parents=True, exist_ok=False)
                database = root / 'results.sqlite'
                with _database(database, create=True) as db:
                    db.execute('CREATE TABLE results (task TEXT PRIMARY KEY, name TEXT, format TEXT, status TEXT, eligible INTEGER, payload TEXT, search TEXT)')
                    db.execute('CREATE INDEX results_status ON results(status)')
                with self.lock:
                    item.root, item.db = root, database
                    item.total, item.state = len(records), 'running'
                grouped = {}
                for record in records:
                    grouped.setdefault(record['fileId'], []).append(record)
                for index, group in enumerate(grouped.values()):
                    while item.pause.is_set() and not item.cancel.wait(.1):
                        pass
                    if item.cancel.is_set():
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
                        self._worker(item, valid, root / ('source-' + str(index)))
                with self.lock:
                    item.state = 'cancelled' if item.cancel.is_set() else 'completed'
        except Exception as error:
            with self.lock:
                item.state = 'cancelled' if item.cancel.is_set() else 'failed'
                item.error = error.message if isinstance(error, GuardError) else 'Не удалось завершить анализ. Результаты конвертации сохранены в AIO.'
        finally:
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
             'sha256': {str(p): next((a.get('sha256') for a in r.get('outputs', ())
                        if a.get('name') == p.name), None) for p in r['paths']}}
            for r in records]}
        request_path = root / 'request.json'
        request_path.write_text(json.dumps(request), encoding='utf-8')
        command = ([sys.executable, '--aio-guard-comparison', str(request_path)]
                   if getattr(sys, 'frozen', False) else
                   [sys.executable, '-m', 'app_backend.converter_guard.comparison_worker', str(request_path)])
        output = root / 'outcomes.jsonl'
        done = set()
        elapsed, previous = 0., time.monotonic()
        try:
            with ProcessTree(command, cwd=Path(__file__).resolve().parents[2]) as tree:
                while True:
                    now = time.monotonic()
                    if not item.pause.is_set():
                        elapsed += now - previous
                    previous = now
                    paused = root / 'pause'
                    if item.pause.is_set(): paused.touch(exist_ok=True)
                    else: paused.unlink(missing_ok=True)
                    if item.cancel.is_set() or elapsed > 20 + 12 * len(records):
                        break
                    if output.exists():
                        # A worker owns this bounded private JSONL. Ignore the
                        # incomplete final line until its next flush.
                        with output.open(encoding='utf-8') as stream:
                            for line in stream:
                                if not line.endswith('\n') or len(line) > 256 * 1024: continue
                                value = json.loads(line)
                                tid = value.get('taskId')
                                if tid in done: continue
                                record = next((r for r in records if r['taskId'] == tid), None)
                                if record is None: continue
                                self._store(item, record, value['outcome'])
                                done.add(tid)
                    if tree.process.poll() is not None: break
                    time.sleep(.08)
        except (OSError, ValueError, KeyError):
            pass
        if not item.cancel.is_set():
            for record in records:
                if record['taskId'] not in done:
                    self._store(item, record, self._unknown('ANALYSIS_INCOMPLETE',
                        'Проверка не завершилась в отведённых ресурсах. Потеря содержимого не установлена.'))

    def snapshot(self, job_id, *, offset=0, limit=50, filter='all', q=''):
        item = self._get(job_id)
        if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 100:
            raise GuardError('COMPARISON_PAGE', 'Неверная страница анализа.')
        if filter not in ('all', *STATUSES) or not isinstance(q, str) or len(q) > 200:
            raise GuardError('COMPARISON_FILTER', 'Неверный фильтр анализа.')
        with self.lock:
            result = {'jobId': job_id, 'analysisId': item.identity, 'state': item.state,
                'processed': item.processed, 'total': item.total, 'summary': dict(item.summary),
                'error': item.error, 'offset': offset, 'limit': limit, 'items': [],
                'totalFiltered': item.processed, 'retry': copy.deepcopy(item.retry)}
        if item.db and Path(item.db).exists():
            clauses, params = [], []
            if filter != 'all': clauses.append('status=?'); params.append(filter)
            if q:
                clauses.append("search LIKE ? ESCAPE '\\'")
                q = unicodedata.normalize('NFC', q).casefold()
                pattern = '%' + q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
                params.append(pattern)
            where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
            with _database(item.db) as db:
                result['totalFiltered'] = db.execute('SELECT COUNT(*) FROM results' + where, params).fetchone()[0]
                result['items'] = [json.loads(row[0]) for row in db.execute(
                    'SELECT payload FROM results' + where + ' ORDER BY rowid LIMIT ? OFFSET ?', [*params, limit, offset])]
        return result

    def detail(self, job_id, task_id):
        item = self._get(job_id)
        if not isinstance(task_id, str) or not item.db or not Path(item.db).is_file():
            raise GuardError('COMPARISON_TASK', 'Результат анализа ещё недоступен.', status=404)
        with _database(item.db) as db:
            row = db.execute('SELECT payload FROM results WHERE task=?', (task_id,)).fetchone()
        if row is None: raise GuardError('COMPARISON_TASK', 'Преобразование не найдено.', status=404)
        result = json.loads(row[0])
        with self.guard.comparison_inputs(job_id) as records:
            record = next((r for r in records if r['taskId'] == task_id), None)
            if record is None: raise GuardError('COMPARISON_TASK', 'Преобразование недоступно.', status=404)
            result['previews'] = {side: self._preview_meta(job_id, task_id, side, path)
                for side, path in [('source', record['source']), ('result', self._primary(record))]}
        return result

    @staticmethod
    def _primary(record):
        paths = record['paths']
        return next((p for p in paths if p.suffix.lower() == '.' + record['format']), paths[0] if paths else None)

    @staticmethod
    def _preview_meta(job_id, task_id, side, path):
        if path is None: return {'kind': 'unavailable', 'note': 'Файл недоступен для просмотра.'}
        suffix = path.suffix.lower()
        kind = 'pdf' if suffix == '.pdf' else 'image' if suffix in ('.png','.jpg','.jpeg','.gif','.webp','.bmp') else 'text'
        if suffix != '.pdf' and path.stat().st_size > MAX_PREVIEW:
            return {'kind': 'unavailable', 'note': 'Встроенный просмотр ограничен 64 МиБ. Конвертация и сохранение доступны.'}
        return {'kind': kind, 'name': path.name, 'size': path.stat().st_size,
                'url': '/api/converter-guard/compare/preview?' + urlencode({'job': job_id, 'task': task_id, 'side': side}),
                'note': ('Текстовое представление; оформление не показано.' if kind == 'text' else
                         'Уменьшенный просмотр изображения; для анимации показан первый кадр.' if kind == 'image' else '')}

    @contextmanager
    def preview(self, job_id, task_id, side, *, page=0):
        if side not in ('source', 'result'): raise GuardError('COMPARISON_SIDE', 'Неверная сторона сравнения.')
        with self.guard.comparison_inputs(job_id) as records:
            record = next((r for r in records if r['taskId'] == task_id), None)
            if record is None: raise GuardError('COMPARISON_TASK', 'Преобразование недоступно.', status=404)
            path = record['source'] if side == 'source' else self._primary(record)
            meta = self._preview_meta(job_id, task_id, side, path)
            if meta['kind'] == 'unavailable':
                yield {'json': meta}; return
            if meta['kind'] == 'text':
                yield {'json': self._text_preview(job_id, path)}; return
            if meta['kind'] in ('pdf','image'):
                data = self._text_preview(job_id, path,
                    mode='pdf_preview' if meta['kind'] == 'pdf' else 'image_preview', page=page)
                if 'png' in data:
                    raw = data.pop('png')
                    with io.BytesIO(raw) as stream:
                        yield {'stream':stream,'size':len(raw),'mime':'image/png',**data}
                else: yield {'json':data}
                return
            with path.open('rb') as stream:
                yield {'stream': stream, 'size': path.stat().st_size,
                       'mime': 'application/pdf' if meta['kind'] == 'pdf' else mimetypes.guess_type(path.name)[0] or 'application/octet-stream'}

    def _text_preview(self, job_id, path, *, mode='preview', page=0):
        if not self.preview_gate.acquire(blocking=False):
            raise GuardError('COMPARISON_PREVIEW_BUSY', 'Предыдущий предпросмотр ещё готовится. Повторите через несколько секунд.', status=409)
        try:
            with self.guard._lock:
                parent = self.guard._get(job_id)
                root = parent.root / 'comparison' / ('preview-' + secrets.token_hex(12))
            root.mkdir(parents=True)
            request = root / 'request.json'
            request.write_text(json.dumps({'mode':mode, 'source':str(path), 'page':page}), encoding='utf-8')
            command = ([sys.executable, '--aio-guard-comparison', str(request)]
                       if getattr(sys, 'frozen', False) else
                       [sys.executable, '-m', 'app_backend.converter_guard.comparison_worker', str(request)])
            until = time.monotonic() + 12
            with ProcessTree(command, cwd=Path(__file__).resolve().parents[2]) as tree:
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

    def control(self, job_id, action):
        item = self._get(job_id)
        with self.lock:
            if action == 'cancel':
                item.cancel.set(); item.retry_cancel.set(); item.pause.clear()
            elif action in ('pause', 'resume') and item.state not in FINAL:
                if action == 'pause': item.pause.set(); item.state = 'paused'
                else: item.pause.clear(); item.state = 'running'
            elif action not in ('pause', 'resume'):
                raise GuardError('COMPARISON_ACTION', 'Неизвестная команда анализа.')
        return self.snapshot(job_id)

    def retry(self, job_id, analysis_id, task_ids):
        item = self._get(job_id)
        if analysis_id != item.identity or item.state != 'completed':
            raise GuardError('COMPARISON_STALE', 'Дождитесь завершения актуального анализа.', status=409)
        if (not isinstance(task_ids, list) or not task_ids or
                any(not isinstance(t,str) or len(t)>100 for t in task_ids) or
                len(task_ids) != len(set(task_ids))):
            raise GuardError('COMPARISON_TASKS', 'Выберите преобразования без повторений.')
        groups = {}
        with _database(item.db) as db:
            for tid in task_ids:
                row = db.execute('SELECT payload FROM results WHERE task=?', (tid,)).fetchone()
                value = json.loads(row[0]) if row else {}
                candidates = value.get('recipeCandidates', [])
                if not candidates: raise GuardError('AUTOMATIC_RECIPE_UNAVAILABLE', 'Для выбранного результата нет проверенной автонастройки.')
                groups.setdefault(candidates[0], []).append(tid)
        with self.lock:
            if self.closed:
                raise GuardError('GUARD_CLOSED', 'Приложение завершает работу.', status=503)
            if item.retry and item.retry.get('state') not in FINAL:
                raise GuardError('COMPARISON_RETRY_BUSY', 'Повторная конвертация уже выполняется.', status=409)
            item.retry_cancel.clear()
            item.retry_jobs = []
            item.retry = {'state': 'queued', 'jobId': None, 'jobIds': [], 'error': None}
            self.retry_executor.submit(self._retry, item, groups)
        return self.snapshot(job_id)

    def _retry(self, item, groups):
        try:
            for recipe, tasks in groups.items():
                with self.lock: item.retry['state'] = 'preparing'
                created = self.guard.retry_comparison(item.job_id, tasks, recipe, cancel=item.retry_cancel)
                identity = created['jobId']
                with self.lock:
                    item.retry_jobs.append(identity)
                    item.retry.update(state='running', jobId=identity, jobIds=list(item.retry_jobs))
                while not item.retry_cancel.is_set():
                    state = self.guard.status(identity)
                    with self.lock: item.retry.update(completed=state['completed'], total=state['total'])
                    if state['finished']: break
                    time.sleep(.15)
                if item.retry_cancel.is_set():
                    self.guard.cancel(identity)
                    raise GuardError('CANCELLED', 'Повтор отменён. Первый результат сохранён.')
                with self.lock: item.retry['state'] = 'analyzing'
                while True:
                    if item.retry_cancel.is_set():
                        raise GuardError('CANCELLED', 'Повторная проверка отменена.')
                    try:
                        self.start(identity)
                        break
                    except GuardError as error:
                        if error.code != 'COMPARISON_BUSY': raise
                        time.sleep(.15)
                child = self._get(identity)
                while child.state not in FINAL and not item.retry_cancel.wait(.1):
                    pass
                if item.retry_cancel.is_set():
                    self.control(identity, 'cancel')
                    raise GuardError('CANCELLED', 'Повторная проверка отменена.')
                verification = self._verify_retry(item, child, tasks)
                with self.lock: item.retry['verification'] = verification
            with self.lock: item.retry['state'] = 'completed'
        except Exception as error:
            with self.lock:
                item.retry.update(state='cancelled' if item.retry_cancel.is_set() else 'failed',
                    error=error.message if isinstance(error,GuardError) else 'Повтор не завершён. Первый результат остаётся в AIO.')

    @staticmethod
    def _verify_retry(parent, child, task_ids):
        if child.state != 'completed' or not child.db:
            return {'assessment':'not_comparable', 'scope':'checked_subset'}
        with _database(parent.db) as db:
            before = [json.loads(db.execute('SELECT payload FROM results WHERE task=?', (tid,)).fetchone()[0])
                      for tid in task_ids]
        with _database(child.db) as db:
            after = [json.loads(r[0]) for r in db.execute('SELECT payload FROM results')]
        previous = {(r['fileId'],r['format']):r for r in before}
        old_losses = new_losses = 0
        comparable = bool(after) and len(after) == len(before)
        for row in after:
            old = previous.get((row['fileId'],row['format']))
            if old is None or row['status'] == 'not_checked': comparable = False; continue
            scope = set(row.get('checkedFeatures', ()))
            if row['status'] not in ('checked', 'format_limitation'): comparable = False
            if not scope or not set(old.get('checkedFeatures', ())).issubset(scope): comparable = False
            proof = {'cell_values': {'cell_values_single_grid', 'all_sheet_values_unordered'},
                     'frames': {'frames'}, 'pages': {'pages'},
                     'image_dimensions': {'image_dimensions'}, 'alpha_channel': {'alpha_channel'}}
            for finding in old.get('findings', ()):
                if finding.get('category') == 'confirmed_loss' and 'identical_file_bytes' not in scope:
                    required = proof.get(finding.get('feature'), set())
                    if not required.intersection(scope): comparable = False
            old_losses += sum(f.get('category') == 'confirmed_loss' for f in old.get('findings', ()))
            new_losses += sum(f.get('category') == 'confirmed_loss' for f in row.get('findings', ()))
        return {'assessment': ('checked_subset_improved' if comparable and old_losses > 0 and new_losses == 0
                               else 'no_confirmed_improvement' if comparable else 'not_comparable'),
                'scope':'checked_subset', 'beforeConfirmed':old_losses, 'afterConfirmed':new_losses,
                'note':'Полная визуальная идентичность не проверяется. Первый вариант сохранён.'}

    def close(self):
        with self.lock:
            self.closed = True
            for item in self.jobs.values(): item.cancel.set(); item.retry_cancel.set(); item.pause.clear()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.retry_executor.shutdown(wait=False, cancel_futures=True)
