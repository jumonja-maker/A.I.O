"""Isolated preparation only; never included in the product tree."""
import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.request import Request, urlopen

BASE='ad3492dd701082cf1d961a68d0d99de6a08457dd'
TEMP={'.github/workflows/aio-retirement-probe.yml','tools/qa/_prepare_retirement.py'}
NEW={'tests/test_module_retirement.py','tools/qa/runtime_frozen_acceptance.py','docs/review/AIO_MODULE_RETIREMENT_M16_M17_2026-10-30_RU.md'}

def git(*args):return subprocess.check_output(['git',*args],text=True).strip()
def blob(data):return hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
def replace(text,before,after,count=1):
    assert text.count(before)==count,(before[:120],text.count(before),count)
    return text.replace(before,after)
def read(name):return Path(name).read_text(encoding='utf-8')
def write(name,text):
    ast.parse(text) if name.endswith('.py') else None
    Path(name).write_text(text,encoding='utf-8',newline='\n')
def cut(text,predicate):
    nodes=[n for n in ast.walk(ast.parse(text)) if predicate(n)]
    spans=[]
    for n in nodes:
        first=min([n.lineno]+[d.lineno for d in getattr(n,'decorator_list',[])])
        spans.append((first,n.end_lineno))
    lines=text.splitlines(keepends=True)
    return ''.join(s for i,s in enumerate(lines,1) if not any(a<=i<=b for a,b in spans))
def methods(text,names):
    found={n.name for n in ast.walk(ast.parse(text)) if isinstance(n,(ast.FunctionDef,ast.ClassDef)) and n.name in names}
    assert found==set(names),(found,names)
    return cut(text,lambda n:isinstance(n,(ast.FunctionDef,ast.ClassDef)) and n.name in names)
def telemetry_if(n):return isinstance(n,ast.If) and 'self.telemetry' in ast.unparse(n.test)
def telemetry_call(n):return isinstance(n,ast.Expr) and isinstance(n.value,ast.Call) and ast.unparse(n.value.func)=='self._telemetry'

if sys.argv[1]=='prepare':
    baseline=git('ls-tree','-r','--name-only',BASE).splitlines()
    for name in baseline:
        p=Path(name)
        if p.is_file():assert blob(p.read_bytes())==git('rev-parse',BASE+':'+name),('base changed',name)
    retired=[]
    for name in baseline:
        p=Path(name)
        remove=(name in {'app_backend/blackbox_telemetry.py','app_backend/qt_observation.py','app_backend/converter_guard/diagnostic_export.py','tests/test_viewer_entrypoint_premerge.py','tests/test_telemetry_priority_v20.py'}
            or name.startswith('app_backend/converter_guard/') and (p.name.startswith('comparison_') or p.name.startswith('preview'))
            or name.startswith('src/guard-comparison')
            or name.startswith('tests/test_comparison_') or name.startswith('tests/test_blackbox_')
            or name.startswith('tests/guard_comparison')
            or name.startswith('tools/qa/guard_v19_')
            or name in {'tools/qa/guard_before_after_b19_b25_acceptance.py','tools/qa/guard_v21_scale_acceptance.py','tools/qa/guard_session_native_acceptance.py','tools/qa/guard_v21_xlsx_acceptance.py','tools/qa/guard_remaining_bag_stress3.py'})
        if remove:
            retired.append({'path':name,'blob':blob(p.read_bytes()),'tests':sum(isinstance(n,ast.FunctionDef) and n.name.startswith('test_') for n in ast.walk(ast.parse(read(name)))) if p.suffix=='.py' and name.startswith('tests/') else 0})
            p.unlink()
    # No comparative service or worker survives the public process/API boundary.
    p='launcher.py';s=read(p)
    s=cut(s,lambda n:isinstance(n,ast.If) and "'--aio-guard-comparison'" in ast.unparse(n.test))
    s=replace(s,"        from app_backend.converter_guard.preflight_native import main\n        main(sys.argv[2])\n", "        from app_backend.converter_guard.preflight_native import main\n        main(sys.argv[2])\n        raise SystemExit(0)\n")
    s=replace(s,'    from app_backend.desktop import launch_desktop',"    # Removed/private unknown worker switches must not fall through to GUI.\n    if len(sys.argv)>1 and sys.argv[1].startswith('--aio-guard-'):\n        raise SystemExit(2)\n    from app_backend.desktop import launch_desktop")
    write(p,s)
    p='app_backend/converter_guard/http_api.py';s=read(p)
    s=replace(s,'        from .comparison_service import ComparisonService\n        self.comparison = ComparisonService(guard)\n','')
    s=replace(s,'        # Reject new work and interrupt HTTP bodies first. Comparison shutdown\n        # can wait for worker leases; it must not leave admission open meanwhile.','        # Reject new work and interrupt in-flight HTTP bodies before shutdown.')
    s=replace(s,'        self.comparison.close()\n','')
    s=methods(s,{'_comparison_get'})
    s=cut(s,lambda n:isinstance(n,ast.If) and "'/compare" in ast.unparse(n.test))
    s=replace(s,'            expected_analysis_id = None\n','')
    s=replace(s,"                if outcome.get('closed'):self.comparison.release(identity)\n",'')
    s=replace(s,'                self.comparison.release(identity)\n','')
    check="""            if path == PREFIX + '/compare' or path.startswith(PREFIX + '/compare/'):
                handler.close_connection = True
                raise GuardError('MODULE_RETIRED', 'Модуль «До/После» исключён из приложения.', status=410)
"""
    for anchor in ("            self._authorize(handler, download=path == PREFIX + '/result')\n","            self._authorize(handler)\n"):
        s=replace(s,anchor,anchor+check)
    s=replace(s,'                body = self._body(handler)\n',"                body = self._body(handler)\n            if path in (PREFIX + '/prepare', PREFIX + '/jobs') and body.get('retainSources', False) is not False:\n                raise GuardError('MODULE_RETIRED', 'Хранение исходников для «До/После» отключено.', status=410)\n")
    s=replace(s,"'qualityInspectionDefault': False,","'qualityInspectionDefault': False, 'comparisonAvailable': False,")
    write(p,s)
    # Keep the generic retained-input lease and provenance of older sessions,
    # but remove every executable comparative retry and external observer hook.
    p='app_backend/converter_guard/core.py';s=read(p)
    s=methods(s,{'_telemetry_record','retry_comparison','_RetryCancellation'})
    s=cut(s,telemetry_if)
    s=cut(s,lambda n:isinstance(n,ast.If) and '_RetryCancellation' in ast.unparse(n.test))
    s=replace(s,', telemetry=None, history_parent=None',', history_parent=None')
    s=replace(s,'        self.telemetry = telemetry\n','')
    start=s.index('        # Best-effort observation of already-committed GUARD state.')
    end=s.index('    def _get(',start)
    s=s[:start]+s[end:]
    start=s.index('                                    # If the current batch exposes a native worker,')
    end=s.index('                            routing = ',start)
    s=s[:start]+'\n'+s[end:]
    s=replace(s,'prepare_recipe(job.comparison_recipe)','prepare_recipe(None)')
    s=s.replace('_comparison_path','_owned_file_path').replace('def comparison_inputs(', 'def retained_inputs(')
    s=s.replace("'comparisonAvailable': bool(j.retain_sources and not j.running and\n                                  j.artifacts and any(not f.source_released for f in j.files)),", "'comparisonAvailable': False,")
    s=s.replace("'comparisonAvailable':bool(job.retain_sources and not job.running and\n                    job.artifacts and any(not f.source_released for f in job.files)),", "'comparisonAvailable':False,")
    s=s.replace('UI comparison jobs keep disk-owned originals until their','Legacy retained jobs keep disk-owned originals until their')
    s=s.replace('HTTP/comparison reader','HTTP/diagnostic reader')
    assert 'self.telemetry' not in s and '_RetryCancellation' not in s
    assert "'comparisonAvailable':bool" not in s and "'comparisonAvailable': bool" not in s
    write(p,s)
    p='app_backend/converter_transport.py';s=read(p)
    s=methods(s,{'_telemetry'})
    start=s.index('        for file_id in ids:\n');end=s.index('        for i, source in enumerate(files):',start)
    s=s[:start]+s[end:]
    s=cut(s,telemetry_call)
    s=replace(s,'    def __init__(self, *, telemetry=None):\n        self.telemetry = telemetry','    def __init__(self):')
    start=s.index('        # Report the actual terminal snapshot even if the HTTP handler fails')
    end=s.index('        try:',start);s=s[:start]+s[end:]
    s=replace(s,'                started_at = time.monotonic()\n','')
    write(p,s)
    p='app_backend/server.py';s=read(p)
    s=replace(s,'from .blackbox_telemetry import from_environment\n','')
    s=replace(s,'        self.telemetry = from_environment()\n        self.converter = ConverterTransport(telemetry=self.telemetry)','        self.converter = ConverterTransport()')
    s=replace(s,'                try:\n                    if self.telemetry is not None:\n                        self.telemetry.close()\n                finally:\n                    super().server_close()','                super().server_close()')
    s=replace(s,'            telemetry=server.telemetry,\n','')
    write(p,s)
    p='app_backend/desktop.py';s=read(p)
    s=cut(s,lambda n:isinstance(n,ast.If) and ('server.telemetry' in ast.unparse(n.test) or 'self._gui_observation' in ast.unparse(n.test)))
    s=replace(s,'        self._gui_observation = None\n','')
    write(p,s)
    p='index.html';s=read(p)
    s=''.join(line for line in s.splitlines(keepends=True) if 'src/guard-comparison' not in line)
    write(p,s)
    p='src/converter-bridge.js';s=read(p)
    s=replace(s,'{files:metadata(inputs),retainSources:true}','{files:metadata(inputs)}')
    s=replace(s,'Saved sessions stay available for explicit До/После and Close.','Keep the final UI snapshot until explicit Close; saved spools may be reaped.')
    write(p,s)
    p='app_backend/converter_lifecycle_ui_patch.py';s=read(p)
    s=replace(s,'<button class="aio-cv__small-btn" type="button" data-cv-beforeafter>До/После</button>','')
    write(p,s)
    p='src/guard-queue.js';s=read(p)
    s=replace(s,'<button type="button" data-g-compare>Анализ до/после</button>','')
    start=s.index("    action('[data-g-compare]',async()=>{");end=s.index("    action('[data-g-discard]'",start)
    s=s[:start]+s[end:]
    start=s.index("    const compare=bar.querySelector('[data-g-compare]');");end=s.index("    bar.querySelector('[data-g-discard]')",start)
    s=s[:start]+s[end:]
    write(p,s)
    # Existing unrelated test boundaries remain: adapt only removed public
    # feature expectations, not result ownership or cancellation requirements.
    p='tests/test_guard_queue_save_v21.py';write(p,read(p).replace('guard.comparison_inputs(job)','guard.retained_inputs(job)'))
    p='tests/test_session_lifecycle_b16_b27.py';write(p,replace(read(p),"self.assertTrue(guard.status(job)['comparisonAvailable'])","self.assertFalse(guard.status(job)['comparisonAvailable'])"))
    for p in ('tests/guard_ui_dom_smoke.mjs','tests/native_ui_dom_smoke.mjs'):
        s=read(p);s=replace(s,"['Закрыть','Сохранить результаты','До/После']","['Закрыть','Сохранить результаты']")
        s=s.replace('constantThreeActions','constantTwoActions');write(p,s)
    p='tests/test_media_bounds_v21.py';s=read(p)
    s=replace(s,'from app_backend.converter_guard.comparison_analysis import analyze_pair\n','')
    s=methods(s,{'test_before_after_reports_truncated_pixels_as_loss'})
    write(p,s)
    p='tests/test_boundaries_v21.py';s=read(p)
    s=methods(s,{'test_preview_analysis_token_reaches_service_and_rejects_ambiguous_tokens','test_release_prunes_comparison_only_after_guard_accepts_release','test_shutdown_closes_http_admission_and_socket_before_comparison_wait'})
    # Replacement public admission/release tests are in test_module_retirement.
    write(p,s)
    p='tests/test_transport_audit_v18.py';s=read(p)
    start=s.index('        class CancelAtPackaging:');end=s.index("        self.assertEqual(error.exception.code, 'CANCELLED')",start)
    s=s[:start]+'''        original = transport._check_packaging_cancel
        def at_packaging(request):
            if request.packaging:
                transport.cancel(request.identity)
            return original(request)
        with patch('app_backend.converter_transport.convert_path', side_effect=self.convert), \\
                patch.object(transport, '_check_packaging_cancel', side_effect=at_packaging), \\
                patch.object(zipfile._ZipWriteFile, 'write', side_effect=AssertionError('No archive bytes may be written')):
            with self.assertRaises(LibreOfficeError) as error:
                self.execute()
'''+s[end:]
    write(p,s)
    p='AllInOne.spec';s=read(p)
    s=replace(s,"              'app_backend.aio_converter_upgrade'],", "              'app_backend.aio_converter_upgrade',\n              'app_backend.blackbox_telemetry', 'app_backend.qt_observation',\n              'app_backend.converter_guard.comparison_service',\n              'app_backend.converter_guard.comparison_worker'],")
    s=replace(s,"if sys.platform == 'win32':", """# A stale import must not silently bring a frozen feature back into EXE.
retired_names=('app_backend.blackbox_telemetry','app_backend.qt_observation',
               'app_backend.converter_guard.comparison_',
               'app_backend.converter_guard.preview',
               'app_backend.converter_guard.diagnostic_export')
if any(any(row[0].startswith(name) for name in retired_names) for row in a.pure):
    raise RuntimeError('Retired feature leaked into bundled Python modules')
if sys.platform == 'win32':""")
    write(p,s)
    p='.github/workflows/aio-hosted-qa.yml';s=read(p)
    s=replace(s,"  AIO_BLACKBOX_EVENTS: ''\n",'')
    s=replace(s,'tools/qa/guard_v19_frozen_acceptance.py','tools/qa/runtime_frozen_acceptance.py')
    write(p,s)
    # Do not silently delete mixed/manual QA scripts from the running test
    # suite. The archived original versions remain at the immutable BASE.
    Path('retirement-inventory.json').write_text(json.dumps(retired,indent=2),encoding='utf-8')
    print('RETIRED_SOURCE_INVENTORY='+json.dumps(retired))
    print('REMOVED_FEATURE_TEST_METHODS='+str(sum(x['tests'] for x in retired)+4))
    print('RETIREMENT_PREPARED_BASE='+BASE)
elif sys.argv[1]=='publish':
    names=set(git('diff','--name-only',BASE).splitlines())|NEW
    names-=TEMP
    names.discard('')
    token=os.environ['GH_TOKEN'];repo=os.environ['GITHUB_REPOSITORY']
    def request(path,value):
        req=Request('https://api.github.com/repos/'+repo+path,json.dumps(value).encode(),
            headers={'Authorization':'Bearer '+token,'Content-Type':'application/json','Accept':'application/vnd.github+json'})
        with urlopen(req,timeout=30) as r:return json.load(r)
    entries=[]
    for name in sorted(names):
        assert name in NEW or name in git('ls-tree','-r','--name-only',BASE).splitlines(),name
        p=Path(name)
        if p.exists():
            raw=p.read_bytes();sha=blob(raw)
            result=request('/git/blobs',{'content':raw.decode('utf-8'),'encoding':'utf-8'})
            assert result['sha']==sha
        else:sha=None
        entries.append({'path':name,'mode':'100644','type':'blob','sha':sha})
    result=request('/git/trees',{'base_tree':git('rev-parse',BASE+'^{tree}'),'tree':entries})
    print('RETIREMENT_PRODUCT_TREE='+json.dumps({'base':BASE,'tree':result['sha'],'files':entries}),flush=True)
else:raise SystemExit('prepare or publish')
