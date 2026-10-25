// Original Designer + real loopback HTTP + synthetic Calc import, not Qt GUI.
// Reproduce the actual 510/510 state and click its blue cancel button.
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {createRequire} from 'node:module';
import {webcrypto} from 'node:crypto';
const require=createRequire(import.meta.url);
const {parseHTML}=require(process.env.AIO_DOM_MODULE);
const base=process.env.AIO_TEST_SERVER;
assert.ok(base,'Requires local-only authenticated test server');
const realFetch=globalThis.fetch.bind(globalThis);
const {window,document}=parseHTML(await (await realFetch(base+'/')).text());
window.location=new URL(base+'/');window.URL=URL;
window.matchMedia=()=>({matches:false,addEventListener(){},removeEventListener(){}});
window.getComputedStyle=()=>new Proxy({getPropertyValue:()=>'',gap:'8px',padding:'7px'},
  {get:(t,k)=>t[k]||''});
window.requestAnimationFrame=callback=>setTimeout(callback,1);
window.cancelAnimationFrame=clearTimeout;
window.localStorage={getItem:()=>null,setItem(){}};
window.HTMLElement.prototype.getBoundingClientRect=()=>({
  left:0,right:600,top:0,bottom:400,width:600,height:400});
window.HTMLElement.prototype.focus=function(){};
window.HTMLElement.prototype.showModal=function(){this.open=true;};
window.HTMLElement.prototype.close=function(){this.open=false;};
window.AllInOneNativeShell=true;
let jobId, cancellationCalls=0;
const token=document.body.dataset.aioToken;
const apiFetch=async(url,options={})=>{
  const resolved=new URL(url,base);
  const response=await realFetch(resolved,options);
  if(resolved.pathname==='/api/converter-guard/prepare'&&response.ok)
    jobId=(await response.clone().json()).jobId;
  if(resolved.pathname==='/api/converter-guard/cancel'&&response.ok)
    cancellationCalls++;
  return response;
};
window.fetch=apiFetch;
const context=vm.createContext({window,document,fetch:apiFetch,console,URL,Blob,File,
  FormData,AbortController,crypto:webcrypto,setTimeout,clearTimeout,setInterval,
  clearInterval,requestAnimationFrame:window.requestAnimationFrame,
  cancelAnimationFrame:clearTimeout,MutationObserver:window.MutationObserver,
  CustomEvent:window.CustomEvent});
for(const script of document.querySelectorAll('script[src]')){
  const path=script.getAttribute('src'),response=await apiFetch('/'+path);
  assert.equal(response.ok,true,path);
  vm.runInContext(await response.text(),context,{filename:path});
}
async function until(check,message,seconds=25){
  const stop=Date.now()+seconds*1000;
  while(Date.now()<stop){
    if(await check())return;
    await new Promise(resolve=>setTimeout(resolve,30));
  }
  throw Error(message+'; job='+jobId+'; status='+
    document.querySelector('[data-cv-status]')?.textContent);
}
[...document.querySelectorAll('.nav-item')]
  .find(node=>node.textContent.includes('Конвертер')).click();
await until(()=>document.querySelector('[data-cv-add]')?.disabled===false,'UI did not mount');
const picker=document.querySelector('[data-cv-picker]');
const files=Array.from({length:510},(_,i)=>
  new File(['a,b\n1,2\n'],'table-'+String(i).padStart(4,'0')+'.csv'));
Object.defineProperty(picker,'files',{configurable:true,value:files});
picker.dispatchEvent(new window.Event('change'));
await until(()=>Boolean(jobId),'Guard preparation was not created');
let atRoutes;
await until(async()=>{
  const response=await realFetch(base+'/api/converter-guard/jobs?job='+encodeURIComponent(jobId),
    {headers:{'X-AIO-Token':token}});
  if(!response.ok)return false;
  const snapshot=await response.json();
  if(snapshot.state==='checking'&&snapshot.preflightPhase==='routes'&&
     snapshot.preflightCompleted===510&&snapshot.preflightMatrixTotal>0){
    atRoutes=snapshot;return true;
  }
  return false;
},'Native route matrix never became visible',45);
const header=document.querySelector('.aio-guard-state-button');
await until(()=>header?.textContent.includes('Направления'),'Route progress absent in header');
const run=document.querySelector('[data-cv-run]');
assert.ok(run,'Blue cancel button missing');
assert.equal(run.disabled,false);
assert.match(run.textContent,/Отменить проверку/);
run.click();
assert.match(run.textContent,/Отменяем проверку/,'Click must acknowledge cancellation immediately');
assert.equal(run.disabled,true);
await until(()=>cancellationCalls===1,'No authenticated backend /cancel request');
await until(async()=>{
  const response=await realFetch(base+'/api/converter-guard/jobs?job='+encodeURIComponent(jobId),
    {headers:{'X-AIO-Token':token}});
  if(response.status===404)return true; // cancelled temp spool released
  if(!response.ok)return false;
  const value=await response.json();
  return value.state==='cancelled' || value.finished===true;
},'Cancel did not release the checking state',15);
await until(()=>run.textContent!=='Отменяем проверку…'&&run.disabled===false,
  'Run button stayed disabled after cancellation',15);
console.log(JSON.stringify({status:'PASS',scope:'real Guard HTTP + designer DOM; synthetic native import',
  inputs:510,preflightCompleted:atRoutes.preflightCompleted,
  matrixTotal:atRoutes.preflightMatrixTotal,cancelRequests:cancellationCalls,
  immediateButtonFeedback:true,backendStopped:true}));
window.AllInOneBridge.converter.dispose();
