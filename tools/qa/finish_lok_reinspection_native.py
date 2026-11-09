"""Require repeated Linux LOK imports to release their own source locks.

These generated controls exercise the real C ABI with fresh, owned sources.
No foreign lock is deleted. Windows has a separate native UNO acceptance path.
The HTML-to-PDF control also reads an embedded local image independently, so
cleanup cannot be implemented by moving imports away from relative assets.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import threading
import time
from zipfile import ZipFile, ZIP_DEFLATED
import zlib

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def png(width, height, colour):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind+data))
    rows = (b'\0' + bytes(colour)*width)*height
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width,height,8,2,0,0,0)) + chunk(b'IDAT',zlib.compress(rows)) + chunk(b'IEND',b'')


def make_sources(folder):
    assets = folder/'assets'
    assets.mkdir(parents=True)
    (assets/'marker.png').write_bytes(png(37, 19, (17,101,203)))
    marker = 'AIO_LOK_REINSPECTION_00123'
    (folder/'source.html').write_text('<!doctype html><html><head><meta charset="utf-8"></head>'
        '<body><p>'+marker+' Москва</p><img src="assets/marker.png" width="148" height="76"></body></html>',encoding='utf-8')
    (folder/'source.csv').write_text('ID,VALUE\n000123,1.25\n000007,2.5\n',encoding='utf-8')
    for ext in ('tab','tsv'):
        (folder/('source.'+ext)).write_text('ID\tVALUE\n000123\t1.25\n000007\t2.5\n',encoding='utf-8')
    (folder/'source.dif').write_text('TABLE\n0,1\n"AIO"\nVECTORS\n0,2\n""\nTUPLES\n0,2\n""\nDATA\n0,0\n""\n-1,0\nBOT\n1,0\n"ID"\n1,0\n"VALUE"\n-1,0\nBOT\n1,0\n"000123"\n0,25\nV\n-1,0\nEOD\n',encoding='ascii')
    office='urn:oasis:names:tc:opendocument:xmlns:office:1.0'
    text='urn:oasis:names:tc:opendocument:xmlns:text:1.0'
    (folder/'source.fodt').write_text('<office:document xmlns:office="'+office+'" '
        'xmlns:text="'+text+'" office:mimetype="application/vnd.oasis.opendocument.text" office:version="1.2">'
        '<office:body><office:text><text:p>'+marker+'</text:p></office:text></office:body></office:document>',encoding='utf-8')
    with ZipFile(folder/'source.docx','x',compression=ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr('_rels/.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        z.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:body><w:p><w:r><w:t>'+marker+'</w:t></w:r></w:p></w:body></w:document>')
    return marker


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if sys.platform != 'linux':
        parser.error('This acceptance requires the real Linux LOK path')
    args.output.mkdir(parents=True,exist_ok=False)
    from app_backend.converter_guard import LibreOfficeAdapter
    from app_backend.converter_guard.adapter import prepare_profile
    from app_backend.converter_guard.preflight import inspect_source
    from pypdf import PdfReader
    source_root=args.output/'sources'
    source_root.mkdir()
    marker=make_sources(source_root)
    sources=[source_root/('source.'+ext) for ext in ('html','csv','dif','tab','tsv','docx','fodt')]
    originals={p:sha(p) for p in [*sources,source_root/'assets/marker.png']}
    adapter=LibreOfficeAdapter(args.executable)
    assert adapter.available and adapter._registry().valid, 'Required native engine/registry missing'
    cancel=threading.Event()
    report={'schema':1,'status':'INCOMPLETE','scope':'real Linux native C ABI import, repeated same source path; native HTML relative asset export',
        'sourceSha':os.environ.get('AIO_QA_SOURCE_SHA') or subprocess.run(['git','rev-parse','HEAD'],cwd=ROOT,check=True,capture_output=True,text=True).stdout.strip(),
        'preflightNativeSha256':sha(ROOT/'app_backend/converter_guard/preflight_native.py'),
        'cases':[], 'globalOfficeShutdownRequired':False,
        'lockDeletionUsed':False, 'sourceCopiesUsedByRuntime':False, 'qualityCompared':False}
    try:
        for source in sources:
            for attempt in (1,2):
                tick=time.monotonic()
                proof=inspect_source(args.executable,source,args.output/(source.suffix[1:]+'-'+str(attempt)),
                    cancel=cancel,timeout=45,prepare_profile=prepare_profile,native=adapter._registry())
                expected=1 if source.suffix in ('.csv','.dif','.tab','.tsv') else 0
                lock=source.with_name('.~lock.'+source.name+'#')
                case={'source':source.name,'attempt':attempt,'proof':proof,'expectedNativeKind':expected,
                      'sourceUnchanged':sha(source)==originals[source], 'ownSourceLockExists':lock.exists(),
                      'seconds':round(time.monotonic()-tick,3)}
                report['cases'].append(case)
                assert proof.get('state')=='detected' and proof.get('basis')=='native_preflight_document_kind' and proof.get('nativeKind')==expected,case
                assert case['sourceUnchanged'] and not case['ownSourceLockExists'],case
        html=source_root/'source.html'
        proof=adapter.preflight(html,args.output/'html-export-preflight',cancel=cancel,timeout=45)
        assert proof.get('state')=='detected',proof
        adapter.prepare_source(proof)
        paths=adapter.convert(html,'pdf',args.output/'relative-asset-pdf',cancel=cancel,timeout=90,on_started=lambda:None)
        pdf=next(p for p in paths if p.suffix=='.pdf')
        reader=PdfReader(pdf)
        text='\n'.join(page.extract_text() or '' for page in reader.pages)
        images=[image.image.convert('RGB') for page in reader.pages for image in page.images]
        matching=[]
        for image in images:
            pixel=image.getpixel((image.width//2,image.height//2))
            if max(abs(a-b) for a,b in zip(pixel,(17,101,203)))<=5:
                matching.append({'size':list(image.size),'centreRgb':list(pixel)})
        report['relativeAsset']={'status':'PASS' if marker in text and matching else 'FAIL',
            'pages':len(reader.pages),'textMarkerPresent':marker in text,'embeddedImages':len(images),
            'matchingImageReadback':matching,'pdfSha256':sha(pdf),'runtimeSourceParentPreserved':True}
        assert report['relativeAsset']['status']=='PASS',report['relativeAsset']
        assert len(report['cases']) == 14
        report['status']='PASS'
    except BaseException as error:
        report['status']='FAIL'
        report['error']={'type':type(error).__name__,'message':str(error)[-4000:],'code':getattr(error,'code',None)}
    finally:
        report['allOriginalHashesUnchanged']=all(sha(p)==h for p,h in originals.items())
        if not report['allOriginalHashesUnchanged']:
            report['status']='FAIL'
        report['reinspectionNativeImportCount']=len(report['cases'])
        (args.output/'evidence.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print('AIO_POSIX_REINSPECTION='+json.dumps(report,ensure_ascii=True),flush=True)
    return 0 if report['status']=='PASS' else 1


if __name__=='__main__':
    raise SystemExit(main())
