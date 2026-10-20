"""Explicit real-engine check; creates only synthetic data, never runs at startup."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from app_backend.converter_guard import LibreOfficeAdapter
from app_backend.server import start_server


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--work',type=Path,required=True)
    parser.add_argument('--formats',default='pdf',help='Explicit small native smoke, e.g. pdf,xlsx,dbf,png')
    args=parser.parse_args()
    work=args.work.resolve();work.mkdir(parents=True,exist_ok=False)
    sources=work/'sources';sources.mkdir()
    for i in range(3):
        (sources/('table'+str(i)+'.csv')).write_text(
            'name,amount\nalpha,'+str(10+i)+'\nbeta,20\n',encoding='utf-8')
    from PIL import Image
    Image.new('RGB',(96,64),(30,90,180)).save(sources/'image.bmp')
    paths=[sources/('table'+str(i)+'.csv') for i in range(3)]+[sources/'image.bmp']
    hashes={p.name:digest(p) for p in paths}

    class ObservedAdapter(LibreOfficeAdapter):
        def __init__(self):
            super().__init__();self.inspections=[];self.exports=[]

        def preflight(self,source,*a,**kw):
            started=time.monotonic()
            result=super().preflight(source,*a,**kw)
            self.inspections.append({'name':source.name,'seconds':round(time.monotonic()-started,4),
                                     'state':result.get('state')})
            return result

        def convert(self,source,target,*a,**kw):
            assert digest(source)==hashes[source.name]
            self.exports.append({'name':source.name,'format':target})
            return super().convert(source,target,*a,**kw)

    adapter=ObservedAdapter()
    assert adapter.available,'Set AIO_LIBREOFFICE to an installed office'
    version=subprocess.run([str(adapter.executable),'--version'],check=True,capture_output=True,
                           text=True,timeout=15).stdout.strip()
    server,token=start_server();server.guard.adapter=adapter
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    try:
        result=subprocess.run(['node',str(Path(__file__).with_name('guard_v07_native_bridge.mjs'))],
            env=dict(os.environ,AIO_TEST_SERVER='http://127.0.0.1:'+str(server.server_port),
                     AIO_TEST_TOKEN=token,AIO_TEST_SOURCES=json.dumps([str(p) for p in paths]),
                     AIO_TEST_FORMATS=args.formats,
                     AIO_TEST_ARCHIVE=str(work/'results.zip')),
            capture_output=True,text=True,timeout=300)
        assert result.returncode==0,result.stdout+'\n'+result.stderr
        evidence=json.loads(result.stdout)
        assert len(adapter.inspections)==4,adapter.inspections
        assert all(digest(p)==hashes[p.name] for p in paths)
        from pypdf import PdfReader
        with ZipFile(work/'results.zip') as archive:
            pdfs=[name for name in archive.namelist() if name.lower().endswith('.pdf')]
            assert len(pdfs)==3
            extracted=[]
            for index,name in enumerate(pdfs):
                pdf=work/('csv-'+str(index)+'.pdf');pdf.write_bytes(archive.read(name))
                reader=PdfReader(pdf)
                text='\n'.join(page.extract_text() or '' for page in reader.pages)
                assert 'alpha' in text and 'beta' in text,text
                extracted.append({'pages':len(reader.pages),'alphaAndBetaPresent':True})
        evidence.update(engine=version,inspectionTimes=adapter.inspections,
                        exportCount=len(adapter.exports),sourceHashes=hashes,
                        sourceBytesUnchanged=True,syntheticCsvPdfText=extracted,
                        windows='NOT_RUN',qtNativeSave='NOT_RUN',
                        qualityCorpus='NOT_RUN; text presence is not a layout/quality guarantee')
        (work/'evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(evidence,ensure_ascii=False))
    finally:
        server.shutdown();server.server_close();worker.join(5)
    assert not worker.is_alive()


if __name__=='__main__':
    main()
