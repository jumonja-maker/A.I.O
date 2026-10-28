// Read-only recovery diagnostics; production queue UI, not a fabricated view.
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
const flush=async()=>{for(let i=0;i<8;i++)await new Promise(r=>setTimeout(r,0));};
let packet={jobs:[],warnings:[{folder:'aio-guard-unfinished',code:'RECOVERY_UNFINISHED_PREPARATION',manualRecovery:true}],storage:{jobs:0,maxJobs:16,unsavedOutputs:0,unsavedBytes:0}};
let writes=0;
window.AllInOneNativeShell=true;
window.AllInOneBridge={converter:{recoverable:async()=>packet,canRetry:()=>false,
  discardRetained:async()=>{writes++;throw Error('No deletion requested');},
  saveRetained:async()=>{writes++;throw Error('No save requested');}}};
vm.runInNewContext(readFileSync(new URL('../src/guard-queue.js',import.meta.url),'utf8'),{
  window,document,fetch:async()=>{throw Error('No request expected');},URL,AbortController,
  CustomEvent:window.CustomEvent,MutationObserver:window.MutationObserver,
  setTimeout,clearTimeout,setInterval,clearInterval,console
});
await flush();
const current=()=>document.querySelector('[data-g-current]').textContent;
assert.match(current(),/Проблемы восстановления/);
assert.doesNotMatch(current(),/Есть результаты|повреждённым журналом/);
assert.equal(document.querySelector('[data-g-discard]').disabled,true);
assert.equal(document.querySelector('[data-g-report]').disabled,false);
assert.equal(document.querySelector('[data-g-recovery]').hidden,false);
assert.match(document.querySelector('[data-g-recovery-list]').textContent,/RECOVERY_UNFINISHED_PREPARATION/);
assert.match(document.querySelector('[data-g-recovery-list]').textContent,/незавершённая подготовка/);
assert.match(document.querySelector('[data-g-recovery-list]').textContent,/не подтверждены/);
document.querySelector('.aio-guard-state-button').click();await flush();
packet={...packet,warnings:[{folder:'<img src=x>',code:'RECOVERY_CLEANUP_RETRY'},
  {folder:'aio-guard-ledger',code:'RECOVERY_LEDGER_INCOMPLETE',integrity:'torn_tail'},
  {folder:'aio-guard-new',code:'FUTURE_RECOVERY_CODE'}]};
window.dispatchEvent(new window.Event('focus'));await flush();
const details=document.querySelector('[data-g-recovery-list]');
assert.match(details.textContent,/очистка/);
assert.match(details.textContent,/RECOVERY_LEDGER_INCOMPLETE/);
assert.match(details.textContent,/torn_tail/);
assert.match(details.textContent,/FUTURE_RECOVERY_CODE/);
assert.equal(details.querySelectorAll('img').length,0,'Untrusted labels must remain text');
assert.match(current(),/3/);
packet={...packet,warnings:Array.from({length:60},(_,i)=>({folder:'folder-'+i,code:'RECOVERY_METADATA_INVALID'}))};
window.dispatchEvent(new window.Event('focus'));await flush();
assert.equal(details.querySelectorAll('[data-g-recovery-entry]').length,40,'Bound the diagnostic DOM');
assert.match(details.textContent,/20/);
packet={...packet,warnings:[]};window.dispatchEvent(new window.Event('focus'));await flush();
assert.equal(document.querySelector('[data-g-recovery]').hidden,true);
assert.equal(details.children.length,0);
assert.equal(writes,0);
window.dispatchEvent(new window.Event('pagehide'));
console.log(JSON.stringify({status:'PASS',unfinishedNotCorrupt:true,actualCodesVisible:true,noInventedResults:true,readOnly:true,boundedRows:40,htmlEscaped:true,clearsOnRefresh:true}));
