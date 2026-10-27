"""Native DBF/JPEG matrix and read-only TIFF/ORP probes on Windows Qt/LO."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import threading

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from test_native_routes_b13_b31 import cp1251_dbf
from test_guard_epub_v17 import create_epub
from app_backend.converter_guard import LibreOfficeAdapter
from app_backend.converter_guard.media_fidelity import inspect_media
from app_backend.converter_guard.qt_media import _frames, _qt, _write_tiff
from openpyxl import load_workbook


def sha(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def pixels(path):
    QImage=_qt()[4];reader=_qt()[5](str(path));reader.setDecideFormatFromContent(True)
    image=reader.read();assert not image.isNull(),reader.errorString()
    image=image.convertToFormat(QImage.Format.Format_RGBA8888)
    bits=image.constBits();stride=image.bytesPerLine();width=image.width()
    raw=b''.join(bytes(bits[y*stride:y*stride+width*4]) for y in range(image.height()))
    assert len(set(raw))>4,'blank or trivial native JPEG output'
    return {'width':image.width(),'height':image.height(),'rgbaSha256':hashlib.sha256(raw).hexdigest()}


def tiff_probe(root, report, cancel):
    QImage=_qt()[4];Reader=_qt()[5]
    report['tiffProbe']=[]
    for count in (6,7,8):
        source=root/f'tiff-{count}.tiff'
        def images():
            for i in range(count):
                image=QImage(16,12,QImage.Format.Format_RGBA8888);image.fill(0xff000000|((i+1)*0x10203))
                yield image
        with source.open('xb') as stream:
            _write_tiff(stream,images(),{'width':16,'height':12,'frames':1,'pages':count},cancel)
        info=inspect_media(source,validate_container=True)
        row={'pages':count,'sha256':sha(source),'profile':info,'currentReader':[],'freshReader':[]}
        reader=Reader(str(source))
        for i in range(count):
            before=reader.currentImageNumber();jump=True if i==0 else reader.jumpToImage(i)
            image=reader.read() if jump else None
            row['currentReader'].append({'index':i,'before':before,'jump':jump,
                'after':reader.currentImageNumber(),'decoded':image is not None and not image.isNull()})
        for i in range(count):
            fresh=Reader(str(source));jump=True if i==0 else fresh.jumpToImage(i)
            image=fresh.read() if jump else None
            row['freshReader'].append({'index':i,'jump':jump,'decoded':image is not None and not image.isNull()})
            del fresh
        try:
            scratch=root/f'tiff-scratch-{count}';scratch.mkdir()
            row['currentAioDecoded']=sum(1 for _ in _frames(source,scratch,info,cancel))
        except Exception as error:
            row['currentAioError']={'code':getattr(error,'code',type(error).__name__),'reason':str(error)}
        report['tiffProbe'].append(row)
        print('TIFF_PROBE='+json.dumps(row,ensure_ascii=True),flush=True)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--executable',type=Path,required=True);args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    report={'status':'RUNNING','scope':'generated native controls; not preserved 680-file FULLTEST',
            'dbf':[],'jpeg':[],'negative':[],'sourceSHA256':{},'orp':{'status':'not_tested','reason':'no_positive_Base_report_fixture'}}
    cancel=threading.Event();adapter=LibreOfficeAdapter(args.executable)
    try:
        tiff_probe(args.output,report,cancel)
        with adapter.batch(args.output/'office'):
            native=adapter._registry()
            report['orp']['installedExporters']=sorted(native.export_candidates.get(('orp','com.sun.star.report.ReportDefinition'),()))
            broken=bytearray(cp1251_dbf());broken[8]=0
            for name,data,expected in (('unknown-driver',cp1251_dbf(driver=0),'DBF_ENCODING_NOT_CONFIRMED'),
                    ('broken-header',bytes(broken),'DBF_SOURCE_LAYOUT_INVALID'),
                    ('conflicting-encoding',cp1251_dbf(utf8=True),'DBF_ENCODING_AMBIGUOUS'),
                    ('truncated',cp1251_dbf()[:-25],'DBF_SOURCE_TRUNCATED')):
                source=args.output/(name+'.dbf');source.write_bytes(data);before=sha(source);started=[]
                for operation in ('preflight','convert'):
                    try:
                        if operation=='preflight':adapter.preflight(source,args.output/(name+'-inspect'),cancel=cancel,timeout=30)
                        else:adapter.convert(source,'xlsx',args.output/(name+'-output'),cancel=cancel,timeout=30,on_started=lambda:started.append(True))
                    except GuardError as error:assert error.code==expected,(name,error.code,expected)
                    else:raise AssertionError('ambiguous/damaged DBF reached native import')
                assert not started and sha(source)==before
                report['negative'].append({'source':source.name,'sha256':before,'status':'SAFE_REJECTION','code':expected,'nativeExportStarted':False})
            # Explicit pre-import cancellation/deadline boundaries. These do
            # not claim to time out a long-running DBF decode (owned native
            # import cancellation is covered separately by the session QA).
            control_source=args.output/'budget-control.dbf';control_source.write_bytes(cp1251_dbf())
            control_hash=sha(control_source)
            for name,control,timeout,expected in (
                    ('cancelled',threading.Event(),30,('CANCELLED',)),
                    ('expired-budget',threading.Event(),1e-9,('ENGINE_TIMEOUT','PREFLIGHT_TIMEOUT'))):
                if name=='cancelled':control.set()
                try:
                    value=adapter.preflight(control_source,args.output/(name+'-inspect'),cancel=control,timeout=timeout)
                    assert value.get('state')!='detected' and value.get('code') in expected,value
                    code=value['code']
                except GuardError as error:
                    assert error.code in expected,error.code;code=error.code
                assert sha(control_source)==control_hash
                report['negative'].append({'source':control_source.name,'sha256':control_hash,'status':'SAFE_REJECTION',
                    'code':code,'boundary':name,'scope':'before_native_import','nativeExportStarted':False})
            for level in range(1,9):
                source=args.output/f'dbf-{level}.dbf';source.write_bytes(cp1251_dbf(level))
                before=sha(source);report['sourceSHA256'][source.name]=before
                proof=adapter.preflight(source,args.output/f'dbf-inspect-{level}',cancel=cancel,timeout=90)
                assert proof.get('state')=='detected',proof
                assert proof.get('selectedImportFilter')=='dBase' and proof.get('selectedImportOptions')=='34',proof
                adapter.prepare_source(proof)
                outputs=adapter.convert(source,'xlsx',args.output/f'dbf-xlsx-{level}',cancel=cancel,timeout=120,on_started=lambda:None)
                primary=next(p for p in outputs if p.suffix=='.xlsx')
                book=load_workbook(primary,read_only=True,data_only=True,keep_links=False)
                try:rows=list(book.active.values)
                finally:book.close()
                assert len(rows)==level+3,(level,rows)
                for i,values in enumerate(rows[1:],1):
                    assert str(values[0]).strip()==f'{i:03d}' and values[1].strip()==f'Строка {i} уровень {level}',values
                    assert float(values[2])==i*1.25,values
                assert sha(source)==before
                row={'level':level,'status':'PASS','mode':adapter.execution_mode(),
                     'nativeImportFilter':proof['selectedImportFilter'],'importOptions':proof['selectedImportOptions'],
                     'records':len(rows)-1,'lastRecord':rows[-1],'sourceSha256':before,'outputSha256':sha(primary)}
                report['dbf'].append(row);print('DBF_CASE='+json.dumps(row,ensure_ascii=True),flush=True)
            for level in range(1,9):
                html=args.output/f'web-{level}.html'
                body='<html><head><meta charset="utf-8"/></head><body><h1>AIO JPEG L%d</h1><p>Кириллица и строки.</p>%s</body></html>'%(level,
                    ''.join('<p>Контроль %d / строка %d</p>'%(level,i) for i in range(level)))
                html.write_text(body,encoding='utf-8')
                htmlproof=adapter.preflight(html,args.output/f'html-inspect-{level}',cancel=cancel,timeout=90)
                assert htmlproof.get('state')=='detected',htmlproof
                sources={'html':html}
                for ext in ('htm','xhtml'):
                    path=args.output/f'web-{level}.{ext}';path.write_text(body,encoding='utf-8');sources[ext]=path
                epub=args.output/f'book-{level}.epub';create_epub(epub);sources['epub']=epub
                adapter.prepare_source(htmlproof)
                oth_outputs=adapter.convert(html,'oth',args.output/f'create-oth-{level}',cancel=cancel,timeout=120,on_started=lambda:None)
                sources['oth']=next(p for p in oth_outputs if p.suffix=='.oth')
                for extension,source in sources.items():
                    before=sha(source);report['sourceSHA256'][str(source.relative_to(args.output))]=before
                    proof=adapter.preflight(source,args.output/f'inspect-{extension}-{level}',cancel=cancel,timeout=90)
                    assert proof.get('state')=='detected',proof
                    controls={}
                    for target in ('jpg','jpeg','jfif','jif','jpe','pdf'):
                        decision=adapter.route_decision(target,proof)
                        assert decision=='registered',(extension,level,target,decision,proof)
                        adapter.prepare_source(proof)
                        paths=adapter.convert(source,target,args.output/f'jpeg-{extension}-{level}-{target}',
                            cancel=cancel,timeout=120,on_started=lambda:None)
                        primary=next(p for p in paths if p.suffix.lstrip('.') in (target,'jpg','jpeg'))
                        if target=='pdf':
                            assert primary.read_bytes().startswith(b'%PDF-');continue
                        with primary.open('rb') as stream:assert stream.read(3)==b'\xff\xd8\xff'
                        image=pixels(primary)
                        if target in ('jpg','jpeg'):controls[target]=image
                        else:
                            assert image==controls['jpg']==controls['jpeg'],(extension,level,target,image,controls)
                            row={'extension':extension,'level':level,'target':target,'status':'PASS',
                                 'nativeImportFilter':proof.get('selectedImportFilter'),
                                 'nativeTextDocument':proof.get('nativeTextDocument'),
                                 'mode':adapter.execution_mode(),'nativeExportFilter':adapter.export_info().get('filter'),
                                 'decoded':image,'sourceSha256':before,'outputSha256':sha(primary)}
                            report['jpeg'].append(row);print('JPEG_CASE='+json.dumps(row,ensure_ascii=True),flush=True)
                    assert sha(source)==before
        assert len(report['dbf'])==8 and len(report['jpeg'])==120 and len(report['negative'])==6
        report['status']='PASS'
    except Exception as error:
        report.update(status='FAIL',error=type(error).__name__,code=getattr(error,'code',None),
                      diagnostic=getattr(error,'diagnostic',None),reason=str(error))
        raise
    finally:
        (args.output/'evidence.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print('NATIVE_ROUTE_SUMMARY='+json.dumps({k:v for k,v in report.items() if k not in ('dbf','jpeg','tiffProbe')},ensure_ascii=True),flush=True)


if __name__=='__main__':main()
