// Production queue DOM lifecycle; no claim about native Qt RSS.
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
const {parseHTML}=createRequire(import.meta.url)(process.env.AIO_DOM_MODULE);
const {window,document}=parseHTML('<html><body data-aio-token="fixture"><section class="aio-cv"><div class="aio-cv__format-head"></div><footer class="aio-cv__footer"></footer></section></body></html>');
window.HTMLElement.prototype.showModal=function(){this.open=true;};
window.HTMLElement.prototype.close=function(){this.open=false;this.dispatchEvent(new window.Event('close'));};
const selectPrototype=Object.getPrototypeOf(document.createElement('select'));
Object.defineProperty(selectPrototype,'value',{configurable:true,
  get(){return this.querySelector('option[selected]')?.value||this.querySelector('option')?.value||'';},
  set(value){for(const option of this.querySelectorAll('option'))option.selected=option.value===value;}});
const deferred=()=>{let resolve;return {promise:new Promise(r=>{resolve=r;}),resolve};};
const flush=async()=>{for(let i=0;i<8;i++)await new Promise(r=>setTimeout(r,0));};
const one='a'.repeat(32),two='b'.repeat(32);
let jobs=[],heldRefresh=null,heldPreview=null,walks=0,nextClock=0;
const clocks=new Map(),previewCalls=[];
window.AllInOneNativeShell=true;
window.AllInOneBridge={converter:{canRetry:()=>false,recoverable:async()=>{
  if(heldRefresh){const current=heldRefresh;heldRefresh=null;return current.promise;}
  return {jobs};
}}};
const fetch=async(path,options={})=>{
  const id=new URL(path,'http://localhost').searchParams.get('job');
  previewCalls.push({id,signal:options.signal});
  if(heldPreview){const current=heldPreview;heldPreview=null;return current.promise;}
  return {ok:true,json:async()=>({jobId:id,files:[{fileId:id,name:'retained-'+id[0],targets:[{format:'pdf',state:'ready'}]}]})};
};
vm.runInNewContext(readFileSync(new URL('../src/guard-queue.js',import.meta.url),'utf8'),{
  window,document,fetch,URL,AbortController,CustomEvent:window.CustomEvent,
  MutationObserver:window.MutationObserver,setTimeout,clearTimeout,
  setInterval:callback=>{const id=++nextClock;clocks.set(id,callback);return id;},clearInterval:id=>clocks.delete(id),console
});
await flush();
const files=Array.from({length:1000},(_,i)=>{
  const targets=Array.from({length:83},(_,j)=>({format:'f'+j,state:'queued'}));
  Object.defineProperty(targets,Symbol.iterator,{value:function*(){walks++;yield*Array.prototype.values.call(this);}});
  return {fileId:String(i),name:'file-'+i,targets};
});
const report={jobId:one,revision:1,state:'running',finished:false,files,total:83000,completed:0,succeeded:0,failed:0,cancelled:0,filtered:0,
  currentTask:{fileId:'0',name:'file-0',format:'pdf',stage:'export',startedMs:Date.now()}};
const emit=(type,detail)=>window.dispatchEvent(new window.CustomEvent(type,{detail}));
for(let cycle=0;cycle<2;cycle++){
  emit('aio-guard-state',report);
  document.querySelector('.aio-guard-state-button').click();await flush();
  assert.equal(document.querySelectorAll('.aio-guard-task').length,50);
  assert.equal(walks,(cycle+1)*1000,'each reset must release the previous cache, including its 83k task references');
  document.querySelector('.aio-guard-popup').close();
  assert.equal(document.querySelectorAll('.aio-guard-task').length,0,'closing status releases its off-screen task page');
  emit(cycle?'aio-guard-save-confirmed':'aio-guard-state',cycle?{jobId:one,released:true,savedAll:true}:null);
  await flush();
  assert.equal(document.querySelectorAll('.aio-guard-task').length,0,'closed popup must release its hidden task DOM');
  assert.equal(clocks.size,0,'reset/save must stop elapsed timers');
}
emit('aio-guard-state',report);document.querySelector('.aio-guard-state-button').click();await flush();
assert.equal(document.querySelectorAll('.aio-guard-task').length,50);
emit('aio-guard-job-released',{jobId:one,discarded:true});await flush();
assert.equal(document.querySelectorAll('.aio-guard-task').length,0,'explicit discard must clear current snapshot and rows');
assert.equal(clocks.size,0);

// An old /recoverable response must not resurrect a package already discarded.
const old=deferred();heldRefresh=old;window.dispatchEvent(new window.Event('focus'));await flush();
emit('aio-guard-job-released',{jobId:one,discarded:true});
old.resolve({jobs:[{jobId:one,outputs:1,state:'ready'}]});await flush();
assert.equal(document.querySelector('[data-g-jobs]').children.length,0,'stale recovery response cannot resurrect a released job');

// At most the selected retained preview remains relevant; changing it aborts old transport.
jobs=[one,two].map(jobId=>({jobId,outputs:1,unsavedOutputs:1,state:'ready'}));
const first=deferred();heldPreview=first;window.dispatchEvent(new window.Event('focus'));await flush();
const firstCall=previewCalls.at(-1);assert.equal(firstCall.id,one);
const select=document.querySelector('[data-g-jobs]');select.value=two;select.dispatchEvent(new window.Event('change'));await flush();
assert.equal(firstCall.signal.aborted,true,'changing retained package must abort its old full snapshot request');
first.resolve({ok:true,json:async()=>({jobId:one,files:[{fileId:'old',name:'STALE_RESULT',targets:[{format:'pdf',state:'ready'}]}]})});
await flush();document.querySelector('.aio-guard-state-button').click();await flush();
assert.doesNotMatch(document.querySelector('.aio-guard-list').textContent,/STALE_RESULT/);
assert.match(document.querySelector('.aio-guard-list').textContent,/retained-b/);
const last=deferred();heldPreview=last;select.value=one;select.dispatchEvent(new window.Event('change'));await flush();
const lastCall=previewCalls.at(-1);document.querySelector('.aio-cv').remove();await flush();
assert.equal(lastCall.signal.aborted,true,'tool unmount must abort outstanding preview and release view resources');
last.resolve({ok:true,json:async()=>({jobId:one,files:[]})});await flush();
window.dispatchEvent(new window.Event('pagehide'));
assert.equal(clocks.size,0);
console.log(JSON.stringify({status:'PASS',cycles:2,files:1000,tasks:83000,closedDomReleased:true,discardCleared:true,staleRecoveryIgnored:true,previewAborted:true,toolUnmountSafe:true}));
