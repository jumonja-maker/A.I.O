"""One-off source preparation. Candidate only; never moves a branch or ships."""
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request

BASE = '1646af04841e409079d1bb52150ff10670dc31f1'
REPO = 'jumonja-maker/A.I.O'
BASE_TREE = '293ef48e697a8ec66eabf05d7c3dde6a92e976e5'
PRODUCT = ['app_backend/converter_guard/recovery_scan.py',
           'app_backend/converter_guard/core.py', 'app_backend/converter_guard/http_api.py',
           'src/converter-bridge.js', 'src/guard-queue.js', 'src/guard-queue.css',
           'tests/test_recovery_scan.py', 'tests/test_recovery_scan_dom.py',
           'tests/recovery_scan_dom.mjs',
           'docs/review/AIO_RECOVERY_SCAN_M02_M03_2026-10-29_RU.md']


def once(text, old, new):
    if text.count(old) != 1:
        raise RuntimeError('Non-unique source anchor: ' + repr(old[:120]))
    return text.replace(old, new)


def check(*args):
    subprocess.run(args, check=True, timeout=240)


baseline = subprocess.run([sys.executable, '-m', 'unittest',
    'test_recovery_scan.RecoveryScanTests.test_http_background_returns_before_scan', '-v'],
    capture_output=True, text=True, timeout=30)
print(baseline.stdout, baseline.stderr, flush=True)
if baseline.returncode == 0 or 'HTTP waited for the recovery scan' not in baseline.stderr:
    raise RuntimeError('Baseline responsiveness defect was not reproduced')
print('AIO_BASELINE_REPRODUCED=recovery_http_waits_for_disk', flush=True)

path = Path('app_backend/converter_guard/core.py'); text = path.read_text(encoding='utf-8')
text = once(text, '        self._recovery_lock = threading.Lock()\n', '''        self._recovery_lock = threading.Lock()
        from .recovery_scan import RecoveryScan
        self._recovery_scanner = RecoveryScan(self._load_recovery, self._recovery_scan_plan, self._lock)

    def _recovery_scan_plan(self):
        # Called under the state RLock: metadata only, no filesystem operations.
        if self._closed:
            return 'closed'
        if not self._recovery_loaded:
            return 'pending'
        if self._recovery_pending:
            return 'pending' if len(self._jobs) < self.limits.max_jobs else 'waiting_for_slot'
        return 'complete'
''')
text = once(text, '''        # Excel/startup does no recovery scan. The first explicit recovery or
        # retained-job lookup runs on its HTTP caller, never the Qt UI thread.''', '''        # Excel/startup does no recovery scan. The UI opts into one dedicated
        # worker; legacy/direct lookups keep their synchronous validation contract.''')
text = once(text, '''        for root in roots:
            with self._lock:
''', '''        for root in roots:
            if self._recovery_scanner.cancel.is_set():
                return  # Shutdown preserves every unprocessed directory.
            with self._lock:
''')
text = once(text, 'actual = sha256_stream(restored, cancel=threading.Event())',
            'actual = sha256_stream(restored, cancel=self._recovery_scanner.cancel)')
text = once(text, '''                    if self._closed or identity in self._jobs:
                        store.close()
                        continue
                    self._jobs[identity] = job''', '''                    if self._closed or identity in self._jobs:
                        store.close()
                        continue
                    # A live conversion can claim the last slot while disk
                    # validation runs. Defer this intact root, never over-admit.
                    if len(self._jobs) >= self.limits.max_jobs:
                        store.close()
                        if root not in self._recovery_pending:
                            self._recovery_pending.append(root)
                        continue
                    self._jobs[identity] = job''')
text = once(text, '''                store.close()  # Never delete an unreadable recovery directory.
                self._salvage_orphan(root)''', '''                store.close()  # Never delete an unreadable recovery directory.
                if self._recovery_scanner.cancel.is_set():
                    return  # Cancellation is not corruption and must not salvage.
                self._salvage_orphan(root)''')
text = once(text, '''                if identity in self._jobs or self._closed:
                    store.close()
                    return
                self._jobs[identity] = job''', '''                if identity in self._jobs or self._closed:
                    store.close()
                    return
                if len(self._jobs) >= self.limits.max_jobs:
                    store.close()
                    if root not in self._recovery_pending:
                        self._recovery_pending.append(root)
                    return
                self._jobs[identity] = job''')
text = once(text, '''    def recoverable(self):
        self._prune(reap_saved=True)
        self._load_recovery()
        with self._lock:
            return {'storage': self.storage_stats(),''', '''    def recoverable(self, *, background=False, retry_scan=False):
        if type(background) is not bool or type(retry_scan) is not bool:
            raise GuardError('RECOVERY_REQUEST', 'Некорректный режим поиска результатов.')
        self._prune(reap_saved=True, defer_cleanup=background)
        if background:
            self._recovery_scanner.request(retry=retry_scan)
        else:
            self._load_recovery()
        with self._lock:
            scan = {'recoveryScan': self._recovery_scanner.snapshot()} if background else {}
            return {**scan, 'storage': self.storage_stats(),''')
text = once(text, '    def _prune(self, *, reap_saved=False):',
            '    def _prune(self, *, reap_saved=False, defer_cleanup=False):')
text = once(text, '''            for job in expired:
                job.released = True
                job.cancel.set()''', '''            for job in expired:
                job.released = True
                job.cleanup_background = job.cleanup_background or defer_cleanup
                job.cancel.set()''')
text = once(text, '''    def close(self):
        with self._lock:
            self._closed = True''', '''    def close(self):
        with self._lock:
            self._closed = True
            self._recovery_scanner.cancel.set()''')
text = once(text, '''        self._executor.shutdown(wait=True)
        with self._lock:''', '''        self._executor.shutdown(wait=True)
        self._recovery_scanner.close()
        with self._lock:''')
path.write_text(text, encoding='utf-8')

path = Path('app_backend/converter_guard/http_api.py'); text = path.read_text(encoding='utf-8')
text = once(text, '''            if path == PREFIX + '/recoverable':
                return self._json(handler,200, self.guard.recoverable())''', '''            if path == PREFIX + '/recoverable':
                query = parse_qs(urlsplit(handler.path).query, keep_blank_values=True)
                for flag in ('background', 'retry'):
                    if flag in query and query[flag] not in (['0'], ['1']):
                        raise GuardError('RECOVERY_REQUEST', 'Некорректный режим поиска результатов.')
                if query.get('background') == ['1']:
                    return self._json(handler,200, self.guard.recoverable(
                        background=True, retry_scan=query.get('retry') == ['1']))
                return self._json(handler,200, self.guard.recoverable())''')
path.write_text(text, encoding='utf-8')

path = Path('src/converter-bridge.js'); text = path.read_text(encoding='utf-8')
text = once(text, "    async recoverable() {return api('/recoverable',undefined,'GET');},", """    async recoverable({retry=false}={}) {
      return api('/recoverable?background=1'+(retry===true?'&retry=1':''),undefined,'GET');
    },""")
path.write_text(text, encoding='utf-8')

path = Path('src/guard-queue.js'); text = path.read_text(encoding='utf-8')
text = once(text, '  let renderedRecoveryWarnings=null;', '''  let renderedRecoveryWarnings=null;
  let recoveryScan=null,recoveryPoll=null,refreshRetry=false;
  function stopRecoveryPoll(){if(recoveryPoll!==null){clearTimeout(recoveryPoll);recoveryPoll=null;}}
  function scheduleRecoveryPoll(){
    stopRecoveryPoll();
    if(recoveryScan?.state==='scanning'&&bar?.isConnected&&bar.querySelector('.aio-guard-popup')?.open)
      recoveryPoll=setTimeout(()=>{recoveryPoll=null;refresh();},750);
  }
  function renderRecoveryScan(){
    const box=bar?.querySelector('[data-g-recovery-scan]');if(!box)return;
    const kind=recoveryScan?.state;
    box.hidden=!kind||kind==='complete'||kind==='closed';
    const message=kind==='error'
      ?'Поиск результатов не завершён. Файлы не удалены; повторите проверку.'
      :kind==='waiting_for_slot'?'Остальные папки ожидают свободного слота Guard.'
      :'Проверяем результаты предыдущих запусков. Уже проверенные пакеты доступны ниже.';
    text('[data-g-recovery-progress]',message);
    box.querySelector('[data-g-recovery-retry]').hidden=kind!=='error';
  }''')
text = once(text, '''    if(!next){cancelPreview();dropRows();stateButton?.remove();stateButton=null;root=bar=null;if(clock){clearInterval(clock);clock=null;}return;}''', '''    if(!next){if(root)refreshRevision++;stopRecoveryPoll();recoveryScan=null;cancelPreview();dropRows();stateButton?.remove();stateButton=null;root=bar=null;if(clock){clearInterval(clock);clock=null;}return;}''')
text = once(text, '''    root=next;rowCache=paintedRows=null;renderedRecoveryWarnings=null;''', '''    refreshRevision++;stopRecoveryPoll();recoveryScan=null;
    root=next;rowCache=paintedRows=null;renderedRecoveryWarnings=null;''')
text = once(text, '''      '<div data-g-counts></div><div class="aio-guard-warning" data-g-warning></div>'+''', '''      '<div data-g-counts></div><div class="aio-guard-warning" data-g-warning></div>'+
      '<div class="aio-guard-recovery-scan" data-g-recovery-scan role="status" aria-live="polite" hidden><span data-g-recovery-progress></span><button type="button" data-g-recovery-retry hidden>Повторить поиск</button></div>'+''')
text = once(text, "    footer.append(bar);attachTopButton();", """    footer.append(bar);attachTopButton();
    bar.querySelector('[data-g-recovery-retry]').addEventListener('click',()=>refresh({retry:true}));""")
text = once(text, '''      if(dialog.open)return;
      dropRows();''', '''      if(dialog.open)return;
      stopRecoveryPoll();dropRows();''')
start=text.index('  async function refresh() {'); end=text.index('  function render() {',start)
text=text[:start]+'''  async function refresh({retry=false}={}) {
    if(!bar||!api()?.recoverable)return;
    if(refreshPending){refreshAgain=true;refreshRetry=refreshRetry||retry===true;return;}
    stopRecoveryPoll();refreshPending=true;const revision=refreshRevision,view=bar;
    try{const found=await api().recoverable({retry:retry===true});
      if(revision!==refreshRevision||bar!==view)return;
      jobs=found.jobs||[];warnings=found.warnings||[];storage=found.storage||null;
      recoveryScan=found.recoveryScan||null;render();
    }catch(_){
      if(revision===refreshRevision&&bar===view){
        recoveryScan={state:'error',code:'RECOVERY_TRANSPORT_FAILED'};render();
      }
    }finally{
      refreshPending=false;
      if(refreshAgain){const retryNext=refreshRetry;refreshAgain=refreshRetry=false;refresh({retry:retryNext});}
      else if(revision===refreshRevision&&bar===view)scheduleRecoveryPoll();
    }
  }
'''+text[end:]
text = once(text, '    bar.hidden=!state&&!jobs.length&&!warnings.length;', '''    bar.hidden=!state&&!jobs.length&&!warnings.length&&
      !['pending','scanning','error','waiting_for_slot'].includes(recoveryScan?.state);''')
text = once(text, '    renderCurrent();renderRecoveryWarnings();', '    renderCurrent();renderRecoveryWarnings();renderRecoveryScan();')
text = once(text, '        option.title=job.jobId;select.append(option);', "        option.title=(job.label||'Результаты')+' · '+job.jobId;select.append(option);")
text = once(text, '''    actionFeedback=null;
    const active=state?.currentTask;''', '''    actionFeedback=null;
    if(!state&&['pending','scanning','error'].includes(recoveryScan?.state)){
      text('[data-g-current]',recoveryScan.state==='error'?'Поиск предыдущих результатов не завершён':'Проверяем предыдущие результаты…');return;
    }
    const active=state?.currentTask;''')
text = once(text, '''    refreshRevision++;refreshAgain=false;cancelPreview();dropRows();state=null;jobs=[];warnings=[];storage=null;renderedRecoveryWarnings=null;''', '''    refreshRevision++;refreshAgain=refreshRetry=false;stopRecoveryPoll();recoveryScan=null;cancelPreview();dropRows();state=null;jobs=[];warnings=[];storage=null;renderedRecoveryWarnings=null;''')
path.write_text(text, encoding='utf-8')

path=Path('src/guard-queue.css')
path.write_text(path.read_text(encoding='utf-8')+'''
/* Recovery rows must shrink with the dialog, including long source names. */
.aio-guard-save{min-width:0}
.aio-guard-save select{flex:1 1 180px;min-width:0;width:0;max-width:100%;text-overflow:ellipsis}
.aio-guard-recovery-scan{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:8px 0;overflow-wrap:anywhere}
.aio-guard-recovery-scan[hidden]{display:none}
.aio-guard-recovery-scan span{flex:1;min-width:0}
''', encoding='utf-8')

check(sys.executable, '-m', 'compileall', '-q', 'app_backend', 'tests')
check('node', '--check', 'src/guard-queue.js')
check('node', '--check', 'src/converter-bridge.js')
check(sys.executable, '-m', 'unittest', 'test_recovery_scan', 'test_recovery_cleanup_responsiveness',
      'test_save_snapshot_report', 'test_guard_save_queue_v09', 'test_guard_save_readback_v16',
      'test_comparison_recovery_v20', 'test_boundaries_v21', 'test_recovery_scan_dom', '-v')


def github(path, data=None):
    payload = None if data is None else json.dumps(data).encode('utf-8')
    request = urllib.request.Request('https://api.github.com/repos/'+REPO+'/'+path,
        data=payload, headers={'Authorization':'Bearer '+os.environ['GH_TOKEN'],
            'Accept':'application/vnd.github+json', 'Content-Type':'application/json',
            'X-GitHub-Api-Version':'2022-11-28'})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)

if github('git/ref/heads/fix/fulltest-critical-hosted-lo-20261029')['object']['sha'] != BASE:
    raise RuntimeError('QA branch moved; candidate cannot use a stale base')
entries=[]
for filename in PRODUCT:
    content=Path(filename).read_text(encoding='utf-8')
    blob=github('git/blobs', {'content':content,'encoding':'utf-8'})
    entries.append({'path':filename,'mode':'100644','type':'blob','sha':blob['sha']})
tree=github('git/trees', {'base_tree':BASE_TREE,'tree':entries})['sha']
commit=github('git/commits', {'message':'fix(recovery): nonblocking validated discovery and bounded recovery UI',
    'tree':tree,'parents':[BASE]})['sha']
print('AIO_CANDIDATE_COMMIT='+commit, flush=True)
print('AIO_CANDIDATE_PARENT='+BASE, flush=True)
print('AIO_REFS_UNCHANGED=true', flush=True)
