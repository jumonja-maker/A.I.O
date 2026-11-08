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
  {quality:{status:'checked_subset',qualityCompared:true,unknown:['rendered_pixels']},
    checkedFeatures:['pixel_dimensions'],unknownReasons:['quality_comparison_incomplete'],saveStatus:'unsaved',
    resultChecks:[{check:'media_properties',result:{width:48,height:32,dpi:[99.9998,99.9998],frames:1,pages:1}}]},
  {quality:null,checkedFeatures:['<literal-property>'],unknownReasons:['quality_comparison_not_requested'],
    saveStatus:'partial',readbackVerified:false,formatLimitations:['formulas_as_values']},
  {quality:null,saveStatus:'saved',readbackVerified:true},
  {quality:null,warningCode:'MEDIA_PHOTOCD_BASE_ONLY',
    warningMessage:'Для результата использован слой PhotoCD Base (768 × 512 пикселей до поворота). Наличие и сохранность слоёв 4Base, 16Base и более высокого разрешения не проверены; сравнение с ними не выполнялось.'},
];
for(let index=0;index<variants.length;index++){
  const value=variants[index];
  const visibleWarning=value.warningMessage||warning;
  const task={format:'jpeg',state:value.error?'error':'ready',warnings:[{code:value.warningCode||'MEDIA_ALPHA_REMOVED',message:visibleWarning}],...value};
  window.dispatchEvent(new window.CustomEvent('aio-guard-state',{detail:{
    jobId:'a'.repeat(32),revision:index+1,state:'ready',finished:true,total:1,completed:1,
    succeeded:value.error?0:1,failed:value.error?1:0,files:[{fileId:'one',name:'image.png',targets:[task]}]
  }}));
  if(!document.querySelector('.aio-guard-popup').open)document.querySelector('.aio-guard-state-button').click();
  await flush();
  const detail=document.querySelector('.aio-guard-task small');assert.ok(detail);
  if(value.error)assert.equal(detail.textContent,value.error);
  else{
    assert.ok(detail.textContent.includes(visibleWarning),'quality summary must not hide a warning: '+index);
    if(value.warningCode==='MEDIA_PHOTOCD_BASE_ONLY'){
      assert.ok(detail.textContent.includes('4Base, 16Base'));
      assert.ok(detail.textContent.includes('не проверены'));
      assert.equal(task.resultChecks,undefined,'PhotoCD scope stays visible without technical metadata');
    }
    if(value.quality)assert.ok(detail.textContent.includes(value.quality.status==='risks_found'?
      'Возможны потери структуры':value.quality.qualityCompared?'Часть структуры проверена':'Структуру сопоставить не удалось'));
    assert.equal(detail.children.length,0,'warning remains text, never markup');
    assert.ok(detail.textContent.includes('Внешний вид, шрифты и расположение объектов не сравнивались.'));
    if(value.checkedFeatures)assert.ok(detail.textContent.includes('Проверки результата:'));
    if(value.unknownReasons)assert.ok(detail.textContent.includes('Границы проверки:'));
    if(value.resultChecks)assert.ok(detail.textContent.includes('48 × 32 пикс. · 100 × 100 DPI'));
    if(value.saveStatus==='unsaved')assert.ok(detail.textContent.includes('Копия на диске: не сохранена'));
    if(value.saveStatus==='partial'){
      assert.ok(detail.textContent.includes('Копия на диске: сохранена частично'));
      assert.ok(detail.textContent.includes('формулы записываются значениями'));
      assert.ok(detail.textContent.includes('<literal-property>'));
    }
    if(value.readbackVerified)assert.ok(detail.textContent.includes('байты прочитаны и сверены.'));
  }
}
window.dispatchEvent(new window.Event('pagehide'));
console.log(JSON.stringify({status:'PASS',qualityAndWarningsVisible:true,errorPriority:true,warningTextEscaped:true,variants:variants.length}));
