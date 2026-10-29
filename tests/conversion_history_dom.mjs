import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {pathToFileURL} from 'node:url';
import path from 'node:path';
const {parseHTML}=await import(pathToFileURL(process.env.AIO_DOM_MODULE||path.resolve('node_modules/linkedom/esm/index.js')).href);
const {window,document}=parseHTML('<html><body data-aio-token="test-token"><dialog></dialog></body></html>');
const dialog=document.querySelector('dialog');dialog.open=true;
const requests=[],created=[],revoked=[],downloads=[];let consent=false;
window.confirm=()=>consent;
const originalClick=window.HTMLElement.prototype.click;
window.HTMLElement.prototype.click=function(){
  if(this.tagName==='A'){downloads.push({name:this.download,href:this.href});return;}
  return originalClick.call(this);
};
const context=vm.createContext({window,document,console,Date,Blob,AbortController,setTimeout,clearTimeout,
 URL:{createObjectURL(value){created.push(value);return 'blob:test-'+created.length;},revokeObjectURL(value){revoked.push(value);}},
 fetch(url,options){return new Promise(resolve=>requests.push({url,options,
   reply(data,ok=true){resolve({ok,json:async()=>data});}}));}});
vm.runInContext(fs.readFileSync('src/conversion-history.js','utf8'),context);
const panel=window.AIOConversionHistory.mount(dialog);
const box=dialog.querySelector('details');
const tick=async()=>{for(let i=0;i<3;i++)await new Promise(setImmediate);};
const click=label=>{const b=[...box.querySelectorAll('button')].find(v=>v.textContent===label);assert.ok(b,label);b.dispatchEvent(new window.Event('click'));};
const toggle=open=>{box.open=open;box.dispatchEvent(new window.Event('toggle'));};
const item=(id='a'.repeat(32))=>({jobId:id,state:'partial',firstInput:'<img src=x onerror=evil()>',inputCount:1,total:2,succeeded:1,failed:1,outputs:1,savedOutputs:0,recordedMs:1,updatedMs:1});
const page=(items=[item()],nextCursor=null)=>({enabled:true,items,nextCursor,writeWarning:null});
const latest=()=>requests[requests.length-1];
const reply=async(data,ok=true)=>{latest().reply(data,ok);await tick();};
const cases=[];

assert.equal(requests.length,0,'mount must not scan/poll');
toggle(true);assert.equal(requests.length,1);assert.equal(latest().options.headers['X-AIO-Token'],'test-token');
await reply(page());
assert.equal(box.querySelectorAll('[data-history-job]').length,1);
assert.equal(box.querySelector('img'),null,'source names must remain text');
assert.equal(box.querySelector('strong').textContent,item().firstInput);
assert.ok(box.textContent.includes('ошибок: 1'));cases.push('lazy authenticated load and inert names');

click('Обновить историю');await reply(page([item()],30));
click('Далее');assert.ok(latest().url.includes('before=30'));await reply(page([item('b'.repeat(32))]));
click('Назад');assert.ok(!latest().url.includes('before='));await reply(page());cases.push('keyset next and previous');

let count=requests.length;click('Удалить запись');assert.equal(requests.length,count,'cancelled confirmation cannot delete');
consent=true;click('Удалить запись');
assert.equal(latest().url,'/api/converter-guard/history/delete');
assert.deepEqual(JSON.parse(latest().options.body),{jobId:'a'.repeat(32),confirm:true});
await reply({jobId:'a'.repeat(32),deleted:true,resultFilesTouched:false});
await reply(page([]));assert.equal(box.querySelectorAll('[data-history-job]').length,0);cases.push('explicit report-only delete');

click('Обновить историю');await reply(page());
click('Обновить историю');await reply({error:'История временно недоступна'},false);
assert.equal(box.querySelectorAll('[data-history-job]').length,1,'transport failure cannot clear old records');cases.push('read failure retains prior rows');
click('Обновить историю');await reply(page(Array.from({length:26},(_,i)=>item(i.toString(16).padStart(32,'0')))));
assert.equal(box.querySelectorAll('[data-history-job]').length,1,'oversized response rejected');cases.push('bounded DOM inventory');

click('Скачать отчёт');await reply({historySnapshot:{jobId:'b'.repeat(32)}});
assert.equal(created.length,0,'mismatched report cannot be downloaded');
click('Скачать отчёт');await reply({historySnapshot:{jobId:'a'.repeat(32)},qualityCompared:false,files:[]});
assert.equal(created.length,1);assert.equal(downloads.length,1);cases.push('report download identity and no result save claim');

click('Обновить историю');const obsolete=latest();
dialog.open=false;dialog.dispatchEvent(new window.Event('close'));assert.equal(obsolete.options.signal.aborted,true);
dialog.open=true;toggle(true);const fresh=latest();assert.notEqual(fresh,obsolete);
await reply(page([item('c'.repeat(32))]));obsolete.reply(page([item('d'.repeat(32))]));await tick();
assert.equal(box.querySelector('[data-history-job]').getAttribute('data-history-job'),'c'.repeat(32));cases.push('closed and obsolete requests cannot repaint');

click('Обновить историю');const hidden=latest();window.dispatchEvent(new window.Event('pagehide'));
assert.equal(hidden.options.signal.aborted,true);hidden.reply(page());await tick();
count=requests.length;await tick();assert.equal(requests.length,count);cases.push('pagehide stops requests with no polling');

toggle(true);const detached=latest();panel.dispose();
assert.equal(detached.options.signal.aborted,true);detached.reply(page());await tick();
assert.equal(dialog.querySelector('details'),null);assert.ok(revoked.includes('blob:test-1'));panel.dispose();
assert.ok(requests.every(r=>r.url.startsWith('/api/converter-guard/history')));cases.push('dispose clears rows and Blob URLs, no recovery or result endpoints');
console.log('HISTORY_DOM=PASS '+JSON.stringify({cases,scope:'production history module + real DOM; controlled transport and download picker'}));
