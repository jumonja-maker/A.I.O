"""Strict synthetic colour controls through the production LibreOfficeAdapter."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys
import threading
import time
import xml.etree.ElementTree as ET
from zipfile import ZipFile, ZIP_STORED
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.qa.fulltest_vector_pages_native import fixture, inspect_page
from tools.qa.vector_page_fixture import write_pptx_fixture, COLOURS, A, P
OFFICE='urn:oasis:names:tc:opendocument:xmlns:office:1.0'
DRAW='urn:oasis:names:tc:opendocument:xmlns:drawing:1.0'

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def write_odp(path):
    flat=ET.fromstring(fixture('fodp'))
    content=ET.Element('{'+OFFICE+'}document-content', {'{'+OFFICE+'}version':'1.2'})
    for child in flat:content.append(copy.deepcopy(child))
    styles=ET.Element('{'+OFFICE+'}document-styles', {'{'+OFFICE+'}version':'1.2'})
    ET.SubElement(styles,'{'+OFFICE+'}styles')
    with ZipFile(path,'x') as archive:
        archive.writestr('mimetype','application/vnd.oasis.opendocument.presentation',compress_type=ZIP_STORED)
        archive.writestr('content.xml',ET.tostring(content,encoding='utf-8',xml_declaration=True))
        archive.writestr('styles.xml',ET.tostring(styles,encoding='utf-8',xml_declaration=True))
        archive.writestr('META-INF/manifest.xml','<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" manifest:version="1.2"><manifest:file-entry manifest:full-path="/" manifest:media-type="application/vnd.oasis.opendocument.presentation"/><manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/><manifest:file-entry manifest:full-path="styles.xml" manifest:media-type="text/xml"/></manifest:manifest>')

def write_themed_pptx(path, rgb_source):
    with ZipFile(rgb_source) as archive:
        parts={name:archive.read(name) for name in archive.namelist()}
    for index in (1,2,3):
        name='ppt/slides/slide%d.xml'%index
        root=ET.fromstring(parts[name])
        for shape in root.iter('{'+P+'}sp'):
            label=shape.find('./{'+P+'}nvSpPr/{'+P+'}cNvPr')
            if label is not None and label.get('name','').startswith('Marker'):
                fill=shape.find('./{'+P+'}spPr/{'+A+'}solidFill')
                assert fill is not None
                fill.clear();ET.SubElement(fill,'{'+A+'}schemeClr',{'val':'accent%d'%index})
        parts[name]=ET.tostring(root,encoding='utf-8',xml_declaration=True)
    theme=ET.fromstring(parts['ppt/theme/theme1.xml'])
    for index,colour in enumerate(COLOURS,1):
        node=theme.find('.//{'+A+'}accent%d/{'%index+A+'}srgbClr')
        assert node is not None
        node.set('val',colour)
    parts['ppt/theme/theme1.xml']=ET.tostring(theme,encoding='utf-8',xml_declaration=True)
    with ZipFile(path,'x') as archive:
        for name,data in parts.items():archive.writestr(name,data)

def main():
    from app_backend.converter_guard import LibreOfficeAdapter
    from PySide6.QtGui import QGuiApplication
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args();args.output.mkdir(exist_ok=False,parents=True)
    app=QGuiApplication.instance() or QGuiApplication([])
    cancel=threading.Event();adapter=LibreOfficeAdapter(args.executable)
    paths=[];originals={};cases=[];counter=0;tick=time.monotonic()
    def remember(path):originals[path]=sha(path);return path
    def convert(path,target):
        nonlocal counter
        counter+=1
        proof=adapter.preflight(path,args.output/('preflight-%d'%counter),cancel=cancel,timeout=90)
        assert proof.get('state')=='detected',proof
        adapter.prepare_source(proof)
        outputs=adapter.convert(path,target,args.output/('task-%d'%counter),cancel=cancel,timeout=120,on_started=lambda:None)
        assert adapter.execution_mode()=='native_uno'
        return outputs,adapter.export_info()
    try:
        flat=args.output/'theme-free.fodp';flat.write_text(fixture('fodp'),encoding='utf-8')
        paths.append(('flat_rgb',remember(flat)))
        grouped=args.output/'grouped.fodp';doc=ET.fromstring(fixture('fodp'))
        for page in doc.iter('{'+DRAW+'}page'):
            group=ET.Element('{'+DRAW+'}g')
            for child in list(page):page.remove(child);group.append(child)
            page.append(group)
        grouped.write_bytes(ET.tostring(doc,encoding='utf-8',xml_declaration=True))
        paths.append(('group_rgb',remember(grouped)))
        packaged=args.output/'theme-free.odp';write_odp(packaged)
        paths.append(('packaged_rgb',remember(packaged)))
        rgb=args.output/'independent.pptx';write_pptx_fixture(rgb)
        paths.append(('pptx_rgb',remember(rgb)))
        themed=args.output/'themed.pptx';write_themed_pptx(themed,rgb)
        paths.append(('pptx_theme',remember(themed)))
        with adapter.batch(args.output/'office'):
            for name,source in paths:
                outputs,info=convert(source,'pptx')
                target=remember(next(p for p in outputs if p.suffix.lower()=='.pptx'))
                checks=[v for v in info.get('resultChecks',[]) if v.get('check')=='native_pptx_solid_rgb']
                if name in ('flat_rgb','group_rgb','packaged_rgb'):
                    assert len(checks)==1 and checks[0]['status']=='checked_subset',(name,info)
                    assert checks[0]['qualityCompared'] is False
                    # Baseline older engines may have no invented binding.
                    assert 0<=checks[0]['normalizedFills']<=3,checks
                else:
                    assert not checks,(name,checks)  # genuine PPTX bypasses workaround entirely
                if name=='pptx_theme':
                    with ZipFile(target) as archive:
                        for i in (1,2,3):
                            root=ET.fromstring(archive.read('ppt/slides/slide%d.xml'%i))
                            values=[e.get('val') for e in root.iter('{'+A+'}schemeClr')]
                            assert 'accent%d'%i in values,(i,values)
                bmp,_=convert(target,'bmp')
                manifest=json.loads(next(p for p in bmp if p.name=='AIO-pages.json').read_text(encoding='utf-8'))
                assert manifest['pageCount']==3
                by_name={p.name:p for p in bmp}
                assert [v['page'] for v in manifest['pages']]==[1,2,3]
                markers=[inspect_page(by_name[row['file']],i) for i,row in enumerate(manifest['pages'])]
                row={'case':name,'status':'PASS','pages':3,'checks':checks,
                     'markers':markers,'sourceUnchanged':sha(source)==originals[source],
                     'themeBindingsPreserved':True if name=='pptx_theme' else None}
                assert row['sourceUnchanged'];cases.append(row)
                print('PPTX_COLOUR_CASE='+json.dumps(row),flush=True)
        assert len(cases)==5
        assert all(sha(p)==v for p,v in originals.items()),'source changed'
        result={'status':'PASS','scope':'five synthetic PPTX colour controls; not historical FULLTEST or full visual QA',
                'cases':cases,'sourceUnchanged':True,'elapsedSeconds':round(time.monotonic()-tick,3)}
    except Exception as error:
        result={'status':'FAIL','cases':cases,'error':str(error)}
        (args.output/'evidence.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print('PPTX_COLOURS_NATIVE='+json.dumps(result),flush=True)
        raise
    (args.output/'evidence.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print('PPTX_COLOURS_NATIVE='+json.dumps(result),flush=True)

if __name__=='__main__':main()
