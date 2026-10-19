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
const plans=[],reports=[];
const apiFetch=async (url,options={})=>{
  const resolved=new URL(url,base);
  if(resolved.pathname==='/api/converter-guard/jobs'&&options.method==='POST')
    plans.push(JSON.parse(options.body));
  const response=await nativeFetch(resolved,options);
  if(resolved.pathname==='/api/converter-guard/jobs'&&resolved.search&&response.status===200)
    reports.push(await response.clone().json());
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
  Object.defineProperty(picker,'files',{configurable:true,value:[file]});
  picker.dispatchEvent(new window.Event('change'));
}
function target(label) {
  for(const category of [...document.querySelectorAll('[data-cv-categories] [role="tab"]')]) {
    category.click();
    const button=document.querySelector('[aria-label="Выбрать '+label+'"]');
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
add(new File(['W'],'renamed.bmp'));
await until(()=>rows().length===1&&stable(),'renamed Writer source');
target('DOCX').click(); // clear the persisted single selection
const selected=['PDF','DOCX','ODT','DBF','PNG'];
for(const label of selected) {const button=target(label);assert.equal(button.disabled,false);button.click();}
run.click();await until(()=>!save.hidden,'broad renamed conversion');
const plan=plans.at(-1),report=reports.at(-1);
assert.deepEqual(plan.plan[0].formats,selected.map(s=>s.toLowerCase()));
assert.deepEqual(report.files[0].targets.map(t=>t.format),plan.plan[0].formats);
assert.equal(report.total,5);assert.equal(report.succeeded,3);assert.equal(report.filtered,2);
assert.equal(report.attempted,3);assert.equal(report.failed,0);
assert.equal(report.files[0].nativeRoute.documentService,'com.sun.star.text.TextDocument');
await until(()=>rows()[0].dataset.aioFpState==='ready','skipped routes are not painted as errors');
assert.ok(rows()[0].querySelector('.aio-fp-label').textContent.includes('пропущено Guard: 2'));
const issues=document.querySelector('.aio-cv-issues-button');issues.click();
assert.ok(issues.textContent.startsWith('Итоги'));
assert.ok(document.querySelector('.aio-cv-issues-note').textContent.includes('Пропущено Guard: 2'));
assert.ok(document.querySelector('.aio-cv-issues-note').textContent.includes('Ошибок: 0'));
assert.ok(document.querySelector('.aio-cv-issues-list').textContent.includes('Направление пропущено Guard'));
save.click();await until(()=>save.hidden&&stable(),'renamed save');
await until(()=>rows()[0].querySelector('.aio-fp-check')?.hidden,'saved check clears');
assert.equal(downloaded,2);
rows()[0].querySelector('[data-op="remove"]').click();
await until(()=>rows().length===0&&stable(),'remove renamed');
add(new File(['BM'],'completed.bmp'));
await until(()=>rows().length===1&&stable(),'first cancel-case input');
add(new File(['HOLD'],'hold.docx'));
await until(()=>rows().length===2&&stable(),'second cancel-case input');
for(const label of selected) {const button=target(label);if(button.getAttribute('aria-pressed')==='true')button.click();}
target('PDF').click();run.click();
await until(()=>rows()[1].dataset.aioFpState==='converting','second file is really active');
assert.equal(run.textContent,'Отменить');run.click();
await until(()=>!save.hidden,'cancel retains the first completed output');
assert.equal(reports.at(-1).state,'cancelled');
assert.equal(reports.at(-1).succeeded,1);assert.equal(reports.at(-1).cancelled,1);
assert.deepEqual(rows().map(r=>r.dataset.aioFpState),['ready','cancelled']);
save.click();await until(()=>save.hidden&&stable(),'save partial cancellation');
await until(()=>rows()[0].querySelector('.aio-fp-check')?.hidden,'partial cancel saved check clears');
assert.equal(downloaded,3);
const status=await (await apiFetch('/api/converter-guard/status',{
  headers:{'X-AIO-Token':document.body.dataset.aioToken}})).json();
assert.equal(status.storage.jobs,0);assert.equal(status.storage.reservedBytes,0);
console.log(JSON.stringify({status:'PASS',scope:'preserved DOM + real Guard HTTP; synthetic native/save',
  unknownSelectable:true,broadRenamedPlan:5,nativeAttempts:3,filtered:2,errors:0,
  honestIssues:true,skipsDoNotTurnReadyRed:true,savedChecksClear:true,
  cancelledJobRetainsCompletedOutput:true,releasedSpool:true}));
