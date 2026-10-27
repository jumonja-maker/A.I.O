"""One-shot B28 patch application and post-QA publication to its literal feature branch.

No main/ref rewrite, installation, release, user-data deletion or background service.
"""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import urllib.request

BASE='b47578a6a401fef2ae20499748926b7ce7977a6e'
BRANCH='fix/guard-v23-b28-dif-content-20261027'
ROOT=Path(__file__).resolve().parents[2]
NEW_FILES=('app_backend/converter_guard/dif_table_contract.py','tests/b28_dif_fixtures.py',
 'tests/test_dif_table_contract_v23.py','tools/qa/guard_b28_dif_native_acceptance.py',
 'docs/review/AIO_B28_DIF_V23_2026-10-27_RU.md')
BLOBS={'app_backend/converter_guard/adapter.py':'8cc08a0e0f95cddbad71461750bcbde79a6c542e',
       'app_backend/converter_guard/export_recipe.py':'8c065113f91f272883680de21691e21b2e834350'}


def replace_one(text,old,new):
    if text.count(old)!=1:raise ValueError('source replacement anchor changed')
    return text.replace(old,new,1)


def apply():
    texts={}
    for name,expected in BLOBS.items():
        raw=(ROOT/name).read_bytes()
        actual=hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
        if actual!=expected:raise ValueError('reviewed base blob mismatch:'+name)
        texts[name]=raw.decode('utf-8')
    name='app_backend/converter_guard/adapter.py';text=texts[name]
    old="""            filtered = getattr(self._local, 'import_filter', None)
            filter_opts = getattr(self._local, 'import_options', None)
            if filtered and filter_opts:"""
    new="""            filtered = getattr(self._local, 'import_filter', None)
            filter_opts = getattr(self._local, 'import_options', None)
            # A live UNO session does not imply this export uses UNO: ambiguous
            # export recipes and launch-only fallback arrive here too. Protect
            # the ACTUAL CLI boundary, not just the earlier session predicate.
            from .dif_evidence import is_dif_source
            if filtered == 'DIF' and is_dif_source(source):
                from .dif_table_contract import check_dif_table
                original_dif = source
                source, summary = self._prepare_dif_snapshot(
                    original_dif, workspace / 'dif-cli-input', cancel=control)
                table_check = check_dif_table([source], 'fods', original_dif, cancel=control)
                self._local.export_info['sourcePreparation'] = {**summary, 'tableCheck': table_check}
                self._local.export_info['resultChecks'] = list(
                    self._local.export_info.get('resultChecks', ())) + [summary['numericCheck']] + (
                        [table_check] if table_check else [])
                warnings = tuple(self._local.export_info.get('warnings', ())) + tuple(summary.get('warnings', ()))
                if table_check and table_check.get('status') == 'not_checked':
                    warnings += ('DIF_TABLE_CONTENT_NOT_CHECKED',)
                self._local.export_info['warnings'] = tuple(dict.fromkeys(warnings))
                self._local.export_info['difCliSnapshot'] = True
                filtered, filter_opts = 'OpenDocument Spreadsheet Flat XML', None
            if filtered and filter_opts:"""
    text=replace_one(text,old,new)
    old="""        return paths

    def _prepare_dif_snapshot"""
    new="""        # Mandatory DIF cell contract, independent from optional quality
        # comparison. It checks the original against the final native result.
        from .dif_table_contract import check_dif_table
        table_check = check_dif_table(paths, target, source, cancel=identity_control)
        if table_check:
            self._local.export_info['resultChecks'] = list(
                self._local.export_info.get('resultChecks', ())) + [table_check]
            if table_check.get('status') == 'not_checked':
                self._local.export_info['warnings'] = tuple(dict.fromkeys((
                    *self._local.export_info.get('warnings', ()), 'DIF_TABLE_CONTENT_NOT_CHECKED')))
        return paths

    def _prepare_dif_snapshot"""
    texts[name]=replace_one(text,old,new)
    name='app_backend/converter_guard/export_recipe.py'
    texts[name]=replace_one(texts[name],'WARNING_TEXT = {',"""WARNING_TEXT = {
    'DIF_TABLE_CONTENT_NOT_CHECKED': (
        'Полная проверка ячеек DIF для этого результата не завершена: неподдержанная '
        'грамматика, сопоставление таблиц либо бюджет проверки. Это не доказательство '
        'повреждения и не подтверждение сохранности всех значений. Причина указана в отчёте.'),""")
    for name,text in texts.items():ast.parse(text,filename=name)
    for name,text in texts.items():(ROOT/name).write_text(text,encoding='utf-8',newline='\n')
    seal={'base':BASE,'scope':'B28 product bytes prior to Windows acceptance',
          'files':{p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in (*BLOBS,*NEW_FILES)}}
    out=ROOT/'docs/review/evidence/b28/source-seal.json';out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(seal,ensure_ascii=False,indent=2),encoding='utf-8')
    print('B28_STAGED_SOURCE_SEAL='+json.dumps(seal),flush=True)


def publish(evidence):
    evidence=Path(evidence)
    unit=json.loads((evidence/'unit.json').read_text(encoding='utf-8-sig'))
    native=json.loads((evidence/'b28-native/evidence.json').read_text(encoding='utf-8-sig'))
    baseline=json.loads((evidence/'b28-baseline/evidence.json').read_text(encoding='utf-8-sig'))
    if unit.get('status')!='PASS' or unit.get('failed') or unit.get('errors'):raise ValueError('source gate failed')
    if native.get('status')!='PASS' or native.get('passed')!=48 or native.get('failed') or not native.get('sourceBytesUnchanged'):raise ValueError('native 48 case gate failed')
    if not (evidence/'other-gates-pass.txt').is_file():raise ValueError('other native gates missing')
    seal_path=ROOT/'docs/review/evidence/b28/source-seal.json'
    seal=json.loads(seal_path.read_text(encoding='utf-8'))
    for path,value in seal['files'].items():
        if hashlib.sha256((ROOT/path).read_bytes()).hexdigest()!=value:raise ValueError('tested bytes changed:'+path)
    output=ROOT/'docs/review/evidence/b28'
    for name,value in [('windows-unit.json',unit),('windows-b28-native.json',native),('windows-b28-baseline.json',baseline)]:
        (output/name).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    acceptance={'status':'PASS_TARGETED_QA','base':BASE,'workflowHead':os.environ['B28_EXPECTED_HEAD'],
                'windowsRun':int(os.environ['GITHUB_RUN_ID']),'sourceSeal':seal['files'],
                'fulltest':'NOT_RUN','installer':'NOT_RUN','mainChanged':False,'scope':'B28 only, full source and native gates'}
    (output/'acceptance.json').write_text(json.dumps(acceptance,ensure_ascii=False,indent=2),encoding='utf-8')
    allowed=(*BLOBS,*NEW_FILES,*(str(p.relative_to(ROOT)).replace('\\','/') for p in output.glob('*.json')))
    expected=os.environ['B28_EXPECTED_HEAD']
    if os.environ.get('GITHUB_REPOSITORY')!='jumonja-maker/A.I.O':raise ValueError('wrong repository')
    token=os.environ['B28_GITHUB_TOKEN'];endpoint='https://api.github.com/repos/jumonja-maker/A.I.O/'
    def api(path,value=None,method=None):
        req=urllib.request.Request(endpoint+path, data=None if value is None else json.dumps(value).encode(),
            method=method or ('GET' if value is None else 'POST'),
            headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json','Content-Type':'application/json','X-GitHub-Api-Version':'2022-11-28','User-Agent':'AIO-B28-QA'})
        with urllib.request.urlopen(req,timeout=60) as response:return json.load(response)
    ref='git/refs/heads/'+BRANCH
    if api(ref)['object']['sha']!=expected:raise ValueError('feature head advanced; do not overwrite')
    base_tree=api('git/commits/'+expected)['tree']['sha'];entries=[]
    for name in dict.fromkeys(allowed):
        content=(ROOT/name).read_text(encoding='utf-8')
        blob=api('git/blobs',{'content':content,'encoding':'utf-8'})['sha']
        entries.append({'path':name,'mode':'100644','type':'blob','sha':blob})
    tree=api('git/trees',{'base_tree':base_tree,'tree':entries})['sha']
    commit=api('git/commits',{'message':'fix(guard): protect actual DIF CLI boundary and reject silent cell loss (B28)\n\nExact product bytes passed Windows source/native gates; main and historical results unchanged.', 'tree':tree,'parents':[expected]})['sha']
    if api(ref)['object']['sha']!=expected:raise ValueError('feature head advanced during publication')
    api(ref,{'sha':commit,'force':False},method='PATCH')
    print('B28_PUBLISHED_COMMIT='+commit,flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['apply','publish']);p.add_argument('--evidence');args=p.parse_args()
    if args.action=='apply':apply()
    else:publish(args.evidence)
