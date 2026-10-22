"""Explicit local corpus QA. Never dispatches workflows, modifies inputs or installs.

Default: each input -> PDF, only when the real Guard preflight admits it.
--all-native-targets explicitly requests every admitted native export instead.
Timeouts here are QA settings, not new application limits. Reports can contain
private filenames; keep this output local. Full visual fidelity is not asserted.
"""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import sys
import time
import tracemalloc

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from app_backend.converter_guard import ConverterGuard, GuardLimits, LibreOfficeAdapter


def digest(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--fixtures',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--executable',type=Path)
    parser.add_argument('--targets',default='pdf')
    parser.add_argument('--manifest',type=Path,help='Optional pinned list of name/sha256 entries')
    parser.add_argument('--all-native-targets',action='store_true')
    parser.add_argument('--timeout',type=float,default=90)
    parser.add_argument('--python-memory',action='store_true')
    args=parser.parse_args()
    sources=args.fixtures.resolve(strict=True);output=args.output.resolve()
    if output.is_relative_to(sources):parser.error('Output must be outside the input corpus')
    output.mkdir(parents=True,exist_ok=False)
    paths=sorted(p for p in sources.iterdir() if p.is_file() and not p.is_symlink())
    if args.manifest:
        pinned=json.loads(args.manifest.read_text())
        for row in pinned:
            if Path(row['name']).name!=row['name']:raise ValueError('Invalid manifest path')
        paths=sorted(sources/row['name'] for row in pinned)
        assert len(set(paths))==len(paths),'Duplicate manifest source'
        assert all(p.is_file() and not p.is_symlink() for p in paths)
        assert all(digest(sources/row['name'])==row['sha256'] for row in pinned),'Source hash mismatch'
    assert paths,'No fixture files'
    originals={p.name:digest(p) for p in paths}
    if args.python_memory:tracemalloc.start()
    memory={}
    def measure(stage):
        if args.python_memory:
            current,peak=tracemalloc.get_traced_memory()
            memory[stage]={'currentBytes':current,'peakBytes':peak,'scope':'Python allocations only; not native RSS'}
    adapter=LibreOfficeAdapter(args.executable)
    assert adapter.available,'LibreOffice required'
    guard=ConverterGuard(adapter=adapter,recovery_parent=output/'owned-jobs',
        limits=GuardLimits(file_timeout=args.timeout,preflight_timeout=args.timeout))
    evidence={'status':'FAIL','platform':platform.system(),'sourceFiles':len(paths),
              'visibleQt':'NOT_RUN','fullVisualFidelity':'NOT_RUN','pythonMemory':memory}
    tick=time.monotonic()
    try:
        identity=guard.create([{'id':str(i),'name':p.name,'size':p.stat().st_size}
                               for i,p in enumerate(paths)],prepare_only=True)['jobId']
        for i,p in enumerate(paths):
            with p.open('rb') as stream:guard.upload(identity,str(i),stream,p.stat().st_size)
        measure('uploaded')
        start=time.monotonic();inspection=guard.preflight(identity)
        evidence['preflightMs']=round((time.monotonic()-start)*1000)
        by_id={fmt['id']:set(fmt.get('verifiedFileIds',[])) for fmt in inspection['formats']}
        requested=list(by_id) if args.all_native_targets else args.targets.split(',')
        plan=[{'fileId':str(i),'formats':[fmt for fmt in requested if str(i) in by_id.get(fmt,())]}
              for i in range(len(paths))]
        plan=[row for row in plan if row['formats']]
        evidence['plannedTasks']=sum(len(row['formats']) for row in plan)
        evidence['preflight']=[{'name':row['name'],'sha256':originals[row['name']],
            'proof':row.get('preflight'),'admittedTargets':[fmt for fmt,ids in by_id.items() if row['id'] in ids]}
            for row in inspection['files']]
        (output/'preflight.json').write_text(json.dumps(evidence['preflight'],ensure_ascii=False,indent=2))
        assert plan,'No admitted requested routes'
        measure('prepared')
        start=time.monotonic()
        guard.start(identity,plan=plan,file_ids=[row['fileId'] for row in plan],only_verified=True,
                    archive_mode='on_demand')
        revision=None;last_print=0
        while True:
            state=guard.status(identity,since_revision=revision,delta=True)
            revision=state['revision']
            if not state.get('unchanged'):
                if state['finished']:break
                if time.monotonic()-last_print>=1:
                    print(json.dumps({'completed':state['completed'],'total':state['total'],
                                      'currentTask':state['currentTask']}),flush=True)
                    last_print=time.monotonic()
            time.sleep(.05)
        evidence['queueMs']=round((time.monotonic()-start)*1000)
        state=guard.status(identity)
        report=guard.report(identity)
        (output/'conversion-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        evidence['counts']={k:state[k] for k in ('total','completed','succeeded','failed','filtered','cancelled','officeStarts')}
        assert state['total']==evidence['plannedTasks']==state['completed']
        measure('converted')
        if state['result']:
            destination=output/'saved';destination.mkdir()
            start=time.monotonic();saved=guard.save_folder(state['result']['ticket'],destination)
            evidence['folderSaveMs']=round((time.monotonic()-start)*1000)
            assert saved['savedAll'] and saved['snapshotComplete']
            folder=destination/saved['name']
            manifest=json.loads((folder/'AIO-results-manifest.json').read_text())
            for row in manifest['outputs']:
                path=folder/row['path']
                assert path.stat().st_size==row['size'] and digest(path)==row['sha256']
            evidence['savedOutputs']=len(manifest['outputs'])
            evidence['savedByteReadback']='PASS'
            guard.release(identity)
        else:guard.release(identity,discard=True)
        measure('released')
        assert originals=={p.name:digest(p) for p in paths},'Source bytes changed'
        evidence['originalsUnchanged']='PASS'
        assert guard.storage_stats()['jobs']==0
        evidence['releasedStorage']=guard.storage_stats()
        evidence['status']='PASS'
    finally:
        guard.close();measure('closed')
        evidence['totalMs']=round((time.monotonic()-tick)*1000)
        (output/'evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2))
        if args.python_memory:tracemalloc.stop()
        print(json.dumps({k:v for k,v in evidence.items() if k!='preflight'},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
