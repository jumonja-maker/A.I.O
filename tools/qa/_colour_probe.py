"""Disposable investigation only; not part of the product branch."""
import json
import os
from pathlib import Path
import sys
import threading
import xml.etree.ElementTree as ET
import zipfile
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from app_backend.converter_guard import LibreOfficeAdapter, uno_session
from tools.qa.fulltest_vector_pages_native import fixture, digest, inspect_page
from tools.qa.vector_page_fixture import write_pptx_fixture
from PySide6.QtGui import QGuiApplication
app=QGuiApplication.instance() or QGuiApplication([])
base=Path(os.environ['RUNNER_TEMP'])/'aio-colour-probe'
base.mkdir(exist_ok=False)
source=base/'original.fodp';source.write_text(fixture('fodp'),encoding='utf-8')
independent=base/'independent.pptx';write_pptx_fixture(independent)
originals={p:digest(p) for p in (source,independent)}
original=uno_session.WORKER_SOURCE
anchor='                            model.storeToURL(target.as_uri(), tuple(export_options))'
assert original.count(anchor)==1
helper=r'''
def qa_colour_state(model):
    rows=[]
    for page in model.getDrawPages():
        shapes=[]
        for shape in page:
            item={}
            for name in ('FillColor','FillStyle','FillComplexColor','FillColorTheme','FillColorLumMod','FillColorLumOff'):
                try:
                    value=shape.getPropertyValue(name)
                    item[name]=({'type':value.getType(),'theme':value.getThemeColorType()} if name=='FillComplexColor' and value is not None else str(value))
                    item[name+'State']=str(shape.getPropertyState(name))
                except Exception as error:
                    item[name]=type(error).__name__
            shapes.append(item)
        rows.append(shapes)
    return rows

def qa_colour_change(model,mode):
    for page in model.getDrawPages():
        for shape in page:
            if shape.FillStyle.value!='SOLID':
                continue
            rgb=shape.FillColor
            if mode=='clear_theme':
                shape.FillColorTheme=-1
            elif mode=='null_complex':
                shape.FillComplexColor=None
            elif mode=='same_rgb':
                shape.FillColor=rgb
            elif mode=='reset_then_rgb':
                shape.setPropertyToDefault('FillComplexColor')
                shape.FillColor=rgb
'''
records=[]
for mode in ('baseline','clear_theme','null_complex','same_rgb','reset_then_rgb'):
    injection="                            qa_before=qa_colour_state(model)\n                            qa_colour_change(model,%r)\n                            qa_after=qa_colour_state(model)\n                            (target.parent.parent/'_colour-model.json').write_text(json.dumps({'before':qa_before,'after':qa_after}),encoding='utf-8')\n"%mode
    uno_session.WORKER_SOURCE=helper+original.replace(anchor,injection+anchor)
    adapter=LibreOfficeAdapter(Path(os.environ['AIO_QA_ENGINE']))
    cancel=threading.Event()
    with adapter.batch(base/('office-'+mode)):
        for index,path in enumerate((source,independent)):
            row={'mode':mode,'source':path.name}
            task=base/('%s-%d'%(mode,index))
            try:
                proof=adapter.preflight(path,base/('preflight-%s-%d'%(mode,index)),cancel=cancel,timeout=90)
                assert proof.get('state')=='detected',proof
                adapter.prepare_source(proof)
                outputs=adapter.convert(path,'pptx',task,cancel=cancel,timeout=90,on_started=lambda:None)
                target=next(p for p in outputs if p.suffix.lower()=='.pptx')
                with zipfile.ZipFile(target) as archive:
                    row['slides']=[{'name':name,'colours':[(e.tag.split('}')[-1],e.attrib) for e in ET.fromstring(archive.read(name)).iter() if e.tag.endswith('Clr')]} for name in sorted(archive.namelist()) if name.startswith('ppt/slides/slide') and name.endswith('.xml')]
                row['state']=json.loads((task/'_colour-model.json').read_text(encoding='utf-8'))
                row['status']='OBSERVED'
            except Exception as error:
                row['status']='ERROR';row['error']=str(error)
                for p in task.rglob('_colour-model.json'):
                    row['state']=json.loads(p.read_text(encoding='utf-8'))
            records.append(row)
            print('COLOUR_PROBE='+json.dumps(row),flush=True)
assert all(digest(p)==v for p,v in originals.items())
assert any(r['mode']=='baseline' and r['source']=='original.fodp' and r['status']=='OBSERVED' for r in records),records
print('COLOUR_PROBE_COMPLETE='+json.dumps({'sourceUnchanged':True,'records':len(records),'productChanged':False}),flush=True)
