"""M20 native EMF/WMF/BMP pages: actual adapter, not the historical FULLTEST.

Each page has a unique text token and a distinct solid colour. Vector outputs
must carry only their own text token; BMP pixels must show only their own band.
These are selected preservation properties, not complete visual equivalence.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.qa.vector_page_fixture import TOKENS, COLOURS as RGB_COLOURS, write_pptx_fixture

COLOURS = ['#'+colour for colour in RGB_COLOURS]


def fixture(kind):
    if kind not in ('fodp', 'fodg'):
        raise ValueError('unsupported native page fixture')
    component = 'presentation' if kind == 'fodp' else 'drawing'
    mimetype = 'presentation' if kind == 'fodp' else 'graphics'
    styles = ''.join(
        '<style:style style:name="Mark%d" style:family="graphic">'
        '<style:graphic-properties draw:fill="solid" draw:fill-color="%s" '
        'draw:stroke="none"/></style:style>' % (i, colour)
        for i, colour in enumerate(COLOURS))
    styles += ('<style:style style:name="Text" style:family="graphic">'
               '<style:graphic-properties draw:fill="none" draw:stroke="none"/>'
               '</style:style>')
    pages = ''.join(
        '<draw:page draw:name="Page%d">'
        '<draw:frame draw:style-name="Text" svg:x="1cm" svg:y="1cm" '
        'svg:width="16cm" svg:height="3cm"><draw:text-box><text:p>%s</text:p>'
        '</draw:text-box></draw:frame>'
        '<draw:rect draw:style-name="Mark%d" svg:x="1cm" svg:y="5cm" '
        'svg:width="5cm" svg:height="3cm"/></draw:page>' % (i+1, token, i)
        for i, token in enumerate(TOKENS))
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
            'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
            'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
            'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
            'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" '
            'office:mimetype="application/vnd.oasis.opendocument.'+mimetype+'" office:version="1.2">'
            '<office:automatic-styles>'+styles+'</office:automatic-styles>'
            '<office:body><office:'+component+'>'+pages+'</office:'+component+'>'
            '</office:body></office:document>')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inspect_page(path, page):
    if path.suffix.lower() in ('.emf', '.wmf'):
        data = path.read_bytes()
        found = [any(token.encode(encoding) in data for encoding in ('ascii', 'utf-16-le'))
                 for token in TOKENS]
        assert found == [i == page for i in range(3)], (path.name, found)
        return {'page':page+1, 'textTokens':found, 'scope':'encoded_page_text_tokens'}
    from PIL import Image
    with Image.open(path) as image:
        assert image.format == 'BMP', (path.name,image.format)
        # QA only: bounded samples, with uniform markers much larger than a
        # sample cell. Production Guard never performs this pixel comparison.
        sample = image.convert('RGB')
        sample.thumbnail((320,320))
        counts = [0,0,0]  # red, blue, green
        for r,g,b in sample.getdata():
            counts[0] += r > 80 and r > 2*g and r > 2*b
            counts[1] += b > 80 and b > 2*r and b > 2*g
            counts[2] += g > 80 and g > 2*r and g > 2*b
        assert counts[page] >= 100, (path.name,counts)
        assert all(n <= 10 for i,n in enumerate(counts) if i != page), (path.name,counts)
        return {'page':page+1,'pixelSize':list(image.size),'markerPixels':counts,
                'scope':'sampled_native_raster_markers'}


def main():
    from app_backend.converter_guard import LibreOfficeAdapter
    from PySide6.QtGui import QGuiApplication
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    gui=QGuiApplication.instance() or QGuiApplication([])
    cancel=threading.Event()
    adapter=LibreOfficeAdapter(args.executable)
    cases=[]
    roundtrip_audit=None
    originals={}
    started=time.monotonic()
    counter=0
    def proof(path):
        nonlocal counter
        counter+=1
        result=adapter.preflight(path,args.output/('probe-%d'%counter),cancel=cancel,timeout=90)
        assert result.get('state')=='detected',result
        return result
    def convert(path,kind,evidence):
        nonlocal counter
        counter+=1
        adapter.prepare_source(evidence)
        return adapter.convert(path,kind,args.output/('task-%d-%s'%(counter,kind)),
                               cancel=cancel,timeout=120,on_started=lambda:None)
    try:
        sources=[]
        for kind in ('fodp','fodg'):
            source=args.output/('three-pages.'+kind)
            source.write_text(fixture(kind),encoding='utf-8')
            sources.append(source)
            originals[source]=digest(source)
        with adapter.batch(args.output/'office'):
            pp=proof(sources[0])
            # The legacy fixture is generated through the real adapter.
            generated=next(p for p in convert(sources[0],'ppt',pp) if p.suffix.lower()=='.ppt')
            sources.append(generated)
            originals[generated]=digest(generated)
            # Independent OOXML input: native FODP->PPTX creation was found to
            # replace distinct RGB fills with one theme colour on Windows.
            # Never repair that export or accept its altered colours as truth.
            # The separate roundtrip audit below keeps that issue observable.
            independent=args.output/'independent-three-pages.pptx'
            write_pptx_fixture(independent)
            sources.append(independent)
            originals[independent]=digest(independent)
            for source in sources:
                evidence=proof(source)
                for kind in ('emf','wmf','bmp'):
                    tick=time.monotonic()
                    exported=convert(source,kind,evidence)
                    outputs=sorted((p for p in exported if p.suffix.lower()=='.'+kind),key=lambda p:p.name)
                    assert len(outputs)==3,(source.name,kind,[p.name for p in exported])
                    manifests=[p for p in exported if p.name=='AIO-pages.json']
                    assert len(manifests)==1,exported
                    manifest=json.loads(manifests[0].read_text(encoding='utf-8'))
                    assert manifest['pageCount']==3,manifest
                    by_name={p.name:p for p in outputs}
                    rows=manifest['pages']
                    assert [row['page'] for row in rows]==[1,2,3],rows
                    assert len({row['file'] for row in rows})==3,rows
                    markers=[inspect_page(by_name[row['file']],i) for i,row in enumerate(rows)]
                    receipts=[v for v in adapter.export_info().get('resultChecks',())
                              if v.get('check')=='native_preservation_contract']
                    assert len(receipts)==1,receipts
                    receipt=receipts[0]
                    assert receipt['expectedUnits']==receipt['observedOutputs']==3,receipt
                    assert receipt['unit']=='pages' and receipt['qualityCompared'] is False,receipt
                    assert adapter.execution_mode()=='native_uno',adapter.execution_mode()
                    row={'sourceType':source.suffix,'target':kind,'status':'PASS','pages':3,
                         'markers':markers,'preservationContract':receipt,
                         'sourceUnchanged':digest(source)==originals[source],
                         'ms':round((time.monotonic()-tick)*1000)}
                    assert row['sourceUnchanged'],source.name
                    cases.append(row)
                    print('VECTOR_PAGE_CASE='+json.dumps(row),flush=True)
            # The former OPEN regression is now mandatory, independently of
            # the twelve direct-input page tests. Never repair/recolour its
            # native output or accept altered markers as expected truth.
            generated=next(p for p in convert(sources[0],'pptx',pp) if p.suffix.lower()=='.pptx')
            originals[generated]=digest(generated)
            generated_proof=proof(generated)
            bmp=convert(generated,'bmp',generated_proof)
            listing=json.loads(next(p for p in bmp if p.name=='AIO-pages.json').read_text(encoding='utf-8'))
            assert listing['pageCount']==3 and len(listing['pages'])==3,listing
            images={p.name:p for p in bmp if p.suffix.lower()=='.bmp'}
            observations=[]
            for i,row in enumerate(listing['pages']):
                try:
                    observations.append({'status':'MATCH',**inspect_page(images[row['file']],i)})
                except AssertionError as error:
                    observations.append({'status':'MISMATCH','page':i+1,'detail':str(error)})
            assert all(v['status']=='MATCH' for v in observations), observations
            roundtrip_audit={
                'status':'PASS',
                'code':'FODP_PPTX_FILL_COLOUR_ROUNDTRIP',
                'scope':'separate FODP->PPTX creation audit; excluded from the 12 direct-input acceptances',
                'observations':observations,'sourceSha256':originals[sources[0]],
                'generatedPptxSha256':originals[generated],
                'investigationRun':40933330624,
                'resolved':True,
                'resolutionScope':'synthetic_theme_free_odf_solid_rgb_only',
            }
            print('FODP_PPTX_COLOUR_AUDIT='+json.dumps(roundtrip_audit),flush=True)
        assert len(cases)==12,cases
        assert all(digest(path)==sha for path,sha in originals.items()),'source changed'
        result={'status':'PASS','scope':'12 synthetic native multi-page EMF/WMF/BMP conversions; not historical FULLTEST',
                'cases':cases,'sourceUnchanged':True,'separateRoundtripAudit':roundtrip_audit,
                'elapsedSeconds':round(time.monotonic()-started,3)}
    except Exception as error:
        result={'status':'FAIL','error':str(error),'code':getattr(error,'code',None),
                'diagnostic':getattr(error,'diagnostic',None),'cases':cases,'separateRoundtripAudit':roundtrip_audit}
        (args.output/'evidence.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print('FULLTEST_VECTOR_PAGES_NATIVE='+json.dumps(result),flush=True)
        raise
    (args.output/'evidence.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print('FULLTEST_VECTOR_PAGES_NATIVE='+json.dumps(result),flush=True)


if __name__=='__main__':
    main()
