"""7,000-task ownership/late-save/recovery test with a synthetic native boundary.

Uses real Guard disk/journal/save code. This is not LibreOffice speed, visual
fidelity or native-process RSS acceptance. All generated files belong to QA.
"""
import argparse
import gc
import hashlib
import io
import json
from pathlib import Path
import sys
import threading
import time
import tracemalloc

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from app_backend.converter_guard import ConverterGuard,GuardError


class EnduranceAdapter:
    available=True
    def __init__(self):self.waiting=threading.Event();self.resume=threading.Event()
    def convert(self,source,target,workspace,*,cancel,on_started,**_):
        on_started()
        if source.name=='0999.fixture' and target=='svg':
            self.waiting.set()
            while not self.resume.wait(.01):
                if cancel.is_set():raise GuardError('CANCELLED','QA cancelled')
        if source.name=='0017.fixture' and target=='csv':
            raise GuardError('ENGINE_OUTPUT_MISSING','Injected independent route failure')
        output=workspace/'output';output.mkdir(parents=True)
        primary=output/(source.stem+'.'+target)
        primary.write_bytes(('SYNTHETIC:'+source.name+':'+target).encode())
        if target=='html':
            sidecar=output/'image.bin';sidecar.write_bytes(('SIDECAR:'+source.name).encode())
            return [primary,sidecar]
        return [primary]


def verify(folder):
    manifest=json.loads((folder/'AIO-results-manifest.json').read_text())
    for row in manifest['outputs']:
        path=folder/row['path']
        with path.open('rb') as stream:actual=hashlib.file_digest(stream,'sha256').hexdigest()
        assert actual==row['sha256'] and path.stat().st_size==row['size']
        content=path.read_bytes()
        expected=f"{int(row['fileId']):04d}.fixture".encode()
        assert expected in content,'File ownership crossed between tasks'
        assert path.name=='image.bin' or content.endswith(b':'+row['format'].encode()),'Target ownership mismatch'
    return len(manifest['outputs'])


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    root=args.output.resolve();root.mkdir(parents=True,exist_ok=False)
    tracemalloc.start();adapter=EnduranceAdapter()
    guard=ConverterGuard(adapter=adapter,recovery_parent=root/'recovery')
    evidence={'status':'FAIL','scope':'1000 sources / 7000 tasks; synthetic exports, real Guard/save/journal',
              'nativeLibreOffice':'NOT_RUN','engineRss':'NOT_MEASURED'}
    tick=time.monotonic();continue_copy=threading.Event();copy_entered=threading.Event();thread=None
    memory={}
    def sample(stage):memory[stage]=dict(zip(('currentBytes','peakBytes'),tracemalloc.get_traced_memory()))
    try:
        identity=guard.create([{'id':str(i),'name':f'{i:04d}.fixture','size':1} for i in range(1000)],
                              ['pdf','odt','html','txt','csv','png','svg'])['jobId']
        for i in range(1000):guard.upload(identity,str(i),io.BytesIO(b'x'),1)
        guard.start(identity,archive_mode='on_demand')
        assert adapter.waiting.wait(120),'Last export never reached QA barrier'
        state=guard.status(identity)
        assert (state['completed'],state['succeeded'],state['failed'])==(6999,6998,1)
        assert state['currentTask']['fileId']=='999' and state['currentTask']['format']=='svg'
        sample('nearComplete');destination=root/'saved';destination.mkdir()
        saved=[];errors=[]
        copy_observed={'current':0,'total':0}
        def progress(current,total):
            copy_observed.update(current=current,total=total)
            if current==10:
                copy_entered.set()
                if not continue_copy.wait(60):raise AssertionError('Save barrier not released')
            if current%1000==0 or current==total:
                print(json.dumps({'qaCopyProgress':current,'qaCopyTotal':total}),flush=True)
        def save():
            try:saved.append(guard.save_folder(state['partialResult']['ticket'],destination,on_progress=progress))
            except Exception as error:errors.append(error)
        thread=threading.Thread(target=save);thread.start();assert copy_entered.wait(5)
        # Each command returns a complete 7,000-task snapshot. On Windows,
        # two sequential snapshots under tracemalloc can exceed 1 second in
        # aggregate without being blocked by the deliberately stalled copy.
        # Preserve the 1s responsiveness limit PER command, and separately
        # prove the save barrier is still held while both return.
        start=time.monotonic();guard.pause(identity,True)
        paused_at=time.monotonic();guard.pause(identity,False)
        resumed_at=time.monotonic()
        evidence['pauseDuringBlockedSaveMs']=round((paused_at-start)*1000)
        evidence['resumeDuringBlockedSaveMs']=round((resumed_at-paused_at)*1000)
        evidence['controlDuringBlockedSaveMs']=round((resumed_at-start)*1000)
        assert not continue_copy.is_set() and thread.is_alive(),'Save barrier was not held during control'
        assert max(evidence['pauseDuringBlockedSaveMs'],
                   evidence['resumeDuringBlockedSaveMs'])<1000,(
            'A single Guard pause/resume command was blocked while save was stalled')
        adapter.resume.set();until=time.monotonic()+5
        while not guard.status(identity)['finished']:
            assert time.monotonic()<until;time.sleep(.01)
        copy_resume_at=time.monotonic()
        continue_copy.set();thread.join(180)
        evidence['saveCopyAfterResumeMs']=round((time.monotonic()-copy_resume_at)*1000)
        evidence['copyProgressAtJoin']=dict(copy_observed)
        assert not thread.is_alive(), ('Copy did not finish within 180s after release',
                                      evidence['copyProgressAtJoin'])
        assert not errors, [(getattr(error,'code',type(error).__name__),str(error)) for error in errors]
        assert saved[0]['saved'] and not saved[0]['savedAll'] and saved[0]['snapshotComplete']
        evidence['partialSavedOutputs']=verify(destination/saved[0]['name'])
        assert evidence['partialSavedOutputs']==7998
        latest=guard.status(identity)
        assert (latest['succeeded'],latest['failed'],latest['cancelled'])==(6999,1,0)
        assert latest['files'][-1]['targets'][-1]['state']=='ready','Late output was wrongly acknowledged'
        guard.close();sample('closedWithRecovery')
        guard=ConverterGuard(adapter=adapter,recovery_parent=root/'recovery')
        restored=guard.status(identity);assert restored['recovered'] and restored['succeeded']==6999
        assert restored['files'][-1]['targets'][-1]['state']=='ready'
        final=guard.save_folder(restored['result']['ticket'],destination)
        assert final['savedAll'];evidence['finalSavedOutputs']=verify(destination/final['name'])
        assert evidence['finalSavedOutputs']==7999
        guard.release(identity)
        assert guard.storage_stats()=={'jobs':0,'reservedBytes':0,'tickets':0,'running':0,'queued':0}
        del state,latest,restored
        gc.collect();sample('qaSnapshotsDropped')
        evidence.update(status='PASS',succeeded=6999,failed=1,recoveredLateOutput=True,allSavedHashes='PASS',
                        taskOwnership='PASS',releasedStorage=guard.storage_stats())
    finally:
        adapter.resume.set();continue_copy.set()
        if thread:thread.join(30)
        guard.close();sample('releasedAndClosed');tracemalloc.stop()
        evidence['pythonAllocations']=memory;evidence['durationMs']=round((time.monotonic()-tick)*1000)
        (root/'evidence.json').write_text(json.dumps(evidence,indent=2))
        print(json.dumps(evidence),flush=True)


if __name__=='__main__':main()
