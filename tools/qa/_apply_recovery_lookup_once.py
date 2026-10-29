"""Follow-up source preparation: no ref writes, only a checked candidate."""
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request
ROOT=Path(__file__).resolve().parents[2];os.chdir(ROOT)
BASE='35c8636a9fdbedb05eb4e92ddb3e761b8081d1f9'
TREE='bad495d58c7f3cf335519fdadd11fea8ba07a2dc'

def edit(path,old,new,count=1):
    p=Path(path);text=p.read_text(encoding='utf-8');assert text.count(old)==count,(path,old)
    p.write_text(text.replace(old,new),encoding='utf-8')

def run(*args):subprocess.run(args,check=True)

env={**os.environ,'PYTHONPATH':str(ROOT)+os.pathsep+str(ROOT/'tests')}
proof=subprocess.run([sys.executable,'-m','unittest','test_recovery_current_lookup.CurrentRecoveryLookup.test_late_gui_get_never_triggers_full_recovery'],env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
print(proof.stdout,flush=True)
assert proof.returncode!=0 and 'LATE_LOOKUP_MUST_NOT_SCAN_DISK' in proof.stdout
print('AIO_BASELINE_REPRODUCED=late_status_and_report_start_recovery',flush=True)
core='app_backend/converter_guard/core.py'
edit(core,'''               result_delta=False):
        if job_id not in self._jobs:
            self._load_recovery()''','''               result_delta=False, recover=True):
        if type(recover) is not bool:
            raise GuardError('RECOVERY_REQUEST', 'Неверный режим поиска задания.')
        if recover and job_id not in self._jobs:
            self._load_recovery()''')
edit(core,'''        self._prune()
        with self._lock:
            job = self._get(job_id)
            if not include_trace''','''        self._prune(defer_cleanup=not recover)
        with self._lock:
            job = self._get(job_id)
            if not include_trace''')
edit(core,'''    def report(self, job_id):
        if job_id not in self._jobs:
            self._load_recovery()''','''    def report(self, job_id, *, recover=True):
        if type(recover) is not bool:
            raise GuardError('RECOVERY_REQUEST', 'Неверный режим поиска задания.')
        if recover and job_id not in self._jobs:
            self._load_recovery()''')
api_path='app_backend/converter_guard/http_api.py'
edit(api_path,'    def get(self, handler, path):','''    @staticmethod
    def _allow_recovery_lookup(handler):
        query = parse_qs(urlsplit(handler.path).query, keep_blank_values=True)
        if 'lookup' not in query:
            return True  # Legacy callers can explicitly look up retained jobs.
        if query['lookup'] != ['current']:
            raise GuardError('RECOVERY_REQUEST', 'Неверный режим поиска задания.')
        return False

    def get(self, handler, path):''')
edit(api_path,'                                          delta=delta, result_delta=result_delta)',
    '                                          delta=delta, result_delta=result_delta,\n                                          recover=self._allow_recovery_lookup(handler))')
edit(api_path,"self.guard.report(self._identity(self._query(handler, 'job'))))",
    "self.guard.report(self._identity(self._query(handler, 'job')),\n                    recover=self._allow_recovery_lookup(handler)))")
bridge='src/converter-bridge.js'
edit(bridge,"  async function api(path, body, method = 'POST', signal) {",'''  async function api(path, body, method = 'POST', signal) {
    // All job IDs used by this UI came from the live API. A late response for
    // a released ID must not start disk recovery as a side effect.
    if(method==='GET'&&(path.startsWith('/jobs?')||path.startsWith('/report?')))
      path+='&lookup=current';''')
edit(bridge,"encodeURIComponent(operation.jobId) + (full ? '' : '&delta=1&resultDelta=1')",
    "encodeURIComponent(operation.jobId) + '&lookup=current' + (full ? '' : '&delta=1&resultDelta=1')")
queue='src/guard-queue.js'
edit(queue,"'/api/converter-guard/jobs?job='+encodeURIComponent(id),",
    "'/api/converter-guard/jobs?job='+encodeURIComponent(id)+'&lookup=current',")
edit(queue,"'/api/converter-guard/report?job='+encodeURIComponent(path):",
    "'/api/converter-guard/report?job='+encodeURIComponent(path)+'&lookup=current':")
edit(queue,"      jobs.some(job=>job.recovered)?'Предыдущие результаты доступны в «Состоянии».':'Текущих несохранённых результатов нет.');",
    "      warnings.length?'Проблемы восстановления предыдущих заданий: '+warnings.length:\n      jobs.some(job=>job.recovered)?'Предыдущие результаты доступны в «Состоянии».':'Текущих несохранённых результатов нет.');")
dom='tools/qa/recovery_manual_dom.mjs'
edit(dom,"  fetch:async()=>({ok:true,json:async()=>({jobId:'old',files:[],finished:true})})};",'''  fetch:async url=>{
    assert.equal(new URL(url,'http://127.0.0.1').searchParams.get('lookup'),'current','preview uses current lookup');
    return {ok:true,json:async()=>({jobId:'old',files:[],finished:true})};
  }};''')
# The legacy modal test extracts only production handlers, not the entire UI.
# Load the new production timer helper and assert its effect, retaining every
# existing geometry, focus, row cleanup and stale-close assertion.
modal='tests/guard_state_modal_v16.mjs'
edit(modal,'class Element {', '''const pollStart=source.indexOf('  function stopRecoveryPoll(){');
const pollEnd=source.indexOf('\n',pollStart);
assert.ok(pollStart>0&&pollEnd>pollStart,'production polling cleanup helper must be found');
class Element {'''.replace("indexOf('\n',", "indexOf('\\n',"))
edit(modal,"const context=vm.createContext({bar,stateButton,Number,rowCache:{jobId:'fixture'},paintedRows:[{}]});",'''const cancelledTimers=[];let renders=0;
const context=vm.createContext({bar,stateButton,Number,rowCache:{jobId:'fixture'},paintedRows:[{}],
  recoveryTimer:1,clearTimeout(id){cancelledTimers.push(id);},render(){renders++;}});''')
edit(modal,"vm.runInContext(source.slice(helperStart,helperEnd)+'\\n'+source.slice(start,end),context);",
    "vm.runInContext(source.slice(pollStart,pollEnd)+'\\n'+source.slice(helperStart,helperEnd)+'\\n'+source.slice(start,end),context);")
edit(modal,"function open(){dialog.open=true;stateButton.expanded='true';context.rowCache={jobId:'fixture'};context.paintedRows=[{}];}",
    "function open(){dialog.open=true;stateButton.expanded='true';context.rowCache={jobId:'fixture'};context.paintedRows=[{}];context.recoveryTimer=cancelledTimers.length+1;}")
edit(modal,"assert.equal(rowCleanups,3,'all three real close paths release hidden task DOM');",'''assert.equal(rowCleanups,3,'all three real close paths release hidden task DOM');
assert.deepEqual(cancelledTimers,[1,2,3],'all real close paths cancel the production polling timer');
assert.equal(context.recoveryTimer,null);
assert.equal(renders,3,'closed visibility is refreshed');''')
edit(modal,"assert.equal(rowCleanups,3,'stale close must not dispose the reopened task page');",'''assert.equal(rowCleanups,3,'stale close must not dispose the reopened task page');
assert.deepEqual(cancelledTimers,[1,2,3],'stale close must not cancel reopened polling');
assert.equal(context.recoveryTimer,4);
assert.equal(renders,3,'stale close must not redraw a reopened view');''')
doc='docs/review/AIO_MANUAL_RECOVERY_M02_M03_2026-10-29_RU.md'
with Path(doc).open('a',encoding='utf-8') as stream:
    stream.write('''
## Дополнительный найденный обход

Поздний GET статуса/отчёта уже удалённого задания вызывал общий _load_recovery при неизвестном jobId. GUI теперь передаёт lookup=current во всех своих запросах jobs/report, включая polling, preview и выгрузку отчёта. Неизвестный ID получает 404 без обхода диска; существующий — прежний снимок/ETag. Legacy-вызовы без параметра сохраняют прямое восстановление. Неоднозначные/неизвестные lookup отвергаются до дисковой работы. Новый тест воспроизводит прежнее поведение; добавлены проверки токена нового POST, сохранения legacy, удаления, флагов, HTTP 304/ETag и отложенной уборки при status.

## Замечания первого полного прогона

Run 40914454046 на 35c8636 выявил потерю предупреждения Recovery в сводной строке и неполный контекст прежнего изолированного modal-теста. Предупреждение восстановлено в продукте; прежний recovery-UI тест не изменён. Modal-тест подключает настоящий новый stopRecoveryPoll из исходника и дополнительно проверяет отмену таймера при всех трёх закрытиях и отсутствие отмены при устаревшем close. Прежние проверки геометрии, фокуса и очистки строк сохранены. Это не отключение теста и не свидетельство GUI-приёмки.
''')
run(sys.executable,'-m','compileall','-q','app_backend','tests','tools/qa')
run('node','--check',bridge);run('node','--check',queue);run('node','--check',modal)
for name in ('test_recovery_current_lookup.py','test_recovery_manual_scan.py','test_recovery_cleanup_responsiveness.py','test_save_snapshot_report.py','test_boundaries_v21.py','test_comparison_recovery_v20.py','test_guard_recovery_ui_v22.py','test_guard_save_readback_v16.py'):
    run(sys.executable,'-m','unittest','discover','-s','tests','-p',name,'-v')
print('AIO_CURRENT_LOOKUP_TARGETED=PASS',flush=True)
paths=[core,api_path,bridge,queue,dom,doc,modal,'tests/test_recovery_current_lookup.py']
url='https://api.github.com/repos/jumonja-maker/A.I.O'
def api(path,body=None):
    request=urllib.request.Request(url+path,data=None if body is None else json.dumps(body).encode(),
      headers={'Authorization':'Bearer '+os.environ['GH_TOKEN'],'Accept':'application/vnd.github+json','Content-Type':'application/json'},method='GET' if body is None else 'POST')
    with urllib.request.urlopen(request,timeout=30) as response:return json.load(response)
assert api('/git/ref/heads/fix/fulltest-critical-hosted-lo-20261029')['object']['sha']==BASE
entries=[]
for path in paths:
    blob=api('/git/blobs',{'content':Path(path).read_text(encoding='utf-8'),'encoding':'utf-8'})
    entries.append({'path':path,'mode':'100644','type':'blob','sha':blob['sha']})
tree=api('/git/trees',{'base_tree':TREE,'tree':entries})
commit=api('/git/commits',{'message':'fix(recovery): isolate late lookups and retain warning and modal lifecycle guarantees','tree':tree['sha'],'parents':[BASE]})
print('AIO_CANDIDATE_COMMIT='+commit['sha'],flush=True)
print('AIO_CANDIDATE_PARENT='+BASE,flush=True)
print('AIO_REFS_UNCHANGED=true',flush=True)
