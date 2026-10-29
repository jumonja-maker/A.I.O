import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {pathToFileURL} from 'node:url';
const {parseHTML}=await import(pathToFileURL(process.env.AIO_DOM_MODULE).href);
const source=fs.readFileSync('src/guard-queue.js','utf8');
const pending={jobs:[],warnings:[],storage:null,recoveryScan:{state:'scanning',complete:false}};
const empty={jobs:[],warnings:[],storage:null,recoveryScan:{state:'complete',complete:true}};
const tick=async()=>{for(let i=0;i<12;i++)await Promise.resolve();};
function setup(){
  const {window,document}=parseHTML('<html><body data-aio-token="qa"><main class="aio-cv"><div class="aio-cv__format-head"></div><div class="aio-cv__footer"></div></main></body></html>');
  const timers=new Map();let serial=0,observer;const calls=[];
  let answer=()=>Promise.resolve(pending);
  const proto=window.HTMLElement.prototype;
  proto.showModal=function(){this.open=true;this.setAttribute('open','');};
  proto.close=function(){this.open=false;this.removeAttribute('open');this.dispatchEvent(new window.Event('close'));};
  proto.focus=function(){};
  window.AllInOneBridge={converter:{
    recoverable(options){calls.push(options);return answer();},getHtmlSources:()=>[],canRetry:()=>false
  }};
  const sandbox={window,document,console,CustomEvent:window.CustomEvent,
    MutationObserver:class{constructor(fn){observer=fn;}observe(){}disconnect(){}},
    setTimeout(fn,ms){const id=++serial;timers.set(id,{fn,ms});return id;},
    clearTimeout(id){timers.delete(id);},setInterval(){return 99999;},clearInterval(){},
    fetch:async()=>({ok:true,json:async()=>({files:[],finished:true})})};
  vm.runInNewContext(source,sandbox,{filename:'src/guard-queue.js'});
  return {window,document,timers,calls,setAnswer(fn){answer=fn;},mutate(){observer();},
    q(selector){return document.querySelector(selector);},
    poll(){const [id,entry]=[...timers][0];timers.delete(id);entry.fn();}};
}
const checked=[];
{
  const e=setup();await tick();
  assert.equal(e.q('.aio-guard-queue').hidden,false);
  assert.equal(e.q('.aio-guard-state-button').disabled,false);
  assert.equal(e.q('[data-g-recovery-scan]').hidden,false);
  assert.match(e.q('[data-g-recovery-progress]').textContent,/Проверяем/);
  assert.equal(e.timers.size,0,'closed popup must not poll');
  e.q('.aio-guard-state-button').click();await tick();
  assert.equal(e.timers.size,1);assert.equal([...e.timers.values()][0].ms,750);
  e.poll();await tick();assert.equal(e.timers.size,1,'only one timer is retained');
  e.q('.aio-guard-popup').close();assert.equal(e.timers.size,0);
  checked.push('pending_is_visible_and_polling_is_bound_to_open_dialog');
}
{
  const e=setup();await tick();let complete;
  e.setAnswer(()=>new Promise(resolve=>{complete=resolve;}));
  e.q('.aio-guard-state-button').click();await tick();
  e.q('.aio-guard-popup').close();complete(pending);await tick();
  assert.equal(e.timers.size,0,'late reply must not rearm a closed popup');
  e.setAnswer(()=>Promise.resolve(pending));
  e.q('.aio-guard-state-button').click();await tick();assert.equal(e.timers.size,1);
  e.setAnswer(()=>Promise.resolve(empty));e.poll();await tick();assert.equal(e.timers.size,0);
  checked.push('close_during_request_and_completion_stop_polling');
}
{
  const e=setup();await tick();let complete;
  e.setAnswer(()=>new Promise(resolve=>{complete=resolve;}));
  e.q('.aio-guard-state-button').click();await tick();
  for(let i=0;i<20;i++)e.window.dispatchEvent(new e.window.Event('focus'));
  assert.equal(e.calls.length,2,'refresh requests must coalesce');
  e.setAnswer(()=>Promise.resolve(empty));complete(pending);await tick();
  assert.equal(e.calls.length,3,'one follow-up, not twenty');
  checked.push('concurrent_refreshes_coalesce');
}
{
  const e=setup();await tick();
  e.setAnswer(()=>Promise.resolve({...pending,recoveryScan:{state:'error',code:'RECOVERY_SCAN_FAILED'}}));
  e.q('.aio-guard-state-button').click();await tick();
  assert.equal(e.q('[data-g-recovery-retry]').hidden,false);assert.equal(e.timers.size,0);
  e.setAnswer(()=>Promise.resolve(pending));e.q('[data-g-recovery-retry]').click();await tick();
  assert.equal(e.calls.at(-1).retry,true);assert.equal(e.timers.size,1);
  e.setAnswer(()=>Promise.reject(new Error('transport failed')));e.poll();await tick();
  assert.equal(e.q('[data-g-recovery-retry]').hidden,false);assert.equal(e.timers.size,0);
  assert.match(e.q('[data-g-recovery-progress]').textContent,/не завершён/);
  checked.push('scan_and_transport_errors_are_explicit_with_manual_retry');
}
{
  const e=setup();await tick();let complete;
  e.setAnswer(()=>new Promise(resolve=>{complete=resolve;}));
  e.q('.aio-guard-state-button').click();await tick();
  e.window.dispatchEvent(new e.window.Event('pagehide'));
  complete({...pending,jobs:[{jobId:'old',label:'STALE RESULT',outputs:1}]});await tick();
  assert.equal(e.timers.size,0);assert.doesNotMatch(e.document.body.textContent,/STALE RESULT/);
  checked.push('pagehide_rejects_late_recovery_response');
}
{
  const e=setup();await tick();let complete;
  e.setAnswer(()=>new Promise(resolve=>{complete=resolve;}));
  e.q('.aio-guard-state-button').click();await tick();
  e.q('.aio-cv').remove();e.mutate();
  complete(pending);await tick();assert.equal(e.timers.size,0);
  assert.equal(e.q('.aio-guard-queue'),null);
  checked.push('unmount_stops_polling_and_ignores_old_view');
}
const css=fs.readFileSync('src/guard-queue.css','utf8');
assert.match(css,/\.aio-guard-save select\{flex:1 1 180px;min-width:0;width:0;max-width:100%/);
console.log('AIO_RECOVERY_SCAN_DOM='+JSON.stringify({status:'PASS',checks:checked,
  scope:'production JS in real DOM with controlled transport/timers; no pixel layout or installed GUI'}));
