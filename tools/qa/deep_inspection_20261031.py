"""Final breadth pass: re-run immutable phase 2 plus HTTP/DOM/history/startup controls.
Diagnostic success means the audit executed, NOT that the product is safe to release.
"""
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[2]
PHASE2='9dc82fa2ba4e26caab79b86ddbd9cf1587bd8c50'
source=subprocess.check_output(['git','show',PHASE2+':tools/qa/deep_inspection_20261031.py'],cwd=ROOT).decode()
# Replay the already reviewed probes with exactly the same implementation.
exec(compile(source,'immutable-phase2-audit','exec'),globals())

import http.client

def http_boundary_matrix():
    from app_backend.server import start_server
    with tempfile.TemporaryDirectory() as temp:
        root=Path(temp)
        server,token=start_server(guard_recovery_parent=root/'guard',guard_history_parent=root/'history',legacy_recovery_parent=root/'legacy')
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        port=server.server_port;results=[]
        origin='http://127.0.0.1:'+str(port)
        cases=[
          ('guard_valid','GET','/api/converter-guard/recoverable?mode=snapshot',None,{},200),
          ('guard_missing_token','GET','/api/converter-guard/recoverable?mode=snapshot',None,{'X-AIO-Token':None},403),
          ('guard_wrong_token','GET','/api/converter-guard/recoverable?mode=snapshot',None,{'X-AIO-Token':'wrong'},403),
          ('guard_foreign_origin','GET','/api/converter-guard/recoverable?mode=snapshot',None,{'Origin':'https://invalid.example'},403),
          ('guard_foreign_host','GET','/api/converter-guard/recoverable?mode=snapshot',None,{'Host':'attacker.invalid'},403),
          ('guard_bad_mode','GET','/api/converter-guard/recoverable?mode=unknown',None,{},400),
          ('guard_duplicate_mode','GET','/api/converter-guard/recoverable?mode=snapshot&mode=snapshot',None,{},400),
          ('legacy_negative_offset','GET','/api/converter/recover?offset=-1',None,{},400),
          ('legacy_duplicate_limit','GET','/api/converter/recover?limit=20&limit=20',None,{},400),
          ('legacy_missing_token','GET','/api/converter/recover',None,{'X-AIO-Token':None},403),
          ('legacy_foreign_origin','GET','/api/converter/recover',None,{'Origin':'https://invalid.example'},403),
          ('legacy_unconfirmed_decline','POST','/api/converter/decline',{'confirm':False,'scope':'all_previous'},{},422),
          ('legacy_extra_decline_parameter','POST','/api/converter/decline',{'confirm':True,'scope':'all_previous','extra':'x'},{},422),
          ('unknown_legacy_command','POST','/api/converter/unknown',{}, {},404),
          ('retired_compare','GET','/api/converter-guard/compare',None,{},410),
          ('static_py_not_exposed','GET','/app_backend/server.py',None,{},404),
          ('static_traversal','GET','/src/../index.html',None,{},404),
          ('static_foreign_origin','GET','/',None,{'Origin':'https://invalid.example'},403),
        ]
        try:
            for name,method,path,body,overrides,expected in cases:
                conn=http.client.HTTPConnection('127.0.0.1',port,timeout=3)
                headers={'X-AIO-Token':token,'Origin':origin,'Host':'127.0.0.1:'+str(port)}
                for key,value in overrides.items():
                    if value is None:headers.pop(key,None)
                    else:headers[key]=value
                payload=None if body is None else json.dumps(body).encode()
                if payload is not None:headers['Content-Type']='application/json'
                try:
                    conn.request(method,path,body=payload,headers=headers)
                    response=conn.getresponse();response.read()
                    results.append({'case':name,'actual':response.status,'expected':expected,'pass':response.status==expected})
                finally:conn.close()
        finally:
            server.shutdown();server.server_close();thread.join(5)
        return {'issue_reproduced':any(not r['pass'] for r in results),'control_pass':all(r['pass'] for r in results),'cases':results,
                'scope':'18 bounded loopback requests, not a full penetration test'}

def production_dom_controls():
    names=('legacy_recovery_ui.mjs','conversion_history_dom.mjs','guard_ui_lifecycle_v20.mjs','guard_queue_dom_v08.mjs')
    results=[]
    for name in names:
        result=subprocess.run(['node',str(ROOT/'tests'/name)],cwd=ROOT,capture_output=True,text=True,timeout=35)
        results.append({'file':name,'exit':result.returncode,'stdout':result.stdout[-2500:],'stderr':result.stderr[-1600:]})
    return {'issue_reproduced':any(r['exit'] for r in results),'control_pass':all(r['exit']==0 for r in results),'cases':results,
            'scope':'actual production JS in pinned DOM fixture; not installed Qt GUI or system DPI'}

def guard_missing_output_manual_refresh():
    with guarded_fixture(formats=('pdf',)) as (base,g,job):
        path=job.artifacts[0].path;path.unlink()
        state=g.status(job.id);packet=g.recoverable(scan=True)
        matching=[j for j in packet['jobs'] if j['jobId']==job.id]
        return {'issue_reproduced':state['state']=='ready' and bool(matching) and matching[0]['unsavedOutputs']==1,
                'state':state['state'],'fileExists':path.exists(),'retainedRows':matching,
                'scope':'external deletion of app-owned temporary output in current process, not the historical user build'}

def history_tombstone_control():
    from app_backend.converter_guard import ConverterGuard
    from test_converter_guard_v02 import FakeAdapter,wait_finished
    with tempfile.TemporaryDirectory() as temp:
        root=Path(temp);history=root/'history';owned=root/'owned';owned.mkdir()
        g=ConverterGuard(adapter=FakeAdapter(),spool_parent=owned,history_parent=history)
        try:
            identity=g.create([{'id':'f','name':'source.txt','size':4}],['pdf'])['jobId']
            g.upload(identity,'f',io.BytesIO(b'keep'),4);g.start(identity,archive_mode='on_demand');wait_finished(g,identity)
            # Re-record synchronously to establish the history-write boundary.
            job=g._jobs[identity];g._remember_history(job)
            count=len(g.history_list()['items']);snapshot=g.history_report(identity)
            private=json.dumps(snapshot)
            redacted=all(a.ticket not in private for a in job.artifacts)
            g.delete_history(identity);g._remember_history(job)
            deleted=len(g.history_list()['items'])==0
            source_kept=all(a.path.exists() for a in job.artifacts)
        finally:g.close()
        another=ConverterGuard(adapter=FakeAdapter(),spool_parent=owned,history_parent=history)
        try:restart_empty=len(another.history_list()['items'])==0
        finally:another.close()
        passed=count==1 and redacted and deleted and source_kept and restart_empty
        return {'issue_reproduced':not passed,'control_pass':passed,'recordsBefore':count,'tokensRedacted':redacted,
                'delayedWriteCannotResurrect':deleted,'historyDeleteKeptResultBytes':source_kept,'restartEmpty':restart_empty}

def owned_save_destination_control():
    from app_backend.converter_guard import GuardError
    with guarded_fixture() as (base,g,job):
        code=None
        try:g.save_folder(job.artifacts[0].ticket,job.root)
        except GuardError as e:code=e.code
        passed=bool(code) and all(a.path.exists() and not a.saved for a in job.artifacts)
        return {'issue_reproduced':not passed,'control_pass':passed,'code':code,'sourceUnchanged':passed}

def cold_import_cost():
    samples=[]
    with tempfile.TemporaryDirectory() as temp:
        for module in ('app_backend.server','app_backend.desktop'):
            values=[]
            for _ in range(3):
                code='import time; t=time.perf_counter(); import '+module+'; print(round((time.perf_counter()-t)*1000,3))'
                env=dict(os.environ,AIO_GUARD_DATA_DIR=temp)
                result=subprocess.run([sys.executable,'-c',code],cwd=ROOT,env=env,capture_output=True,text=True,timeout=25)
                if result.returncode:
                    values.append({'notMeasured':result.stderr[-1000:]})
                else:values.append(float(result.stdout.strip().splitlines()[-1]))
            samples.append({'module':module,'importMilliseconds':values})
        touched=list(Path(temp).rglob('*'))
    return {'issue_reproduced':False,'measurement':samples,'dataFilesCreatedByImport':len(touched),
            'scope':'fresh Python subprocess imports with warm filesystem cache; not frozen app launch time'}

for name,operation in [('http_authorization_and_input_matrix',http_boundary_matrix),
 ('production_dom_lifecycle_controls',production_dom_controls),('guard_deleted_output_stays_ready',guard_missing_output_manual_refresh),
 ('history_tombstone_and_redaction',history_tombstone_control),('owned_folder_destination_refused',owned_save_destination_control),
 ('cold_import_cost',cold_import_cost)]:
    probe(name,operation)
emit('FINAL_SUMMARY',{'diagnosticCompleted':True,'releaseReady':False,'baseline':BASE,'productChanged':False,
     'probes':len(old.RESULTS),'issueReproductions':sum(r.get('issue_reproduced') is True for r in old.RESULTS),
     'positiveControls':sum(r.get('control_pass') is True for r in old.RESULTS),
     'harnessErrors':sum(r['execution']=='harness_error' for r in old.RESULTS)})
