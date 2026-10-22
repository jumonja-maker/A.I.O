// Real converter bridge; controlled transport and host acknowledgements.
// These are lifecycle regressions, not Windows or Qt acceptance.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {webcrypto} from 'node:crypto';

const code=readFileSync(new URL('../src/converter-bridge.js',import.meta.url),'utf8');
const jobId='a'.repeat(32),firstTicket='b'.repeat(43),bundleTicket='c'.repeat(43);
const input=[{id:'one',file:{name:'input.docx',size:1}}];
const tick=()=>new Promise(resolve=>setTimeout(resolve,1));
async function until(test){for(let i=0;i<500;i++){if(test())return;await tick();}throw Error('Fixture did not settle');}
function deferred(){let resolve;const promise=new Promise(r=>resolve=r);return {resolve,promise};}
const artifact=ticket=>({jobId,ticket,name:'result.pdf',downloadUrl:'/api/converter-guard/result?ticket='+ticket});

function fixture(options={}){
  const calls=[],events=[];
  let started=false,done=false,paused=false,revision=2,polls=0,bundled=false;
  const state=()=>({jobId,revision,finished:done,state:done?'ready':'running',
    total:2,completed:done?2:1,succeeded:done?2:1,failed:0,cancelled:0,filtered:0,pending:done?0:1,
    currentTask:done?null:{taskId:jobId+':1',fileId:'one',name:'input.docx',format:'html',stage:'export',startedMs:Date.now()},
    pauseRequested:paused,
    files:[{fileId:'one',name:'input.docx',state:done?'ready':'converting',targets:[
      {format:'pdf',state:'ready',outputs:[artifact(firstTicket)]},
      {format:'html',state:done?'ready':'converting',outputs:[]}]}],
    bundleDeferred:done&&options.deferBundle&&!bundled,
    packagingError:options.bundleFails&&bundled?'BUNDLE_STORAGE_FAILED':null,
    result:done?{...artifact(options.deferBundle&&!bundled?firstTicket:bundleTicket),
      name:options.deferBundle&&!bundled?'result.pdf':'results.zip',
      folderOnly:options.deferBundle&&!bundled,outputs:[artifact(firstTicket)]}:null,
    partialResult:{...artifact(firstTicket),outputsCount:1,folderOnly:true}});
  const root={dispatchEvent:event=>{events.push(event);return true;}};
  const progress={};
  for(const name of ['begin','dispose','finishTargets','failBatch','beginSave','finishSave'])
    progress[name]=value=>events.push({type:name,detail:value});
  const window={AllInOneNativeShell:options.browser!==true,
    AllInOneBridge:{saveExcel:async result=>options.hostSave?.(result)||{saved:true,savedAll:false,snapshotComplete:true,outputs:1,jobRunning:true}},
    AIOFileProgress:{attach:()=>progress},dispatchEvent:event=>{events.push(event);return true;}};
  class CustomEvent{constructor(type,{detail}={}){this.type=type;this.detail=detail;}}
  const fetch=async(url,init={})=>{
    const path=String(url).replace('/api/converter-guard','');
    const body=typeof init.body==='string'?JSON.parse(init.body):null;
    calls.push({path,body,headers:init.headers});
    const response=(value,status=200)=>({ok:status>=200&&status<300,status,
      headers:{get:name=>name==='ETag'?'"'+jobId+':'+(value?.revision??0)+'"':null},
      json:async()=>JSON.parse(JSON.stringify(value))});
    if(path==='/prepare')return response({jobId});
    if(path.startsWith('/input?'))return response({ok:true});
    if(path==='/preflight')return response({available:true,files:[],formats:['pdf','html'].map(id=>({id,verifiedFileIds:['one']}))});
    if(path==='/start'){started=true;return response(state());}
    if(path.startsWith('/jobs?')){
      if(!started)return response({jobId,revision:1,finished:false,state:'prepared',files:[],result:null});
      polls++;
      const override=await options.poll?.({polls,path,state:state(),finish:()=>{done=true;revision++;}});
      return response(override||state());
    }
    if(path==='/saved')return response({jobId,saved:true,savedAll:true,outputs:2});
    if(path==='/bundle'){bundled=true;revision++;return response(state(),202);}
    if(path==='/release')return response({ok:true});
    if(path==='/pause'){paused=body.paused;revision++;return response(state());}
    if(path==='/cancel-file'){revision++;return response({...state(),files:state().files.map(f=>({...f,cancellationRequested:true}))});}
    if(path==='/cancel'){
      if(options.cancelFails)return response({error:'Temporary failure',code:'SERVER_BUSY'},503);
      done=true;revision++;return response({...state(),state:'cancelled'});
    }
    throw Error('Unexpected route '+path);
  };
  const context=vm.createContext({window,document:{body:{dataset:{aioToken:'fixture'}},querySelector:()=>root},
    fetch,CustomEvent,crypto:webcrypto,setTimeout:callback=>setTimeout(callback,1)});
  vm.runInContext(code,context);
  return {bridge:window.AllInOneBridge.converter,calls,events,
    finish(){done=true;revision++;},started:()=>started,polled:()=>polls};
}

const completed=[];
{
  const f=fixture();
  const operation=f.bridge.convertMany({files:input,formatIds:['pdf','html']});
  await until(()=>f.polled()>0);
  f.finish();await operation;
  f.bridge.resetProgress();
  assert.equal(f.calls.some(c=>c.path==='/release'),false,
    'Clearing UI must not discard successful unsaved exports');
  completed.push('UI reset retains completed unsaved outputs');
}
{
  const f=fixture();
  const operation=f.bridge.convertMany({files:input,formatIds:['pdf','html']});
  await until(()=>f.polled()>0);
  f.bridge.resetProgress();
  await assert.rejects(operation,error=>error.code==='CANCELLED');
  assert.equal(f.calls.some(c=>c.path==='/release'),false,
    'Detaching UI while active must leave partial outputs recoverable');
  completed.push('UI generation change preserves partial live outputs');
}
{
  const f=fixture();
  const operation=f.bridge.convertMany({files:input,formatIds:['pdf','html']});
  await until(()=>f.polled()>0);
  const saved=await f.bridge.saveRetained(jobId,'folder');
  assert.equal(saved.saved,true);assert.equal(saved.savedAll,false);
  assert.equal(f.calls.some(c=>c.path==='/release'||c.path==='/cancel'),false);
  assert.equal(f.events.some(e=>e.type==='aio-guard-saved'),false);
  assert.equal(f.events.find(e=>e.type==='aio-guard-save-confirmed').detail.released,false);
  f.finish();await operation;
  completed.push('live folder snapshot never releases or cancels active conversion');
}
{
  const f=fixture({hostSave:async()=>({saved:true,savedAll:true,snapshotComplete:true,outputs:2})});
  const operation=f.bridge.convertMany({files:input,formatIds:['pdf','html']});
  await until(()=>f.polled()>0);
  await f.bridge.saveRetained(jobId,'folder');
  assert.equal(f.calls.some(c=>c.path==='/release'),false,'host ack must not erase a job still observed by convert()');
  assert.equal(f.events.some(e=>e.type==='aio-guard-saved'),false);
  f.finish();await operation;
  completed.push('save completion cannot race terminal /jobs consumption');
}
{
  const f=fixture({poll:({polls,finish})=>{if(polls===2)throw new TypeError('temporary socket failure');if(polls>=3)finish();}});
  const result=await f.bridge.convertMany({files:input,formatIds:['pdf','html']});
  assert.equal(result.ok,true);
  assert.equal(f.calls.some(c=>c.path==='/release'||c.path==='/cancel'),false);
  assert.ok(f.events.some(e=>e.type==='aio-guard-state'&&e.detail?.statusReadError));
  completed.push('temporary status transport failure reconnects without cancelling native work');
}
{
  const f=fixture({poll:({polls,path,finish})=>{
    if(polls===1)return {jobId,revision:4,baseRevision:3,delta:true,files:[]};
    assert.equal(path.includes('delta=1'),false);finish();
  }});
  assert.equal((await f.bridge.convertMany({files:input,formatIds:['pdf','html']})).ok,true);
  assert.equal(f.calls.some(c=>c.path==='/release'),false);
  completed.push('missing delta base is recovered through full snapshot');
}
{
  const f=fixture();
  await assert.rejects(f.bridge.skipCurrent(),error=>error.code==='NO_CURRENT_TASK');
  await assert.rejects(f.bridge.pause(),error=>error.code==='NO_ACTIVE_JOB');
  assert.equal(f.calls.length,0);
  completed.push('unavailable commands produce an explicit error instead of silent success');
}
{
  const gate=deferred();
  const f=fixture({poll:({polls,finish})=>{
    if(polls===1)return gate.promise;
    finish();
  }});
  const operation=f.bridge.convertMany({files:input,formatIds:['pdf','html']});
  await until(()=>f.polled()>0);
  const accepted=await f.bridge.pause(true);
  assert.equal(accepted.pauseRequested,true);
  const boundary=f.events.length;
  gate.resolve({jobId,revision:2,finished:false,state:'running',files:[]});
  await operation;
  const published=f.events.slice(boundary).filter(e=>e.type==='aio-guard-state');
  assert.ok(published.length);
  assert.ok(published.every(e=>e.detail.revision>=accepted.revision));
  completed.push('accepted pause updates the view and an older poll cannot roll it back');
}
{
  const f=fixture({cancelFails:true});
  const operation=f.bridge.convertMany({files:input,formatIds:['pdf','html']});
  await until(()=>f.polled()>0);
  await assert.rejects(f.bridge.cancel(),error=>error.code==='SERVER_BUSY');
  await assert.rejects(f.bridge.retryFailed(),error=>error.code==='CLIENT_BUSY');
  f.finish();assert.equal((await operation).ok,true);
  completed.push('rejected cancellation and premature retry preserve live queue');
}
{
  const gate=deferred(),f=fixture({hostSave:()=>gate.promise});
  const operation=f.bridge.convertMany({files:input,formatIds:['pdf','html']});
  await until(()=>f.polled()>0);
  const saving=f.bridge.saveRetained(jobId,'folder');
  await tick();
  await assert.rejects(f.bridge.saveRetained(jobId,'folder'),error=>error.code==='SAVE_BUSY');
  await assert.rejects(f.bridge.discardRetained(jobId),error=>error.code==='LIVE_JOB_DISCARD');
  gate.resolve({saved:true,savedAll:false,snapshotComplete:true,outputs:1});await saving;
  await assert.rejects(f.bridge.saveRetained(jobId,'zip'),error=>error.code==='ZIP_REQUIRES_FINISHED');
  assert.equal(f.calls.some(c=>c.path==='/bundle'),false);
  f.finish();await operation;
  completed.push('duplicate save, live ZIP and live result deletion are rejected without altering queue');
}
{
  const seen=[],f=fixture({deferBundle:true,hostSave:result=>{seen.push(result);return {saved:false,cancelled:true};}});
  const operation=f.bridge.convertMany({files:input,formatIds:['pdf','html']});
  await until(()=>f.polled()>0);f.finish();
  const converted=await operation;
  assert.equal(f.calls.find(c=>c.path==='/start').body.archiveMode,'on_demand');
  assert.equal(converted.targetReport.bundleDeferred,true);
  await f.bridge.saveResult({result:converted.result});
  assert.equal(seen[0].saveMode,'folder');
  assert.equal(f.calls.some(c=>c.path==='/bundle'||c.path==='/release'),false);
  completed.push('native default save uses every retained output in a folder without building ZIP');
}
{
  const f=fixture({browser:true});
  const operation=f.bridge.convertMany({files:input,formatIds:['pdf','html']});
  await until(()=>f.polled()>0);f.finish();await operation;
  assert.equal(f.calls.find(c=>c.path==='/start').body.archiveMode,'auto');
  completed.push('browser retains automatic archive download contract');
}
{
  const seen=[],f=fixture({deferBundle:true,hostSave:result=>{seen.push(result);return {saved:false,cancelled:true};}});
  const operation=f.bridge.convertMany({files:input,formatIds:['pdf','html']});
  await until(()=>f.polled()>0);f.finish();await operation;
  await f.bridge.saveRetained(jobId,'zip');
  assert.equal(f.calls.filter(c=>c.path==='/bundle').length,1);
  assert.equal(seen[0].ticket,bundleTicket);
  assert.equal(seen[0].saveMode,undefined);
  assert.equal(f.calls.some(c=>c.path==='/saved'||c.path==='/release'),false);
  completed.push('explicit ZIP request builds archive and uses its new ticket without acknowledging a cancelled save');
}
{
  let saved=0;
  const f=fixture({deferBundle:true,bundleFails:true,hostSave:()=>{saved++;return {saved:false};}});
  const operation=f.bridge.convertMany({files:input,formatIds:['pdf','html']});
  await until(()=>f.polled()>0);f.finish();await operation;
  await assert.rejects(f.bridge.saveRetained(jobId,'zip'),error=>error.code==='BUNDLE_UNAVAILABLE');
  assert.equal(saved,0,'A primary artifact must not be offered as a complete ZIP');
  assert.equal(f.calls.some(c=>c.path==='/saved'||c.path==='/release'),false);
  completed.push('failed explicit archive does not save or acknowledge only the first artifact');
}
{
  const seen=[],f=fixture({deferBundle:true,hostSave:result=>{seen.push(result);return {saved:false,cancelled:true};}});
  const operation=f.bridge.convertMany({files:input,formatIds:['pdf','html']});
  await until(()=>f.polled()>0);f.finish();const converted=await operation;
  await f.bridge.saveResult({result:converted.result,mode:'zip'});
  assert.equal(f.calls.filter(c=>c.path==='/bundle').length,1);
  assert.equal(seen[0].ticket,bundleTicket);
  assert.equal(seen[0].saveMode,undefined);
  completed.push('direct explicit ZIP save cannot mistake the first artifact for the complete archive');
}
{
  const f=fixture({poll:({state,finish})=>{finish();return {...state,revision:state.revision+1,finished:true,state:'ready',completed:2,
    files:state.files.map(file=>({...file,targets:file.targets.map(t=>({...t,state:'saved'}))})),
    result:{...artifact(bundleTicket),name:'results.zip'}};}});
  await f.bridge.convertMany({files:input,formatIds:['pdf','html']});
  const updates=f.events.filter(event=>event.type==='finishTargets');
  assert.equal(updates.at(-1).detail.files[0].allCopiesSaved,true);
  completed.push('saved targets keep their saved status during subsequent queue updates');
}
console.log(JSON.stringify({status:'PASS',scope:'actual JS bridge with controlled native-save and transport boundaries',checks:completed}));
