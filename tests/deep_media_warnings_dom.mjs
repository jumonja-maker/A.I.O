// Loss warnings remain visible together with the existing quality summary.
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
const {parseHTML}=createRequire(import.meta.url)(process.env.AIO_DOM_MODULE);
const {window,document}=parseHTML('<html><body data-aio-token="fixture"><section class="aio-cv"><div class="aio-cv__format-head"></div><footer class="aio-cv__footer"></footer></section></body></html>');
window.HTMLElement.prototype.showModal=function(){this.open=true;};
window.HTMLElement.prototype.close=function(){this.open=false;this.dispatchEvent(new window.Event('close'));};
window.AllInOneNativeShell=true;
window.AllInOneBridge={converter:{canRetry:()=>false,recoverable:async()=>({jobs:[]})}};
const timers=new Map();let timerId=0;
const flush=async()=>{for(let i=0;i<6;i++)await new Promise(resolve=>setTimeout(resolve,0));};
const source=process.env.AIO_QUEUE_SOURCE||new URL('../src/guard-queue.js',import.meta.url);
vm.runInNewContext(readFileSync(source,'utf8'),{
  window,document,fetch:async()=>{throw Error('This test should not request a retained preview');},
  URL,AbortController,CustomEvent:window.CustomEvent,MutationObserver:window.MutationObserver,
  setTimeout,clearTimeout,setInterval:callback=>{const id=++timerId;timers.set(id,callback);return id;},
  clearInterval:id=>timers.delete(id),console
});
await flush();
const warning='Прозрачность не сохранена <literal-warning>';
const variants=[
  {quality:null},
  {quality:{status:'risks_found',qualityCompared:true}},
  {quality:{status:'checked_subset',qualityCompared:true}},
  {quality:{status:'not_checked',qualityCompared:false}},
  {quality:{status:'checked_subset',qualityCompared:true},error:'Ошибка экспорта'},
];
for(let index=0;index<variants.length;index++){
  const value=variants[index];
  const task={format:'jpeg',state:value.error?'error':'ready',warnings:[{code:'MEDIA_ALPHA_REMOVED',message:warning}],...value};
  window.dispatchEvent(new window.CustomEvent('aio-guard-state',{detail:{
    jobId:'a'.repeat(32),revision:index+1,state:'ready',finished:true,total:1,completed:1,
    succeeded:value.error?0:1,failed:value.error?1:0,files:[{fileId:'one',name:'image.png',targets:[task]}]
  }}));
  if(!document.querySelector('.aio-guard-popup').open)document.querySelector('.aio-guard-state-button').click();
  await flush();
  const detail=document.querySelector('.aio-guard-task small');assert.ok(detail);
  if(value.error)assert.equal(detail.textContent,value.error);
  else{
    assert.ok(detail.textContent.includes(warning),'quality summary must not hide a known loss: '+index);
    if(value.quality)assert.ok(detail.textContent.includes(value.quality.status==='risks_found'?
      'Возможны потери структуры':value.quality.qualityCompared?'Часть структуры проверена':'Структуру сопоставить не удалось'));
    assert.equal(detail.children.length,0,'warning remains text, never markup');
  }
}
window.dispatchEvent(new window.Event('pagehide'));
console.log(JSON.stringify({status:'PASS',qualityAndWarningsVisible:true,errorPriority:true,warningTextEscaped:true,variants:variants.length}));
