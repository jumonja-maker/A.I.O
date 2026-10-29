"""Native AIO fidelity controls plus original CSV samples, not the complete FULLTEST."""
import argparse
import hashlib
import json
import struct
from pathlib import Path
import sys
import threading
import time
from zipfile import ZipFile, ZIP_DEFLATED
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from app_backend.converter_guard import LibreOfficeAdapter
from app_backend.converter_guard.errors import GuardError


def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    p=argparse.ArgumentParser();p.add_argument('--executable',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    # This native acceptance driver runs headless, outside the desktop app.
    # QtPdf requires an owned QGuiApplication; production already has one.
    # Keep it alive for every page-rendering case (CI sets offscreen platform).
    from PySide6.QtGui import QGuiApplication
    qa_gui_app = QGuiApplication.instance() or QGuiApplication([])
    from openpyxl import Workbook
    from PIL import Image
    book=Workbook();book.remove(book.active)
    for n in range(4):
        sheet=book.create_sheet('Sheet_'+str(n+1));sheet.append(['ID','Marker','Amount','Name']);sheet.append(['00123','SHEET_'+str(n+1),14.5+n,'Товар Б'])
    source=args.output/'four-sheets.xlsx';book.save(source);book.close()
    raster=args.output/'original.tga';Image.new('RGB',(24,16),(12,34,56)).save(raster)
    jpeg=args.output/'original.jpg';Image.new('RGB',(24,16),(23,45,67)).save(jpeg)
    # Exact PBM/PGM/PPM samples. These must not become white A4 Draw pages.
    pnm_sources={}
    pnm_data={
        'pbm': b'P4\n40 20\n'+bytes([0xAA,0x55,0xAA,0x55,0xAA]*20),
        'pgm': b'P5\n40 20\n255\n'+bytes(i % 256 for i in range(800)),
        'ppm': b'P6\n40 20\n255\n'+bytes((i*3) % 256 for i in range(2400)),
    }
    for kind, raw in pnm_data.items():
        path=args.output/('original.'+kind);path.write_bytes(raw)
        pnm_sources[kind]=path
    slides=args.output/'slides.fodp'
    slides.write_text('''<?xml version="1.0" encoding="UTF-8"?><office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" office:mimetype="application/vnd.oasis.opendocument.presentation" office:version="1.2"><office:body><office:presentation>'''+''.join('<draw:page draw:name="Slide%d"><draw:frame svg:x="1cm" svg:y="1cm" svg:width="15cm" svg:height="4cm"><draw:text-box><text:p>AIO_SLIDE_%d TEXT_00123</text:p></draw:text-box></draw:frame></draw:page>'%(i,i) for i in range(1,4))+'''</office:presentation></office:body></office:document>''',encoding='utf-8')
    legacy_sources={}
    xbm=args.output/'01_LEVEL_1.xbm'
    xbm.write_bytes(
        b'#define 01_LEVEL_1_width 40\n#define 01_LEVEL_1_height 20\n'
        +b'static char 01_LEVEL_1_bits[] = {\n'
        +(',\n'.join(', '.join('0xAA' if j%2==0 else '0x55'
                       for j in range(i,min(i+12,100)))
                       for i in range(0,100,12))).encode('ascii')
        +b'\n};\n')
    legacy_sources['xbm']=xbm
    xpm=args.output/'01_LEVEL_1.xpm'
    xpm.write_text(
        '/* XPM */\nstatic char *_1_LEVEL_1[] = {\n'
        '"40 20 2 1",\n". c #000000",\n"  c #FFFFFF",\n'
        +',\n'.join('"' + ('. '*20 if i%2==0 else ' .' *20) + '"' for i in range(20))
        +'\n};\n', encoding='ascii')
    legacy_sources['xpm']=xpm
    # Content-confirmed PCX/Sun Raster: no white Draw page or size changes.
    rgb = bytes(value for y in range(20) for x in range(40)
                for value in (x * 5 % 256, y * 9 % 256, 67))
    img = Image.frombytes('RGB', (40, 20), rgb)
    pcx = args.output/'legacy.pcx'
    img.save(pcx, format='PCX')
    legacy_sources['pcx'] = pcx
    ras = args.output/'legacy.ras'
    bgr = bytes(value for i in range(0, len(rgb), 3)
                for value in (rgb[i+2], rgb[i+1], rgb[i]))
    ras.write_bytes(struct.pack('>8I', 0x59a66a95, 40, 20, 24,
                                len(bgr), 1, 0, 0) + bgr)
    legacy_sources['ras'] = ras
    # Self-contained DOCX with three explicit page breaks; no test-only
    # Python conversion engine or additional runtime package is required.
    writer=args.output/'three-pages.docx'
    paragraphs=''.join(
        '<w:p><w:r><w:t>AIO_WRITER_PAGE_%d_ID_00%d</w:t></w:r></w:p>' % (index,index)
        + ('<w:p><w:r><w:br w:type="page"/></w:r></w:p>' if index < 3 else '')
        for index in range(1,4))
    with ZipFile(writer,'w',ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml',
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '</Types>')
        archive.writestr('_rels/.rels',
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            '</Relationships>')
        archive.writestr('word/document.xml',
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:body>' + paragraphs
            + '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/>'
            '<w:pgMar w:top="720" w:right="720" w:bottom="720" w:left="720"/>'
            '</w:sectPr></w:body></w:document>')
    # Multi-page PDF uses native QtPdf rendering instead of a one-page
    # LibreOffice image export. Different page sizes make loss observable.
    from pypdf import PdfWriter
    pdf_doc=PdfWriter()
    for width,height in ((72,144),(144,72),(72,72)):
        pdf_doc.add_blank_page(width=width,height=height)
    multipage_pdf=args.output/'three-page-original.pdf'
    with multipage_pdf.open('wb') as stream:
        pdf_doc.write(stream)
    originals={str(f):digest(f) for f in (source,raster,jpeg,slides,writer,multipage_pdf,*pnm_sources.values(),*legacy_sources.values())}
    cancel=threading.Event();adapter=LibreOfficeAdapter(args.executable)
    results=[];counter=0
    def proof(path):
        nonlocal counter
        counter+=1
        value=adapter.preflight(path,args.output/('probe-%d'%counter),cancel=cancel,timeout=90)
        assert value.get('state')=='detected',value
        return value
    def convert(path,target,proven):
        nonlocal counter
        counter+=1;adapter.prepare_source(proven)
        start=time.monotonic()
        files=adapter.convert(path,target,args.output/('task-%d-%s'%(counter,target)),cancel=cancel,timeout=120,on_started=lambda:None)
        print('NATIVE_TIMING='+json.dumps({'source':path.name,'target':target,'ms':round((time.monotonic()-start)*1000),'mode':adapter.execution_mode()}),flush=True)
        return files
    try:
        with adapter.batch(args.output/'office'):
            tg=proof(raster)
            assert 'com.sun.star.drawing.DrawingDocument' in tg.get('documentServices',[]),tg
            assert 'com.sun.star.text.TextDocument' not in tg.get('documentServices',[]),tg
            results.append({'case':'TGA_model','status':'PASS','filter':tg.get('selectedImportFilter')})
            jp=proof(jpeg)
            for target in ('jpg','jpeg','jfif','jif','jpe'):
                files=convert(jpeg,target,jp);out=next(f for f in files if f.suffix=='.'+target)
                assert digest(out)==originals[str(jpeg)],target
            results.append({'case':'JPEG_five_aliases','status':'PASS','bytePreserved':True})
            # M26: raw pixel dimensions and values, not LibreOffice's
            # virtual sheet/canvas. Qt is already bundled with the app.
            pnm_modes=[]
            for kind,path in pnm_sources.items():
                pp=proof(path)
                assert pp.get('mediaProfile',{}).get('format')==kind,pp
                assert 'png' in pp.get('qtMediaTargets',()),pp
                exported=convert(path,'png',pp)
                png=next(f for f in exported if f.suffix=='.png')
                with Image.open(path) as before,Image.open(png) as after:
                    assert before.size == after.size == (40,20),(kind,before.size,after.size)
                    if before.convert('RGB').tobytes()!=after.convert('RGB').tobytes():
                        print('RASTER_PIXEL_DIAGNOSTIC='+json.dumps({
                            'kind':kind,'sourceFirst':list(before.convert('RGB').getdata())[:8],
                            'outputFirst':list(after.convert('RGB').getdata())[:8],
                            'sourceSize':before.size,'outputSize':after.size
                        }),flush=True)
                    assert before.convert('RGB').tobytes()==after.convert('RGB').tobytes(),kind
                assert adapter.execution_mode()=='qt_media_frames',kind
                pnm_modes.append(kind)
            results.append({'case':'portable_raster_canvas','status':'PASS','types':pnm_modes})
            legacy_modes=[]
            for kind,path in legacy_sources.items():
                pp=proof(path)
                assert pp.get('mediaProfile',{}).get('format')==kind,pp
                assert 'png' in pp.get('qtMediaTargets',()),pp
                exported=convert(path,'png',pp)
                png=next(f for f in exported if f.suffix=='.png')
                with Image.open(path) as before,Image.open(png) as after:
                    assert before.size == after.size == (40,20),(kind,before.size,after.size)
                    if before.convert('RGB').tobytes()!=after.convert('RGB').tobytes():
                        print('RASTER_PIXEL_DIAGNOSTIC='+json.dumps({
                            'kind':kind,'sourceFirst':list(before.convert('RGB').getdata())[:8],
                            'outputFirst':list(after.convert('RGB').getdata())[:8],
                            'sourceSize':before.size,'outputSize':after.size
                        }),flush=True)
                    assert before.convert('RGB').tobytes()==after.convert('RGB').tobytes(),kind
                assert adapter.execution_mode()=='qt_media_frames',kind
                legacy_modes.append(kind)
            results.append({'case':'legacy_pcx_ras_xbm_xpm_canvas','status':'PASS','types':legacy_modes})
            wp=proof(writer)
            assert 'com.sun.star.text.TextDocument' in wp.get('documentServices',[]),wp
            for target in ('png','jpg'):
                files=convert(writer,target,wp)
                frames=[p for p in files if p.suffix.lower()=='.'+target]
                assert len(frames)==3,(target,len(frames))
                assert len({digest(frame) for frame in frames})==3,(target,'identical pages')
                manifest=json.loads(next(f for f in files if f.name=='AIO-pages.json').read_text(encoding='utf-8'))
                assert manifest['pageCount']==3 and len(manifest['pages'])==3
                assert [record['page'] for record in manifest['pages']]==[1,2,3]
                results.append({'case':'writer_three_pages_'+target,'status':'PASS','pages':3})
            pdf_proof=proof(multipage_pdf)
            assert pdf_proof.get('mediaProfile',{}).get('format')=='pdf',pdf_proof
            for target in ('png','jpg'):
                exported=convert(multipage_pdf,target,pdf_proof)
                images=[p for p in exported if p.suffix.lower()=='.'+target]
                assert len(images)==3,(target,images)
                with Image.open(images[0]) as first,Image.open(images[1]) as second,Image.open(images[2]) as third:
                    assert [v.size for v in (first,second,third)]==[(144,288),(288,144),(144,144)],target
                listing=json.loads(next(p for p in exported if p.name=='AIO-pages.json').read_text(encoding='utf-8'))
                assert listing['pageCount']==3 and [x['page'] for x in listing['pages']]==[1,2,3]
                assert adapter.execution_mode()=='qt_media_frames',target
                results.append({'case':'pdf_three_pages_'+target,'status':'PASS','pages':3})
            bk=proof(source)
            for target in ('slk','sylk'):
                files=convert(source,target,bk);values=[f.read_bytes() for f in files if f.suffix=='.'+target]
                assert len(values)==4,(target,len(values))
                assert all(sum(('SHEET_'+str(i)).encode() in v for v in values)==1 for i in range(1,5)),values
                results.append({'case':'four_sheets_'+target,'status':'PASS','sheets':4})
            # M25: every DBF containing Russian UTF-8 must carry a matching
            # codepage label through the normal save/manifest output list.
            files=convert(source,'dbf',bk)
            dbfs=[f for f in files if f.suffix=='.dbf']
            cpgs=[f for f in files if f.suffix=='.cpg']
            assert len(dbfs)==4 and len(cpgs)==4,(len(dbfs),len(cpgs))
            for dbf in dbfs:
                assert 'Товар Б'.encode('utf-8') in dbf.read_bytes(),dbf
                assert dbf.with_suffix('.cpg').read_text(encoding='ascii').strip()=='UTF-8',dbf
            results.append({'case':'four_sheets_dbf_utf8_cpg','status':'PASS','sheets':4})
            files=convert(source,'xhtml',bk)
            out=next(f for f in files if f.suffix=='.xhtml')
            assert ET.parse(out).getroot().tag=='{http://www.w3.org/1999/xhtml}html'
            results.append({'case':'real_xhtml','status':'PASS'})
            sp=proof(slides)
            for target in ('png','svg'):
                files=convert(slides,target,sp);pages=[f for f in files if f.suffix=='.'+target]
                assert len(pages)==3,(target,len(pages))
                assert len({digest(page) for page in pages})==3, 'Repeated or empty page exports'
                if target=='svg':
                    for index,page in enumerate(pages,1):
                        assert 'AIO_SLIDE_'+str(index) in page.read_text(encoding='utf-8'),page
                mapping=json.loads(next(f for f in files if f.name=='AIO-pages.json').read_text())
                assert mapping['pageCount']==3 and len(mapping['pages'])==3
                results.append({'case':'three_pages_'+target,'status':'PASS','pages':3})
            ppt=next(f for f in convert(slides,'ppt',sp) if f.suffix=='.ppt')
            for path,pf in ((slides,sp),(ppt,proof(ppt))):
                try:
                    files=convert(path,'xhtml',pf)
                except GuardError as error:
                    assert error.code in ('ENGINE_OUTPUT_CONTENT_LOSS','ENGINE_OUTPUT_VALIDATION_INCOMPLETE'),(error.code,error.diagnostic)
                    results.append({'case':path.suffix+'_xhtml','status':'SAFE_REJECTION','code':error.code})
                else:
                    text=''.join(f.read_text(encoding='utf-8') for f in files if f.suffix=='.xhtml')
                    assert all('AIO_SLIDE_'+str(n) in text for n in range(1,4)),text[:200]
                    results.append({'case':path.suffix+'_xhtml','status':'PASS','textRetained':True})
        for path,expected in originals.items():assert digest(path)==expected,path
        assert len(results)==16,results
        from fulltest_csv_native import run as run_csv_native
        csv_report=run_csv_native(args.executable,args.output/'csv-decimal-originals')
        report={'status':'PASS','scope':'synthetic native controls plus six original CSV/TAB/TSV; not complete FULLTEST 200',
                'cases':results,'sourceUnchanged':True,'csvDecimalAcceptance':csv_report}
    except Exception as error:
        report={'status':'FAIL','cases':results,'error':type(error).__name__,'reason':str(error),'code':getattr(error,'code',None),'diagnostic':getattr(error,'diagnostic',None)}
        raise
    finally:
        (args.output/'evidence.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print('FULLTEST_BATCH1_NATIVE='+json.dumps(report,ensure_ascii=True),flush=True)


if __name__=='__main__':main()
