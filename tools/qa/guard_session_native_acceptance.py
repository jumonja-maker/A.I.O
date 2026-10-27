"""Targeted native save, optional diagnostics, recovery and owned cancellation."""
import argparse
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sys
import time
import zipfile

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from app_backend.converter_guard import ConverterGuard,LibreOfficeAdapter,GuardLimits
from app_backend.converter_guard.comparison_service import ComparisonService
from app_backend.converter_guard.diagnostic_export import diagnostic_package
from guard_v08_native_acceptance import fixtures


def sha(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def finished(guard,job,seconds=180):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        state=guard.status(job)
        if state['finished']:return state
        time.sleep(.02)
    raise TimeoutError('native session queue timeout')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--executable',type=Path,required=True);args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    report={'status':'RUNNING','scope':'actual native LO queue/save/recovery/cancel; not FULLTEST or installed EXE', 'checks':[]}
    guard=service=None
    try:
        sources=fixtures(args.output/'fixtures');source=sources['xlsx'];original=sha(source)
        parent=args.output/'owned'
        guard=ConverterGuard(adapter=LibreOfficeAdapter(args.executable),recovery_parent=parent)
        job=guard.create([{'id':'book','name':source.name,'size':source.stat().st_size}],
                         ['xlsx','pdf'],retain_sources=True)['jobId']
        with source.open('rb') as stream:guard.upload(job,'book',stream,source.stat().st_size)
        guard.start(job,archive_mode='on_demand');state=finished(guard,job)
        assert state['succeeded']==2 and state['failed']==0,guard.report(job)
        assert guard.close_conversion(job)['confirmationRequired']
        output=state['result']['outputs'][0];saved_file=args.output/'saved-one.bin'
        with guard.open_result(output['ticket']) as (stream,_,__):saved_file.write_bytes(stream.read())
        guard.verify_saved_download(output['ticket'],saved_file)
        guard.confirm_saved(job,output['ticket'])
        assert sum(not o['saved'] for o in guard.status(job)['result']['outputs'])==1
        saved_hash=sha(saved_file)
        guard.close();guard=ConverterGuard(adapter=LibreOfficeAdapter(args.executable),recovery_parent=parent)
        state=guard.status(job)
        assert sum(o['saved'] for o in state['result']['outputs'])==1
        all_saved=guard.save_folder(state['result']['ticket'],args.output)
        assert all_saved['savedAll'] and all_saved['readbackVerified']
        disk=args.output/all_saved['name']
        manifest=json.loads((disk/'AIO-results-manifest.json').read_text(encoding='utf-8'))
        for row in manifest['outputs']:assert sha(disk/row['path'])==row['sha256'] and row['readbackVerified']
        assert guard.recoverable()['jobs'][0]['unsavedOutputs']==0
        service=ComparisonService(guard);service.start(job)
        deadline=time.monotonic()+90
        while service.snapshot(job)['state'] not in ('completed','failed'):
            if time.monotonic()>deadline:raise TimeoutError('comparison did not finish')
            time.sleep(.05)
        analysis=service.snapshot(job);assert analysis['state']=='completed',analysis
        assert guard.storage_stats()['unsavedOutputs']==0
        detail=service.detail(job,job+':0',expected_analysis_id=analysis['analysisId'])
        diagnostic=diagnostic_package(guard,job,job+':0',detail,output_index=0)
        assert guard.storage_stats()['unsavedOutputs']==0
        diagnostic_file=args.output/'saved-diagnostic.zip'
        with guard.open_result(diagnostic['ticket']) as (stream,_,__):diagnostic_file.write_bytes(stream.read())
        guard.verify_saved_download(diagnostic['ticket'],diagnostic_file)
        guard.confirm_saved(job,diagnostic['ticket'])
        with zipfile.ZipFile(diagnostic_file) as archive:
            assert len([n for n in archive.namelist() if n.startswith('result/')])==1
            assert not any(n.startswith('source/') for n in archive.namelist())
        assert guard.close_conversion(job)['closed'];service.release(job)
        assert sha(saved_file)==saved_hash and sha(source)==original
        for row in manifest['outputs']:assert sha(disk/row['path'])==row['sha256']
        report['checks'].append({'status':'PASS','name':'native-save-recovery-analysis-close',
            'nativeResults':2,'partialSaveRestored':True,'optionalDiagnosticDidNotChangeUnsaved':True,
            'originalSha256':original,'savedFirstSha256':saved_hash,'diagnosticSha256':sha(diagnostic_file)})
        service.close();service=None;guard.close();guard=None

        # A real large Writer import is cancelled, without stalling/replacing
        # the native engine with a fixture. Only its owned process is tracked.
        source=args.output/'cancel-native.docx'
        with zipfile.ZipFile(source,'x',compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
            archive.writestr('_rels/.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="r1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
            archive.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'+''.join('<w:p><w:r><w:t>AIO native cancellation marker %d with retained source data.</w:t></w:r></w:p>'%i for i in range(2000))+'<w:sectPr/></w:body></w:document>')
        before=sha(source);offices=[];native_phases=[]
        class TrackingAdapter(LibreOfficeAdapter):
            @contextmanager
            def batch(self,*values,**options):
                with super().batch(*values,**options) as office:
                    if hasattr(office,'request'):
                        original_request=office.request
                        def track(value,**request_options):
                            callback=request_options.get('on_stage',lambda stage:None)
                            def observed(stage):
                                if value.get('operation')=='export' and stage in ('import','export') and office.tree:
                                    native_phases.append((stage,office.tree.process))
                                callback(stage)
                            return original_request(value,**{**request_options,'on_stage':observed})
                        office.request=track
                    offices.append(office);yield office
        guard=ConverterGuard(adapter=TrackingAdapter(args.executable),recovery_parent=args.output/'cancel-owned',
                             limits=GuardLimits(file_timeout=120))
        job=guard.create([{'id':'cancel','name':source.name,'size':source.stat().st_size}],prepare_only=True,retain_sources=True)['jobId']
        with source.open('rb') as stream:guard.upload(job,'cancel',stream,source.stat().st_size)
        guard.preflight(job,timeout_seconds=120)
        guard.start(job,formats=['pdf'],file_ids=['cancel'],only_verified=True,archive_mode='on_demand');deadline=time.monotonic()+60;phase=None;process=None
        while time.monotonic()<deadline:
            state=guard.status(job);phase=(state.get('currentTask') or {}).get('stage')
            if native_phases:
                phase,process=native_phases[-1];break
            if state['finished']:
                report['cancelFailure']=guard.report(job)
                report['officeStates']=[{'type':type(office).__name__,'starts':getattr(office,'starts',None),'disabled':getattr(office,'disabled',None)} for office in offices]
                raise AssertionError('native cancellation opportunity not observed')
            time.sleep(.005)
        assert process is not None,'actual native worker not observed'
        owned=guard._jobs[job];outcome=guard.close_conversion(job,discard=True)
        assert outcome['closed'];owned.future.result(timeout=30)
        assert process.poll() is not None and sha(source)==before
        report['checks'].append({'name':'actual-native-close-cancellation','status':'PASS',
            'phase':phase,'workerPid':process.pid,'workerExited':True,'originalSha256':before,
            'descendantPidInventory':'not_independently_enumerated'})
        report['status']='PASS'
    except Exception as error:
        report.update(status='FAIL',code=getattr(error,'code',None),reason=str(error),error=type(error).__name__);raise
    finally:
        if service is not None:service.close()
        if guard is not None:guard.close()
        (args.output/'evidence.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print('SESSION_NATIVE='+json.dumps(report,ensure_ascii=True),flush=True)


if __name__=='__main__':main()
