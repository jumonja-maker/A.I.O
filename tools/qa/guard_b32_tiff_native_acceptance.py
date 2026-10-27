"""24 generated TIFF page cases; actual Qt, independent container/pixel checks."""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys
import threading
import zlib

ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from app_backend.converter_guard import LibreOfficeAdapter
from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard.media_fidelity import inspect_media
from app_backend.converter_guard.qt_media import _qt,_write_tiff

W,H=32,24
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def expected(index):
    return b''.join(bytes(((x*7+index*31)%256,(y*11+index*47)%256,(x+y+index*61)%256,255))
                    for y in range(H) for x in range(W))
def rgba(image):
    QImage=_qt()[4];image=image.convertToFormat(QImage.Format.Format_RGBA8888)
    assert (image.width(),image.height())==(W,H)
    bits=image.constBits();stride=image.bytesPerLine()
    return b''.join(bytes(bits[y*stride:y*stride+W*4]) for y in range(H))
def png_frames(path):
    raw=path.read_bytes();assert raw.startswith(b'\x89PNG\r\n\x1a\n')
    pos=8;frames=[];compressed=None
    while pos+12<=len(raw):
        n=struct.unpack('>I',raw[pos:pos+4])[0];kind=raw[pos+4:pos+8];data=raw[pos+8:pos+8+n];pos+=12+n
        if kind==b'fcTL':
            if compressed is not None:frames.append(compressed)
            assert struct.unpack('>II',data[4:12])==(W,H);compressed=bytearray()
        elif kind==b'IDAT':compressed.extend(data)
        elif kind==b'fdAT':compressed.extend(data[4:])
    if compressed is not None:frames.append(compressed)
    result=[]
    for data in frames:
        scan=zlib.decompress(data);assert len(scan)==H*(1+W*4)
        assert all(scan[y*(1+W*4)]==0 for y in range(H))
        result.append(b''.join(scan[y*(1+W*4)+1:(y+1)*(1+W*4)] for y in range(H)))
    return result
def webp_frames(path):
    raw=path.read_bytes();assert raw[:4]==b'RIFF' and raw[8:12]==b'WEBP'
    pos=12;frames=[];QImage=_qt()[4]
    while pos+8<=len(raw):
        kind=raw[pos:pos+4];n=int.from_bytes(raw[pos+4:pos+8],'little');data=raw[pos+8:pos+8+n];pos+=8+n+(n&1)
        if kind==b'ANMF':
            assert data[:6]==b'\0'*6
            assert int.from_bytes(data[6:9],'little')+1==W and int.from_bytes(data[9:12],'little')+1==H
            payload=data[16:];static=b'RIFF'+(4+len(payload)).to_bytes(4,'little')+b'WEBP'+payload
            image=QImage.fromData(static);assert not image.isNull()
            frames.append(rgba(image))
    return frames
def pdf_frames(path):
    from pypdf import PdfReader
    pages=PdfReader(path).pages;result=[]
    for page in pages:
        objects=page['/Resources'].get('/XObject',{})
        if hasattr(objects,'get_object'):objects=objects.get_object()
        images=[obj.get_object() for obj in objects.values()
                if obj.get_object().get('/Subtype')=='/Image' and obj.get_object().get('/ColorSpace')=='/DeviceRGB']
        assert len(images)==1
        image=images[0];assert (image['/Width'],image['/Height'],image['/BitsPerComponent'])==(W,H,8)
        rgb=image.get_data();assert len(rgb)==W*H*3
        result.append(b''.join(rgb[i:i+3]+b'\xff' for i in range(0,len(rgb),3)))
    return result

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--executable',type=Path,required=True);args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False);cancel=threading.Event()
    report={'status':'RUNNING','scope':'24 generated TIFF controls; not historical corpus or FULLTEST','cases':[],'negative':[]}
    try:
        adapter=LibreOfficeAdapter(args.executable);QImage=_qt()[4]
        for count in (6,7,8):
            expected_pages=[expected(i) for i in range(count)]
            for ext in ('tif','tiff'):
                source=args.output/f'pages-{count}.{ext}'
                def images():
                    for raw in expected_pages:yield QImage(raw,W,H,W*4,QImage.Format.Format_RGBA8888).copy()
                with source.open('xb') as stream:_write_tiff(stream,images(),{'width':W,'height':H,'frames':1,'pages':count},cancel)
                before=sha(source);profile=inspect_media(source,validate_container=True)
                assert profile['pages']==count
                proof=adapter.preflight(source,args.output/f'inspect-{count}-{ext}',cancel=cancel,timeout=60)
                assert proof['state']=='detected',proof
                for target,reader in (('pdf',pdf_frames),('png',png_frames),('webp',webp_frames),('apng',png_frames)):
                    adapter.prepare_source(proof)
                    paths=adapter.convert(source,target,args.output/f'export-{count}-{ext}-{target}',cancel=cancel,timeout=120,on_started=lambda:None)
                    output=next(p for p in paths if p.suffix=='.'+target);actual=reader(output)
                    assert actual==expected_pages,(count,ext,target,len(actual))
                    assert sha(source)==before
                    row={'source':source.name,'target':target,'status':'PASS','pages':len(actual),
                         'firstLastAndAllPixelsVerified':True,'sourceSha256':before,'outputSha256':sha(output),
                         'mode':adapter.execution_mode(),'checks':adapter.export_info().get('resultChecks',[])}
                    report['cases'].append(row);print('TIFF_CASE='+json.dumps(row,ensure_ascii=True),flush=True)
                broken=args.output/f'truncated-{count}-{ext}.tiff';broken.write_bytes(source.read_bytes()[:-200])
                damaged=sha(broken)
                try:
                    rejected=adapter.preflight(broken,args.output/f'bad-inspect-{count}-{ext}',cancel=cancel,timeout=60)
                except GuardError as error:
                    rejected={'state':'rejected','code':error.code}
                assert rejected['state']!='detected',rejected
                assert sha(broken)==damaged
                report['negative'].append({'status':'SAFE_REJECTION','source':broken.name,'sha256':damaged,'code':rejected.get('code')})
        assert len(report['cases'])==24;report['status']='PASS'
    except Exception as error:
        report.update(status='FAIL',reason=str(error),code=getattr(error,'code',None));raise
    finally:
        (args.output/'evidence.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print('TIFF_SUMMARY='+json.dumps({k:v for k,v in report.items() if k!='cases'},ensure_ascii=True),flush=True)

if __name__=='__main__':main()
