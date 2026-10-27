"""Native HTML positives and upfront safe exclusions, with strict negative checks."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import sys
import struct
import threading
import zlib
from xml.etree import ElementTree as ET

ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from app_backend.converter_guard import LibreOfficeAdapter,GuardError
from app_backend.converter_guard.presentation_evidence import check_presentation_html
from app_backend.converter_guard.postconditions import check_native_output
from guard_v16_bugbag_acceptance import presentation_seed,DRAW,OFFICE,SLIDE_MARKER

def chunk(kind,data):return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
PNG=base64.b64encode(b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',32,24,8,2,0,0,0))+
    chunk(b'IDAT',zlib.compress((b'\0'+bytes((70,120,200))*32)*24))+chunk(b'IEND',b'')).decode()
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def fixture(path,level,*,image=False,draw=False):
    root=ET.fromstring(presentation_seed());body=root.find('{%s}body'%OFFICE);container=body[0]
    if draw:
        root.set('{%s}mimetype'%OFFICE,'application/vnd.oasis.opendocument.graphics');container.tag='{%s}drawing'%OFFICE
    page=container[0]
    for index in range(1,level):
        duplicate=ET.fromstring(ET.tostring(page));duplicate.set('{%s}name'%DRAW,'Page%d'%index);container.append(duplicate)
    if image:
        for p in container:
            frame=ET.SubElement(p,'{%s}frame'%DRAW)
            for key,value in (('x','1cm'),('y','5cm'),('width','2cm'),('height','2cm')):
                frame.set('{urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0}'+key,value)
            img=ET.SubElement(frame,'{%s}image'%DRAW)
            ET.SubElement(img,'{%s}binary-data'%OFFICE).text=PNG
    path.write_bytes(ET.tostring(root,encoding='utf-8',xml_declaration=True))

def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--executable',type=Path,required=True);args=p.parse_args()
    args.output.mkdir(parents=True,exist_ok=False);cancel=threading.Event();adapter=LibreOfficeAdapter(args.executable)
    report={'status':'RUNNING','scope':'generated native HTML controls, not historical FULLTEST','positive':[],'excluded':[],'negative':[],
            'orp':{'status':'not_tested','reason':'no_positive_Base_ReportDefinition_fixture'}}
    try:
        with adapter.batch(args.output/'office'):
            for level in range(1,9):
                for kind,has_image,is_draw in (('text',False,False),('graphics',True,False),('draw',False,True)):
                    source=args.output/(f'{kind}-{level}'+('.fodg' if is_draw else '.fodp'));fixture(source,level,image=has_image,draw=is_draw)
                    before=sha(source);proof=adapter.preflight(source,args.output/f'inspect-{kind}-{level}',cancel=cancel,timeout=90)
                    assert proof['state']=='detected',proof
                    for target in ('html','htm'):
                        route=adapter.route_decision(target,proof)
                        if kind=='text' or kind=='draw' and level==1:
                            assert route=='registered',(kind,level,proof)
                            adapter.prepare_source(proof)
                            paths=adapter.convert(source,target,args.output/f'export-{kind}-{level}-{target}',cancel=cancel,timeout=120,on_started=lambda:None)
                            html=next(p for p in paths if p.suffix=='.'+target)
                            assert html.read_text(encoding='utf-8').count(SLIDE_MARKER)>=level
                            report['positive'].append({'status':'PASS','source':source.name,'target':target,'sourceSha256':before,
                                'outputSha256':sha(html),'profile':proof['htmlContentProfile'],'checks':adapter.export_info().get('resultChecks',[])})
                        else:
                            assert route=='unavailable',(kind,level,proof)
                            reason=adapter._registry().route_reason(target,proof)
                            adapter.prepare_source(proof)
                            with_output=args.output/f'excluded-{kind}-{level}-{target}'
                            try:adapter.convert(source,target,with_output,cancel=cancel,timeout=120,on_started=lambda:None)
                            except GuardError as error:assert error.code==reason,(error.code,reason)
                            else:raise AssertionError('unsupported route started an export')
                            assert not list(with_output.rglob('*'))
                            report['excluded'].append({'status':'SAFE_UNAVAILABLE','source':source.name,'target':target,'reason':reason,'sourceSha256':before,'profile':proof['htmlContentProfile']})
                    assert sha(source)==before
            source=args.output/'graphics-1.fodp';empty=args.output/'graphics-1.html';empty.write_text('<html><head><title>shell</title></head><body></body></html>')
            missing_dir=args.output/'missing-resource';missing_dir.mkdir()
            missing=missing_dir/empty.name;missing.write_text('<html><body><p>Control</p><img src="missing.png"></body></html>')
            for name,expected_code,check in (('empty_html','ENGINE_OUTPUT_CONTENT_EMPTY',lambda:check_native_output([empty],'html',source,cancel=cancel)),
                               ('lost_slide_graphics','ENGINE_OUTPUT_CONTENT_LOSS',lambda:check_presentation_html([empty],'html',source,cancel=cancel)),
                               ('missing_resource','ENGINE_OUTPUT_RESOURCE_MISSING',lambda:check_native_output([missing],'html',source,cancel=cancel))):
                try:check()
                except GuardError as error:
                    assert error.code==expected_code,error.code
                    report['negative'].append({'name':name,'status':'SAFE_REJECTION','code':error.code})
                else:raise AssertionError('empty output accepted')
        assert len(report['positive'])==18 and len(report['excluded'])==30 and len(report['negative'])==3
        report['status']='PASS'
    except Exception as error:report.update(status='FAIL',reason=str(error),code=getattr(error,'code',None));raise
    finally:
        (args.output/'evidence.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print('HTML_SUMMARY='+json.dumps({k:v for k,v in report.items() if k not in ('positive','excluded')},ensure_ascii=True),flush=True)

if __name__=='__main__':main()
