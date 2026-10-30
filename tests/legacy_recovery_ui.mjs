import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
const {parseHTML}=createRequire(import.meta.url)(process.env.AIO_DOM_MODULE);
const {window,document}=parseHTML('<html><body data-aio-token="fixture"><section class="aio-cv"><div class="aio-cv__format-head"></div><footer class="aio-cv__footer"></footer></section></body></html>');
window.HTMLElement.prototype.showModal=function(){this.open=true;};
window.HTMLElement.prototype.close=function(){this.open=false;this.dispatchEvent(new window.Event('close'));};
const flush=async()=>{for(let i=0;i<8;i++)await new Promise(r=>setTimeout(r,0));};
const calls=[],saves=[];
let yes=false,late=null,saveOutcome={downloadStarted:true},packet=null;
window.confirm=()=>yes;
window.AllInOneBridge={saveExcel:async result=>{saves.push(result);return saveOutcome;},
  converter:{recoverable:async()=>({jobs:[],warnings:[],storage:{}}),canRetry:()=>false}};
const item=n=>({requestId:n.toString(16).padStart(32,'0'),name:n===0?'<img src=x> Результат.zip':'result-'+n+'.pdf',
  size:123,partial:n===0});
const inventory=offset=>({items:Array.from({length:offset?1:20},(_,i)=>item(offset+i)),
  total:21,more:offset===0,storageAvailable:true,recoveryWarnings:[],
  storage:{maxOwnedBytes:10000,usedBytes:8000,storageMeasurementOk:true}});
const fetch=async(url,options)=>{
  calls.push({url,options});
  assert.equal(options.headers['X-AIO-Token'],'fixture');
  assert.equal(options.cache,'no-store');
  if(late)return await new Promise(resolve=>{late=resolve;});
  let body;
  if(url.includes('recover?offset='))body=packet||inventory(url.includes('offset=20')?20:0);
  else if(url.includes('recover?request='))body={state:'ready',result:{name:'result.zip',ticket:'T'.repeat(43),
    downloadUrl:'/api/converter/result?ticket='+'T'.repeat(43)}};
  else if(url.endsWith('/release')){
    assert.equal(options.method,'POST');assert.deepEqual(JSON.parse(options.body),{ticket:'T'.repeat(43)});body={ok:true};
  }else throw Error('Unexpected URL '+url);
  return {ok:true,json:async()=>body};
};
const context={window,document,fetch,AbortController,URL,CustomEvent:window.CustomEvent,
  MutationObserver:window.MutationObserver,setTimeout,clearTimeout,setInterval,clearInterval,console};
for(const name of ['legacy-recovery.js','guard-queue.js'])
  vm.runInNewContext(readFileSync(new URL('../src/'+name,import.meta.url),'utf8'),context);
await flush();
assert.equal(document.querySelectorAll('[data-legacy-recovery]').length,0,
  'Ordinary product must not mount legacy recovery');
const explicitLegacy = window.AIOLegacyRecovery.mount(document.querySelector('.aio-guard-popup'));
assert.equal(document.querySelectorAll('[data-legacy-recovery]').length,1,
  'Explicit compatibility fixture can still mount the component');
assert.equal(calls.length,0,'No legacy inventory scanning/polling at mount');
document.querySelector('.aio-guard-state-button').click();await flush();
assert.equal(calls.length,0,'Collapsed details stay idle');
const panel=document.querySelector('[data-legacy-recovery]');
panel.open=true;panel.dispatchEvent(new window.Event('toggle'));await flush();
assert.equal(document.querySelectorAll('[data-l-list] > div').length,20);
assert.equal(panel.querySelectorAll('img').length,0);
assert.match(panel.textContent,/есть ошибки преобразования/);
assert.match(panel.querySelector('[data-l-storage]').textContent,/8000/);
assert.equal(panel.querySelector('[data-l-prev]').disabled,true);
panel.querySelector('[data-l-next]').click();await flush();
assert.match(panel.querySelector('[data-l-page]').textContent,/21–21 из 21/);
assert.equal(panel.querySelector('[data-l-next]').disabled,true);
panel.querySelector('[data-l-prev]').click();await flush();
panel.querySelector('[data-l-action="save"]').click();await flush();
assert.equal(saves.length,1);
assert.equal(calls.filter(call=>call.url.endsWith('/release')).length,0,'Saving never releases the only retained copy');
assert.match(panel.querySelector('[data-l-status]').textContent,/не подтверждена/);
saveOutcome={saved:true};panel.querySelector('[data-l-action="save"]').click();await flush();
assert.match(panel.querySelector('[data-l-status]').textContent,/Проверьте/);
assert.equal(calls.filter(call=>call.url.endsWith('/release')).length,0);
panel.querySelector('[data-l-action="discard"]').click();await flush();
assert.equal(calls.filter(call=>call.url.endsWith('/release')).length,0,'Cancel confirmation makes no mutation');
yes=true;panel.querySelector('[data-l-action="discard"]').click();await flush();
assert.equal(calls.filter(call=>call.url.endsWith('/release')).length,1);
packet={...inventory(0),recoveryWarnings:Array.from({length:70},()=>({code:'<img src=x> UNKNOWN'}))};
panel.querySelector('[data-l-refresh]').click();await flush();
assert.equal(panel.querySelectorAll('[data-l-warnings] p').length,40);
assert.equal(panel.querySelectorAll('img').length,0);
late=true;panel.querySelector('[data-l-action="save"]').click();await flush();
assert.equal(typeof late,'function');
const dialog=document.querySelector('.aio-guard-popup');dialog.close();
const before=panel.querySelector('[data-l-status]').textContent;
const resolve=late;late=null;resolve({ok:true,json:async()=>({state:'ready',result:{ticket:'T'.repeat(43),
  downloadUrl:'/api/converter/result?ticket='+'T'.repeat(43)}})});await flush();
assert.equal(panel.querySelector('[data-l-status]').textContent,before,'Closed State ignores late replies');
assert.equal(saves.length,2,'A late lookup cannot open a save dialog after close');
document.querySelector('.aio-guard-state-button').click();await flush();
assert.equal(panel.querySelector('[data-l-refresh]').disabled,false,'Reopened State remains usable');
late=true;panel.querySelector('[data-l-action="save"]').click();await flush();
const pageReply=late;late=null;
window.dispatchEvent(new window.Event('pagehide'));
explicitLegacy.dispose(); // Explicit compatibility callers own teardown, not the production queue.
pageReply({ok:true,json:async()=>({state:'ready',result:{ticket:'T'.repeat(43),
  downloadUrl:'/api/converter/result?ticket='+'T'.repeat(43)}})});await flush();
assert.equal(saves.length,2,'Explicit teardown ignores a late lookup');
assert.equal(panel.isConnected,false,'Explicit compatibility teardown disposes the component');
console.log(JSON.stringify({status:'PASS',lazyDiscovery:true,paginated:20,htmlEscaped:true,
  partialVisible:true,noAutomaticRelease:true,explicitDiscard:true,browserDownloadNotSaved:true,
  nativeCopyStillRetained:true,warningsBounded:40,lateReplyIgnored:true,reopenUsable:true}));
