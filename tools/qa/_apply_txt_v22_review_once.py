"""One-shot bounded patch application/publication for the isolated v22 review.

Only three exact reviewed base blobs are edited. Publication is gated by the
recorded native/source QA and may advance only the literal v22 feature branch.
No main/QA ref, user result folder or installed application is written.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen

ROOT=Path(__file__).resolve().parents[2]
REPO='jumonja-maker/A.I.O'
BRANCH='fix/guard-v22-writer-txt-tail-recovery-ui-20261026'
BASE='6062eb717095cf009d96603e7b6fbcd370b1f81d'
BASE_BLOBS={
 'app_backend/converter_guard/adapter.py':'15ee3c921127a412ca3520f0c61590a3ea57b717',
 'app_backend/converter_guard/export_recipe.py':'8b9e61d3b06bdda58a0a065c01b757e22b6d1ced',
 'src/guard-queue.js':'3d3e7fc30d94c3e9a798a0f9dd7aa852043a24c1',
}
NEW_PRODUCT='app_backend/converter_guard/writer_text_tail.py'


def git_blob(data):return hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def replace(text, old, new):
    if text.count(old)!=1:raise RuntimeError('Ambiguous patch anchor: '+old[:100])
    return text.replace(old,new,1)


def apply(evidence):
    evidence.mkdir(parents=True,exist_ok=False)
    originals={}
    for name,expected in BASE_BLOBS.items():
        data=(ROOT/name).read_bytes()
        if git_blob(data)!=expected:raise RuntimeError('Reviewed base blob changed: '+name)
        originals[name]=data.decode('utf-8')
    text=originals['app_backend/converter_guard/adapter.py']
    text=replace(text,'from .errors import GuardError\n',
        'from .errors import GuardError\nfrom .writer_text_tail import TEXT_FILTERS, prepare_writer_text_source\n')
    text=replace(text,'''        try:
            native = self._registry()
            retry_recipe = getattr(self._local, 'retry_recipe', None)''', '''        preparation_scope = ExitStack()
        try:
            native = self._registry()
            retry_recipe = getattr(self._local, 'retry_recipe', None)''')
    text=replace(text,"            export_args = {'convert_spec': recipe.cli_spec(target)} if recipe else {}\n",'''            export_args = {'convert_spec': recipe.cli_spec(target)} if recipe else {}
            # Keep the original native importer/exporter and original bytes.
            # Only a confirmed Writer plain-text recipe gets a disposable
            # terminal paragraph; PDF/Office/Calc routes are untouched.
            if recipe and recipe.filter_name in TEXT_FILTERS:
                source, text_preparation = preparation_scope.enter_context(
                    prepare_writer_text_source(source, workspace / 'writer-text-input', cancel=control))
                if text_preparation:
                    self._local.export_info['sourcePreparation'] = text_preparation
                    self._local.export_info['warnings'] = tuple(dict.fromkeys(
                        (*self._local.export_info.get('warnings', ()),
                         *text_preparation['warnings'])))
''')
    text=replace(text,'''        finally:
            if self._local.metrics is not None:
                self._local.metrics['adapterMs']''', '''        finally:
            preparation_scope.close()
            if self._local.metrics is not None:
                self._local.metrics['adapterMs']''')
    patched={'app_backend/converter_guard/adapter.py':text}
    text=originals['app_backend/converter_guard/export_recipe.py']
    text=replace(text,'WARNING_TEXT = {\n', '''WARNING_TEXT = {
    'NATIVE_WRITER_TEXT_TAIL_PARAGRAPH': (
        'Перед текстовым экспортом Writer во временную копию OOXML добавлен один пустой '
        'абзац после конечной таблицы: это предотвращает повтор последней ячейки в LibreOffice. '
        'Исходный файл и содержимое существующих частей не изменены. '
        'TXT остаётся обычным текстом; оформление и изображения не сохраняются.'),
''')
    patched['app_backend/converter_guard/export_recipe.py']=text
    text=originals['src/guard-queue.js']
    text=replace(text,'  let refreshRevision=0, refreshAgain=false;\n',
                 '  let refreshRevision=0, refreshAgain=false;\n  let renderedRecoveryWarnings=null;\n')
    anchor='  const api=()=>window.AllInOneBridge?.converter;\n'
    addition='''  const recoveryReasons={
    RECOVERY_UNFINISHED_PREPARATION:'Осталась незавершённая подготовка. Готовые результаты не подтверждены.',
    RECOVERY_MANIFEST_MISSING:'Не найдено описание задания job.json.',
    RECOVERY_LEDGER_INCOMPLETE:'Журнал результатов неполон. Полный состав результатов не подтверждён.',
    RECOVERY_METADATA_INVALID:'Служебные сведения задания не удалось прочитать или проверить.',
    RECOVERY_OUTPUT_MISSING:'Ожидаемый выходной файл отсутствует или изменён.',
    RECOVERY_UNTRACKED_OUTPUTS:'Найдены файлы без подтверждённой записи в журнале.',
    RECOVERY_RETIREMENT_INVALID:'Маркер удаления не прошёл проверку. Каталог не удалён.',
    RECOVERY_CLEANUP_RETRY:'Не завершилась очистка ранее удалённого задания.',
    RECOVERY_SALVAGED_UNVERIFIED:'Найдены файлы, но полнота и связь с исходниками не подтверждены.',
    RECOVERY_WRITE_FAILED:'Запись служебного журнала не завершилась.'
  };
  function renderRecoveryWarnings(){
    const box=bar?.querySelector('[data-g-recovery]');
    if(!box||renderedRecoveryWarnings===warnings)return;
    renderedRecoveryWarnings=warnings;box.hidden=!warnings.length;
    const list=box.querySelector('[data-g-recovery-list]');list.replaceChildren();
    box.querySelector('summary').textContent='Подробности восстановления: '+warnings.length;
    for(const warning of warnings.slice(0,40)){
      const row=document.createElement('p');row.setAttribute('data-g-recovery-entry','');
      const code=typeof warning?.code==='string'?warning.code.slice(0,96):'RECOVERY_UNKNOWN';
      const folder=typeof warning?.folder==='string'?warning.folder.slice(0,160):'Задание не определено';
      const integrity=typeof warning?.integrity==='string'?' · '+warning.integrity.slice(0,64):'';
      row.textContent=folder+' · '+code+integrity+' — '+
        (recoveryReasons[code]||'Причина требует проверки. Сохраните технический отчёт.');
      list.append(row);
    }
    if(warnings.length>40){const note=document.createElement('p');
      note.textContent='Ещё предупреждений: '+(warnings.length-40)+'. Полный список — в техническом отчёте.';list.append(note);}
  }
'''
    text=replace(text,anchor,addition+anchor)
    text=replace(text,'    root=next;rowCache=paintedRows=null;\n',
                 '    root=next;rowCache=paintedRows=null;renderedRecoveryWarnings=null;\n')
    old='''      '<div data-g-counts></div><div class="aio-guard-warning" data-g-warning></div>'+'''
    new=old+'''
      '<details data-g-recovery hidden><summary>Подробности восстановления</summary><div data-g-recovery-list style="max-height:12rem;overflow:auto;overflow-wrap:anywhere"></div></details>'+'''
    text=replace(text,old,new)
    text=replace(text,'    renderCurrent();\n    text(\'[data-g-counts]\'',
                 '    renderCurrent();renderRecoveryWarnings();\n    text(\'[data-g-counts]\'')
    text=replace(text,"    if(warnings.length)text('[data-g-warning]','Не удалось прочитать журнал '+warnings.length+' заданий. Файлы не удалены. Сохраните технический отчёт для восстановления.');",
                 "    if(warnings.length)text('[data-g-warning]','Проблемы восстановления: '+warnings.length+' предупреждений. Причины перечислены ниже; это не подтверждение повреждения документов.');")
    text=replace(text,"warnings.length?'Есть результаты с повреждённым журналом'",
                 "warnings.length?'Проблемы восстановления предыдущих заданий: '+warnings.length")
    text=replace(text,'state=null;jobs=[];warnings=[];storage=null;\n',
                 'state=null;jobs=[];warnings=[];storage=null;renderedRecoveryWarnings=null;\n')
    patched['src/guard-queue.js']=text
    # No partial file publication if even one anchor or base blob differs.
    for name,text in patched.items():(ROOT/name).write_text(text,encoding='utf-8',newline='')
    seals={name:digest(ROOT/name) for name in (*BASE_BLOBS,NEW_PRODUCT)}
    (evidence/'source-seal.json').write_text(json.dumps({'base':BASE,'sha256':seals},indent=2),encoding='utf-8')
    print('PATCH_APPLIED='+json.dumps(seals),flush=True)


def publish(evidence):
    if os.environ.get('GITHUB_REPOSITORY')!=REPO:raise RuntimeError('Wrong repository')
    expected=os.environ['AIO_PATCH_HEAD']
    if len(expected)!=40 or any(c not in '0123456789abcdef' for c in expected):raise ValueError('Bad source SHA')
    seal=json.loads((evidence/'source-seal.json').read_text())
    if seal['base']!=BASE:raise RuntimeError('Wrong patch base')
    for name,sha in seal['sha256'].items():
        if digest(ROOT/name)!=sha:raise RuntimeError('Tested source changed: '+name)
    artifact_paths={
      'windows-unit.json':evidence/'unit.json',
      'windows-native-txt.json':evidence/'native-txt'/'evidence.json',
      'windows-native-v20.json':evidence/'native-v20'/'evidence.json',
      'windows-native-v08.json':evidence/'native-v08'/'evidence.json',
    }
    payloads={}
    for name,path in artifact_paths.items():
        raw=path.read_bytes();value=json.loads(raw)
        if value.get('status')!='PASS':raise RuntimeError('Missing PASS gate: '+name)
        if len(raw)>1024*1024:raise RuntimeError('Evidence budget exceeded')
        payloads['docs/review/evidence/v22/'+name]=raw.decode('utf-8')
    payloads['docs/review/evidence/v22/source-seal.json']=(evidence/'source-seal.json').read_text()
    acceptance={'status':'PASS_TARGETED_QA','base':BASE,'reviewHead':expected,
      'windowsRun':int(os.environ['GITHUB_RUN_ID']),'productSHA256':seal['sha256'],
      'fulltest':'NOT_RUN','newInstaller':'NOT_RUN','mainChanged':False,
      'scope':'18 preparation units + recovery DOM + full source regression + real native TXT/CLI/v20/v08 gates'}
    payloads['docs/review/evidence/v22/acceptance.json']=json.dumps(acceptance,ensure_ascii=False,indent=2)+'\n'
    for name in BASE_BLOBS:payloads[name]=(ROOT/name).read_text(encoding='utf-8')
    token=os.environ['GITHUB_TOKEN']
    def api(method,path,data=None):
        raw=None if data is None else json.dumps(data,ensure_ascii=False).encode('utf-8')
        request=Request('https://api.github.com/repos/'+REPO+path,data=raw,method=method,
            headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json',
                     'Content-Type':'application/json','X-GitHub-Api-Version':'2022-11-28'})
        with urlopen(request,timeout=30) as response:return json.load(response)
    ref='/git/refs/heads/'+BRANCH
    if api('GET','/git/ref/heads/'+BRANCH)['object']['sha']!=expected:
        raise RuntimeError('Feature branch advanced; refusing to overwrite it')
    parent=api('GET','/git/commits/'+expected)
    tree=api('POST','/git/trees',{'base_tree':parent['tree']['sha'],'tree':[
        {'path':name,'mode':'100644','type':'blob','content':text} for name,text in payloads.items()]})
    commit=api('POST','/git/commits',{'message':'fix: prevent terminal-table Writer TXT loop and clarify recovery warnings; Windows QA passed',
                                   'tree':tree['sha'],'parents':[expected]})
    if api('GET','/git/ref/heads/'+BRANCH)['object']['sha']!=expected:
        raise RuntimeError('Feature branch advanced before publication')
    api('PATCH',ref,{'sha':commit['sha'],'force':False})
    print('PUBLISHED_V22_PATCH_SHA='+commit['sha'],flush=True)
    print('PUBLISHED_V22_TREE='+tree['sha'],flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('mode',choices=['apply','publish']);parser.add_argument('--evidence',type=Path,required=True)
    args=parser.parse_args();(apply if args.mode=='apply' else publish)(args.evidence)
