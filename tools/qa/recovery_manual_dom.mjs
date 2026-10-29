import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {pathToFileURL} from 'node:url';
const {parseHTML}=await import(pathToFileURL(process.env.AIO_DOM_MODULE).href);
const {window}=parseHTML('<html><body data-aio-token="test"><section class="aio-cv"><header class="aio-cv__format-head"></header><footer class="aio-cv__footer"></footer></section></body></html>');
const {document}=window;
const tick=()=>new Promise(resolve=>setTimeout(resolve,15));
async function settle(){for(let i=0;i<5;i++)await tick();}
const clone=value=>JSON.parse(JSON.stringify(value));
let snapshot={jobs:[],warnings:[],storage:{jobs:0,maxJobs:8,unsavedOutputs:0,unsavedBytes:0,cleanupPending:0},
  recoveryScan:{available:true,state:'idle',loaded:false,pendingFolders:0}};
const calls=[];let scans=0,fail=false,confirm=true,hold=null;
window.AllInOneNativeShell=true;
window.confirm=()=>confirm;
window.AllInOneBridge={converter:{
  async recoverable(options){calls.push(options);if(hold){const pending=hold;hold=null;return pending;}if(fail)throw Error('offline');return clone(snapshot);},
  async scanRecovery(){scans++;snapshot.recoveryScan.state='scanning';return clone(snapshot);},
  getHtmlSources(){return [];},getQualityCheck(){return false;},canRetry(){return false;},
  async discardRetained(id){snapshot.jobs=snapshot.jobs.filter(job=>job.jobId!==id);snapshot.storage.cleanupPending=1;
    window.dispatchEvent(new window.CustomEvent('aio-guard-job-released',{detail:{jobId:id}}));}
}};
const observerCallbacks=[];
// DOM nodes are real linkedom nodes; mutation delivery and native dialog are controlled.
class Observer{constructor(callback){observerCallbacks.push(callback);}observe(){}disconnect(){}}
const context={window,document,console,MutationObserver:Observer,CustomEvent:window.CustomEvent,
  AbortController,setTimeout,clearTimeout,setInterval,clearInterval,URL,
  fetch:async url=>{
    assert.equal(new URL(url,'http://127.0.0.1').searchParams.get('lookup'),'current','preview uses current lookup');
    return {ok:true,json:async()=>({jobId:'old',files:[],finished:true})};
  }};
vm.runInNewContext(fs.readFileSync('src/guard-queue.js','utf8'),context);
await settle();
assert.equal(scans,0);
assert.equal(calls[0]?.scan,false,'mount must request an in-memory snapshot only');
let dialog=document.querySelector('dialog');
function shimDialog(node){node.showModal=function(){this.open=true;this.setAttribute('open','');};node.close=function(){this.open=false;this.removeAttribute('open');this.dispatchEvent(new window.Event('close'));};}
shimDialog(dialog);
const click=selector=>document.querySelector(selector).dispatchEvent(new window.Event('click'));
assert.equal(document.querySelector('.aio-guard-state-button').disabled,false,'empty queue still permits manual recovery');
click('.aio-guard-state-button');await settle();
assert.equal(dialog.open,true);assert.equal(scans,0);
assert.match(document.querySelector('[data-g-scan-status]').textContent,/автоматически не загружаются/);
assert.equal(document.querySelector('[data-g-jobs]').getAttribute('size'),'4','bounded inline list, no native dropdown');
click('[data-g-scan]');await settle();
assert.equal(scans,1);assert.match(document.querySelector('[data-g-scan-status]').textContent,/проверяем целостность/);
const beforePoll=calls.length;await new Promise(resolve=>setTimeout(resolve,1100));
assert.ok(calls.length>beforePoll,'visible scan must refresh progressively');
dialog.close();await settle();const closedCalls=calls.length;
await new Promise(resolve=>setTimeout(resolve,1100));assert.equal(calls.length,closedCalls,'closed dialog must stop polling');
snapshot={...snapshot,jobs:[{jobId:'old',recovered:true,label:'<img src=x onerror=bad()>',outputs:2,unsavedOutputs:2,state:'partial'}],
  recoveryScan:{available:true,state:'completed',loaded:true,pendingFolders:0}};
click('.aio-guard-state-button');await settle();
assert.equal(scans,1,'reopening does not restart disk discovery');
assert.equal(document.querySelectorAll('[data-g-jobs] option').length,1);
assert.equal(document.querySelector('[data-g-jobs] img'),null,'labels stay inert');
assert.match(document.querySelector('[data-g-storage]').textContent,/текущих несохранённых: 0/);
assert.match(document.querySelector('[data-g-storage]').textContent,/предыдущих пакетов в списке: 1/);
fail=true;click('[data-g-refresh]');await settle();
assert.match(document.querySelector('[data-g-scan-status]').textContent,/Не удалось обновить/);
assert.equal(document.querySelectorAll('[data-g-jobs] option').length,1,'transport errors retain known results');
fail=false;click('[data-g-refresh]');await settle();
const chosen=document.querySelector('[data-g-jobs]');
chosen.firstElementChild.selected=true;
chosen.dispatchEvent(new window.Event('change'));await settle();
assert.equal(chosen.value,'old');
assert.equal(document.querySelector('[data-g-folder]').disabled,false,'list selection enables save');
confirm=false;click('[data-g-discard]');await settle();
assert.equal(snapshot.jobs.length,1);assert.match(document.querySelector('[data-g-current]').textContent,/отменено/);
confirm=true;click('[data-g-discard]');await settle();
assert.equal(snapshot.jobs.length,0);assert.doesNotMatch(document.querySelector('[data-g-current]').textContent,/Обновляем состояние/);
assert.match(document.querySelector('[data-g-scan-status]').textContent,/Удаляем служебные папки/);
snapshot.storage.cleanupPending=0;click('[data-g-refresh]');await settle();
assert.doesNotMatch(document.querySelector('[data-g-scan-status]').textContent,/Удаляем служебные папки/);
// A late snapshot from an unmounted converter cannot repopulate a new view.
let finish;hold=new Promise(resolve=>{finish=resolve;});
click('[data-g-refresh]');await tick();
const oldRoot=document.querySelector('.aio-cv');oldRoot.remove();observerCallbacks[0]();
const replacement=document.createElement('section');replacement.className='aio-cv';
replacement.innerHTML='<header class="aio-cv__format-head"></header><footer class="aio-cv__footer"></footer>';
document.body.append(replacement);observerCallbacks[0]();shimDialog(document.querySelector('dialog'));
finish({...snapshot,jobs:[{jobId:'stale',label:'obsolete',outputs:1,unsavedOutputs:1}]});await settle();
assert.equal(document.querySelectorAll('[data-g-jobs] option').length,0,'old view response discarded');
window.dispatchEvent(new window.Event('pagehide'));await settle();const hiddenCalls=calls.length;
window.dispatchEvent(new window.Event('focus'));await settle();assert.equal(calls.length,hiddenCalls);
assert.ok(calls.every(options=>options?.scan===false));
console.log(JSON.stringify({status:'PASS',scope:'production queue + real DOM; controlled HTTP, mutation delivery and native dialog',
  manualScanOnly:true,snapshotOnlyPolls:true,visibleProgress:true,closedPollingStopped:true,
  boundedInlineList:true,recoveredNotCurrent:true,transportFailureKeepsResults:true,
  cancelledDiscardPreservesResults:true,cleanupCompletionVisible:true,staleUnmountRejected:true,pagehideStopsRequests:true}));
