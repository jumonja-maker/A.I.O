// Real bridge / queue code; controlled transport and DOM, no native claim.
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import {webcrypto} from 'node:crypto';

const bridgeCode=readFileSync(new URL('../src/converter-bridge.js',import.meta.url),'utf8');
const jobId='a'.repeat(32), ticketA='b'.repeat(43), ticketB='c'.repeat(43), ticketC='d'.repeat(43);
const output=(ticket,saved=false)=>({jobId,ticket,saved,name:ticket[0]+'.pdf',downloadUrl:'/api/converter-guard/result?ticket='+ticket});
const file=(id,saved=false)=>({fileId:id,name:id+'.docx',targets:[{format:'pdf',state:saved?'saved':'ready'}]});
const result=outputs=>({...output(ticketA),outputs});
const state=(revision,outputs,extra={})=>({jobId,revision,files:[file('one'),file('two')],
  total:2,completed:2,succeeded:2,failed:0,cancelled:0,filtered:0,finished:false,state:'packaging',
  result:outputs===null?null:result(outputs),...extra});

async function bridgeFixture(steps,MapClass=Map){
  let started=false,index=0;
  const calls=[],events=[];
  class CustomEvent{constructor(type,{detail}={}){Object.assign(this,{type,detail});}}
  const window={AllInOneNativeShell:true,dispatchEvent:event=>events.push(event),AllInOneBridge:{}};
  const fetch=async(url,options={})=>{
    const path=String(url).replace('/api/converter-guard','');calls.push(path);
    let body;
    if(path==='/prepare')body={jobId};
    else if(path.startsWith('/input?'))body={ok:true};
    else if(path==='/preflight')body={available:true,files:[],formats:[{id:'pdf',verifiedFileIds:['one','two']}]};
    else if(path==='/start'){started=true;body=state(1,[output(ticketA)]);}
    else if(path.startsWith('/jobs?'))body=started?steps[index++](path):state(0,null,{files:[],state:'prepared'});
    else throw Error('Unexpected path '+path);
    return {ok:true,status:200,headers:{get:()=>String(body.revision||0)},json:async()=>structuredClone(body)};
  };
  vm.runInNewContext(bridgeCode,{window,document:{body:{dataset:{aioToken:'test'}},querySelector:()=>null},
    fetch,CustomEvent,crypto:webcrypto,Map:MapClass,setTimeout:callback=>setTimeout(callback,0)});
  const completed=await window.AllInOneBridge.converter.convertMany({files:['one','two'].map(id=>({id,file:{name:id+'.docx',size:1}})),formatIds:['pdf']});
  return {completed,calls,events};
}
const checks=[];
{
  const f=await bridgeFixture([
    path=>{assert.match(path,/&delta=1&resultDelta=1$/);return state(2,[output(ticketB)],{delta:true,baseRevision:1,resultOutputsDelta:true,files:[file('two')]});},
    ()=>state(3,[output(ticketA,true)],{delta:true,baseRevision:2,resultOutputsDelta:true,files:[file('one',true)]}),
    ()=>state(4,[],{delta:true,baseRevision:3,resultOutputsDelta:true,files:[],finished:true,state:'ready'})
  ]);
  assert.deepEqual(Array.from(f.completed.result.outputs,o=>[o.ticket,o.saved]),[[ticketA,true],[ticketB,false]],
    'Delta must preserve older outputs and upsert saved metadata by ticket');
  const initial=f.events.find(e=>e.detail?.revision===1).detail;
  assert.equal(initial.result.outputs.length,1,'Merging must not mutate earlier published snapshots');
  assert.equal(initial.result.outputs[0].saved,false);
  checks.push('negotiated outputs delta retains inventory, saved metadata and old snapshots');
}
{
  const f=await bridgeFixture([
    ()=>state(3,[output(ticketB)],{delta:true,baseRevision:2,resultOutputsDelta:true,files:[]}),
    path=>{assert.equal(path.includes('delta='),false,'Resync must request a full inventory');
      assert.equal(path.includes('resultDelta='),false);return state(4,[output(ticketC)],{finished:true,state:'ready'});}
  ]);
  assert.deepEqual(Array.from(f.completed.result.outputs,o=>o.ticket),[ticketC]);
  checks.push('lost delta requests full state and replaces inventory');
}
{
  const f=await bridgeFixture([
    ()=>state(2,[output(ticketC)],{delta:true,baseRevision:1,files:[],finished:true,state:'ready'})
  ]);
  assert.deepEqual(Array.from(f.completed.result.outputs,o=>o.ticket),[ticketC],
    'Legacy full outputs inside a file delta replace rather than accumulate');
  checks.push('legacy full output representation remains compatible');
}
{
  const f=await bridgeFixture([
    ()=>state(2,null,{delta:true,baseRevision:1,resultOutputsDelta:true,files:[]}),
    ()=>state(3,[output(ticketB)],{delta:true,baseRevision:2,resultOutputsDelta:true,files:[],finished:true,state:'ready'}),
    path=>{assert.equal(path.includes('delta='),false);return state(4,[output(ticketB),output(ticketC)],{finished:true,state:'ready'});}
  ]);
  assert.deepEqual(Array.from(f.completed.result.outputs,o=>o.ticket),[ticketB,ticketC]);
  assert.equal(f.events.find(event=>event.detail?.revision===2).detail.result,null);
  checks.push('null result clears old inventory; first incremental inventory requires full resync');
}


// A long queue must not rebuild the complete output-ticket index on every
// incremental poll. The output snapshots themselves remain immutable.
{
  let indexed=0;
  class CountedMap extends Map {
    constructor(entries){
      const values=entries?Array.from(entries):[];
      if(values.length&&values.every(pair=>typeof pair[0]==='string'&&pair[0].length===43&&Number.isInteger(pair[1])))indexed+=values.length;
      super(values);
    }
  }
  const steps=Array.from({length:100},(_,i)=>()=>state(i+2,[output(String(i).padStart(43,'0'))],
    {delta:true,baseRevision:i+1,resultOutputsDelta:true,files:[],finished:i===99,state:i===99?'ready':'packaging'}));
  const f=await bridgeFixture(steps,CountedMap);
  assert.equal(f.completed.result.outputs.length,101);
  assert.equal(new Set(f.completed.result.outputs.map(row=>row.ticket)).size,101);
  assert.ok(indexed<=2,'100 output deltas must not rebuild growing ticket indexes (indexed '+indexed+' rows)');
  const first=f.events.find(e=>e.detail?.revision===1).detail;
  assert.equal(first.result.outputs.length,1,'performance improvement preserves the original immutable snapshot');
  checks.push({outputDeltas:100,finalOutputs:101,indexedInventoryRows:indexed,immutableSnapshots:true});
}

if(process.env.AIO_DOM_MODULE){
  const require=createRequire(import.meta.url),{parseHTML}=require(process.env.AIO_DOM_MODULE);
  const {window,document}=parseHTML('<html><body data-aio-token="test"><section class="aio-cv"><div class="aio-cv__format-head"></div><footer class="aio-cv__footer"></footer></section></body></html>');
  window.HTMLElement.prototype.showModal=function(){this.open=true;};
  window.HTMLElement.prototype.close=function(){this.open=false;};
  // linkedom exposes only a getter; browser selects also accept assignment.
  const selectPrototype=Object.getPrototypeOf(document.createElement('select'));
  const selectValue=Object.getOwnPropertyDescriptor(selectPrototype,'value');
  if(!selectValue?.set)Object.defineProperty(selectPrototype,'value',{
    get:selectValue.get,set(value){for(const option of this.querySelectorAll('option'))option.selected=option.value===value;}});
  let walks=0,previewLoads=0,previewSaved=false;
  const oldId='e'.repeat(32);
  const retainedJob={jobId:oldId,outputs:1,unsavedOutputs:1,state:'ready',running:false};
  window.AllInOneBridge={converter:{recoverable:async()=>({jobs:[retainedJob]}),canRetry:()=>false}};
  const fetch=async()=>{previewLoads++;return {ok:true,json:async()=>({jobId:oldId,files:[file('old',previewSaved)]})};};
  const intervals=new Map();let nextInterval=0;
  const context=vm.createContext({window,document,fetch,URL,console,CustomEvent:window.CustomEvent,
    MutationObserver:window.MutationObserver,setTimeout,clearTimeout,
    setInterval:callback=>{const id=++nextInterval;intervals.set(id,callback);return id;},clearInterval:id=>intervals.delete(id)});
  vm.runInContext(readFileSync(new URL('../src/guard-queue.js',import.meta.url),'utf8'),context);
  await new Promise(resolve=>setTimeout(resolve,0));
  const files=Array.from({length:1000},(_,i)=>{
    const targets=Array.from({length:83},(_,j)=>({format:'format'+j,state:'queued'}));
    Object.defineProperty(targets,Symbol.iterator,{value:function*(){walks++;yield*Array.prototype.values.call(this);}});
    return {fileId:String(i),name:'file-'+i,targets};
  });
  const report={jobId:oldId,revision:1,files,state:'running',finished:false,total:83000,completed:0,succeeded:0,failed:0,cancelled:0,filtered:0};
  window.dispatchEvent(new window.CustomEvent('aio-guard-state',{detail:report}));
  document.querySelector('.aio-guard-state-button').click();
  await new Promise(resolve=>setTimeout(resolve,0));
  const before=walks,first=document.querySelector('.aio-guard-task');
  const targets=files[999].targets.map((task,i)=>i===82?{...task,state:'error'}:task);
  Object.defineProperty(targets,Symbol.iterator,{value:function*(){walks++;yield*Array.prototype.values.call(this);}});
  const changed={...files[999],targets};
  const started=performance.now();
  window.dispatchEvent(new window.CustomEvent('aio-guard-state',{detail:{...report,revision:2,files:files.map((file,i)=>i===999?changed:file)}}));
  const deltaMs=performance.now()-started;
  assert.equal(walks-before,1,'One-file delta must enumerate only that file, not every task in the batch');
  assert.equal(document.querySelector('.aio-guard-task'),first,'Off-page delta preserves visible row/focus');
  assert.match(document.querySelector('[data-g-page]').textContent,/83000 задач/);
  // Switch the observed current job while the retained selector keeps oldId.
  window.dispatchEvent(new window.CustomEvent('aio-guard-state',{detail:{...report,jobId,revision:3,files:[]}}));
  const select=document.querySelector('[data-g-jobs]');select.value=oldId;select.dispatchEvent(new window.Event('change'));
  await new Promise(resolve=>setTimeout(resolve,0));
  assert.match(document.querySelector('.aio-guard-task').textContent,/Готово/);
  const loadedBeforeSave=previewLoads;previewSaved=true;
  window.dispatchEvent(new window.CustomEvent('aio-guard-save-confirmed',{detail:{jobId:oldId,released:false,savedAll:false}}));
  await new Promise(resolve=>setTimeout(resolve,0));
  assert.ok(previewLoads>loadedBeforeSave,'Partial save must refresh retained preview even when output count is unchanged');
  assert.match(document.querySelector('.aio-guard-task').textContent,/Сохранено/);
  checks.push({queue:'83,000 tasks, single-file delta',changedFileWalks:walks-before,deltaMs,partialSavePreviewReload:true});
}else checks.push('DOM subcases NOT_RUN: AIO_DOM_MODULE not supplied');
console.log(JSON.stringify({status:'PASS',checks}));
