// Opt-in DOM + real HTTP acceptance; does not emulate a native Qt save dialog.
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {createRequire} from 'node:module';
import {webcrypto} from 'node:crypto';
const require = createRequire(import.meta.url);
const nativeFetch = globalThis.fetch.bind(globalThis);
const {parseHTML} = require(process.env.AIO_DOM_MODULE);
const base = process.env.AIO_TEST_SERVER;
const html = await (await fetch(base + '/')).text();
const {window, document} = parseHTML(html);
window.location = new URL(base + '/');
window.matchMedia = () => ({matches:false,addEventListener(){},removeEventListener(){}});
window.getComputedStyle = () => new Proxy({getPropertyValue:()=>'',gap:'8px',padding:'7px'}, {get:(target,key)=>target[key]||''});
window.requestAnimationFrame = callback=>setTimeout(callback,1);
window.cancelAnimationFrame = clearTimeout;
window.localStorage = {getItem:()=>null,setItem(){}};
window.HTMLElement.prototype.getBoundingClientRect = () => ({left:0,right:600,top:0,bottom:400,width:600,height:400});
window.HTMLElement.prototype.focus = function(){};
const apiFetch = (url,options)=>nativeFetch(new URL(url,base),options);
window.fetch=apiFetch; window.URL=URL;
const context=vm.createContext({window,document,fetch:apiFetch,console,URL,Blob,File,
  AbortController,crypto:webcrypto,setTimeout,clearTimeout,setInterval,clearInterval,
  requestAnimationFrame:window.requestAnimationFrame,cancelAnimationFrame:clearTimeout,
  MutationObserver:window.MutationObserver,CustomEvent:window.CustomEvent});
for(const script of document.querySelectorAll('script[src]')) {
  const name=script.getAttribute('src');
  const response=await apiFetch('/'+name);
  assert.equal(response.ok,true,name);
  vm.runInContext(await response.text(),context,{filename:name});
}
async function until(check,message) {
  const deadline=Date.now()+10000;
  while(Date.now()<deadline){if(check())return;await new Promise(resolve=>setTimeout(resolve,10));}
  throw new Error(message+': '+document.querySelector('[data-cv-status]')?.textContent);
}
const converterNav=[...document.querySelectorAll('.nav-item')].find(n=>n.textContent.includes('Конвертер'));
assert.ok(converterNav,'Converter navigation exists');converterNav.click();
await until(()=>document.querySelector('[data-cv-picker]'),'Converter did not mount');
const picker=document.querySelector('[data-cv-picker]');
Object.defineProperty(picker,'files',{configurable:true,value:[new File(['bad!'],'broken.docx'),new File(['good'],'Отчёт_'+ 'я'.repeat(140)+'.docx'),new File(['good'],'table.xlsx')]});
picker.dispatchEvent(new window.Event('change'));
if(process.env.AIO_UI_EXPECT_UNAVAILABLE==='1') {
  await until(()=>document.querySelector('[data-cv-status]').textContent.includes('LibreOffice не найден'),'Missing-engine reason not displayed');
  assert.equal(document.querySelector('.aio-cv__format-main').disabled,true);
  assert.equal(document.querySelector('[data-cv-run]').disabled,true);
  const status=await (await apiFetch('/api/converter/status',{headers:{'X-AIO-Token':document.body.dataset.aioToken}})).json();
  assert.equal(status.storage.jobs,0);
  console.log(JSON.stringify({status:'PASS',missingEngineReasonVisible:true,scope:'DOM/HTTP'}));
  process.exit(0);
}
await until(()=>document.querySelector('.aio-cv__format-main')&&!document.querySelector('.aio-cv__format-main').disabled,'No PDF capability');
document.querySelector('.aio-cv__format-main').click();
const run=document.querySelector('[data-cv-run]'); assert.equal(run.disabled,false);run.click();
await until(()=>!document.querySelector('[data-cv-save]').hidden,'No completed partial batch');
await until(()=>[...document.querySelectorAll('.aio-cv__file[data-file-id]')].every(r=>r.dataset.aioFpState),'Progress did not decorate replaced file rows');
let rows=[...document.querySelectorAll('.aio-cv__file[data-file-id]')];
assert.deepEqual(rows.map(r=>r.dataset.aioFpState),['error','ready','ready']);
assert.equal(rows.filter(r=>!r.querySelector('.aio-fp-check').hidden).length,2);
let cancelSave=true,downloaded=null;
window.AllInOneBridge.saveExcel=async output=>{
  if(cancelSave){cancelSave=false;return {cancelled:true};}
  const response=await apiFetch(output.downloadUrl);
  assert.equal(response.ok,true);downloaded=Buffer.from(await response.arrayBuffer());return {saved:true};
};
const save=document.querySelector('[data-cv-save]');save.click();
await until(()=>document.querySelector('[data-cv-status]').textContent.includes('отменено'),'Cancelled save not shown');
await until(()=>[...document.querySelectorAll('.aio-cv__file[data-file-id]')].every(r=>r.dataset.aioFpState),'Progress did not restore after save cancel');
rows=[...document.querySelectorAll('.aio-cv__file[data-file-id]')];
assert.equal(rows.filter(r=>!r.querySelector('.aio-fp-check').hidden).length,2);
await until(()=>!save.disabled,'Save button did not recover');save.click();
await until(()=>document.querySelector('[data-cv-save]').hidden,'Confirmed save not shown');
await until(()=>[...document.querySelectorAll('.aio-cv__file[data-file-id]')].every(r=>r.dataset.aioFpState),'Progress did not restore after save');
assert.equal(downloaded.subarray(0,2).toString(),'PK');
rows=[...document.querySelectorAll('.aio-cv__file[data-file-id]')];
assert.equal(rows.filter(r=>!r.querySelector('.aio-fp-check').hidden).length,0);
const token=document.body.dataset.aioToken;
let status=await (await apiFetch('/api/converter/status',{headers:{'X-AIO-Token':token}})).json();
assert.equal(status.storage.jobs,0);
// Deselect the failed file and repeat the conversion without reopening A.I.O.
rows[0].querySelector('[data-op="select"]').click();
assert.equal(document.querySelector('[data-cv-run]').disabled,false);
document.querySelector('[data-cv-run]').click();
await until(()=>!document.querySelector('[data-cv-save]').hidden,'Repeat did not complete');
Object.defineProperty(picker,'files',{configurable:true,value:[new File(['good'],'new-file.docx')]});
picker.dispatchEvent(new window.Event('change'));
await until(()=>!document.querySelector('.aio-fp-row'),'Progress observer did not dispose');
await new Promise(resolve=>setTimeout(resolve,50));
status=await (await apiFetch('/api/converter/status',{headers:{'X-AIO-Token':token}})).json();
assert.equal(status.storage.jobs,0);
console.log(JSON.stringify({status:'PASS',partialBatch:true,cancelSaveRetry:true,checksClearedAfterSave:true,repeatWithoutRestart:true,inputChangeReleasesOldResult:true,storageAfterReset:status.storage,scope:'DOM/HTTP; native Qt dialog NOT_RUN'}));
