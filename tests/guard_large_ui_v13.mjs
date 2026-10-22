// Real queue/progress/issues modules, 83,000 tasks; DOM is not a Qt renderer.
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {createRequire} from 'node:module';
import {readFileSync} from 'node:fs';
const require=createRequire(import.meta.url);
const {parseHTML}=require(process.env.AIO_DOM_MODULE);
const {window,document}=parseHTML('<html><body data-aio-token="fixture"><section class="aio-cv"><div class="aio-cv__pane--left"></div><div class="aio-cv__pane--right"><div class="aio-cv__format-head"><h2 class="aio-cv__pane-title">Форматы</h2></div></div><footer class="aio-cv__footer"></footer></section></body></html>');
const clocks=new Map();let clockId=0,layoutReads=0,targetWalks=0;
window.AllInOneBridge={converter:{recoverable:async()=>({jobs:[]}),canRetry:()=>false}};
window.AllInOneNativeShell=true;
window.requestAnimationFrame=callback=>setTimeout(callback,0);
window.cancelAnimationFrame=clearTimeout;
window.getComputedStyle=()=>{layoutReads++;throw Error('Progress must not force layout');};
window.HTMLElement.prototype.showModal=function(){this.open=true;};
window.HTMLElement.prototype.close=function(){this.open=false;};
const context=vm.createContext({window,document,CustomEvent:window.CustomEvent,
  MutationObserver:window.MutationObserver,requestAnimationFrame:window.requestAnimationFrame,
  setInterval:callback=>{const id=++clockId;clocks.set(id,callback);return id;},
  clearInterval:id=>clocks.delete(id),setTimeout,clearTimeout,
  fetch:async()=>{throw Error('Unexpected network request');},URL,console});
for(const path of ['../src/guard-queue.js','../addons/file-converter/aio-file-progress.js','../addons/file-converter/aio-converter-issues.js'])
  vm.runInContext(readFileSync(new URL(path,import.meta.url),'utf8'),context,{filename:path});
await new Promise(r=>setTimeout(r,0));
const root=document.querySelector('.aio-cv'),left=root.querySelector('.aio-cv__pane--left');
const files=Array.from({length:1000},(_,i)=>{
  const row=document.createElement('div');row.className='aio-cv__file';row.dataset.fileId=String(i);
  row.innerHTML='<div class="aio-cv__file-info"><span class="aio-cv__file-name">file-'+i+'</span></div><div class="aio-cv__file-tools"><button>×</button></div>';
  left.append(row);
  const targets=Array.from({length:83},(_,j)=>({taskId:i+':'+j,format:'format'+j,state:'queued'}));
  Object.defineProperty(targets,Symbol.iterator,{value:function*(){targetWalks++;yield* Array.prototype.values.call(this);}});
  return {fileId:String(i),name:'file-'+i+'.docx',state:'converting',targets};
});
let state={jobId:'a'.repeat(32),revision:1,state:'running',finished:false,files,total:83000,
  completed:0,succeeded:0,failed:0,cancelled:0,filtered:0,
  currentTask:{fileId:'0',name:'file-0.docx',format:'pdf',stage:'export',startedMs:Date.now()}};
window.dispatchEvent(new window.CustomEvent('aio-guard-state',{detail:state}));
const stateButton=document.querySelector('.aio-guard-state-button');
assert.ok(stateButton,'A permanent top-row State control must exist');
assert.ok(stateButton.closest('.aio-cv__format-head'),'State belongs with Problems and Select All');
stateButton.click();
assert.equal(document.querySelector('.aio-guard-popup').open,true,'State opens task dialog');
await new Promise(r=>setTimeout(r,0));
assert.equal(document.querySelectorAll('.aio-guard-task').length,50);
assert.match(document.querySelector('[data-g-page]').textContent,/83000 задач/);
const first=document.querySelector('.aio-guard-task'),walks=targetWalks;
const tickStart=performance.now();
for(let i=0;i<100;i++)for(const tick of clocks.values())tick();
assert.equal(targetWalks,walks,'Clock must not walk targets');
assert.equal(document.querySelector('.aio-guard-task'),first,'Clock must preserve queue DOM/focus');
window.dispatchEvent(new window.CustomEvent('aio-guard-state',{detail:{...state,revision:2}}));
assert.equal(targetWalks,walks,'Header-only update must reuse the task view');
assert.equal(document.querySelector('.aio-guard-task'),first);
const clockMs=performance.now()-tickStart;
const updated={...files[999],targets:files[999].targets.map((t,i)=>i===82?{...t,state:'error',error:'Tail failure'}:t)};
state={...state,revision:3,files:files.map((f,i)=>i===999?updated:f)};
window.dispatchEvent(new window.CustomEvent('aio-guard-state',{detail:state}));
assert.equal(document.querySelector('.aio-guard-task'),first,'Off-page changes must preserve visible rows');
const search=document.querySelector('.aio-guard-tools input');search.value='file-999';search.dispatchEvent(new window.Event('input'));
const filter=document.querySelector('.aio-guard-tools select');filter.querySelector('[value="error"]').selected=true;filter.dispatchEvent(new window.Event('change'));
assert.equal(document.querySelectorAll('.aio-guard-task').length,1);
assert.match(document.querySelector('.aio-guard-task').textContent,/Tail failure/);

const progress=window.AIOFileProgress.attach(root);progress.begin({runId:'v13',fileIds:files.map(f=>f.fileId)});
const one={fileId:'0',state:'converting',succeeded:1,total:2,targets:[{format:'pdf',state:'ready'},{format:'html',state:'converting'}]};
progress.finishTargets({runId:'v13',partial:true,files:[one]});
assert.equal(left.querySelectorAll('.aio-fp-paint-content').length,0,'No cloned file cards');
assert.equal(left.querySelectorAll('.aio-fp-paint').length,1000);
assert.equal(left.querySelector('.aio-fp-paint').hidden,false);
progress.finishTargets({runId:'v13',partial:true,files:[{...one,state:'ready',succeeded:2,targets:one.targets.map(t=>({...t,state:'ready'}))}]});
assert.equal(left.querySelector('.aio-fp-paint').hidden,true);
assert.equal(left.querySelector('.aio-fp-check').hidden,false);
progress.finishTargets({runId:'v13',partial:true,files:[{...one,state:'ready',allCopiesSaved:true,succeeded:2,targets:one.targets.map(t=>({...t,state:'saved'}))}]});
assert.equal(left.querySelector('.aio-fp-check').hidden,false,'Saved files retain a terminal icon');
assert.equal(left.querySelector('.aio-fp-check').textContent,'✓');
assert.equal(left.querySelector('.aio-cv__file').dataset.aioFpState,'saved');
progress.finishTargets({runId:'v13',partial:true,files:[{fileId:'1',state:'skipped',
  succeeded:0,total:0,targets:[]},{fileId:'2',state:'error',succeeded:0,total:1,
  targets:[{format:'epub',state:'error',error:'Native export failed'}]}]});
assert.equal(left.querySelector('[data-file-id="1"] .aio-fp-check').textContent,'⊘');
assert.equal(left.querySelector('[data-file-id="2"] .aio-fp-check').textContent,'×');
const stableLabel=left.querySelector('.aio-fp-label');let paints=0;
const observer=new window.MutationObserver(records=>{paints+=records.length;});
observer.observe(stableLabel,{subtree:true,childList:true});
document.querySelector('[data-g-current]').textContent='Unrelated queue-clock mutation';
await new Promise(r=>setTimeout(r,20));
assert.equal(paints,0,'Unrelated panel mutations must not repaint files');observer.disconnect();
assert.equal(layoutReads,0);

const issues=window.AIOConverterIssues.attach({root,adapter:{}});
issues.sync({files:[{id:'0',name:'file-0.docx'}],allFiles:[{id:'0',name:'file-0.docx'}],
  formats:[],selectedFormats:['html'],status:'ready',result:{targetReport:{schemaVersion:3,total:1,succeeded:1,failed:0,cancelled:0,attempted:1,
    files:[{fileId:'0',targets:[{format:'html',state:'ready',warnings:[{code:'NATIVE_DRAW_HTML_CONTENT_RISK',message:'Native HTML content risk'}]}]}]}}});
assert.match(document.querySelector('.aio-cv-issues-list').textContent,/Native HTML content risk/);
issues.dispose();progress.dispose();
window.dispatchEvent(new window.CustomEvent('aio-guard-state',{detail:null}));
assert.equal(clocks.size,0,'No elapsed-time timer after queue reset');
assert.equal(left.querySelectorAll('.aio-fp-paint').length,0);
console.log(JSON.stringify({status:'PASS',files:1000,tasks:83000,queueDomRows:50,clockTicks:100,clockMs,
  noTaskWalksOnClock:true,noProgressClones:true,noForcedLayout:true,unrelatedMutationsIgnored:true,nativeWarningVisible:true}));
