"""Isolated source preparation only. Never move refs or ship this script."""
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request

BASE = '1646af04841e409079d1bb52150ff10670dc31f1'
TREE = '293ef48e697a8ec66eabf05d7c3dde6a92e976e5'
REPO = 'jumonja-maker/A.I.O'
ROOT = Path(__file__).resolve().parents[2]
os.chdir(ROOT)


def replace(text, before, after, count=1):
    if text.count(before) != count:
        raise RuntimeError('Unexpected source anchor: '+repr(before[:100]))
    return text.replace(before, after)


def section(text, start, end, transform):
    first, last = text.index(start), text.index(end, text.index(start))
    return text[:first]+transform(text[first:last])+text[last:]


def run(*args):
    subprocess.run(args, check=True)


# Prove that the old HTTP implementation ignored the lightweight query and
# invoked the expensive recovery loader. An unrelated error is not evidence.
env = {**os.environ, 'PYTHONPATH':str(ROOT)+os.pathsep+str(ROOT/'tests')}
probe = subprocess.run([sys.executable,'-m','unittest',
    'test_recovery_manual_scan.ManualRecovery.test_snapshot_http_does_not_start_old_recovery'],
    env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
print(probe.stdout, flush=True)
assert probe.returncode != 0 and 'SNAPSHOT_MUST_NOT_SCAN_OLD_DISK' in probe.stdout
print('AIO_BASELINE_REPRODUCED=snapshot_get_invokes_disk_recovery', flush=True)

p = Path('app_backend/converter_guard/core.py'); source = p.read_text(encoding='utf-8')
source = replace(source, 'class ConverterGuard:',
    'from .recovery_control import RecoveryStopped, check_recovery_stop\n\n\nclass ConverterGuard:')
source = replace(source, '        self._recovery_lock = threading.Lock()\n', '''        self._recovery_lock = threading.Lock()
        # Constructed lazily like the conversion executor: no thread or disk
        # search starts until the user explicitly requests old results.
        self._recovery_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='aio-recovery')
        self._recovery_stop = threading.Event()
        self._recovery_future = None
        self._recovery_scan_state = 'idle'
        self._recovery_scan_id = 0
        self._recovery_scan_error = None
''')
start = source.index('    def _load_recovery(self):')
end = source.index('    def _batch(', start)
source = source[:start]+'''    def _load_recovery(self):
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
            if self._recovery and self._recovery_scan_state != 'scanning':
                if not self._recovery_loaded or self._recovery_pending:
                    self._recovery_scan_state = 'scanning'
                    self._recovery_scan_error = None
                    self._recovery_scan_id += 1
                    # Claim and submit under one lock: shutdown cannot overtake
                    # admission, and repeated clicks cannot queue extra scans.
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
            self._load_recovery()
        except Exception:
            failed = True  # Paths and exception text never enter the UI status.
        finally:
            with self._lock:
                self._recovery_scan_state = ('closed' if self._closed else 'failed' if failed
                    else 'waiting_for_slot' if self._recovery_pending else 'completed')
                self._recovery_scan_error = 'RECOVERY_SCAN_FAILED' if failed and not self._closed else None

    def _recovery_warning(self, value):
        with self._lock:
            if value not in self._recovery_issues:
                self._recovery_issues.append(value)

    def _publish_recovered(self, job):
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

'''+source[end:]

def recovery(text):
    text = replace(text, '        ResultStore.reap_finished_retirements(self._recovery)',
        '        check_recovery_stop(self._recovery_stop)\n        ResultStore.reap_finished_retirements(self._recovery, cancel=self._recovery_stop)')
    text = replace(text, '        for root in roots:\n', '        for root in roots:\n            check_recovery_stop(self._recovery_stop)\n')
    text = replace(text, '''            if len(self._jobs)>=self.limits.max_jobs:
                self._recovery_pending.append(root)
                continue
''', '''            with self._lock:
                if len(self._jobs) >= self.limits.max_jobs:
                    if root not in self._recovery_pending:
                        self._recovery_pending.append(root)
                    continue
''')
    text = text.replace('self._recovery_issues.append(', 'self._recovery_warning(')
    text = replace(text, 'header, records, saved, integrity = store.read()',
        'header, records, saved, integrity = store.read(cancel=self._recovery_stop)')
    text = replace(text, "                for row, original in zip(rows, header['files']):\n",
        "                for row, original in zip(rows, header['files']):\n                    check_recovery_stop(self._recovery_stop)\n")
    text = replace(text, '                                                           item.content_digest)',
        '                                                           item.content_digest, cancel=self._recovery_stop)')
    text = replace(text, "store.input_path(relative, resource['size'], resource.get('digest'))",
        "store.input_path(relative, resource['size'], resource.get('digest'), cancel=self._recovery_stop)")
    text = replace(text, '                for item in files:\n', '                for item in files:\n                    check_recovery_stop(self._recovery_stop)\n')
    text = replace(text, "                        for output in record.get('outputs', []):\n",
        "                        for output in record.get('outputs', []):\n                            check_recovery_stop(self._recovery_stop)\n")
    text = replace(text, '                                        actual = sha256_stream(restored, cancel=threading.Event())', '''                                        try:
                                            actual = sha256_stream(restored, cancel=self._recovery_stop)
                                        except GuardError:
                                            check_recovery_stop(self._recovery_stop)
                                            raise''')
    begin = text.index('                with self._lock:\n                    if self._closed or identity in self._jobs:')
    end = text.index('            except (OSError, ValueError, TypeError, KeyError, GuardError):', begin)
    text = text[:begin]+'''                check_recovery_stop(self._recovery_stop)
                self._publish_recovered(job)
            except RecoveryStopped:
                store.close()
                raise  # Shutdown is not invalid metadata and never triggers salvage.
'''+text[end:]
    return text
source = section(source, '    def _recover_results(', '    def _salvage_outputs(', recovery)
source = section(source, '    def _salvage_outputs(', '    def _salvage_orphan(', lambda text: replace(
    text, '                for name in sorted(names):\n',
    '                for name in sorted(names):\n                    check_recovery_stop(self._recovery_stop)\n'))

def orphan(text):
    begin = text.index('            with self._lock:\n                if identity in self._jobs or self._closed:')
    end = text.index('        except (OSError, ValueError):', begin)
    return text[:begin]+'''            check_recovery_stop(self._recovery_stop)
            self._publish_recovered(job)
        except RecoveryStopped:
            store.close()
            raise
'''+text[end:]
source = section(source, '    def _salvage_orphan(', '    def recoverable(', orphan)
source = replace(source, '''    def recoverable(self):
        self._prune(reap_saved=True)
        self._load_recovery()
        with self._lock:
            return {'storage': self.storage_stats(), 'pendingRecoveryFolders':len(self._recovery_pending),''', '''    def recoverable(self, *, scan=True):
        if type(scan) is not bool:
            raise GuardError('RECOVERY_REQUEST', 'Неверный режим получения результатов.')
        # Lightweight GUI polls do not enumerate directories, hash documents,
        # wait for an existing scan, or synchronously remove a large tree.
        self._prune(reap_saved=True, defer_cleanup=not scan)
        if scan:
            self._load_recovery()
        with self._lock:
            return {'storage': self.storage_stats(), 'pendingRecoveryFolders':len(self._recovery_pending),
                    'recoveryScan': {'state': self._recovery_scan_state,
                        'available': bool(self._recovery), 'loaded': self._recovery_loaded,
                        'requestId': self._recovery_scan_id,
                        'pendingFolders': len(self._recovery_pending),
                        'errorCode': self._recovery_scan_error},''')
source = replace(source, '    def _prune(self, *, reap_saved=False):',
    '    def _prune(self, *, reap_saved=False, defer_cleanup=False):')
source = section(source, '    def _prune(', '    def _reserve(', lambda text: replace(text,
    '                job.released = True\n',
    '                job.released = True\n                job.cleanup_background = job.cleanup_background or defer_cleanup\n'))
source = section(source, '    def close(self):', '    def storage_stats(', lambda text: replace(replace(text,
    '            self._closed = True\n', '            self._closed = True\n            self._recovery_stop.set()\n'),
    '        self._executor.shutdown(wait=True)\n',
    '        self._recovery_executor.shutdown(wait=True)\n        self._executor.shutdown(wait=True)\n'))
p.write_text(source, encoding='utf-8')

p = Path('app_backend/converter_guard/result_store.py'); source = p.read_text(encoding='utf-8')
source = replace(source, 'import shutil\n', 'import shutil\n\nfrom .recovery_control import check_recovery_stop\n')
source = replace(source, '    def reap_finished_retirements(cls, parent):',
    '    def reap_finished_retirements(cls, parent, *, cancel=None):')
source = replace(source, "        for marker in Path(parent).glob('.aio-guard-*.retiring.json'):\n",
    "        for marker in Path(parent).glob('.aio-guard-*.retiring.json'):\n            check_recovery_stop(cancel)\n")
source = replace(source, "    def read(self):\n        header = self.root / 'job.json'",
    "    def read(self, *, cancel=None):\n        check_recovery_stop(cancel)\n        header = self.root / 'job.json'")
source = replace(source, '                while line := stream.readline(8 * 1024 * 1024 + 1):\n',
    '                while line := stream.readline(8 * 1024 * 1024 + 1):\n                    check_recovery_stop(cancel)\n')
source = replace(source, '    def input_path(self, relative, size, digest):',
    '    def input_path(self, relative, size, digest, *, cancel=None):')
source = replace(source, '        path = self.output_path(relative, size)\n        actual = hashlib.sha256()',
    '        check_recovery_stop(cancel)\n        path = self.output_path(relative, size)\n        actual = hashlib.sha256()')
source = replace(source, '            while block := source.read(64 * 1024):\n                actual.update(block)',
    '            while block := source.read(64 * 1024):\n                check_recovery_stop(cancel)\n                actual.update(block)\n        check_recovery_stop(cancel)')
p.write_text(source, encoding='utf-8')

p = Path('app_backend/converter_guard/http_api.py'); source = p.read_text(encoding='utf-8')
source = replace(source, "            if path == PREFIX + '/recoverable':\n                return self._json(handler,200, self.guard.recoverable())", """            if path == PREFIX + '/recoverable':
                query = parse_qs(urlsplit(handler.path).query, keep_blank_values=True)
                if query and query != {'mode': ['snapshot']}:
                    raise GuardError('RECOVERY_REQUEST', 'Неверный режим получения результатов.')
                return self._json(handler,200, self.guard.recoverable(scan=not bool(query)))""")
source = replace(source, "            identity = self._identity(body.get('jobId'))\n", """            if path == PREFIX + '/recovery-scan':
                if body:
                    raise GuardError('RECOVERY_REQUEST', 'Поиск результатов не принимает параметры задания.')
                return self._json(handler,202, self.guard.start_recovery_scan())
            identity = self._identity(body.get('jobId'))
""")
p.write_text(source, encoding='utf-8')

p = Path('src/converter-bridge.js'); source = p.read_text(encoding='utf-8')
source = replace(source, "    async recoverable() {return api('/recoverable',undefined,'GET');},", """    async recoverable({scan=true}={}) {
      if(typeof scan!=='boolean')throw errorOf('Неверный режим списка результатов.','RECOVERY_REQUEST');
      return api('/recoverable'+(scan?'':'?mode=snapshot'),undefined,'GET');
    },
    async scanRecovery() {return api('/recovery-scan',{});},""")
p.write_text(source, encoding='utf-8')

p = Path('src/guard-queue.js'); source = p.read_text(encoding='utf-8')
source = replace(source, '  let renderedRecoveryWarnings=null;', '''  let renderedRecoveryWarnings=null;
  let recoveryScan=null,recoveryTimer=null,refreshError=false,pageHidden=false;
  function stopRecoveryPoll(){if(recoveryTimer!==null){clearTimeout(recoveryTimer);recoveryTimer=null;}}
  function scheduleRecoveryPoll(){
    stopRecoveryPoll();
    if(pageHidden||refreshError||!bar?.querySelector('.aio-guard-popup')?.open)return;
    if(recoveryScan?.state==='scanning'||storage?.cleanupPending&&!storage?.cleanupRetryJobs)
      recoveryTimer=setTimeout(()=>{recoveryTimer=null;refresh();},1000);
  }
''')
source = replace(source, '        dialog.showModal();', '        bar.hidden=false;dialog.showModal();')
source = replace(source, '    if(!next){cancelPreview();dropRows();',
    '    if(!next){if(root)refreshRevision++;stopRecoveryPoll();cancelPreview();dropRows();')
source = replace(source, '    cancelPreview();dropRows();stateButton?.remove();stateButton=null;\n    root=next;',
    '    refreshRevision++;stopRecoveryPoll();cancelPreview();dropRows();stateButton?.remove();stateButton=null;\n    root=next;')
source = replace(source, "      '<div data-g-counts></div><div class=\"aio-guard-warning\" data-g-warning></div>'+", """      '<div data-g-counts></div><div class="aio-guard-warning" data-g-warning></div>'+
      '<div class="aio-guard-save"><button type="button" data-g-scan>Найти предыдущие результаты</button><button type="button" data-g-refresh>Обновить список</button></div>'+
      '<div data-g-scan-status role="status" aria-live="polite"></div>'+""")
source = replace(source, '<select data-g-jobs aria-label="Несохранённое задание">',
    '<select data-g-jobs size="4" aria-label="Доступные пакеты результатов">')
source = replace(source, "        actionFeedback=result?.saved?'Сохранено файлов: '+(result.outputs??''):\n          'Команда принята. Обновляем состояние…';", """        actionFeedback=result?.message||(result?.cancelled?'Действие отменено.':
          result?.saved?'Сохранено файлов: '+(result.outputs??''):'Команда выполнена.');""")
source = replace(source, "    action('[data-g-pause]',()=>api().pause(!state?.pauseRequested));", """    action('[data-g-scan]',async()=>{
      if(!api()?.scanRecovery)throw Error('Поиск предыдущих результатов недоступен.');
      const found=await api().scanRecovery();
      return {message:found.recoveryScan?.state==='scanning'?'Поиск начат. Ход проверки показан ниже.':'Запрос поиска обработан.'};
    });
    bar.querySelector('[data-g-refresh]').addEventListener('click',()=>refresh());
    action('[data-g-pause]',()=>api().pause(!state?.pauseRequested));""")
source = replace(source, "      if(window.confirm('Удалить несохранённые результаты этого задания?'))await api().discardRetained(selected());", """      if(!window.confirm('Удалить несохранённые результаты этого задания?'))return {cancelled:true};
      await api().discardRetained(selected());
      return {message:'Удаление принято. Состояние очистки показано ниже.'};""")
source = replace(source, ":'/api/converter-guard/recoverable',", ":'/api/converter-guard/recoverable?mode=snapshot',")
source = replace(source, '      if(dialog.open)return;\n      dropRows();',
    '      if(dialog.open)return;\n      stopRecoveryPoll();dropRows();')
source = replace(source, '      if(stateButton?.isConnected)stateButton.focus({preventScroll:true});',
    '      if(stateButton?.isConnected)stateButton.focus({preventScroll:true});\n      render();')
start=source.index('  async function refresh() {');end=source.index('  function render() {',start)
source=source[:start]+'''  async function refresh() {
    if(pageHidden||!bar||!root?.isConnected||!api()?.recoverable)return;
    if(refreshPending){refreshAgain=true;return;}
    stopRecoveryPoll();refreshPending=true;const revision=refreshRevision,view=bar;
    try{
      const found=await api().recoverable({scan:false});
      if(pageHidden||revision!==refreshRevision||bar!==view)return;
      jobs=found.jobs||[];warnings=found.warnings||[];storage=found.storage||null;
      recoveryScan=found.recoveryScan||null;refreshError=false;render();
    }catch(_){
      if(!pageHidden&&revision===refreshRevision&&bar===view){refreshError=true;render();}
    }finally{
      refreshPending=false;
      if(refreshAgain){refreshAgain=false;refresh();}else scheduleRecoveryPoll();
    }
  }
'''+source[end:]
source=replace(source, '    bar.hidden=!state&&!jobs.length&&!warnings.length;\n    if(stateButton){\n      stateButton.disabled=bar.hidden;', '''    bar.hidden=!state&&!jobs.length&&!warnings.length&&!bar.querySelector('.aio-guard-popup').open;
    if(stateButton){
      // Empty current work must not lock the user out of manual recovery.
      stateButton.disabled=false;''')
source=replace(source, "    text('[data-g-storage]',storage?'Слоты Guard: '+storage.jobs+' / '+storage.maxJobs+\n      ' · несохранено: '+storage.unsavedOutputs+' файлов · '+Math.ceil(storage.unsavedBytes/1048576)+' МБ':'');", """    const currentJobs=jobs.filter(job=>!job.recovered);
    text('[data-g-storage]',storage?'Слоты Guard: '+storage.jobs+' / '+storage.maxJobs+
      ' · текущих несохранённых: '+currentJobs.reduce((sum,job)=>sum+(job.unsavedOutputs||0),0)+
      ' · предыдущих пакетов в списке: '+jobs.filter(job=>job.recovered).length:'');
    const scanButton=bar.querySelector('[data-g-scan]');
    scanButton.hidden=!api()?.scanRecovery||recoveryScan?.available===false;
    scanButton.disabled=recoveryScan?.state==='scanning'||
      recoveryScan?.loaded===true&&!recoveryScan?.pendingFolders&&recoveryScan?.state!=='failed';
    scanButton.textContent=recoveryScan?.state==='failed'?'Повторить поиск':
      recoveryScan?.pendingFolders?'Продолжить поиск':'Найти предыдущие результаты';
    const scanMessage=refreshError?'Не удалось обновить список. Уже показанные результаты сохранены. Нажмите «Обновить список».':
      recoveryScan?.state==='scanning'?'Ищем предыдущие результаты и проверяем целостность. Можно закрыть это окно; конвертация не запускается.':
      recoveryScan?.state==='failed'?'Поиск не завершён (RECOVERY_SCAN_FAILED). Доступные файлы не удалены. Можно повторить поиск.':
      recoveryScan?.pendingFolders?'Ожидают свободных слотов: '+recoveryScan.pendingFolders+'. Сохраните или удалите ненужные пакеты и продолжите поиск.':
      recoveryScan?.loaded?'Поиск предыдущих результатов завершён. Это не проверка качества конвертации.':
      'Старые папки автоматически не загружаются. Для проверки нажмите «Найти предыдущие результаты».';
    const cleanupMessage=storage?.cleanupRetryJobs?' · Очистка требует повторной попытки: '+storage.cleanupRetryJobs+'. Нажмите «Обновить список».':
      storage?.cleanupPending?' · Удаляем служебные папки: '+storage.cleanupPending+'. Место учитывается до завершения.':'';
    text('[data-g-scan-status]',scanMessage+cleanupMessage);""")
source=replace(source, "      jobs.some(job=>job.unsavedOutputs>0)?'Есть несохранённые результаты: '+jobs.reduce((sum,job)=>sum+(job.unsavedOutputs||0),0):warnings.length?'Проблемы восстановления предыдущих заданий: '+warnings.length:'Обработка завершена');", """      jobs.some(job=>!job.recovered&&job.unsavedOutputs>0)?'Есть текущие несохранённые результаты: '+jobs.filter(job=>!job.recovered).reduce((sum,job)=>sum+(job.unsavedOutputs||0),0):
      jobs.some(job=>job.recovered)?'Предыдущие результаты доступны в «Состоянии».':'Текущих несохранённых результатов нет.');""")
source=replace(source, '    refreshRevision++;refreshAgain=false;cancelPreview();',
    '    pageHidden=true;stopRecoveryPoll();refreshRevision++;refreshAgain=false;cancelPreview();')
p.write_text(source,encoding='utf-8')

p=Path('src/guard-queue.css')
with p.open('a',encoding='utf-8') as stream:
    stream.write('''
/* Inline listbox: never open a native popup beyond the State dialog. */
.aio-guard-popup select[data-g-jobs]{flex:1 1 100%;width:100%;min-width:0;max-width:100%;height:7rem;max-height:7rem;overflow:auto;box-sizing:border-box}
.aio-guard-popup select[data-g-jobs] option{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
[data-g-scan-status]{font-size:12px;line-height:1.45;overflow-wrap:anywhere;margin:6px 0}
''')

run(sys.executable,'-m','compileall','-q','app_backend','tests','tools/qa')
run('node','--check','src/guard-queue.js')
run('node','--check','src/converter-bridge.js')
for name in ('test_recovery_manual_scan.py','test_recovery_cleanup_responsiveness.py',
             'test_save_snapshot_report.py','test_boundaries_v21.py','test_comparison_recovery_v20.py'):
    run(sys.executable,'-m','unittest','discover','-s','tests','-p',name,'-v')
print('AIO_MANUAL_RECOVERY_TARGETED=PASS',flush=True)

paths = ['app_backend/converter_guard/core.py','app_backend/converter_guard/result_store.py',
    'app_backend/converter_guard/http_api.py','app_backend/converter_guard/recovery_control.py',
    'src/converter-bridge.js','src/guard-queue.js','src/guard-queue.css',
    'tests/test_recovery_manual_scan.py','tools/qa/recovery_manual_dom.mjs',
    'docs/review/AIO_MANUAL_RECOVERY_M02_M03_2026-10-29_RU.md']
api_url='https://api.github.com/repos/'+REPO

def api(path, body=None):
    request=urllib.request.Request(api_url+path,
        data=None if body is None else json.dumps(body).encode(),
        headers={'Authorization':'Bearer '+os.environ['GH_TOKEN'],
                 'Accept':'application/vnd.github+json','Content-Type':'application/json'},
        method='GET' if body is None else 'POST')
    with urllib.request.urlopen(request,timeout=30) as response:return json.load(response)

assert api('/git/ref/heads/fix/fulltest-critical-hosted-lo-20261029')['object']['sha']==BASE
entries=[]
for path in paths:
    blob=api('/git/blobs',{'content':Path(path).read_text(encoding='utf-8'),'encoding':'utf-8'})
    entries.append({'path':path,'mode':'100644','type':'blob','sha':blob['sha']})
tree=api('/git/trees',{'base_tree':TREE,'tree':entries})
commit=api('/git/commits',{'message':'fix(recovery): make old-result discovery explicit and nonblocking',
    'tree':tree['sha'],'parents':[BASE]})
print('AIO_CANDIDATE_COMMIT='+commit['sha'],flush=True)
print('AIO_CANDIDATE_PARENT='+BASE,flush=True)
print('AIO_REFS_UNCHANGED=true',flush=True)
