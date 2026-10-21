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
  let started=false,done=false,paused=false,revision=2,polls=0;
  const state=()=>({jobId,revision,finished:done,state:done?'ready':'running',
    total:2,completed:done?2:1,succeeded:done?2:1,failed:0,cancelled:0,filtered:0,pending:done?0:1,
    currentTask:done?null:{taskId:jobId+':1',fileId:'one',name:'input.docx',format:'html',stage:'export',startedMs:Date.now()},
    pauseRequested:paused,
    files:[{fileId:'one',name:'input.docx',state:done?'ready':'converting',targets:[
      {format:'pdf',state:'ready',outputs:[artifact(firstTicket)]},
      {format:'html',state:done?'ready':'converting',outputs:[]}]}],
    result:done?{...artifact(bundleTicket),name:'results.zip',outputs:[artifact(firstTicket)]}:null,
    partialResult:{...artifact(firstTicket),outputsCount:1,folderOnly:true}});
  const root={dispatchEvent:event=>{events.push(event);return true;}};
  const progress={};
  for(const name of ['begin','dispose','finishTargets','failBatch','beginSave','finishSave'])
    progress[name]=value=>events.push({type:name,detail:value});
  const window={AllInOneNativeShell:true,
    AllInOneBridge:{saveExcel:async()=>options.hostSave?.()||{saved:true,savedAll:false,snapshotComplete:true,outputs:1,jobRunning:true}},
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
console.log(JSON.stringify({status:'PASS',scope:'actual JS bridge with controlled native-save and transport boundaries',checks:completed}));
