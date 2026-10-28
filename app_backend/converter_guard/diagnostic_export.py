"""Explicit one-output diagnostic ZIP; never a new required conversion result."""
import hashlib
import json
from pathlib import Path
import secrets
import tempfile
import zipfile

from .errors import GuardError


def diagnostic_package(guard, job_id, task_id, report, *, output_index=None, include_source=False):
    from .core import _Artifact, CHUNK
    if type(include_source) is not bool or output_index is not None and (
            type(output_index) is not int or output_index < 0):
        raise GuardError('DIAGNOSTIC_SELECTION','Неверный выбор диагностических файлов.')
    with guard._lock:
        job=guard._get(job_id)
        if job.running or job.saving or job.diagnostic_building:
            raise GuardError('SAVE_BUSY','Дождитесь завершения текущей операции.',status=409)
        if len(job.diagnostic_artifacts)>=4:
            raise GuardError('DIAGNOSTIC_LIMIT','Для этого сеанса уже подготовлены четыре диагностических пакета.',status=409)
        job.diagnostic_building=True;job.io+=1
    path=None;reserved=0;retained=False
    try:
        with guard.comparison_inputs(job_id) as records:
            record=next((r for r in records if r['taskId']==task_id),None)
            if record is None:
                raise GuardError('COMPARISON_TASK','Преобразование недоступно.',status=404)
            files=[]
            if record['outputs']:
                if output_index is None or output_index>=len(record['outputs']):
                    raise GuardError('DIAGNOSTIC_SELECTION','Выберите конкретный выходной файл.')
                source=record['outputPaths'][output_index]
                if source is None:
                    raise GuardError('DIAGNOSTIC_OUTPUT_UNAVAILABLE','Выбранный выходной файл недоступен.',status=409)
                meta=record['outputs'][output_index]
                files.append(('result/output'+str(output_index)+Path(meta['name']).suffix.replace('\\','_').replace('/','_'), source, meta.get('sha256')))
            elif output_index is not None:
                raise GuardError('DIAGNOSTIC_SELECTION','У этого преобразования нет выходного файла.')
            if include_source:
                if record['source'] is None:
                    raise GuardError('DIAGNOSTIC_SOURCE_UNAVAILABLE','Исходник недоступен.',status=409)
                files.append(('source/original'+Path(record['name']).suffix.replace('\\','_').replace('/','_'),record['source'],record['sourceSha256']))
            fields=('analysisId','status','category','reason','checkedFeatures','unknown','findings','before','after','outputScope')
            diagnostic={'schemaVersion':1,'optionalDiagnostic':True,'jobId':job_id,'taskId':task_id,
                'conversionState':record['state'],'code':record['code'],'error':record['error'],
                'outputPresent':bool(record['outputs']),'sourceIncluded':include_source,
                'selectedOutputIndex':output_index,'analysis':{k:report[k] for k in fields if k in report}}
            data=json.dumps(diagnostic,ensure_ascii=False,indent=2).encode('utf-8')
            if len(data)>2*1024*1024:
                raise GuardError('DIAGNOSTIC_BUDGET','Диагностика превышает бюджет отдельного пакета.')
            budget=len(data)+sum(p.stat().st_size for _,p,_ in files)+65536
            with guard._lock:
                guard._get(job_id);guard._reserve(job,budget)
                reserved=budget
            folder=Path(tempfile.mkdtemp(prefix='diagnostic-export-',dir=job.root))
            path=folder/('diagnostic-'+secrets.token_hex(12)+'.zip')
            seals=[]
            with zipfile.ZipFile(path,'x',compression=zipfile.ZIP_STORED,allowZip64=True) as archive:
                archive.writestr('AIO-diagnostic.json',data)
                for name,source,expected in files:
                    digest=hashlib.sha256();size=0
                    with source.open('rb') as stream,archive.open(name,'w',force_zip64=True) as output:
                        initial=__import__('os').fstat(stream.fileno())
                        while block:=stream.read(CHUNK):
                            if job.save_cancel.is_set():
                                raise GuardError('SAVE_CANCELLED','Подготовка диагностики отменена.',stage='save')
                            digest.update(block);size+=len(block);output.write(block)
                        final=__import__('os').fstat(stream.fileno())
                    if ((expected and digest.hexdigest()!=expected) or any(
                            getattr(initial,k)!=getattr(final,k) for k in
                            ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns'))):
                        raise GuardError('SAVE_CHANGED','Выбранные байты изменились.',stage='save')
                    seals.append({'path':name,'size':size,'sha256':digest.hexdigest()})
                archive.writestr('AIO-diagnostic-manifest.json',json.dumps({'outputs':seals,'qualityCompared':False}))
            with path.open('rb') as stream:
                digest=hashlib.file_digest(stream,'sha256').hexdigest()
            size=path.stat().st_size
            with guard._lock:
                guard._get(job_id)
                if size>reserved:guard._reserve(job,size-reserved)
                else:guard._unreserve(job,reserved-size)
                reserved=size
                artifact=_Artifact(job.id,path,'AIO-selected-diagnostic.zip',size,secrets.token_urlsafe(32),sha256=digest)
                job.diagnostic_artifacts.append(artifact);guard._tickets[artifact.ticket]=artifact
                guard._record(job,'optional_diagnostic_ready')
                retained=True
                return {**artifact.metadata(),'jobId':job.id,'optionalDiagnostic':True,'requiredForSave':False}
    finally:
        if not retained and path is not None:path.unlink(missing_ok=True)
        with guard._lock:
            if not retained and reserved:guard._unreserve(job,reserved)
            job.diagnostic_building=False;job.io-=1;guard._io_drained.notify_all()
        guard._cleanup(job)
