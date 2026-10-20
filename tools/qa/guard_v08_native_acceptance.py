"""Explicit local QA. Generates owned fixtures; never installs or uses a runner.

Run with --output <new evidence directory>. Windows execution is owner-only.
The optional pypdf check is reported separately, never silently counted PASS.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import struct
import sys
import time
import zipfile

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from app_backend.converter_guard import ConverterGuard, GuardLimits, LibreOfficeAdapter
from app_backend.converter_guard.adapter import prepare_profile
from app_backend.libreoffice_engine import convert_path


def archive(path, parts):
    with zipfile.ZipFile(path,'x',compression=zipfile.ZIP_DEFLATED) as output:
        for name,content in parts.items():output.writestr(name,content)


def fixtures(root):
    root.mkdir()
    archive(root/'document.docx',{
      '[Content_Types].xml':'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
      '_rels/.rels':'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="r1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
      'word/document.xml':'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:rPr><w:b/></w:rPr><w:t>AIO_DOCUMENT_MARKER Проверка документа</w:t></w:r></w:p><w:tbl><w:tblPr><w:tblW w:w="9000" w:type="dxa"/></w:tblPr><w:tblGrid><w:gridCol w:w="4500"/><w:gridCol w:w="4500"/></w:tblGrid><w:tr><w:tc><w:p><w:r><w:t>AIO_TABLE_LEFT</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>AIO_TABLE_RIGHT</w:t></w:r></w:p></w:tc></w:tr></w:tbl><w:p/><w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134"/></w:sectPr></w:body></w:document>'})
    archive(root/'sheet.xlsx',{
      '[Content_Types].xml':'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
      '_rels/.rels':'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="r1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
      'xl/workbook.xml':'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="AIO" sheetId="1" r:id="r1"/></sheets></workbook>',
      'xl/_rels/workbook.xml.rels':'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="r1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
      'xl/worksheets/sheet1.xml':'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>AIO_SHEET_MARKER</t></is></c><c r="B1"><v>1234.5</v></c></row><row r="2"><c r="A2" t="inlineStr"><is><t>Проверка</t></is></c><c r="B2"><f>SUM(B1,10)</f><v>1244.5</v></c></row></sheetData></worksheet>'})
    (root/'table.csv').write_text('name,amount\nalpha,123\nbeta,456\n',encoding='utf-8')
    pixels=bytes((0,0,255,0,255,0,0,0,255,0,0,255,255,255,0,0))
    bmp=b'BM'+struct.pack('<IHHI',54+len(pixels),0,0,54)+struct.pack('<IiiHHIIiiII',40,2,2,1,24,0,len(pixels),2835,2835,0,0)+pixels
    (root/'picture.bmp').write_bytes(bmp)
    return {'docx':root/'document.docx','xlsx':root/'sheet.xlsx','csv':root/'table.csv','bmp':root/'picture.bmp'}


def zip_text(path):
    with zipfile.ZipFile(path) as z:
        assert z.testzip() is None,'Damaged ZIP result: '+path.name
        return '\n'.join(z.read(name).decode('utf-8',errors='replace') for name in z.namelist() if name.endswith('.xml'))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--compare-cli',action='store_true')
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    sources=fixtures(args.output/'fixtures')
    hashes={key:hashlib.sha256(path.read_bytes()).hexdigest() for key,path in sources.items()}
    adapter=LibreOfficeAdapter()
    assert adapter.available,'Install/find the approved LibreOffice before this explicit QA run'
    guard=ConverterGuard(adapter=adapter,limits=GuardLimits(file_timeout=90,preflight_timeout=90),
                         recovery_parent=args.output/'owned-jobs')
    identity=None
    started=time.monotonic()
    try:
        identity=guard.create([{'id':key,'name':path.name,'size':path.stat().st_size}
                               for key,path in sources.items()],prepare_only=True)['jobId']
        for key,path in sources.items():
            with path.open('rb') as source:guard.upload(identity,key,source,path.stat().st_size)
        pf_start=time.monotonic();inspection=guard.preflight(identity)
        preflight_ms=round((time.monotonic()-pf_start)*1000)
        expected={'docx':['pdf','odt','docx'],'xlsx':['pdf','xlsx','csv'],
                  'csv':['pdf','xlsx','dbf'],'bmp':['pdf','png']}
        eligible={f['id']:set(f['verifiedFileIds']) for f in inspection['formats']}
        for key,formats in expected.items():
            for target in formats:assert key in eligible.get(target,set()),'Unconfirmed native route: '+key+' -> '+target
        assert 'bmp' not in eligible.get('dbf',set()),'BMP -> DBF must not be admitted'
        assert 'csv' not in eligible.get('docx',set()),'CSV -> DOCX must not be admitted'
        guard.start(identity,file_ids=list(sources),plan=[{'fileId':key,'formats':values} for key,values in expected.items()],only_verified=True)
        revision=-1
        while True:
            state=guard.status(identity)
            if state['revision']!=revision:
                revision=state['revision'];current=state['currentTask']
                print(json.dumps({'completed':state['completed'],'total':state['total'],'current':current},ensure_ascii=True),flush=True)
            if state['finished']:break
            time.sleep(.1)
        assert state['failed']==0 and state['succeeded']==11,guard.report(identity)
        saved=guard.save_folder(state['result']['ticket'],args.output)
        saved_root=args.output/saved['name']
        outputs={(a['fileId'],a['format']):saved_root/a['archiveName'] for a in state['result']['outputs']}
        for key,path in sources.items():assert hashlib.sha256(path.read_bytes()).hexdigest()==hashes[key]
        for target in ('odt','docx'):
            text=zip_text(outputs['docx',target])
            for marker in ('AIO_DOCUMENT_MARKER','AIO_TABLE_LEFT','AIO_TABLE_RIGHT'):assert marker in text
        assert 'AIO_SHEET_MARKER' in zip_text(outputs['xlsx','xlsx'])
        assert b'AIO_SHEET_MARKER' in outputs['xlsx','csv'].read_bytes()
        assert struct.unpack('<I',outputs['csv','dbf'].read_bytes()[4:8])[0]>=2
        assert outputs['bmp','png'].read_bytes().startswith(b'\x89PNG\r\n\x1a\n')
        quality={'officeContainers':'PASS','textAndTableMarkers':'PASS','sourceBytesUnchanged':'PASS','pdfText':'NOT_RUN'}
        try:
            from pypdf import PdfReader
            text=''.join(p.extract_text() or '' for p in PdfReader(outputs['docx','pdf']).pages)
            for marker in ('AIO_DOCUMENT_MARKER','AIO_TABLE_LEFT','AIO_TABLE_RIGHT'):assert marker in text
            quality['pdfText']='PASS'
        except ImportError:pass
        baseline={}
        if args.compare_cli:
            import threading
            for key,target in (('docx','pdf'),('xlsx','csv')):
                workspace=args.output/('baseline-'+key)
                prepare_profile(workspace/'profile')
                tick=time.monotonic()
                paths=convert_path(sources[key],target,workspace,executable=adapter.executable,cancel=threading.Event())
                baseline[key+'Ms']=round((time.monotonic()-tick)*1000)
                actual=next(p for p in paths if p.suffix=='.'+target)
                if target=='csv':
                    assert actual.read_bytes()==outputs[key,target].read_bytes(),'CSV differs from original CLI defaults'
                    quality['csvMatchesNativeCLI']='PASS'
                elif quality['pdfText']=='PASS':
                    before=PdfReader(actual);after=PdfReader(outputs[key,target])
                    assert len(before.pages)==len(after.pages)
                    assert [(tuple(p.mediabox)) for p in before.pages]==[(tuple(p.mediabox)) for p in after.pages]
                    quality['pdfPageGeometryMatchesCLI']='PASS'
        result={'status':'PASS','scope':'real local LibreOffice, generated 4-file / 11-export corpus; not full visual QA',
                'engine':adapter.executable.name,'preflightMs':preflight_ms,'elapsedMs':round((time.monotonic()-started)*1000),
                'officeStarts':state['officeStarts'],'quality':quality,'baseline':baseline,
                'sourceSHA256':hashes,'report':guard.report(identity)}
        (args.output/'evidence.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({k:v for k,v in result.items() if k!='report'},ensure_ascii=False),flush=True)
    finally:
        guard.close()


if __name__=='__main__':main()
