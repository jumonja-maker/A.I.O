"""Targeted native Writer text export controls; no FULLTEST or user inputs."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from app_backend.converter_guard import LibreOfficeAdapter
from test_writer_text_tail_v22 import seed, BODY


def digest(path):
    with path.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--executable',type=Path,required=True)
    p.add_argument('--require-mode',choices=['native_uno','native_cli'])
    args=p.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    sources={}
    for ext in ('docx','docm'):
        source=args.output/('terminal-table.'+ext);seed(source);sources[ext]=source
    good=args.output/'healthy.docx';seed(good,body=BODY+'<w:p/>');sources['healthy']=good
    before={key:digest(source) for key,source in sources.items()}
    adapter=LibreOfficeAdapter(args.executable);cancel=threading.Event();cases=[]
    report={'status':'RUNNING','scope':'synthetic native Writer text regression; not a FULLTEST',
            'cases':cases,'sourceSHA256':before,'telemetryEnabled':False}
    try:
        with adapter.batch(args.output/'office'):
            proofs={}
            for key,source in sources.items():
                proof=adapter.preflight(source,args.output/('inspect-'+key),cancel=cancel,timeout=60)
                assert proof.get('state')=='detected',(key,proof)
                proofs[key]=proof;adapter.prepare_source(proof)
                for target in (('txt',) if key=='healthy' else ('txt','csv','tsv','tab')):
                    assert adapter.route_decision(target,proof)=='registered',(key,target,proof)
                    tick=time.monotonic();work=args.output/('export-'+key+'-'+target)
                    paths=adapter.convert(source,target,work,cancel=cancel,timeout=25,on_started=lambda:None)
                    mode=adapter.execution_mode();info=adapter.export_info()
                    if args.require_mode:assert mode==args.require_mode,(key,target,mode)
                    primary=[x for x in paths if x.suffix.lower()=='.'+target]
                    assert len(primary)==1,(key,target,paths)
                    text=primary[0].read_text(encoding='utf-8-sig')
                    for marker in ('AIO_TEXT_BODY_22 Русский','001','1.43','Текст ячейки','AIO_LAST_CELL_22'):
                        assert text.count(marker)==1,(key,target,marker,text)
                    assert primary[0].stat().st_size<4096,(key,target,'unexpected repeated text')
                    prepared=info.get('sourcePreparation',{}).get('kind')=='writer_text_terminal_table'
                    assert prepared==(key!='healthy'),(key,info)
                    assert not (work/'writer-text-input').exists(),'Private input retained'
                    cases.append({'source':key,'target':target,'status':'PASS','mode':mode,
                        'elapsedMs':round((time.monotonic()-tick)*1000),'bytes':primary[0].stat().st_size,
                        'sha256':digest(primary[0]),'prepared':prepared,'allMarkersExactlyOnce':True})
                    print('V22_NATIVE_CASE='+json.dumps(cases[-1]),flush=True)
            # Other requested output types must not receive the text-only preparation.
            adapter.prepare_source(proofs['docx'])
            for target in ('docx','pdf'):
                work=args.output/('control-'+target)
                paths=adapter.convert(sources['docx'],target,work,cancel=cancel,timeout=60,on_started=lambda:None)
                assert paths
                assert adapter.export_info().get('sourcePreparation',{}).get('kind')!='writer_text_terminal_table'
                cases.append({'source':'docx','target':target,'status':'PASS','mode':adapter.execution_mode(),'textPreparationUsed':False})
        # Retain CLI as a separate native path, with the same confirmed source proof.
        for key in ('docx','docm'):
            direct=LibreOfficeAdapter(args.executable);direct.prepare_source(proofs[key])
            work=args.output/('cli-'+key);tick=time.monotonic()
            paths=direct.convert(sources[key],'txt',work,cancel=cancel,timeout=25,on_started=lambda:None)
            assert direct.execution_mode()=='native_cli'
            text=paths[0].read_text(encoding='utf-8-sig')
            assert text.count('AIO_LAST_CELL_22')==1 and text.count('1.43')==1
            assert not (work/'writer-text-input').exists()
            cases.append({'source':key,'target':'txt','status':'PASS','mode':'native_cli',
                'elapsedMs':round((time.monotonic()-tick)*1000),'bytes':paths[0].stat().st_size,
                'sha256':digest(paths[0]),'allMarkersExactlyOnce':True})
        assert before=={key:digest(source) for key,source in sources.items()}
        report.update(status='PASS',sourceBytesUnchanged=True)
    except Exception as error:
        report.update(status='FAIL',error=type(error).__name__,reason=str(error))
        raise
    finally:
        (args.output/'evidence.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print('AIO_V22_NATIVE='+json.dumps(report,ensure_ascii=True),flush=True)


if __name__=='__main__':main()
