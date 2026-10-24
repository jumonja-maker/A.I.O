import assert from 'node:assert/strict';
import vm from 'node:vm';
import {createRequire} from 'node:module';
import {webcrypto} from 'node:crypto';
const {parseHTML}=createRequire(import.meta.url)(process.env.AIO_DOM_MODULE);
const base=process.env.AIO_TEST_SERVER,nativeFetch=globalThis.fetch.bind(globalThis);
const {window,document}=parseHTML(await (await nativeFetch(base+'/')).text());
window.location=new URL(base+'/');window.URL=URL;
window.matchMedia=()=>({matches:false,addEventListener(){},removeEventListener(){}});
window.getComputedStyle=()=>new Proxy({getPropertyValue:()=>'',gap:'8px',padding:'7px'},{get:(t,k)=>t[k]||''});
window.requestAnimationFrame=fn=>setTimeout(fn,1);window.cancelAnimationFrame=clearTimeout;
window.localStorage={getItem:()=>null,setItem(){}};
window.HTMLElement.prototype.getBoundingClientRect=()=>({left:0,right:600,top:0,bottom:400,width:600,height:400});
window.HTMLElement.prototype.focus=function(){};
window.HTMLElement.prototype.showModal=function(){this.open=true;};
window.HTMLElement.prototype.close=function(){this.open=false;this.dispatchEvent(new window.Event('close'));};
window.AllInOneNativeShell=true;
const requests=[],proofs=[];
const apiFetch=async(url,options={})=>{
  const u=new URL(url,base);requests.push({path:u.pathname,method:options.method});
  const response=await nativeFetch(u,options);
  if(u.pathname.endsWith('/preflight')&&response.ok)proofs.push(await response.clone().json());
  return response;
};
const context=vm.createContext({window,document,navigator:window.navigator,console,
  URL,Blob,File,fetch:apiFetch,crypto:webcrypto,setTimeout,clearTimeout,setInterval,clearInterval,
  requestAnimationFrame:window.requestAnimationFrame,cancelAnimationFrame:clearTimeout,
  MutationObserver:window.MutationObserver,CustomEvent:window.CustomEvent,AbortController});
for(const script of document.querySelectorAll('script[src]')){
  const path=script.getAttribute('src'),response=await apiFetch('/'+path);
  assert.equal(response.ok,true,path);
  vm.runInContext(await response.text(),context,{filename:path});
}
async function until(check,label){
  const stop=Date.now()+15000;
  while(Date.now()<stop){if(await check())return;await new Promise(r=>setTimeout(r,10));}
  throw Error(label+'; '+document.querySelector('[data-cv-status]')?.textContent);
}
[...document.querySelectorAll('.nav-item')].find(n=>n.textContent.includes('Конвертер')).click();
await until(()=>document.querySelector('[data-cv-add]')?.disabled===false,'mount');
const html='<html><body><img src="images/logo.png"></body></html>';
const original=new File([html],'page.html'),picker=document.querySelector('[data-cv-picker]');
Object.defineProperty(picker,'files',{configurable:true,value:[original]});
picker.dispatchEvent(new window.Event('change'));
await until(()=>proofs.length===1&&document.querySelector('[data-cv-add]')?.disabled===false,'first check');
assert.equal(proofs[0].files[0].preflight.code,'HTML_LOCAL_RESOURCES_NOT_UPLOADED');
assert.equal(document.querySelectorAll('.aio-cv__format-main[aria-pressed="true"]').length,0);
document.querySelector('.aio-guard-state-button').click();
const dialog=document.querySelector('.aio-guard-popup');
assert.equal(dialog.open,true);
await until(()=>!document.querySelector('[data-g-resources]').hidden,'resources control');
const copy=new File([html],'page.html'),asset=new File(['image'],'logo.png');
Object.defineProperty(copy,'webkitRelativePath',{value:'site/page.html'});
Object.defineProperty(asset,'webkitRelativePath',{value:'site/images/logo.png'});
const resources=document.querySelector('[data-g-resource-picker]');
Object.defineProperty(resources,'files',{configurable:true,value:[copy,asset]});
resources.dispatchEvent(new window.Event('change'));
await until(()=>proofs.length===2&&document.querySelector('[data-g-add-resources]').disabled===false,'resource preflight');
await until(()=>proofs[1].files[0].preflight.state==='detected','confirmed import');
assert.equal(dialog.open,true,'resource update keeps State dialog open');
assert.equal(document.querySelectorAll('.aio-cv__file[data-file-id]').length,1);
assert.equal(document.querySelectorAll('.aio-cv__format-main[aria-pressed="true"]').length,0,'no automatic format selection');
assert.equal(requests.filter(r=>r.path.endsWith('/resource')).length,1,'source copy not a resource');
assert.equal(requests.filter(r=>r.path.endsWith('/input')).length,1,'original uploaded once');
assert.equal(requests.filter(r=>r.path.endsWith('/start')).length,0);
document.querySelector('[data-g-close]').click();assert.equal(dialog.open,false);
document.querySelector('.aio-guard-state-button').click();assert.equal(dialog.open,true);
const inside=document.querySelector('[data-g-counts]');
function pointer(target,type,x,y,button=0){
  const event=new window.Event(type,{bubbles:true});
  Object.defineProperties(event,{clientX:{value:x},clientY:{value:y},button:{value:button}});
  target.dispatchEvent(event);
}
pointer(inside,'pointerdown',20,20);
pointer(dialog,'pointerup',-10,-10);
assert.equal(dialog.open,true,'dragging from inside cannot dismiss dialog');
pointer(dialog,'pointerdown',20,20);
pointer(dialog,'pointerup',20,20);
assert.equal(dialog.open,true,'internal dialog padding is not the backdrop');
pointer(dialog,'pointerdown',-10,-10,2);
pointer(dialog,'pointerup',-10,-10,2);
assert.equal(dialog.open,true,'secondary pointer button does not dismiss dialog');
pointer(dialog,'pointerdown',-10,-10);
pointer(dialog,'pointerup',20,20);
assert.equal(dialog.open,true,'a pointer released in dialog padding does not dismiss it');
pointer(dialog,'pointerdown',-10,-10);
pointer(dialog,'pointerup',-10,-10);
assert.equal(dialog.open,false);
assert.equal(document.querySelector('.aio-guard-state-button').getAttribute('aria-expanded'),'false');
document.querySelector('.aio-guard-state-button').click();
dialog.dispatchEvent(new window.Event('cancel',{cancelable:true}));
assert.equal(dialog.open,false,'Escape closes State');
assert.equal(requests.filter(r=>r.path.endsWith('/cancel')).length,0,'closing State never cancels work');
assert.equal(document.querySelector('[data-g-verify]').textContent.includes('автоматически'),true);
console.log(JSON.stringify({status:'PASS',scope:'DOM+HTTP, controlled native import',
  originalUploadedOnce:true,resourceUploads:1,sourceCards:1,automaticExports:0,
  automaticFormatSelection:false,resourceUpdateKeepsDialogOpen:true,dialogCloseAndDrag:true,
  internalPaddingKeepsDialogOpen:true,escapeClosesWithoutCancelling:true}));

