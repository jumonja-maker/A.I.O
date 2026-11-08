"""QA-only end-to-end Guard XHTML->PDF on original generated AIO files.
No production edits, no user data; exact ZIP SHA-256 is pinned.
"""
import argparse, hashlib, json, pathlib, sys, time, zipfile

ROOT=pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
EXPECTED='fc3fedff2160b65c0b463826e317e284e700d1c45994e809a370bbd7d134988f'

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--samples',type=pathlib.Path,required=True)
    parser.add_argument('--engine',type=pathlib.Path,required=True)
    parser.add_argument('--output',type=pathlib.Path,required=True)
    args=parser.parse_args()
    args.output.mkdir(exist_ok=False)
    report={'schema':1,'sourceSha':'b7216d1210489a5fd1eadb21daf2e3b3849e30f4',
            'inputsSha256':sha(args.samples),'cases':[],'status':'INCOMPLETE'}
    guard=None
    try:
        if report['inputsSha256']!=EXPECTED: raise ValueError('Original input ZIP SHA mismatch')
        from PySide6.QtGui import QGuiApplication
        _app=QGuiApplication.instance() or QGuiApplication([])
        from app_backend.converter_guard import ConverterGuard,GuardLimits,LibreOfficeAdapter
        from pypdf import PdfReader
        a=LibreOfficeAdapter(args.engine)
        if not a.available:raise ValueError('Pinned native LibreOffice unavailable')
        inputs=args.output/'inputs';inputs.mkdir()
        orig=[]
        with zipfile.ZipFile(args.samples) as z:
            if z.testzip() is not None:raise ValueError('Input ZIP CRC changed')
            for level in (1,2):
                name=f'NEW_0{level}_LEVEL_{level}.xhtml'
                raw=z.read(name)
                p=inputs/name;p.write_bytes(raw)
                orig.append((f'f{level}',p,hashlib.sha256(raw).hexdigest()))
        guard=ConverterGuard(adapter=a,limits=GuardLimits(file_timeout=90,preflight_timeout=60),
                             recovery_parent=args.output/'recovery')
        job=guard.create([{'id':fid,'name':p.name,'size':p.stat().st_size} for fid,p,h in orig],prepare_only=True)['jobId']
        for fid,p,h in orig:
            with p.open('rb') as src:guard.upload(job,fid,src,p.stat().st_size)
        preflight=guard.preflight(job)
        pdf_verified={f for v in preflight['formats'] if v['id']=='pdf' for f in v.get('verifiedFileIds',())}
        report['pdfVerifiedIds']=sorted(pdf_verified)
        report['preflight']={f['id']:(f.get('preflight') or {}).get('state') for f in preflight['files']}
        if pdf_verified!={fid for fid,p,h in orig}:
            report['status']='PREFLIGHT_NOT_VERIFIED'
            return 0
        guard.start(job,file_ids=[fid for fid,p,h in orig],
                    plan=[{'fileId':fid,'formats':['pdf']} for fid,p,h in orig],only_verified=True)
        begin=time.monotonic()
        while not guard.status(job).get('finished'):
            if time.monotonic()-begin>170:
                guard.cancel(job)
                report['status']='GUARD_DEADLINE'
                return 0
            time.sleep(.12)
        state=guard.status(job)
        report['job']={key:state.get(key) for key in ('state','completed','total','succeeded','failed','finished')}
        report['guardReport']=guard.report(job)
        if state.get('failed') or state.get('succeeded')!=2:
            report['status']='GUARD_REJECTED'
            return 0
        saved_dest=args.output/'saved'
        saved_dest.mkdir()
        saved=guard.save_folder(state['result']['ticket'],saved_dest)
        folder=saved_dest/saved['name']
        report['savedFolder']=saved['name']
        for fid,p,source_hash in orig:
            match=[a for a in state['result']['outputs'] if a['fileId']==fid and a['format']=='pdf']
            if len(match)!=1: raise ValueError('Missing published XHTML PDF for '+fid)
            pdf=folder/match[0]['archiveName']
            pages=PdfReader(pdf).pages
            text='\n'.join(page.extract_text() or '' for page in pages)
            marker=f'AIO-NEW-20261108-L{fid[1]}'
            row={'name':p.name,'sourceSha256':source_hash,'pdfBytes':pdf.stat().st_size,
                 'pdfSha256':sha(pdf),'pdfPages':len(pages),
                 'markerInPdfText':marker in text,'sourceUnchanged':sha(p)==source_hash}
            report['cases'].append(row)
        report['status']='CONVERTED_AND_SAVED' if len(report['cases'])==2 and all(r['markerInPdfText'] and r['sourceUnchanged'] for r in report['cases']) else 'OUTPUT_LOSS'
        return 0
    except BaseException as e:
        report['error']={'type':type(e).__name__,'message':str(e)[-500:]}
        report['status']='ERROR'
        return 2
    finally:
        if guard is not None:guard.close()
        (args.output/'evidence.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf-8')
        print('AIO_XHTML_WINDOWS_E2E='+json.dumps({
            'status':report['status'],'pdfVerified':report.get('pdfVerifiedIds'),
            'job':report.get('job'),'cases':report['cases'],
            'error':report.get('error')},ensure_ascii=False),flush=True)

if __name__=='__main__':raise SystemExit(main())
