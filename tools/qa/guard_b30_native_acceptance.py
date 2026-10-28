"""Targeted B30/B34 Windows LO acceptance; generated corpus, not FULLTEST."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from openpyxl import Workbook
from app_backend.converter_guard import ConverterGuard, LibreOfficeAdapter, GuardLimits
from app_backend.converter_guard.sheet_outputs import read_sheet_manifest


def sha(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def fixture(path, count):
    book=Workbook();book.remove(book.active)
    for index in range(count):
        sheet=book.create_sheet(('Первый','CON','con.other','Последний')[index])
        sheet.append(['ID','Text','Amount','Double','Next','Note','Sheet','Tail'])
        for row in range(1,301):
            sheet.append([f'{row:03d}',f'Строка {row}',row+index*1000,
                f'=C{row+1}*2',f'=D{row+1}+1','line\nbreak' if row==1 else 'plain',
                index,f'END-{index}-{row}'])
    book.save(path);book.close()


def independently_check(paths, source, count):
    meta=read_sheet_manifest(paths)
    assert meta and meta['sheetCount']==count, meta
    outputs={p.name:p for p in paths}
    assert len([p for p in paths if p.suffix in ('.csv','.tab','.tsv')])==count
    seals=[]
    for entry in meta['sheets']:
        i=entry['sourceSheetIndex'];path=outputs[entry['file']]
        with path.open(encoding='utf-8-sig',newline='') as stream:
            rows=list(csv.reader(stream,delimiter=',' if path.suffix=='.csv' else '\t',strict=True))
        assert len(rows)==301 and all(len(r)==8 for r in rows),(path.name,len(rows))
        assert rows[0]==['ID','Text','Amount','Double','Next','Note','Sheet','Tail']
        for row,values in enumerate(rows[1:],1):
            assert values[0]==f'{row:03d}' and values[1]==f'Строка {row}',(i,row,values)
            amount=row+i*1000
            assert [float(v) for v in values[2:5]]==[amount,amount*2,amount*2+1],(i,row,values)
            assert values[5]==('line\nbreak' if row==1 else 'plain')
            assert float(values[6])==i and values[7]==f'END-{i}-{row}'
        seals.append({'file':path.name,'sourceSheetIndex':i,'sourceSheetName':entry['sourceSheetName'],
                      'rows':len(rows),'columns':8,'sha256':sha(path),'checkedCells':len(rows)*8})
    return seals


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--executable',type=Path,required=True);args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    adapter=LibreOfficeAdapter(args.executable)
    cancel=threading.Event();sources={};proofs={}
    report={'schema':1,'status':'RUNNING','scope':'B30/B34 synthetic 4-sheet 2400-formula and 1-sheet corpus; not historical FULLTEST',
            'cases':[],'sourceSHA256':{},'saveRecovery':None}
    def run(count,target,mode):
        source=sources[count];adapter.prepare_source(proofs[count])
        paths=adapter.convert(source,target,args.output/f'{mode}-{count}-{target}',cancel=cancel,
                              timeout=180,on_started=lambda:None)
        cells=independently_check(paths,source,count)
        assert adapter.execution_mode()==mode,adapter.execution_mode()
        assert sha(source)==report['sourceSHA256'][str(count)]
        row={'count':count,'target':target,'mode':mode,'status':'PASS','outputs':cells,
             'nativeImportFilter':proofs[count].get('selectedImportFilter'),
             'export':adapter.export_info()}
        report['cases'].append(row)
        print('B30_CASE='+json.dumps(row,ensure_ascii=True),flush=True)
    try:
        for count in (4,1):
            path=args.output/f'book-{count}.xlsx';fixture(path,count);sources[count]=path
            report['sourceSHA256'][str(count)]=sha(path)
        with adapter.batch(args.output/'office'):
            for count,source in sources.items():
                proofs[count]=adapter.preflight(source,args.output/f'preflight-{count}',cancel=cancel,timeout=90)
                assert proofs[count].get('state')=='detected',proofs[count]
                for target in ('csv','tab','tsv'):
                    assert adapter.route_decision(target,proofs[count])=='registered'
                    run(count,target,'native_uno')
        for target in ('csv','tab','tsv'):run(4,target,'native_cli')
        # Real queue, durable sheet provenance, partial save and recovery.
        guard=ConverterGuard(adapter=LibreOfficeAdapter(args.executable),
            limits=GuardLimits(file_timeout=180),recovery_parent=args.output/'owned-jobs')
        try:
            source=sources[4]
            job=guard.create([{'id':'book','name':source.name,'size':source.stat().st_size}],
                             ['csv'],retain_sources=True)['jobId']
            with source.open('rb') as stream:guard.upload(job,'book',stream,source.stat().st_size)
            guard.start(job,archive_mode='on_demand')
            deadline=time.monotonic()+240
            while True:
                state=guard.status(job)
                if state['finished']:break
                if time.monotonic()>deadline:raise TimeoutError('B30 queue did not finish')
                time.sleep(.1)
            assert state['succeeded']==1 and state['failed']==0,guard.report(job)
            selected=next(o for o in state['result']['outputs'] if o['sourceSheet'])
            destination=args.output/'single-saved.csv'
            with guard.open_result(selected['ticket']) as (stream,_,__):destination.write_bytes(stream.read())
            guard.verify_saved_download(selected['ticket'],destination)
            guard.confirm_saved(job,selected['ticket'])
            task=guard.report(job)['files'][0]['targets'][0]
            assert task['saveStatus']=='partial' and task['qualityCompared'] is False
            guard.close()
            guard=ConverterGuard(adapter=LibreOfficeAdapter(args.executable),recovery_parent=args.output/'owned-jobs')
            state=guard.status(job)
            sheets=[o for o in state['result']['outputs'] if o['sourceSheet']]
            assert len(sheets)==4 and sum(o['saved'] for o in sheets)==1
            assert sum(o['readbackVerified'] for o in sheets)==1
            saved=guard.save_folder(state['result']['ticket'],args.output)
            assert saved['savedAll'] and saved['readbackVerified']
            task=guard.report(job)['files'][0]['targets'][0]
            assert task['saveStatus']=='saved' and task['readbackVerified'] and not task['qualityCompared']
            root=args.output/saved['name']
            disk=json.loads((root/'AIO-results-manifest.json').read_text(encoding='utf-8'))
            for item in disk['outputs']:
                assert sha(root/item['path'])==item['sha256'] and item['readbackVerified']
            report['saveRecovery']={'status':'PASS','readback':True,'sheets':4,'outputs':len(disk['outputs']),
                                    'partialSaveRestored':True,'sourceSha256':sha(source)}
        finally:guard.close()
        assert all(sha(p)==report['sourceSHA256'][str(n)] for n,p in sources.items())
        report['status']='PASS'
    except Exception as error:
        report.update(status='FAIL',error=type(error).__name__,code=getattr(error,'code',None),
                      diagnostic=getattr(error,'diagnostic',None),reason=str(error))
        raise
    finally:
        (args.output/'evidence.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print('B30_SUMMARY='+json.dumps({k:v for k,v in report.items() if k!='cases'},ensure_ascii=True),flush=True)


if __name__=='__main__':main()
