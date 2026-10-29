"""SHA-locked source preparation only. This file is removed in the candidate."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request

REPO='jumonja-maker/A.I.O'
BRANCH='fix/fulltest-critical-hosted-lo-20261029'
assert os.environ['GITHUB_REPOSITORY']==REPO
assert os.environ['GITHUB_REF']=='refs/heads/'+BRANCH
EXPECTED={
 'app_backend/converter_guard/native_fidelity.py':'aa297aea02cedaf23ff446f4ca454328998cd483',
 'app_backend/converter_guard/uno_session.py':'5a27527070480fe9b0c6e4be12ab7f2955986cc1',
 'app_backend/converter_guard/adapter.py':'faeddc8159dec37d43c3821c7f7134c9b327b086',
 'app_backend/converter_guard/export_recipe.py':'f5aacb9dd8faf26364e6e85b49a392f879944046',
 'tools/qa/fulltest_batch1_native.py':'6ec98c9ab36e66770fd3063e944444e6aa1a686a',
}
texts={}
for name,sha in EXPECTED.items():
 data=Path(name).read_bytes()
 assert hashlib.sha1(('blob %d\0'%len(data)).encode()+data).hexdigest()==sha,name
 texts[name]=data.decode('utf-8')

def replace(name,before,after):
 assert texts[name].count(before)==1,(name,before[:80])
 texts[name]=texts[name].replace(before,after,1)

native='app_backend/converter_guard/native_fidelity.py'
replace(native,'from .preservation_contract import PRESERVATION_POLICY_SOURCE\n',
 'from .preservation_contract import PRESERVATION_POLICY_SOURCE\nfrom .calc_pages import CALC_PAGES_SOURCE\n')
replace(native,"NATIVE_FIDELITY_SOURCE = PRESERVATION_POLICY_SOURCE + r'''",
 "NATIVE_FIDELITY_SOURCE = PRESERVATION_POLICY_SOURCE + CALC_PAGES_SOURCE + r'''")
uno='app_backend/converter_guard/uno_session.py'
replace(uno,'                        graphic_pages = (aio_export_graphic_pages(model, remote, prop, target, preservation_plan)',
 "                        calc_export = (aio_export_calc_pages(model, remote, prop, target, req['exportFilter'])\n                            if 'com.sun.star.sheet.SpreadsheetDocument' in services else None)\n                        graphic_pages = (aio_export_graphic_pages(model, remote, prop, target, preservation_plan)")
replace(uno,'                        if graphic_pages is not None:\n                            exported_paths = graphic_pages',
 '                        if calc_export is not None:\n                            exported_paths, preservation_plan = calc_export\n                        elif graphic_pages is not None:\n                            exported_paths = graphic_pages')
replace(uno,"                    if preservation_check is not None:\n                        result['preservationContract'] = preservation_check",
 "                    if preservation_check is not None:\n                        if calc_export is not None:\n                            preservation_check['notChecked'].append('hidden_or_unprinted_cells')\n                        result['preservationContract'] = preservation_check")
replace(uno,'                        # This block does not implement Calc print pagination.',
 '                        # Other Calc graphic filters are not covered by the PNG/JPEG executor.')
replace(uno,"                    if writer_pages is not None:\n                        warnings.append('NATIVE_WRITER_RASTER_PAGES')",
 "                    if calc_export is not None:\n                        warnings.append('NATIVE_CALC_RASTER_PAGES')\n                    if writer_pages is not None:\n                        warnings.append('NATIVE_WRITER_RASTER_PAGES')")
adapter='app_backend/converter_guard/adapter.py'
replace(adapter,"                                                     'NATIVE_WRITER_RASTER_PAGES',\n                                                     'NATIVE_OUTPUT_NOT_CHECKED'))",
 "                                                     'NATIVE_WRITER_RASTER_PAGES',\n                                                     'NATIVE_CALC_RASTER_PAGES',\n                                                     'NATIVE_OUTPUT_NOT_CHECKED'))")
replace(adapter,"            scope = aio_preservation_scope(proof.get('documentServices', ()), target)\n            if scope is not None:",
 "            scope = aio_preservation_scope(proof.get('documentServices', ()), target)\n            if (scope is None and 'com.sun.star.sheet.SpreadsheetDocument' in\n                    proof.get('documentServices', ()) and\n                    target in ('png', 'jpg', 'jpeg', 'jfif', 'jif', 'jpe')):\n                scope = ('native_all_calc_print_pages_v1', 'pages', 'loaded_calc_print_layout')\n            if scope is not None:")
recipes='app_backend/converter_guard/export_recipe.py'
replace(recipes,'WARNING_TEXT = {\n',
 "WARNING_TEXT = {\n    'NATIVE_CALC_RASTER_PAGES': (\n        'Каждая печатная страница Calc сохранена отдельным изображением. '\n        'Имена, листы, диапазоны и разрешение указаны в AIO-pages.json. '\n        'Скрытые листы и ячейки вне печатных областей не включаются; '\n        'полное совпадение импорта и оформления не проверено.'),\n")
driver='tools/qa/fulltest_batch1_native.py'
replace(driver,"        report={'status':'PASS','scope':'synthetic native controls plus six original CSV/TAB/TSV; not complete FULLTEST 200',",
 "        from calc_pages_native import run as run_calc_pages\n        calc_pages_report=run_calc_pages(args.executable,args.output/'calc-print-pages')\n        report={'status':'PASS','scope':'synthetic native controls plus six original CSV/TAB/TSV; not complete FULLTEST 200',")
replace(driver,"                'preservationContracts':contract_receipts}",
 "                'preservationContracts':contract_receipts,'calcPagesAcceptance':calc_pages_report}")
for name,content in texts.items():
 compile(content,name,'exec')
 Path(name).write_text(content,encoding='utf-8',newline='\n')
subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-p','test_calc_pages.py','-v'],check=True)
sys.path.insert(0,str(Path.cwd()))
from app_backend.converter_guard.uno_session import WORKER_SOURCE
compile(WORKER_SOURCE,'<worker>','exec')

def api(method,path,data=None):
 req=urllib.request.Request('https://api.github.com/repos/'+REPO+path,
   data=None if data is None else json.dumps(data).encode(),method=method,
   headers={'Authorization':'Bearer '+os.environ['GH_TOKEN'],
            'Accept':'application/vnd.github+json','Content-Type':'application/json'})
 with urllib.request.urlopen(req,timeout=60) as response:return json.load(response)
parent=os.environ['GITHUB_SHA']
base=api('GET','/git/commits/'+parent)['tree']['sha']
entries=[{'path':p,'mode':'100644','type':'blob','content':c} for p,c in texts.items()]
for p in ('.github/workflows/aio-calc-apply-once.yml','tools/qa/_apply_calc_pages_once.py'):
 entries.append({'path':p,'mode':'100644','type':'blob','sha':None})
tree=api('POST','/git/trees',{'base_tree':base,'tree':entries})['sha']
sha=api('POST','/git/commits',{'message':'fix(calc): render every native print page with guarded page coverage',
 'tree':tree,'parents':[parent]})['sha']
print('AIO_CANDIDATE_COMMIT='+sha,flush=True)
print('AIO_CANDIDATE_PARENT='+parent,flush=True)
print('AIO_REFS_UNCHANGED=true',flush=True)
