"""One-off test/candidate preparation; never shipped or applied at runtime."""
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request

BASE='02baad506ac92688a370c9bfb6a3ef4988bc6a72'
TREE='976dbaa40e382afa6583c5cd045907a707c8d89a'
REPO='jumonja-maker/A.I.O'
PRODUCT=['app_backend/converter_lifecycle_ui_patch.py', 'tests/test_selection_controls.py',
         'tests/selection_controls_dom.mjs', 'tests/guard_ui_dom_smoke.mjs',
         'docs/review/AIO_SELECTION_M05_M06_2026-10-29_RU.md']


def replace_once(text, old, new):
    if text.count(old)!=1: raise RuntimeError('Source anchor changed: '+old[:110])
    return text.replace(old,new,1)


baseline=subprocess.run([sys.executable,'-m','unittest',
    'test_selection_controls.SelectionControls.test_single_card_keeps_nodes_and_scroll',
    'test_selection_controls.SelectionControls.test_bulk_second_click_clears','-v'],
    capture_output=True,text=True,timeout=90)
print(baseline.stdout,baseline.stderr,flush=True)
if (baseline.returncode==0 or 'FORMAT_SELECTION_RECREATED' not in baseline.stderr
        or 'BULK_SECOND_CLICK_DID_NOT_CLEAR' not in baseline.stderr):
    raise RuntimeError('Both baseline defects must be reproduced before applying changes')
print('AIO_BASELINE_REPRODUCED=M05_dom_rebuild_and_M06_no_deselect',flush=True)

path=Path('app_backend/converter_lifecycle_ui_patch.py')
text=path.read_text(encoding='utf-8')
patch=r'''    # M05/M06: selection is a metadata edit, not a new catalogue render.
    # Keep actual focusable buttons and their scroll owner alive.
    once("      if(available.length===selectedFormats.size&&available.every(f=>selectedFormats.has(f.id)))return;",
         "      const clearSelection=available.every(f=>selectedFormats.has(f.id));")
    once("      selectedFormats.clear();available.forEach(f=>selectedFormats.add(f.id));",
         "      selectedFormats.clear();if(!clearSelection)available.forEach(f=>selectedFormats.add(f.id));")
    once("      selectedFormat=available[0].id;formatValues={};currentResult=null;retryResult=null;",
         "      selectedFormat=clearSelection?null:available[0].id;formatValues={};currentResult=null;retryResult=null;")
    once("      status='idle';statusMessage='';realProgress=null;adapter.resetProgress?.();render();",
         "      status='idle';statusMessage='';realProgress=null;adapter.resetProgress?.();refreshFormatSelection();renderOptions();renderSummary();")
    once("      announce('Выбрано форматов: '+available.length);",
         "      announce(clearSelection?'Выбор форматов снят.':'Выбрано форматов: '+available.length);")
    once('    function renderFormats() {',
         """    function refreshFormatSelection() {
      closePopover();
      for(const wrapper of formatsArea.querySelectorAll('[data-format-id]')){
        const checked=String(selectedFormats.has(wrapper.dataset.formatId));
        wrapper.setAttribute('aria-selected',checked);
        wrapper.querySelector('.aio-cv__format-main')?.setAttribute('aria-pressed',checked);
      }
    }
    function renderFormats() {""")
    once("wrapper.dataset.available=String(enabled);",
         "wrapper.dataset.available=String(enabled);wrapper.dataset.formatId=fmt.id;")
    once("formatValues={};currentResult=null;retryResult=null;status='idle';statusMessage='';renderFormats();renderOptions();renderSummary();",
         "formatValues={};currentResult=null;retryResult=null;status='idle';statusMessage='';refreshFormatSelection();renderOptions();renderSummary();")
    once("      selectAllFormatsBtn.setAttribute('aria-pressed',String(available.length>0&&available.every(f=>selectedFormats.has(f.id))));",
         "      const allAvailableSelected=available.length>0&&available.every(f=>selectedFormats.has(f.id));\n"
         "      selectAllFormatsBtn.setAttribute('aria-pressed',String(allAvailableSelected));\n"
         "      selectAllFormatsBtn.textContent=allAvailableSelected?'Снять выбор':'Выбрать всё';\n"
         "      selectAllFormatsBtn.title=allAvailableSelected?'Снять выбор всех форматов':'Выбрать подтверждённые форматы во всех категориях';")
'''
text=replace_once(text,"    return code.encode('utf-8')",patch+"    return code.encode('utf-8')")
path.write_text(text,encoding='utf-8')

path=Path('tests/guard_ui_dom_smoke.mjs');text=path.read_text(encoding='utf-8')
old="""const startsBefore=plans.length;
document.querySelector('[data-cv-select-all-formats]').click();
assert.equal(plans.length,startsBefore,'Select All itself must not start conversion');"""
new="""const startsBefore=plans.length,checksBeforeSelection=preflights.length;
const formatsArea=document.querySelector('[data-cv-formats]');
const pdfChoice=target('PDF'),pdfWrapper=pdfChoice.parentElement;
formatsArea.scrollTop=283;pdfChoice.click();
assert.equal(pdfChoice.isConnected,true,'single click preserves the focused card node');
assert.equal(pdfChoice.parentElement,pdfWrapper);assert.equal(formatsArea.scrollTop,283);
assert.equal(pdfChoice.getAttribute('aria-pressed'),'true');pdfChoice.click();
assert.equal(pdfChoice.getAttribute('aria-pressed'),'false');
const bulk=document.querySelector('[data-cv-select-all-formats]');
const cardsBeforeSelection=[...formatsArea.children];
bulk.click();assert.equal(bulk.textContent,'Снять выбор');
assert.equal(bulk.getAttribute('aria-pressed'),'true');
assert.deepEqual([...formatsArea.children],cardsBeforeSelection);
bulk.click();assert.equal(bulk.textContent,'Выбрать всё');
assert.equal(bulk.getAttribute('aria-pressed'),'false');
assert.equal(document.querySelectorAll('.aio-cv__format-main[aria-pressed="true"]').length,0);
assert.equal(run.disabled,true,'deselection cannot leave a stale executable plan');
assert.equal(formatsArea.scrollTop,283);assert.deepEqual([...formatsArea.children],cardsBeforeSelection);
bulk.click();assert.equal(bulk.textContent,'Снять выбор');
assert.equal(preflights.length,checksBeforeSelection,'selection must reuse existing preflight');
assert.equal(plans.length,startsBefore,'Select All itself must not start conversion');"""
text=replace_once(text,old,new);path.write_text(text,encoding='utf-8')
subprocess.run([sys.executable,'-m','compileall','-q','app_backend','tests'],check=True)
subprocess.run([sys.executable,'-m','unittest','test_selection_controls','-v'],check=True,timeout=90)
from app_backend.converter_assets import read_designer_asset
source=Path(os.environ['RUNNER_TEMP'])/'aio-selection-controller.js'
source.write_bytes(read_designer_asset(str(Path.cwd()),'file-converter.js'))
subprocess.run(['node','--check',str(source)],check=True)


def github(path,data=None):
    payload=None if data is None else json.dumps(data).encode('utf-8')
    request=urllib.request.Request('https://api.github.com/repos/'+REPO+'/'+path,data=payload,
        headers={'Authorization':'Bearer '+os.environ['GH_TOKEN'],'Accept':'application/vnd.github+json',
        'Content-Type':'application/json','X-GitHub-Api-Version':'2022-11-28'})
    with urllib.request.urlopen(request,timeout=30) as response:return json.load(response)

actual=github('git/ref/heads/fix/fulltest-critical-hosted-lo-20261029')['object']['sha']
if actual!=BASE:raise RuntimeError('QA moved; inspect '+actual+' before merging')
entries=[]
for path in PRODUCT:
    blob=github('git/blobs',{'content':Path(path).read_text(encoding='utf-8'),'encoding':'utf-8'})
    entries.append({'path':path,'mode':'100644','type':'blob','sha':blob['sha']})
tree=github('git/trees',{'base_tree':TREE,'tree':entries})['sha']
commit=github('git/commits',{'message':'fix(ui): toggle verified bulk selection without rebuilding format cards',
    'tree':tree,'parents':[BASE]})['sha']
print('AIO_CANDIDATE_COMMIT='+commit,flush=True)
print('AIO_CANDIDATE_PARENT='+BASE,flush=True)
print('AIO_REFS_UNCHANGED=true',flush=True)
