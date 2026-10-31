"""Independent audit of immutable QA 97fabe65. Diagnostic evidence, NOT a release gate.
Only disposable synthetic files are used. Application source is never changed.
"""
from __future__ import annotations
import ast
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from collections import Counter
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
BASE = '97fabe65b64076dc8063e0813865ac70b42bfb14'
RESULTS = []

def emit(kind, value):
    print('DEEP_AUDIT_' + kind + '=' + json.dumps(value, ensure_ascii=True, default=str), flush=True)

def probe(name, operation):
    tick = time.perf_counter()
    try:
        value = operation()
        result = {'case': name, 'execution': 'completed', 'seconds': round(time.perf_counter()-tick, 4), **value}
    except BaseException as error:
        result = {'case':name,'execution':'harness_error','exception':type(error).__name__,
                  'detail':str(error), 'traceback':traceback.format_exc()[-6000:]}
    RESULTS.append(result)
    emit('PROBE', result)

def inventory():
    tracked = subprocess.check_output(['git','ls-files','-z'],cwd=ROOT).decode().split('\0')
    tracked = [p for p in tracked if p]
    changed = subprocess.check_output(['git','diff','--name-only',BASE,'HEAD'],cwd=ROOT).decode().splitlines()
    unexpected = [p for p in changed if p not in ('tools/qa/deep_inspection_20261031.py','.github/workflows/aio-deep-inspection-20261031.yml')]
    assert not unexpected, 'Product code differs from audit baseline: '+repr(unexpected)
    counts = Counter()
    functions, exceptions, busy_loops, whole_reads, methods, hazards = [], [], [], [], [], []
    imports = Counter()
    for relative in tracked:
        path=ROOT/relative
        if not path.is_file():continue
        counts[path.suffix] += 1
        if path.suffix != '.py':continue
        source=path.read_text(encoding='utf-8-sig')
        try:tree=ast.parse(source)
        except SyntaxError as e:
            emit('SYNTAX_ERROR',{'path':relative,'line':e.lineno,'error':str(e)});continue
        app = relative.startswith('app_backend/')
        if app:counts['app_python_lines'] += len(source.splitlines())
        for node in ast.walk(tree):
            if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and app:
                functions.append({'path':relative,'name':node.name,'line':node.lineno,'lines':node.end_lineno-node.lineno+1})
                if relative.endswith(('core.py','excel.py','desktop.py','http_api.py','adapter.py','office_session.py','save_verification.py','archive_verification.py')):
                    methods.append([relative,node.name,node.lineno,node.end_lineno])
            if isinstance(node,ast.ExceptHandler) and app and (node.type is None or isinstance(node.type,ast.Name) and node.type.id in ('Exception','BaseException')):
                exceptions.append([relative,node.lineno,ast.unparse(node.type) if node.type else 'bare'])
            if isinstance(node,ast.Call) and app:
                name=ast.unparse(node.func)
                if name.endswith(('.read_bytes','.read_text','.readall')) or name.endswith('.read') and not node.args:
                    whole_reads.append([relative,node.lineno,name])
                if any(word in name for word in ('sleep','rglob','os.walk','fsync','deepcopy','json.dumps','stat','extractall','eval','exec')):
                    hazards.append([relative,node.lineno,name])
            if isinstance(node,ast.Import):
                for alias in node.names:imports[alias.name.split('.')[0]]+=1
    emit('INVENTORY', {'baseline':BASE,'auditCommit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT).decode().strip(),
          'python':sys.version,'platform':platform.platform(),'trackedFiles':len(tracked),
          'counts':dict(counts),'productChanges':unexpected,'topLargeFunctions':sorted(functions,key=lambda x:-x['lines'])[:35],
          'appFunctionCount':len(functions),'broadExceptionCount':len(exceptions),'wholeReadCount':len(whole_reads)})
    emit('METHOD_MAP', methods)
    emit('WHOLE_READS',whole_reads)
    emit('POTENTIAL_COST_SITES',hazards)
    emit('DIFF_MAIN', subprocess.run(['git','diff','--stat','origin/main',BASE],cwd=ROOT,capture_output=True,text=True).stdout[-15000:])
    for p in ('app_backend/excel.py','app_backend/converter_guard/policy.py','app_backend/converter_guard/office_session.py','app_backend/converter_guard/adapter.py','app_backend/native_process.py','app_backend/downloads.py','app_backend/converter_assets.py','AllInOne.spec'):
        text=(ROOT/p).read_text(encoding='utf-8')
        emit('MODULE_INDEX',{'path':p,'lines':len(text.splitlines()),'bytes':len(text.encode()),'sha256':hashlib.sha256(text.encode()).hexdigest(),
                            'definitions':[[n.name,n.lineno,n.end_lineno] for n in ast.walk(ast.parse(text)) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef))] if p.endswith(('.py','.spec')) else []})

def converter_fixture(source,target,workspace,**kwargs):
    out=workspace/'output';out.mkdir(parents=True)
    result=out/('test.'+target);result.write_bytes(b'%PDF-1.7\ncontrolled audit output')
    return [result]

def seed_verified(parent):
    from app_backend.converter_transport import ConverterTransport
    t=ConverterTransport(recovery_parent=parent)
    req=t.begin('a'*32,None)
    with patch('app_backend.converter_transport.convert_path',side_effect=converter_fixture):
        result=t.execute(req,[{'path':Path('audit.txt'),'warnings':[]}],['f'],['pdf'])
    t.finish(req,retained=True)
    ticket=result['result']['ticket']
    path=t.results[ticket].path
    t.close()
    return req.root,path

def seed_orphan(parent):
    root=parent/('aio-legacy-'+'b'*32)
    out=root/'native/0/0/output/left.pdf';out.parent.mkdir(parents=True)
    out.write_bytes(b'%PDF-1.7\ncontrolled orphan')
    (root/'legacy-result.json').write_bytes(b'{broken')
    return root,out

def salvage_slot_limit():
    from app_backend.converter_transport import ConverterTransport
    with tempfile.TemporaryDirectory() as temp:
        parent=Path(temp)/'legacy';root,out=seed_orphan(parent)
        t=ConverterTransport(recovery_parent=parent,max_owned_roots=1,max_owned_bytes=16*1024*1024)
        try:
            before=t.storage_stats()
            for _ in range(4): t.salvage_workspace(root.name)
            after=t.storage_stats()
            return {'issue_reproduced':after['ownedRoots']>after['maxOwnedRoots'],
                    'before':before,'after':after,'originalUnchanged':out.read_bytes()==b'%PDF-1.7\ncontrolled orphan'}
        finally:t.close()

def scan_shutdown_race():
    from app_backend.converter_transport import ConverterTransport
    import app_backend.converter_transport as mod
    with tempfile.TemporaryDirectory() as temp:
        parent=Path(temp)/'legacy';root,out=seed_verified(parent)
        t=ConverterTransport(recovery_parent=parent,lazy_recovery=True)
        entered=threading.Event();resume=threading.Event();errors=[]
        actual=mod.recover_workspaces
        def slow(*args,**kwargs):
            result=actual(*args,**kwargs);entered.set()
            if not resume.wait(8):raise TimeoutError('audit barrier')
            return result
        def work():
            try:t.scan_recovery()
            except BaseException as e:errors.append(type(e).__name__)
        try:
            with patch.object(mod,'recover_workspaces',side_effect=slow):
                thread=threading.Thread(target=work,daemon=True);thread.start()
                assert entered.wait(5)
                t.close()
                resume.set();thread.join(8)
            owners=[r for r in t.results.values() if getattr(r.temporary.store,'owner',None) is not None]
            return {'issue_reproduced':t.closed and len(owners)>0,'closed':t.closed,
                    'publishedAfterClose':len(t.results),'liveOwnersAfterClose':len(owners),'workerAlive':thread.is_alive(),'errors':errors}
        finally:
            resume.set();t.close()

def missing_journal_path():
    from app_backend.converter_transport import ConverterTransport
    from app_backend.converter_guard.result_store import ResultStore
    with tempfile.TemporaryDirectory() as temp:
        parent=Path(temp)/'legacy';root,out=seed_verified(parent)
        journal=root/'legacy-result.json';value=json.loads(journal.read_text())
        value.pop('checksum');value.pop('path');value['checksum']=ResultStore._digest(value)
        journal.write_text(json.dumps(value))
        t=ConverterTransport(recovery_parent=parent,lazy_recovery=True)
        caught=None
        try:
            try:t.inventory()
            except BaseException as e:caught=type(e).__name__
            return {'issue_reproduced':caught=='KeyError','exception':caught,
                    'originalStillPresent':out.is_file(),'scope':'malformed checksummed journal; controlled fault injection'}
        finally:t.close()

def stale_inventory():
    from app_backend.converter_transport import ConverterTransport
    with tempfile.TemporaryDirectory() as temp:
        parent=Path(temp)/'legacy';root,out=seed_verified(parent)
        t=ConverterTransport(recovery_parent=parent)
        try:
            before=t.inventory()['total'];out.unlink();t.scan_recovery();packet=t.inventory()
            state=t.recover('a'*32)['state']
            return {'issue_reproduced':packet['total']>0 and state=='ready',
                    'before':before,'afterManualRescan':packet['total'],'recoverState':state,
                    'warningCount':len(packet['recoveryWarnings']),'fileExists':out.exists()}
        finally:t.close()

def quota_scan_cost():
    from app_backend.legacy_storage_budget import measured_bytes,LegacyRunControl
    with tempfile.TemporaryDirectory() as temp:
        root=Path(temp);measurements=[]
        previous=0
        for count in (100,3000,12000):
            for i in range(previous,count):
                folder=root/str(i//1000);folder.mkdir(exist_ok=True)
                (folder/str(i)).write_bytes(b'x')
            previous=count
            times=[]
            for _ in range(3):
                tick=time.perf_counter();actual=measured_bytes([root]);times.append((time.perf_counter()-tick)*1000)
            measurements.append({'files':count,'logicalBytes':actual,'scanMilliseconds':times})
        return {'issue_reproduced':False,'measurement':measurements,
                'contract':'all retained storage scanned on native cancellation poll at most every 0.5s; elapsed not a user-machine benchmark'}

def bulk_decline_temporary_salvage():
    from app_backend.converter_transport import ConverterTransport
    with tempfile.TemporaryDirectory() as temp:
        parent=Path(temp)/'legacy';root,out=seed_orphan(parent)
        t=ConverterTransport(recovery_parent=parent)
        try:
            salvaged=t.salvage_workspace(root.name)
            result=t.decline_all_recovery();packet=t.inventory()
            return {'issue_reproduced':result['notDeclined']==0 and packet['total']>0,
                    'decline':result,'remaining':packet['total'],
                    'remainingItems':[{'state':r['state'],'salvaged':r['salvagedUnverified']} for r in packet['items']],
                    'originalRemoved':not root.exists()}
        finally:t.close()

def guard_method_index():
    src=(ROOT/'app_backend/converter_guard/core.py').read_text()
    tree=ast.parse(src)
    selected=('_report','_snapshot','_record','_cleanup_owned','_run','_bundle','save_folder','_acknowledge_outputs','_verify_saved_file','verify_saved_download','create','_accept_outputs')
    for node in ast.walk(tree):
        if isinstance(node,ast.FunctionDef) and node.name in selected:
            lines=src.splitlines()[node.lineno-1:node.end_lineno]
            emit('FOCUSED_SOURCE',{'path':'app_backend/converter_guard/core.py','method':node.name,'start':node.lineno,'end':node.end_lineno,
                                  'text':'\n'.join(lines[:240])})

if __name__=='__main__':
    inventory()
    for name,operation in [('salvage_bypasses_root_limit',salvage_slot_limit),
            ('scan_publishes_after_shutdown',scan_shutdown_race),('malformed_journal_path',missing_journal_path),
            ('manual_rescan_leaves_missing_file_ready',stale_inventory),('quota_whole_store_cost',quota_scan_cost),
            ('decline_all_keeps_salvage_copy',bulk_decline_temporary_salvage)]:
        probe(name,operation)
    guard_method_index()
    emit('SUMMARY', {'diagnosticCompleted':True,'releaseReady':False,'probes':len(RESULTS),
         'issueReproductions':sum(r.get('issue_reproduced') is True for r in RESULTS),
         'harnessErrors':sum(r['execution']=='harness_error' for r in RESULTS),
         'baseline':BASE,'applicationSourceModified':False})
