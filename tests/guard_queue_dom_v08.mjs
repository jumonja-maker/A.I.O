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

const untilReady=async check=>{const deadline=Date.now()+45000;while(Date.now()<deadline){if(await check())return;await new Promise(r=>setTimeout(r,10));}throw Error('Queue UI did not settle');};
[...document.querySelectorAll('.nav-item')].find(n=>n.textContent.includes('Конвертер')).click();
await untilReady(()=>document.querySelector('[data-cv-add]')?.disabled===false);
const picker=document.querySelector('[data-cv-picker]');
const files=Array.from({length:1000},(_,i)=>new File(['a,b\\n1,2\\n'],'table-'+String(i).padStart(4,'0')+'.csv'));
Object.defineProperty(picker,'files',{configurable:true,value:files});
picker.dispatchEvent(new window.Event('change'));
await untilReady(()=>document.querySelectorAll('.aio-cv__file[data-file-id]').length===1000&&document.querySelector('[data-cv-add]').disabled===false);
const pdf=[...document.querySelectorAll('button[aria-label]')].find(n=>n.getAttribute('aria-label')==='Выбрать PDF');
if(!pdf){for(const tab of document.querySelectorAll('[data-cv-categories] [role="tab"]')){tab.click();const target=document.querySelector('[aria-label="Выбрать PDF"]');if(target){target.click();break;}}}else pdf.click();
const run=document.querySelector('[data-cv-run]');assert.equal(run.disabled,false);run.click();
await untilReady(()=>reports.some(r=>r.finished&&r.succeeded===1000));
await untilReady(()=>document.querySelector('.aio-guard-queue')&&!document.querySelector('.aio-guard-queue').hidden);
const details=document.querySelector('.aio-guard-queue details');details.open=true;details.dispatchEvent(new window.Event('toggle'));
await untilReady(()=>document.querySelectorAll('.aio-guard-task').length===50);
assert.ok(document.querySelector('[data-g-page]').textContent.includes('1000 задач'));
document.querySelector('[data-g-next]').click();assert.equal(document.querySelectorAll('.aio-guard-task').length,50);
const search=document.querySelector('.aio-guard-tools input');search.value='table-0999';search.dispatchEvent(new window.Event('input'));
assert.equal(document.querySelectorAll('.aio-guard-task').length,1);
assert.ok(document.querySelector('.aio-guard-task strong').textContent.includes('table-0999.csv'));
assert.ok(document.querySelector('[data-g-counts]').textContent.includes('успешно 1000'));
assert.equal(plans.at(-1).plan.length,1000);
assert.ok(plans.at(-1).plan.every(p=>p.formats.length===1&&p.formats[0]==='pdf'));
console.log(JSON.stringify({status:'PASS',scope:'1000-file DOM + HTTP; synthetic native exports',files:1000,tasks:1000,pageSize:50,search:true,explicitSelection:true}));
window.AllInOneBridge.converter.dispose();
