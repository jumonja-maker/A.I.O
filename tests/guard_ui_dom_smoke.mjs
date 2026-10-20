// Preserved designer + real Guard HTTP + synthetic native adapter.
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {createRequire} from 'node:module';
import {webcrypto} from 'node:crypto';
const require=createRequire(import.meta.url);
const {parseHTML}=require(process.env.AIO_DOM_MODULE);
const base=process.env.AIO_TEST_SERVER, nativeFetch=globalThis.fetch.bind(globalThis);
const {window,document}=parseHTML(await (await nativeFetch(base+'/')).text());
window.location=new URL(base+'/');window.URL=URL;
window.matchMedia=()=>({matches:false,addEventListener(){},removeEventListener(){}});
window.getComputedStyle=()=>new Proxy({getPropertyValue:()=>'',gap:'8px',padding:'7px'},{get:(t,k)=>t[k]||''});
window.requestAnimationFrame=callback=>setTimeout(callback,1);window.cancelAnimationFrame=clearTimeout;
window.localStorage={getItem:()=>null,setItem(){}};
window.HTMLElement.prototype.getBoundingClientRect=()=>({left:0,right:600,top:0,bottom:400,width:600,height:400});
window.HTMLElement.prototype.focus=function(){};
const plans=[],reports=[],preflights=[],beforeStarts=[],startStates=[];
const apiFetch=async (url,options={})=>{
  const resolved=new URL(url,base);
  if(resolved.pathname==='/api/converter-guard/start'&&options.method==='POST') {
    const plan=JSON.parse(options.body);plans.push(plan);
    const state=await (await nativeFetch(base+'/api/converter-guard/jobs?job='+plan.jobId,
      {headers:{'X-AIO-Token':document.body.dataset.aioToken}})).json();
    assert.equal(state.state,'prepared');assert.equal(state.attempted,0);
    beforeStarts.push(state);
  }
  const response=await nativeFetch(resolved,options);
  if(resolved.pathname==='/api/converter-guard/jobs'&&resolved.search&&response.status===200)
    reports.push(await response.clone().json());
  if(resolved.pathname==='/api/converter-guard/preflight'&&response.ok)
    preflights.push(await response.clone().json());
  if(resolved.pathname==='/api/converter-guard/start'&&response.ok)
    startStates.push(await response.clone().json());
  return response;
};
window.fetch=apiFetch;
const context=vm.createContext({window,document,fetch:apiFetch,console,URL,Blob,File,FormData,
  AbortController,crypto:webcrypto,setTimeout,clearTimeout,setInterval,clearInterval,
  requestAnimationFrame:window.requestAnimationFrame,cancelAnimationFrame:clearTimeout,
  MutationObserver:window.MutationObserver,CustomEvent:window.CustomEvent});
for(const script of document.querySelectorAll('script[src]')) {
  const path=script.getAttribute('src'),response=await apiFetch('/'+path);
  assert.equal(response.ok,true,path);
  vm.runInContext(await response.text(),context,{filename:path});
}
async function until(check,message) {
  const deadline=Date.now()+15000;
  while(Date.now()<deadline){if(await check())return;await new Promise(r=>setTimeout(r,10));}
  throw Error(message+': '+document.querySelector('[data-cv-status]')?.textContent);
}
const rows=()=>[...document.querySelectorAll('.aio-cv__file[data-file-id]')];
const stable=()=>document.querySelector('[data-cv-add]')?.disabled===false;
[...document.querySelectorAll('.nav-item')].find(n=>n.textContent.includes('Конвертер')).click();
await until(()=>stable()&&document.querySelectorAll('[role="tab"]').length>=3,'initial catalog');
const picker=document.querySelector('[data-cv-picker]');
function add(file) {
  Object.defineProperty(picker,'files',{configurable:true,value:Array.isArray(file)?file:[file]});
  picker.dispatchEvent(new window.Event('change'));
}
function target(label) {
  for(const category of [...document.querySelectorAll('[data-cv-categories] [role="tab"]')]) {
    category.click();
    const button=document.querySelector('[aria-label="Выбрать '+label+'"], [aria-label="'+label+' недоступен"]');
    if(button)return button;
  }
  throw Error(label+' must remain available for a native attempt');
}
const run=document.querySelector('[data-cv-run]'),save=document.querySelector('[data-cv-save]');
add(new File(['W'],'unknown.unlisted'));
await until(()=>rows().length===1&&stable(),'unknown input');
assert.equal(target('DOCX').disabled,false,'unknown model is selectable, not just enabled in JSON');
target('DOCX').click();
assert.equal(run.disabled,false);
run.click();await until(()=>!save.hidden,'unknown native conversion');
assert.equal(reports.at(-1).succeeded,1);
assert.equal(reports.at(-1).result.ticket,reports.at(-1).result.downloadUrl.split('ticket=')[1]);
let downloaded=0;
window.AllInOneBridge.saveExcel=async result=>{
  const response=await apiFetch(result.downloadUrl);
  assert.equal(response.ok,true);assert.ok((await response.arrayBuffer()).byteLength>0);
  downloaded++;return {saved:true};
};
save.click();await until(()=>save.hidden&&stable(),'confirmed unknown save');
rows()[0].querySelector('[data-op="remove"]').click();
await until(()=>rows().length===0&&stable(),'remove unknown');
add([new File(['W'],'renamed.bmp'),new File(['BM'],'image.bmp'),new File(['a,b'],'table.csv')]);
await until(()=>rows().length===3&&stable(),'three native models prepared');
assert.equal(document.querySelectorAll('.aio-cv__format-main[aria-pressed="true"]').length,0,'new file set must not inherit old selections');
assert.equal(run.disabled,true,'conversion waits for an explicit user selection');
const offered=preflights.at(-1);
const selected=offered.formats.filter(f=>f.verifiedFileIds.length).map(f=>f.id);
assert.equal(selected.length,6,'only registered exports enter Select All');
assert.ok(selected.includes('dbf'));assert.ok(!selected.includes('bmp'));
const startsBefore=plans.length;
document.querySelector('[data-cv-select-all-formats]').click();
assert.equal(plans.length,startsBefore,'Select All itself must not start conversion');
run.click();await until(()=>!save.hidden,'preflight filtered three-file conversion');
const plan=plans.at(-1),report=reports.at(-1);
assert.equal(plan.onlyVerified,true);
assert.deepEqual(plan.plan,offered.files.map(file=>({fileId:file.id,formats:selected.filter(id=>
  offered.formats.find(f=>f.id===id).verifiedFileIds.includes(file.id))})));
assert.equal(report.total,8);assert.equal(report.succeeded,8);assert.equal(report.filtered,0);
assert.equal(report.attempted,8);assert.equal(report.failed,0);
assert.ok(offered.files[0].preflight.documentServices.includes('com.sun.star.text.TextDocument'));
assert.ok(startStates.at(-1).files.flatMap(f=>f.targets).every(t=>t.state!=='skipped'));
await until(()=>rows()[0].dataset.aioFpState==='ready','only confirmed planned targets are painted');
assert.equal(document.querySelector('[data-cv-select-all-formats]').disabled,false,'Select All stays available after an explicit choice');
const issues=document.querySelector('.aio-cv-issues-button');issues.click();
assert.ok(issues.textContent==='Результат');
assert.ok(document.querySelector('.aio-cv-issues-note').textContent.includes('Готово: 8 из 8'));
assert.ok(document.querySelector('.aio-cv-issues-note').textContent.includes('Ошибок: 0'));
save.click();await until(()=>save.hidden&&stable(),'renamed save');
await until(()=>rows()[0].querySelector('.aio-fp-check')?.hidden,'saved check clears');
assert.equal(downloaded,2);
const checksBeforeRemoval=preflights.length;
while(rows().length) {
  const count=rows().length;rows()[0].querySelector('[data-op="remove"]').click();
  await until(()=>rows().length===count-1&&stable(),'remove prior source');
}
assert.equal(preflights.length,checksBeforeRemoval,'removal after save must not restart preflight');
add(new File(['UNKNOWN!'],'unconfirmed.csv'));
await until(()=>rows().length===1&&stable(),'unconfirmed CSV');
assert.equal(document.querySelectorAll('.aio-cv__format-main[aria-pressed="true"]').length,0);
assert.equal(target('DOCX').disabled,true);
assert.equal(target('PDF').disabled,true);
assert.equal(run.disabled,true);
assert.equal(document.querySelector('[data-cv-select-all-formats]').disabled,true);
issues.click();
assert.equal(document.querySelectorAll('.aio-cv-issues-list .aio-cv-issues-item').length,1,
  'one failed preflight is one cause, not an item for every format');
assert.ok(document.querySelector('.aio-cv-issues-list').textContent.includes('PREFLIGHT_IMPORT_UNDETERMINED'));
const retry=document.querySelector('.aio-cv-issues-popover > .aio-cv-issues-retry');
assert.equal(retry.hidden,false);const checksBeforeRetry=preflights.length;retry.click();
await until(()=>preflights.length===checksBeforeRetry+1&&stable(),'explicit unknown retry');
assert.equal(run.disabled,true);
rows()[0].querySelector('[data-op="remove"]').click();
await until(()=>rows().length===0&&stable(),'remove unconfirmed CSV');
add(new File(['BM'],'completed.bmp'));
await until(()=>rows().length===1&&stable(),'first cancel-case input');
add(new File(['HOLD'],'hold.docx'));
await until(()=>rows().length===2&&stable(),'second cancel-case input');
for(const label of selected) {const button=target(label.toUpperCase());if(button.getAttribute('aria-pressed')==='true'&&!button.disabled)button.click();}
target('PDF').click();run.click();
await until(()=>rows()[1].dataset.aioFpState==='converting','second file is really active');
assert.equal(run.textContent,'Отменить');run.click();
await until(()=>!save.hidden,'cancel retains the first completed output');
assert.equal(reports.at(-1).state,'cancelled');
assert.equal(reports.at(-1).succeeded,1);assert.equal(reports.at(-1).cancelled,1);
await until(()=>rows().every(r=>r.dataset.aioFpState),'cancelled run paint restored after UI render');
assert.deepEqual(rows().map(r=>r.dataset.aioFpState),['ready','cancelled']);
save.click();await until(()=>save.hidden&&stable(),'save partial cancellation');
await until(()=>rows()[0].querySelector('.aio-fp-check')?.hidden,'partial cancel saved check clears');
assert.equal(downloaded,3);
const status=await (await apiFetch('/api/converter-guard/status',{
  headers:{'X-AIO-Token':document.body.dataset.aioToken}})).json();
assert.equal(status.storage.jobs,0);assert.equal(status.storage.reservedBytes,0);
console.log(JSON.stringify({status:'PASS',scope:'preserved DOM + real Guard HTTP; synthetic native/save',
  unknownSelectable:true,threeFileSelectAll:true,requestedPairs:8,nativeAttempts:8,unplannedPairs:10,errors:0,
  honestIssues:true,unknownBlockedBeforeStart:true,noAutomaticSelection:true,removalAfterSaveNoPreflight:true,savedChecksClear:true,
  cancelledJobRetainsCompletedOutput:true,releasedSpool:true}));
