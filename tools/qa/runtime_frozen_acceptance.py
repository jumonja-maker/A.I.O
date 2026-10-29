"""Exercise retained AIO workers and absence of retired modules in a real EXE.

The source-worker switch is a development surrogate, never frozen proof.
No new product command, installer, GUI launch or native conversion is added.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import time
import zipfile
import zlib

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))


def require(value,message):
    if not value:raise AssertionError(message)


def digest(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def run_owned(command,folder,seconds,expected_exit=0,*,cwd=None):
    from app_backend.native_process import ProcessTree
    environment=dict(os.environ)
    environment.pop('PYTHONPATH',None);environment.pop('PYTHONHOME',None)
    log=folder/'worker.log';deadline=time.monotonic()+seconds
    with log.open('wb') as stream:
        with ProcessTree(command,cwd=folder if cwd is None else cwd,env=environment,
                         stdout=stream,stderr=stream) as tree:
            while tree.process.poll() is None:
                require(time.monotonic()<deadline,'Owned runtime worker deadline exceeded')
                require(log.stat().st_size<=1024*1024,'Runtime worker diagnostic budget exceeded')
                try:tree.process.wait(timeout=.1)
                except subprocess.TimeoutExpired:pass
            code=tree.process.returncode
    diagnostic=log.read_bytes()[-4096:].decode('utf-8',errors='replace')
    require(code==expected_exit,'Unexpected worker exit '+str(code)+': '+diagnostic)
    return {'exitCode':code}


def fixtures(folder):
    folder.mkdir()
    doc=folder/'sample.docx'
    with zipfile.ZipFile(doc,'w') as archive:
        archive.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        archive.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Alpha Beta Gamma</w:t></w:r></w:p></w:body></w:document>')
    csv=folder/'sample.csv';csv.write_text('Name,Value\nAlpha,12\nBeta,34\n',encoding='utf-8')
    png=folder/'sample.png'
    def chunk(name,data):return struct.pack('!I',len(data))+name+data+struct.pack('!I',zlib.crc32(name+data)&0xffffffff)
    png.write_bytes(b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!2I5B',2,1,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(b'\0\xff\0\0\0\xff\0'))+chunk(b'IEND',b''))
    from pypdf import PdfWriter
    pdf=folder/'sample.pdf'
    writer=PdfWriter();writer.add_blank_page(width=72,height=144);writer.add_blank_page(width=144,height=72)
    with pdf.open('wb') as stream:writer.write(stream)
    writer.close()
    return [('docx',doc,{'words':3}),('csv',csv,{'rows':3,'columns':2}),
            ('png',png,{'width':2,'height':1}),('pdf',pdf,{'pages':2})]


def execute(executable,output,*,seconds=120,source_worker=False):
    executable=Path(executable).resolve(strict=True);output=Path(output).resolve()
    output.mkdir(parents=True,exist_ok=False)
    deadline=time.monotonic()+seconds
    evidence={'schema':1,'status':'PENDING','scope':'retained_structural_worker_and_retired_feature_boundary',
        'frozenVerified':not source_worker,'desktopGUI':'NOT_RUN','LibreOffice':'NOT_RUN','cases':[]}
    controls=fixtures(output/'inputs')
    before={str(p):digest(p) for _,p,_ in controls}
    for name,source,counts in controls:
        work=output/name;work.mkdir()
        request=work/'request.json';request.write_text(json.dumps({'source':str(source),'seconds':8}),encoding='utf-8')
        command=([str(executable),'-m','app_backend.converter_guard.quality_worker',str(request)] if source_worker
                 else [str(executable),'--aio-guard-quality',str(request)])
        result=run_owned(command,work,max(.01,deadline-time.monotonic()),cwd=ROOT if source_worker else None)
        response=work/'quality-result.json'
        require(response.is_file() and 0<response.stat().st_size<1024*1024,'Missing bounded structural evidence')
        actual=json.loads(response.read_text(encoding='ascii'))
        require(actual.get('status')=='checked_subset' and actual.get('complete') is True,'Structural inspection failed: '+name)
        require(all(actual['counts'].get(k)==v for k,v in counts.items()),'Incorrect structural facts: '+name)
        evidence['cases'].append({'case':name,'status':'PASS','counts':counts,**result})
    work=output/'retired-entry';work.mkdir()
    absent=work/'must-not-open.json'
    command=([str(executable),str(ROOT/'launcher.py'),'--aio-guard-comparison',str(absent)] if source_worker
             else [str(executable),'--aio-guard-comparison',str(absent)])
    run_owned(command,work,max(.01,deadline-time.monotonic()),expected_exit=2)
    require(not absent.exists(),'Retired worker touched its request')
    evidence['retiredWorkerRejected']=True
    if not source_worker:
        bundle=executable.parent/'_internal'
        for relative in ('index.html','src/guard-queue.js','src/conversion-history.js'):
            require((bundle/relative).read_bytes()==(ROOT/relative).read_bytes(),'Packaged asset mismatch: '+relative)
        require(not list((bundle/'src').glob('guard-comparison*')),'Retired UI was packaged')
        from PyInstaller.archive.readers import CArchiveReader,ZlibArchiveReader
        archive=CArchiveReader(str(executable))
        pyz=archive.open_embedded_archive('PYZ.pyz')
        names=set(pyz.toc)
        retired=('app_backend.blackbox_telemetry','app_backend.qt_observation',
                 'app_backend.converter_guard.comparison_','app_backend.converter_guard.preview',
                 'app_backend.converter_guard.diagnostic_export')
        require(not any(any(name.startswith(prefix) for prefix in retired) for name in names),
                'Retired Python feature was packaged')
        required={'app_backend.converter_guard.core','app_backend.converter_guard.quality_worker',
                  'app_backend.converter_guard.conversion_history','sqlite3','pypdf'}
        require(required<=names,'Required Guard/history dependency missing: '+str(required-names))
        evidence['packagedModules']={'retiredAbsent':True,'retainedPresent':sorted(required)}
        evidence['assets']='PASS'
    require(all(digest(Path(p))==value for p,value in before.items()),'A worker modified a source')
    evidence.update(status='PASS',fixturesUnchanged=True)
    (output/'evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
    return evidence


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--executable',required=True)
    parser.add_argument('--output',required=True);parser.add_argument('--seconds',type=float,default=120)
    parser.add_argument('--source-worker',action='store_true');args=parser.parse_args()
    require(args.seconds>0,'Positive acceptance timeout required')
    result=execute(args.executable,args.output,seconds=args.seconds,source_worker=args.source_worker)
    print('AIO_RUNTIME_FROZEN_ACCEPTANCE='+json.dumps(result,ensure_ascii=True))


if __name__=='__main__':main()
