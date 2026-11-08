"""QA-only Windows OOXML diagnostic: exact original versus namespace-only handoff copies.

Never edits an original source or product code. Uses an isolated Windows runner,
strict source hashes, independent native LO PDF exports, and real AIO Guard.
A successful program exit means that the comparisons ran, NOT that any
particular format works or that rewriting OOXML is authorized in production.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from xml.etree import ElementTree as ET
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
FIXTURES_SHA256 = 'd921567a21c3290a6486d7c7100cc1d67dd7ac3ec4b900f138ddc4ebeea13def'
BASE_SHA = 'b7216d1210489a5fd1eadb21daf2e3b3849e30f4'
EXTENSIONS = ('docm', 'dotm', 'potm', 'ppsx', 'pptm')
SOURCE_KINDS = ('original', 'namespace_only')


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            result.update(chunk)
    return result.hexdigest()


def semantic_xml(data):
    def node(element):
        return (element.tag, tuple(sorted(element.attrib.items())),
                (element.text or '').strip(), tuple(node(x) for x in element),
                (element.tail or '').strip())
    return node(ET.fromstring(data))


def extract_and_check(archive_path, target):
    if digest(archive_path) != FIXTURES_SHA256:
        raise ValueError('Fixture ZIP SHA256 changed; refusing diagnostic')
    expected = {f'NEW_0{level}_LEVEL_{level}.{ext}' for ext in EXTENSIONS for level in (1, 2)}
    checks = []
    with ZipFile(archive_path) as zipped:
        if zipped.testzip() is not None:
            raise ValueError('Broken outer ZIP CRC')
        names = set(zipped.namelist())
        all_expected = {'PAIRS-MANIFEST.json'} | {f'{kind}/{x}' for kind in SOURCE_KINDS for x in expected}
        if names != all_expected:
            raise ValueError('Unexpected fixture entries (malformed or missing files)')
        manifest = json.loads(zipped.read('PAIRS-MANIFEST.json'))
        if len(manifest['pairs']) != 10:
            raise ValueError('Expected exactly ten independently checked pairs')
        by_name = {x['name']: x for x in manifest['pairs']}
        if set(by_name) != expected:
            raise ValueError('Invalid fixture manifest names')
        for kind in SOURCE_KINDS:
            (target / kind).mkdir(parents=True, exist_ok=False)
        for name in sorted(expected):
            source = zipped.read('original/' + name)
            changed = zipped.read('namespace_only/' + name)
            row = by_name[name]
            if hashlib.sha256(source).hexdigest() != row['originalSha256'] or hashlib.sha256(changed).hexdigest() != row['variantSha256']:
                raise ValueError('Exact original/variant SHA mismatch: ' + name)
            with ZipFile(BytesIO(source)) as a, ZipFile(BytesIO(changed)) as b:
                if a.testzip() is not None or b.testzip() is not None:
                    raise ValueError('Nested OOXML CRC bad: ' + name)
                before = {n: a.read(n) for n in a.namelist()}
                after = {n: b.read(n) for n in b.namelist()}
                if set(before) != set(after):
                    raise ValueError('OOXML member added/removed: ' + name)
                edited = [n for n in before if before[n] != after[n]]
                if edited != ['[Content_Types].xml']:
                    raise ValueError('More than namespace header changed: ' + name + ' ' + repr(edited))
                if semantic_xml(before['[Content_Types].xml']) != semantic_xml(after['[Content_Types].xml']):
                    raise ValueError('Not equivalent XML expanded names: ' + name)
            (target / 'original' / name).write_bytes(source)
            (target / 'namespace_only' / name).write_bytes(changed)
            checks.append({'name': name, 'originalSha256': row['originalSha256'],
                           'variantSha256': row['variantSha256'], 'memberCount':len(before),
                           'changedMember': edited[0], 'expandedXmlEquivalent': True})
    return checks


def native_pdf(soffice, source, working, timeout=55):
    from app_backend.converter_guard.adapter import prepare_profile
    working.mkdir(parents=True, exist_ok=False)
    profile = working / 'profile'
    out = working / 'output'
    out.mkdir()
    prepare_profile(profile)
    command = [str(soffice), '-env:UserInstallation=' + profile.resolve().as_uri(),
               '--headless', '--nologo', '--nodefault', '--norestore',
               '--convert-to', 'pdf', '--outdir', str(out), str(source)]
    start = time.monotonic()
    try:
        completed = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
        result = {'exitCode': completed.returncode,
                  'stdout': completed.stdout.decode('utf-8', 'replace')[-400:],
                  'stderr': completed.stderr.decode('utf-8', 'replace')[-400:]}
    except subprocess.TimeoutExpired:
        result = {'exitCode': 'TIMEOUT', 'stdout':'', 'stderr':'55s direct LO timeout'}
    output = out / (source.stem + '.pdf')
    result['durationSec'] = round(time.monotonic() - start, 3)
    result['pdfCreated'] = bool(output.is_file() and output.stat().st_size > 0)
    if result['pdfCreated']:
        from pypdf import PdfReader
        try:
            pages = PdfReader(output).pages
            text = '\n'.join(p.extract_text() or '' for p in pages)
            level = 1 if 'LEVEL_1' in source.name else 2
            result.update({'pdfBytes': output.stat().st_size, 'pdfSha256': digest(output),
                           'pdfPages': len(pages),
                           'markerPresent':f'AIO-NEW-20261108-L{level}' in text,
                           'pdfTextChars': len(text)})
        except Exception as exc:
            result['pdfParseError'] = type(exc).__name__ + ': ' + str(exc)[-220:]
    return result


def guard_check(kind, inputs, engine, parent, *, perform_conversion=False):
    from PySide6.QtGui import QGuiApplication
    from app_backend.converter_guard import ConverterGuard, GuardLimits, LibreOfficeAdapter
    from app_backend.converter_guard.errors import GuardError
    _app = QGuiApplication.instance() or QGuiApplication([])
    parent.mkdir(parents=True, exist_ok=False)
    guard = ConverterGuard(adapter=LibreOfficeAdapter(engine),
                           limits=GuardLimits(preflight_timeout=55,file_timeout=95),
                           recovery_parent=parent/'guard-owned')
    report = {'group':kind, 'status':'INCOMPLETE', 'preflight':{}, 'pdfVerified': [],
              'errors':[], 'conversion':None}
    job = None
    try:
        records = [(f'id{i:02d}', name, inputs/name) for i,name in enumerate(sorted(p.name for p in inputs.iterdir()))]
        job = guard.create([{'id':fid,'name':name,'size':p.stat().st_size} for fid,name,p in records], prepare_only=True)['jobId']
        for fid,name,p in records:
            with p.open('rb') as stream:
                guard.upload(job, fid, stream, p.stat().st_size)
        values, failures = [], []
        def run():
            try: values.append(guard.preflight(job))
            except BaseException as exc:
                failures.append({'type':type(exc).__name__, 'code':getattr(exc,'code',None), 'message':str(exc)[-320:]})
        worker = threading.Thread(target=run,daemon=True,name='aio-ooxml-' + kind)
        worker.start()
        worker.join(timeout=690)
        if worker.is_alive():
            guard.cancel(job)
            worker.join(timeout=45)
            report['status']='PREFLIGHT_CANCELLED_TIMEOUT'
            report['errors'].append('Preflight exceeded total 690-second deadline')
            return report
        if failures:
            report['status']='PREFLIGHT_ERROR'
            report['errors'].extend(failures)
            return report
        if not values:
            report['status']='PREFLIGHT_NO_REPORT'
            return report
        proof=values[0]
        names={fid:name for fid,name,_ in records}
        report['preflight']={names[r['id']]: {
            key: (r.get('preflight') or {}).get(key)
            for key in ('state','code','reason','documentService','documentServices','modelLoaded','importFilter','diagnostic')
            if (r.get('preflight') or {}).get(key) is not None}
            for r in proof.get('files',[]) if r['id'] in names}
        approved={fid for row in proof.get('formats',[]) if row.get('id')=='pdf' for fid in row.get('verifiedFileIds',())}
        report['pdfVerified']=[names[fid] for fid in sorted(approved)]
        report['guardFinalState']=guard.status(job).get('state')
        report['preflightCounts']=dict(Counter((r.get('preflight') or {}).get('state','NONE') for r in proof.get('files',[])))
        report['status']='DONE'
        if perform_conversion and approved:
            selected=sorted(approved)
            guard.start(job, file_ids=selected,
                        plan=[{'fileId':fid,'formats':['pdf']} for fid in selected],only_verified=True)
            until=time.monotonic()+420
            while time.monotonic()<until and not guard.status(job).get('finished'):
                time.sleep(.12)
            state=guard.status(job)
            if not state.get('finished'):
                guard.cancel(job)
                report['conversion']={'status':'CANCELLED_TIMEOUT', 'job':{k:state.get(k) for k in ('completed','total','succeeded','failed')}}
                return report
            conv={'status':'FINISHED', 'job':{k:state.get(k) for k in ('completed','total','succeeded','failed')}, 'outputChecks': []}
            if state.get('result') and state['result'].get('ticket'):
                folder_parent=parent/'save'
                folder_parent.mkdir()
                saved=guard.save_folder(state['result']['ticket'],folder_parent)
                saved_dir=folder_parent/saved['name']
                for item in state['result'].get('outputs',[]):
                    pdf=saved_dir/item['archiveName']
                    if pdf.is_file() and pdf.suffix.lower()=='.pdf':
                        from pypdf import PdfReader
                        filename=names.get(item['fileId'],'')
                        level = 1 if 'LEVEL_1' in filename else 2
                        pdf_text='\n'.join(page.extract_text() or '' for page in PdfReader(pdf).pages)
                        conv['outputChecks'].append({'file':filename,'bytes':pdf.stat().st_size,
                            'sha256':digest(pdf),'markerPresent':f'AIO-NEW-20261108-L{level}' in pdf_text,
                            'readbackVerified':True})
            report['conversion']=conv
        return report
    except BaseException as exc:
        report['status']='EXCEPTION'
        report['errors'].append({'type':type(exc).__name__, 'code':getattr(exc,'code',None), 'message':str(exc)[-500:]})
        return report
    finally:
        guard.close()


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--samples',type=Path,required=True)
    p.add_argument('--engine',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    if args.output.exists():raise SystemExit('Output directory already exists')
    args.output.mkdir(parents=True)
    evidence={'schema':1,'sourceRevision':BASE_SHA,'inputZipSha256':digest(args.samples),
              'scope':'exact ten handoff OOXML originals vs namespace-only copies on pinned Windows LO',
              'result':'INCOMPLETE','pairs':[],'direct':[],'guard':{},'filesUnchanged':False}
    try:
        evidence['pairs']=extract_and_check(args.samples,args.output/'inputs')
        lo=subprocess.run([str(args.engine),'--version'],capture_output=True,timeout=35,check=False)
        evidence['engineVersion']=lo.stdout.decode('utf-8','replace').strip()
        if lo.returncode!=0 or not evidence['engineVersion'].startswith('LibreOffice 26.8.2.2'):
            raise RuntimeError('Wrong Windows LibreOffice engine: '+evidence['engineVersion'])
        before={kind:{p.name:digest(p) for p in (args.output/'inputs'/kind).iterdir()} for kind in SOURCE_KINDS}
        for row in evidence['pairs']:
            name=row['name']
            for kind in SOURCE_KINDS:
                path=args.output/'inputs'/kind/name
                outcome=native_pdf(args.engine,path,args.output/'native'/kind/name)
                evidence['direct'].append({'name':name,'kind':kind,**outcome})
                print('AIO_OOXML_WIN_NATIVE='+json.dumps({'name':name,'kind':kind,'success':outcome['pdfCreated'],
                    'pages':outcome.get('pdfPages'),'marker':outcome.get('markerPresent'),
                    'exit':outcome['exitCode']},ensure_ascii=False),flush=True)
        for kind in SOURCE_KINDS:
            evidence['guard'][kind]=guard_check(kind,args.output/'inputs'/kind,args.engine,args.output/'guard'/kind,
                                                perform_conversion=(kind=='namespace_only'))
            info=evidence['guard'][kind]
            print('AIO_OOXML_WIN_GUARD='+json.dumps({'kind':kind,'status':info['status'],
                'proof':info.get('preflightCounts'),'pdfVerified':len(info.get('pdfVerified',[])),
                'conversion':info.get('conversion',{}) and info['conversion'].get('job')},ensure_ascii=False),flush=True)
        evidence['filesUnchanged']=all(digest(args.output/'inputs'/kind/name)==sha
            for kind,record in before.items() for name,sha in record.items())
        evidence['result']='DIAGNOSTIC_COMPLETE' if (evidence['filesUnchanged']
            and all(evidence['guard'][k]['status']=='DONE' for k in SOURCE_KINDS)) else 'DIAGNOSTIC_PARTIAL'
        print('AIO_OOXML_WIN_SUMMARY='+json.dumps({'result':evidence['result'],'pairs':len(evidence['pairs']),
            'originalNativePdfs':sum(x['pdfCreated'] for x in evidence['direct'] if x['kind']=='original'),
            'variantNativePdfs':sum(x['pdfCreated'] for x in evidence['direct'] if x['kind']=='namespace_only'),
            'guardVerifiedOriginal':len(evidence['guard']['original'].get('pdfVerified',[])),
            'guardVerifiedVariants':len(evidence['guard']['namespace_only'].get('pdfVerified',[])),
            'unchanged':evidence['filesUnchanged']}),flush=True)
        return 0 if evidence['result']=='DIAGNOSTIC_COMPLETE' else 2
    except BaseException as exc:
        evidence['result']='ERROR'
        evidence['exception']={'type':type(exc).__name__,'message':str(exc)[-550:]}
        raise
    finally:
        (args.output/'evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf-8')

if __name__=='__main__':raise SystemExit(main())
