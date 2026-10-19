// Preserved UI + real HTTP. Save acknowledgment is simulated in this DOM test.
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {createRequire} from 'node:module';
import {webcrypto} from 'node:crypto';
const require = createRequire(import.meta.url);
const {parseHTML} = require(process.env.AIO_DOM_MODULE);
const nativeFetch = globalThis.fetch.bind(globalThis);
const base = process.env.AIO_TEST_SERVER;
const {window, document} = parseHTML(await (await fetch(base + '/')).text());
window.location = new URL(base + '/');
window.matchMedia = () => ({matches:false,addEventListener(){},removeEventListener(){}});
window.getComputedStyle = () => new Proxy({getPropertyValue:()=>'',gap:'8px',padding:'7px'}, {get:(t,k)=>t[k]||''});
window.requestAnimationFrame = callback=>setTimeout(callback,1);
window.cancelAnimationFrame = clearTimeout;
window.localStorage = {getItem:()=>null,setItem(){}};
window.HTMLElement.prototype.getBoundingClientRect = () => ({left:0,right:600,top:0,bottom:400,width:600,height:400});
window.HTMLElement.prototype.focus = function(){};
let metadataRequests = 0;
const apiFetch = (url,options) => {
  if (String(url).includes('/api/converter-guard/inspect')) {
    assert.equal(typeof options.body, 'string', 'inspection uses JSON metadata');
    assert.ok(Array.isArray(JSON.parse(options.body).files));
    metadataRequests++;
  }
  return nativeFetch(new URL(url,base),options);
};
window.fetch=apiFetch;window.URL=URL;
const context=vm.createContext({window,document,fetch:apiFetch,console,URL,Blob,File,FormData,
  AbortController,crypto:webcrypto,setTimeout,clearTimeout,setInterval,clearInterval,
  requestAnimationFrame:window.requestAnimationFrame,cancelAnimationFrame:clearTimeout,
  MutationObserver:window.MutationObserver,CustomEvent:window.CustomEvent});
for(const script of document.querySelectorAll('script[src]')) {
  const path=script.getAttribute('src'), response=await apiFetch('/'+path);
  assert.equal(response.ok,true,path);
  vm.runInContext(await response.text(),context,{filename:path});
}
async function until(check,message) {
  const deadline=Date.now()+10000;
  while(Date.now()<deadline){if(check())return;await new Promise(resolve=>setTimeout(resolve,10));}
  throw new Error(message+': '+document.querySelector('[data-cv-status]')?.textContent);
}
const rows=()=>[...document.querySelectorAll('.aio-cv__file[data-file-id]')];
const stable=()=>document.querySelector('[data-cv-add]')?.disabled===false;
const nav=[...document.querySelectorAll('.nav-item')].find(n=>n.textContent.includes('Конвертер'));
nav.click();
await until(()=>document.querySelector('[data-cv-picker]')&&stable()&&document.querySelectorAll('[data-cv-categories] [role="tab"]').length>=3,'initial UI catalog');
const picker=document.querySelector('[data-cv-picker]');
const search=document.querySelector('[data-cv-search]');
const assertSearch=()=>{assert.equal(search.hidden,false);assert.equal(search.parentElement.hidden,false);};
assertSearch();
assert.equal(picker.accept,'','file picker accepts every source extension');
const categories=[...document.querySelectorAll('[data-cv-categories] [role="tab"]')].map(n=>n.textContent);
assert.ok(categories.includes('Изображения'));
assert.ok(categories.includes('Таблицы'));
assert.ok(categories.includes('Документы'));
function add(files) {
  Object.defineProperty(picker,'files',{configurable:true,value:files});
  picker.dispatchEvent(new window.Event('change'));
}
add([new File(['BM'],'one.bmp')]);
await until(()=>rows().length===1&&stable(),'one file');
assertSearch();
add([new File(['table'],'two.csv'),new File(['unknown'],'three.unknown')]);
await until(()=>rows().length===3&&stable(),'three files');
assertSearch();
rows()[2].querySelector('[data-op="remove"]').click();
await until(()=>rows().length===2&&stable(),'remove third');
rows()[1].querySelector('[data-op="remove"]').click();
await until(()=>rows().length===1&&stable(),'remove second');
assertSearch();
assert.equal(document.querySelector('[data-cv-count]').textContent,'(1)');
const fileArea=document.querySelector('[data-cv-file-area]');
fileArea.scrollTop=123;
rows()[0].click();
assert.equal(rows()[0].dataset.selected,'false','row click deselects exactly once');
assert.equal(fileArea.scrollTop,123,'scroll owner survives rerender');
rows()[0].querySelector('[data-op="select"]').click();
assert.equal(rows()[0].dataset.selected,'true','circle does not double toggle');
rows()[0].querySelector('[data-op="remove"]').click();
await until(()=>rows().length===0&&stable(),'empty list');
assertSearch();
add([new File(['BROKEN'],'broken.docx'),new File(['BM'],'good.bmp'),new File(['a,b'],'table.csv')]);
await until(()=>rows().length===3&&stable(),'mixed inputs');
const format=label=>document.querySelector('[aria-label="Выбрать '+label+'"]');
assert.equal(format('DOCX').disabled,false,'mixed list does not disable Writer target');
document.querySelector('[data-cv-categories] [role="tab"]').click();
format('PDF').click();
const run=document.querySelector('[data-cv-run]');
assert.equal(run.disabled,false);
run.click();
await until(()=>!document.querySelector('[data-cv-save]').hidden,'partial result');
await until(()=>rows().every(r=>r.dataset.aioFpState),'paint initialized');
assert.deepEqual(rows().map(r=>r.dataset.aioFpState),['error','ready','ready']);
const issues=document.querySelector('.aio-cv-issues-button');
issues.click();
assert.ok(document.querySelector('.aio-cv-issues-list').textContent.includes('broken.docx'));
let cancelSave=true, downloaded=false;
window.AllInOneBridge.saveExcel=async result=>{
  if(cancelSave){cancelSave=false;return {cancelled:true};}
  const response=await apiFetch(result.downloadUrl);
  assert.equal(response.ok,true);
  const data=Buffer.from(await response.arrayBuffer());
  assert.equal(data.subarray(0,2).toString(),'PK');
  downloaded=true;
  return {saved:true};
};
const save=document.querySelector('[data-cv-save]');
save.click();
await until(()=>document.querySelector('[data-cv-status]').textContent.includes('отменено'),'save cancellation');
await until(()=>!save.disabled,'save retry enabled');
assert.equal(rows().filter(r=>!r.querySelector('.aio-fp-check').hidden).length,2);
save.click();
await until(()=>save.hidden,'save confirmation');
assert.equal(downloaded,true);
await until(()=>rows().every(r=>r.dataset.aioFpState),'restored saved paint');
assert.equal(rows().filter(r=>!r.querySelector('.aio-fp-check').hidden).length,0);
rows()[0].querySelector('[data-op="select"]').click();
assert.equal(run.disabled,false);
run.click();
await until(()=>!save.hidden,'repeat run without restart');
// Editing a format releases the previous result and its paint observer.
format('TXT').click();
await until(()=>!document.querySelector('.aio-fp-row'),'paint released after target edit');
add(Array.from({length:103},(_,i)=>new File(['BM'],'many_'+i+'.bmp')));
await until(()=>rows().length===106&&stable(),'UI accepts more than 100 files');
assertSearch();
assert.equal(document.querySelector('[data-cv-count]').textContent,'(106)');
run.click();
await until(()=>!save.hidden,'more than 100 files can convert');
await until(()=>rows().filter(r=>r.dataset.selected==='true').every(r=>r.dataset.aioFpState),
  'progress for every selected file above old 100-file cap');
assert.equal(rows().filter(r=>r.dataset.aioFpState==='ready').length,105);
// Explicitly dispose the last retained job before the host fixture closes.
format('PDF').click();
await until(()=>!document.querySelector('.aio-fp-row'),'bulk run release');
console.log(JSON.stringify({status:'PASS',scope:'DOM/HTTP; save acknowledgment simulated',
  permanentSearch:[0,1,3],imageCategory:true,anyInputExtension:true,metadataRequests,
  mixedInputs:true,rowAndCircleSelection:true,scrollRetention:true,partialResults:true,
  saveCancellationRetry:true,checksClearedAfterSave:true,repeat:true,moreThan100Files:true,
  bulkConversionPaint:true}));
