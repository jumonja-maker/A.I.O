"""Windows-only independent comparison of exact AIO 178-corpus failures.

All input bytes are from the user's newly generated 178-file test ZIP.
Source files are checked against an immutable SHA-256 ZIP and rechecked after
real native/Guard probes. This is a diagnostic, not a product code patch.
"""
from __future__ import annotations

import argparse
from collections import Counter
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SAMPLES_ZIP_SHA256 = 'fc3fedff2160b65c0b463826e317e284e700d1c45994e809a370bbd7d134988f'
EXPECTED_EXTENSIONS = {'docm', 'dotm', 'odf', 'odm', 'oth', 'otm', 'potm', 'ppsx', 'pptm', 'psd', 'xhtml'}
PRIMARY_TARGETS = {
    'docm': 'odt', 'dotm': 'odt', 'odf': 'mml', 'odm': 'pdf', 'oth': 'pdf',
    'otm': 'pdf', 'potm': 'odp', 'ppsx': 'odp', 'pptm': 'odp',
    'psd': 'png', 'xhtml': 'pdf',
}
SECONDARY_TARGETS = {'docm':'pdf','dotm':'pdf','odm':'odt','oth':'html',
                     'otm':'odm','potm':'pdf','ppsx':'pdf','pptm':'pdf','psd':'pdf'}


def digest(path):
    h = sha256()
    with Path(path).open('rb') as file:
        for chunk in iter(lambda: file.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def run_native(executable, source, target, work, *, timeout=40):
    from app_backend.converter_guard.adapter import prepare_profile
    output = work/'outputs'
    profile = work/'profile'
    work.mkdir(parents=True, exist_ok=False)
    output.mkdir()
    prepare_profile(profile)
    command = [str(executable), '-env:UserInstallation=' + profile.resolve().as_uri(),
               '--headless', '--nologo', '--nodefault', '--norestore',
               '--convert-to', target, '--outdir', str(output), str(source)]
    begin = time.monotonic()
    try:
        completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   timeout=timeout, check=False)
        code = completed.returncode
        stdout = completed.stdout.decode('utf-8', errors='replace')[:340]
        stderr = completed.stderr.decode('utf-8', errors='replace')[:340]
    except subprocess.TimeoutExpired:
        code, stdout, stderr = 'TIMEOUT', '', 'Native execution exceeded its bound'
    outputs = [{'name': p.name, 'bytes':p.stat().st_size, 'sha256':digest(p)}
               for p in sorted(output.iterdir()) if p.is_file()]
    success = bool(outputs) and all(x['bytes'] > 0 for x in outputs)
    return {'target':target,'success':success,'exitCode':code,'stdout':stdout,
            'stderr':stderr,'seconds':round(time.monotonic()-begin,3),'outputs':outputs}


def classify(source):
    from app_backend.converter_guard.source_contract import inspect_source_contract
    try:
        evidence = inspect_source_contract(source)
        return {key:evidence.get(key) for key in ('state','contentClass','expectedServices','reason','diagnostic','problems','mimetype')
                if evidence.get(key) is not None}
    except Exception as exc:
        return {'exception':type(exc).__name__, 'code':getattr(exc,'code',None),
                'error':str(exc)[-200:]}


def guard_preflight(samples, executable, work, *, deadline=360):
    from app_backend.converter_guard import LibreOfficeAdapter, ConverterGuard, GuardLimits
    work.mkdir(parents=True, exist_ok=False)
    from PySide6.QtGui import QGuiApplication
    app = QGuiApplication.instance() or QGuiApplication([])
    adapter=LibreOfficeAdapter(executable)
    if not adapter.available:
        return {'status':'ENGINE_NOT_AVAILABLE'}
    guard=ConverterGuard(adapter=adapter, limits=GuardLimits(preflight_timeout=45),
                         recovery_parent=work/'guard-owned')
    uploaded={}
    errors=[];outcome=[];job=None
    began=time.monotonic()
    try:
        rows=[{'id':f'f{i:02d}', 'name':path.name, 'size':path.stat().st_size}
              for i,path in enumerate(samples)]
        job=guard.create(rows,prepare_only=True)['jobId']
        for i, path in enumerate(samples):
            uploaded[f'f{i:02d}'] = path.name
            with path.open('rb') as stream:
                guard.upload(job, f'f{i:02d}', stream, path.stat().st_size)
        def worker():
            try: outcome.append(guard.preflight(job))
            except BaseException as e:
                errors.append({'name':type(e).__name__, 'code':getattr(e,'code',None),
                               'message':str(e)[-240:]})
        thread=threading.Thread(target=worker,daemon=True,name='aio-exact22-windows-native-guard')
        thread.start()
        cancelled=False
        while thread.is_alive() and time.monotonic()-began < deadline:
            time.sleep(.2)
        if thread.is_alive():
            cancelled=True
            guard.cancel(job)
            thread.join(timeout=50)
        state=guard.status(job)
        by_source={}
        formats={}
        if outcome:
            proof=outcome[0]
            for row in proof.get('files',[]):
                name=uploaded.get(row.get('id'))
                if name:
                    item=row.get('preflight') or {}
                    by_source[name]={key:item.get(key) for key in ('state','reason','code','error','documentService','documentServices','modelLoaded','basis','importFilter')
                                     if item.get(key) is not None}
            for row in proof.get('formats',[]):
                ext=row.get('id')
                if ext in ('pdf','odt','mml','odp','png','html'):
                    formats[ext]=[uploaded.get(identity,identity) for identity in row.get('verifiedFileIds',())]
        return {'status':'CANCELLED_ON_DEADLINE' if cancelled else 'ERROR' if errors else 'DONE',
                'seconds':round(time.monotonic()-began,3),
                'completed':state.get('preflightCompleted'),
                'total':state.get('preflightTotal'),
                'finalState':state.get('state'),
                'proofStates':dict(Counter(v.get('state','unknown') for v in by_source.values())),
                'sources':by_source,'verifiedTargets':formats,'errors':errors,
                'threadAlive':thread.is_alive()}
    except Exception as exc:
        return {'status':'EXCEPTION','name':type(exc).__name__,'error':str(exc)[-350:]}
    finally:
        guard.close()


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--samples',type=Path,required=True)
    p.add_argument('--executable',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    if args.output.exists(): raise SystemExit('Diagnostic output directory already exists.')
    args.output.mkdir(parents=True)
    report={'schema':1,'scope':'22 exact original AIO-generated sample files; pinned Windows LibreOffice; not full 178',
            'sourceSha':'b7216d1210489a5fd1eadb21daf2e3b3849e30f4',
            'inputsSha256':digest(args.samples),'expectedInputsSha256':SAMPLES_ZIP_SHA256,
            'cases':[], 'controls':{}, 'guard':{}, 'originalsPreserved':False, 'status':'INCOMPLETE'}
    try:
        if report['inputsSha256']!=SAMPLES_ZIP_SHA256:
            raise ValueError('Input ZIP differs from original AIO 178 corpus')
        engine=subprocess.run([str(args.executable),'--version'],capture_output=True,timeout=25)
        report['engineVersion']=engine.stdout.decode('utf-8',errors='replace').strip()
        if engine.returncode!=0 or not report['engineVersion'].startswith('LibreOffice 26.8.2.2'):
            raise ValueError('Pinned Windows LibreOffice is not 26.8.2.2: '+report['engineVersion'])
        source_dir=args.output/'originals';source_dir.mkdir()
        with ZipFile(args.samples) as z:
            names=sorted(z.namelist())
            if len(names)!=22 or z.testzip() is not None or len(set(names))!=22:
                raise ValueError('Expected 22 intact AIO-generated samples')
            samples=[]
            for name in names:
                path=Path(name)
                if len(path.parts)!=1 or path.suffix[1:].lower() not in EXPECTED_EXTENSIONS or path.stem not in ('NEW_01_LEVEL_1','NEW_02_LEVEL_2'):
                    raise ValueError('Unexpected sample file: '+name)
                content=z.read(name)
                if not 1 <= len(content) <= 512*1024:
                    raise ValueError('Unexpected sample size: '+name)
                original=source_dir/name
                original.write_bytes(content)
                samples.append(original)
        before={p.name:digest(p) for p in samples}
        # Controls are created from repo's reviewed QA fixtures, not from samples.
        from tools.qa.guard_v08_native_acceptance import fixtures
        control_dir=args.output/'controls';control_dir.mkdir()
        controls=fixtures(control_dir/'inputs')
        for ext,target in [('docx','pdf'),('xlsx','pdf'),('bmp','png')]:
            source=controls[ext]
            attempt=run_native(args.executable,source,target,control_dir/(ext+'-'+target))
            report['controls'][ext+'-'+target]=attempt
        for path in samples:
            ext=path.suffix[1:].lower()
            item={'file':path.name,'extension':ext,'bytes':path.stat().st_size,'sha256':before[path.name],
                  'sourceContract':classify(path),'directNative':[]}
            for target in (PRIMARY_TARGETS[ext], SECONDARY_TARGETS.get(ext)):
                if not target:continue
                attempt=run_native(args.executable,path,target,
                                   args.output/'native'/(path.name+'--'+target))
                item['directNative'].append(attempt)
            report['cases'].append(item)
            print('AIO_WINDOWS_SOURCE='+json.dumps({'file':item['file'],
                  'contract':item['sourceContract'].get('state'),
                  'native':[(r['target'],r['success']) for r in item['directNative']]},
                  ensure_ascii=False),flush=True)
        # The same exact 22 sources are then uploaded into actual AIO Guard.
        report['guard']=guard_preflight(samples,args.executable,args.output/'guard',deadline=360)
        report['originalsPreserved']=all(digest(p)==before[p.name] for p in samples)
        report['status']=('PASS_DIAGNOSTIC' if report['originalsPreserved'] and
            all(r['success'] for r in report['controls'].values()) and
            report['guard'].get('status')=='DONE' and
            report['guard'].get('completed')==len(samples) else 'INCOMPLETE')
        print('AIO_WINDOWS_22_SUMMARY='+json.dumps({
            'status':report['status'],
            'directSuccessful':sum(1 for r in report['cases'] if any(a['success'] for a in r['directNative'])),
            'directFailedBoth':sum(1 for r in report['cases'] if not any(a['success'] for a in r['directNative'])),
            'guardState':report['guard'].get('status'),
            'guardProofStates':report['guard'].get('proofStates'),
            'originalsPreserved':report['originalsPreserved']},ensure_ascii=False),flush=True)
        return 0 if report['status']=='PASS_DIAGNOSTIC' else 2
    except BaseException as err:
        report['error']={'type':type(err).__name__,'message':str(err)[-350:]}
        report['status']='ERROR'
        raise
    finally:
        (args.output/'evidence.json').write_text(json.dumps(report,indent=2,ensure_ascii=False,sort_keys=True)+'\n',encoding='utf-8')

if __name__=='__main__':
    raise SystemExit(main())
