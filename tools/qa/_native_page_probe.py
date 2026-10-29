"""Isolated native investigation; never included in the product branch."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import xml.etree.ElementTree as ET
import zipfile
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from app_backend.converter_guard import uno_session
from tools.qa import fulltest_vector_pages_native as driver
from PIL import Image
original=uno_session.WORKER_SOURCE
marker="    outputs, mapping, owned = [], [], []\n"
assert original.count(marker)==1
capture=r'''    observed = []
    for di in range(count):
        dp = pages.getByIndex(di)
        shapes = []
        for sj in range(dp.getCount()):
            shape = dp.getByIndex(sj)
            item = {}
            for key in ('FillColor', 'FillStyle', 'ShapeType', 'String', 'Name'):
                try:
                    item[key] = str(getattr(shape, key))[:160]
                except Exception:
                    pass
            shapes.append(item)
        observed.append({'page': di + 1, 'shapes': shapes})
    (target.parent / '_qa-model.json').write_text(json.dumps(observed), encoding='utf-8')
'''
anchor="            exporter.setSourceDocument(pages.getByIndex(index))"
assert original.count(anchor)==1
for mode in ('baseline','fresh_exporter','controller','both'):
    worker=original.replace(marker,capture+marker)
    steps=[]
    if mode in ('fresh_exporter','both'):
        steps.extend(["            exporter = remote.ServiceManager.createInstanceWithContext(",
                      "                'com.sun.star.drawing.GraphicExportFilter', remote)"])
    if mode in ('controller','both'):
        steps.append('            model.getCurrentController().setCurrentPage(pages.getByIndex(index))')
    steps.append(anchor)
    worker=worker.replace(anchor,'\n'.join(steps))
    uno_session.WORKER_SOURCE=worker
    output=Path(os.environ['RUNNER_TEMP'])/('aio-page-probe-'+mode)
    sys.argv=['native-page-probe','--executable',os.environ['AIO_QA_ENGINE'],'--output',str(output)]
    error=None
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            driver.main()
        except Exception as exc:
            error=repr(exc)
    evidence=json.loads((output/'evidence.json').read_text()) if (output/'evidence.json').is_file() else {}
    print('AIO_PROBE_RESULT='+json.dumps({'mode':mode,'status':evidence.get('status'),
         'cases':len(evidence.get('cases',[])),'error':error}),flush=True)
    for path in sorted(output.rglob('*.pptx')):
        with zipfile.ZipFile(path) as archive:
            slides=[]
            for name in sorted(n for n in archive.namelist() if n.startswith('ppt/slides/slide') and n.endswith('.xml')):
                root=ET.fromstring(archive.read(name))
                fills=[e.attrib for e in root.iter() if e.tag.endswith('}srgbClr') or e.tag.endswith('}schemeClr')]
                slides.append({'slide':name,'fills':fills,'text':list(root.itertext())})
        print('AIO_PROBE_OOXML='+json.dumps({'mode':mode,'slides':slides}),flush=True)
    for path in sorted(output.rglob('_qa-model.json')):
        print('AIO_PROBE_MODEL='+json.dumps({'mode':mode,'task':path.parent.name,
              'model':json.loads(path.read_text())}),flush=True)
    for path in sorted(output.rglob('*.bmp')):
        with Image.open(path) as image:
            image=image.convert('RGB');image.thumbnail((160,160))
            counts=[0,0,0]
            for r,g,b in image.getdata():
                counts[0]+=r>80 and r>2*g and r>2*b
                counts[1]+=b>80 and b>2*r and b>2*g
                counts[2]+=g>80 and g>2*r and g>2*b
        print('AIO_PROBE_BMP='+json.dumps({'mode':mode,'task':path.parent.name,
              'file':path.name,'colours':counts}),flush=True)
