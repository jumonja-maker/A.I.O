// Actual browser adapter in a controlled transport. No native-save simulation
// is presented as Qt acceptance. Assertions cover races, ownership and polling.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {webcrypto} from 'node:crypto';

const source = readFileSync(new URL('../src/converter-bridge.js', import.meta.url), 'utf8');
const jobId = 'a'.repeat(32), ticket = 'b'.repeat(43);
const copy = id => ({ticket:id, jobId, name:id+'.pdf',
  downloadUrl:'/api/converter-guard/result?ticket='+id});
const ready = overrides => ({schemaVersion:3,jobId,revision:3,state:'ready',finished:true,
  total:6,completed:6,attempted:3,succeeded:3,failed:0,cancelled:0,filtered:3,pending:0,
  files:[],result:{...copy(ticket),outputs:[copy('c'.repeat(43)),copy('d'.repeat(43))]},
  ...overrides});
const files = [{id:'one',file:{name:'renamed.bmp',size:1}}];
const formats = ['pdf','docx','odt','dbf','png','custom'];
const deferred = () => {let resolve;const promise=new Promise(r=>resolve=r);return {resolve,promise};};

function environment(options={}) {
  const calls=[], events=[];
  const responses=options.statuses || [ready()];
  let statusIndex=0, cancelled=false, bridge;
  const fetch=async (url,init={}) => {
    const path=String(url).replace('/api/converter-guard','');
    const body=typeof init.body==='string'?JSON.parse(init.body):init.body;
    calls.push({path,body,headers:init.headers});
    const reply=(payload,status=200,etag=null) => ({ok:status>=200&&status<300,status,
      headers:{get:name=>name==='ETag'?etag:null},
      json:async()=>{assert.notEqual(status,304,'304 must never be parsed as JSON');return payload;}});
    if(path==='/inspect') return reply({files:[],formats:formats.map(id=>({id,
      incompatibleFileIds:['one'],compatibleFileIds:[]}))});
    if(path==='/prepare') {
      if(options.created) await options.created.promise;
      return reply({jobId},201);
    }
    if(path==='/prepare-update')return options.prepareUpdateError
      ?reply({code:'SPOOL_QUOTA',error:'No configured quota'},413):reply({jobId});
    if(path.startsWith('/input')) {
      if(options.uploadError)return reply({code:'UPLOAD_TRUNCATED',error:'Upload failed'},400);
      return reply({ok:true});
    }
    if(path==='/preflight') {
      if(options.preflight)await options.preflight.promise;
      return reply({available:true,preflightComplete:true,files:[],formats:formats.map(id=>({id,
        verifiedFileIds:['one'],compatibleFileIds:['one'],selectableFileIds:['one']}))});
    }
    if(path==='/cancel') {cancelled=true;return reply({ok:true});}
    if(path==='/start') return reply({ok:true},202);
    if(path.startsWith('/jobs?')) {
      if(options.onPoll) await options.onPoll({bridge,cancelled,index:statusIndex});
      const value=responses[Math.min(statusIndex++,responses.length-1)];
      if(value===304)return reply(null,304);
      return reply(value,200,'"'+jobId+':'+value.revision+'"');
    }
    if(path==='/saved') {events.push('ack:'+body.ticket);return reply({ok:true});}
    if(path==='/release') {events.push('release');return reply({ok:true});}
    throw Error('Unexpected route: '+path);
  };
  const progress={};
  for(const name of ['begin','dispose','finishTargets','failBatch','beginSave','finishSave'])
    progress[name]=value=>events.push([name,value]);
  const window={AllInOneBridge:{saveExcel:async result=>{events.push('save:'+result.ticket);return {saved:true};}},
    AIOFileProgress:{attach:()=>progress}};
  const document={body:{dataset:{aioToken:'session'}},querySelector:()=>({})};
  // Keep real task turns while reducing only the polling delay in this VM.
  const context=vm.createContext({window,document,fetch,crypto:webcrypto,
    setTimeout:callback=>setTimeout(callback,1)});
  vm.runInContext(source,context,{filename:'src/converter-bridge.js'});
  bridge=window.AllInOneBridge.converter;
  return {bridge,window,calls,events};
}

const completed=[];
{
  const e=environment();
  const outcome=await e.bridge.convertMany({files,formatIds:formats});
  const request=e.calls.find(c=>c.path==='/start').body;
  assert.deepEqual(JSON.parse(JSON.stringify(request.plan[0].formats)),formats);
  assert.deepEqual(JSON.parse(JSON.stringify(request.fileIds)),['one']);
  assert.equal(request.onlyVerified,true,'manual selection cannot bypass the verified plan');
  assert.equal(e.calls.some(c=>c.path==='/inspect'),false,'run does not infer type from a filename');
  assert.equal(outcome.result.ticket,ticket);
  const gate=deferred();
  e.window.AllInOneBridge.saveExcel=async()=>{await gate.promise;return {saved:true};};
  const saving=e.bridge.saveResult({result:outcome.result});
  await new Promise(r=>setTimeout(r,1));
  assert.equal(e.events.some(x=>typeof x==='string'&&x.startsWith('ack:')),false);
  gate.resolve();await saving;
  assert.ok(e.events.indexOf('ack:'+ticket)<e.events.indexOf('release'));
  completed.push('preflight before complete plan and native-save acknowledgement ordering');
}
{
  let progress=0;
  const running=ready({revision:2,finished:false,state:'running'});
  const e=environment({statuses:[running,304,304,ready()]});
  await e.bridge.convertMany({files,formatIds:formats,onProgress:()=>progress++});
  assert.equal(progress,2,'unchanged polls must not rerender progress or problems');
  const polls=e.calls.filter(c=>c.path.startsWith('/jobs?'));
  assert.equal(polls[0].headers['If-None-Match'],undefined);
  assert.equal(polls[1].headers['If-None-Match'],'"'+jobId+':2"');
  assert.equal(polls[3].headers['If-None-Match'],'"'+jobId+':2"');
  completed.push('ETag/304 bypasses JSON and repeated UI updates');
}
{
  const created=deferred(),e=environment({created});
  const operation=e.bridge.convertMany({files,formatIds:formats});
  await new Promise(r=>setTimeout(r,1));
  e.bridge.resetProgress();created.resolve();
  await assert.rejects(operation,error=>error.code==='CANCELLED');
  assert.ok(e.calls.some(c=>c.path==='/release'&&c.body.jobId===jobId));
  assert.equal(e.calls.some(c=>c.path.startsWith('/input')),false);
  completed.push('reset during job creation releases the late-created spool');
}
{
  const e=environment({uploadError:true});
  await assert.rejects(e.bridge.convertMany({files,formatIds:formats}),error=>error.code==='UPLOAD_TRUNCATED');
  assert.ok(e.calls.some(c=>c.path==='/release'));
  completed.push('failed upload releases job ownership');
}
{
  const done=ready({state:'failed',succeeded:0,result:null});
  const e=environment({statuses:[done]});
  await assert.rejects(e.bridge.convertMany({files,formatIds:formats}),error=>{
    assert.equal(error.batchReport.total,6);return error.code==='NO_SUCCESSFUL_CONVERSIONS';
  });
  assert.ok(e.calls.some(c=>c.path==='/release'));
  completed.push('failed run preserves diagnostics and releases empty spool');
}
{
  const e=environment({statuses:[ready({packagingError:'BUNDLE_QUOTA'})]});
  const outcome=await e.bridge.convertMany({files,formatIds:formats});
  const saved=[];let cancelSecond=true;
  e.window.AllInOneBridge.saveExcel=async item=>{
    saved.push(item.ticket);
    if(saved.length===2&&cancelSecond){cancelSecond=false;return {cancelled:true};}
    return {saved:true};
  };
  const first=await e.bridge.saveResult({result:outcome.result});
  assert.equal(first.cancelled,true);
  assert.equal(e.events.includes('release'),false,'remaining unsaved copies stay retained');
  const second=await e.bridge.saveResult({result:outcome.result});
  assert.equal(second.saved,true);
  assert.deepEqual(saved,['c'.repeat(43),'d'.repeat(43),'d'.repeat(43)]);
  assert.deepEqual(e.events.filter(x=>typeof x==='string'&&x.startsWith('ack:')),
    ['ack:'+'c'.repeat(43),'ack:'+'d'.repeat(43)]);
  assert.equal(e.events.at(-2),'release');
  completed.push('archive failure saves each copy and resumes after cancelled save');
}
{
  const final=ready({state:'cancelled',cancelled:3,filtered:0});
  const e=environment({statuses:[final],onPoll:async ({bridge})=>{await bridge.cancel();}});
  const outcome=await e.bridge.convertMany({files,formatIds:formats});
  assert.equal(outcome.ok,true);
  assert.equal(outcome.targetReport.state,'cancelled');
  assert.ok(outcome.message.includes('можно сохранить'));
  assert.equal(e.events.includes('release'),false);
  await e.bridge.saveResult({result:outcome.result});
  completed.push('cancelled job still offers completed outputs');
}
{
  const e=environment();
  const info=await e.bridge.inspectFiles({files});
  assert.equal(info.preflightComplete,true);
  assert.equal(e.calls.some(c=>c.path==='/start'),false,'inspection must not start export');
  e.bridge.resetProgress(); // changing selected formats retains preparation
  await e.bridge.inspectFiles({files});
  await e.bridge.convertMany({files,formatIds:formats,onlyVerified:true});
  assert.equal(e.calls.filter(c=>c.path==='/prepare').length,1);
  assert.equal(e.calls.filter(c=>c.path.startsWith('/input')).length,1);
  assert.equal(e.calls.filter(c=>c.path==='/preflight').length,1);
  assert.equal(e.calls.find(c=>c.path==='/start').body.onlyVerified,true);
  assert.ok(e.calls.findIndex(c=>c.path==='/preflight')<e.calls.findIndex(c=>c.path==='/start'));
  completed.push('Select All reuses preflight and uploaded inputs without repeated native import');
}
{
  const e=environment();
  await e.bridge.inspectFiles({files});
  await e.bridge.inspectFiles({files:[]});
  assert.ok(e.calls.some(c=>c.path==='/release'));
  assert.equal(e.calls.some(c=>c.path==='/start'),false);
  completed.push('empty file list releases prepared inputs without export');
}
{
  const e=environment(),extra={id:'two',file:{name:'new.xlsx',size:1}};
  await e.bridge.inspectFiles({files});
  await e.bridge.inspectFiles({files:[...files,extra]});
  await e.bridge.inspectFiles({files:[extra]});
  assert.equal(e.calls.filter(c=>c.path==='/prepare').length,1);
  assert.equal(e.calls.filter(c=>c.path==='/prepare-update').length,1);
  assert.equal(e.calls.filter(c=>c.path.startsWith('/input')).length,2);
  assert.equal(e.calls.some(c=>c.path==='/start'),false);
  completed.push('additions upload only new bytes; removals reuse prepared inputs');
}
{
  const e=environment({prepareUpdateError:true});
  await e.bridge.inspectFiles({files});
  await assert.rejects(e.bridge.inspectFiles({files:[...files,{id:'two',file:{name:'new.xlsx',size:1}}]}),
    error=>error.code==='SPOOL_QUOTA');
  assert.ok(e.calls.some(c=>c.path==='/release'&&c.body.jobId===jobId));
  assert.equal(e.calls.some(c=>c.path==='/start'),false);
  completed.push('failed preparation update releases the previous disk ownership');
}
{
  const preflight=deferred(),e=environment({preflight});
  const inspection=e.bridge.inspectFiles({files});
  while(!e.calls.some(c=>c.path==='/preflight'))await new Promise(r=>setTimeout(r,1));
  await e.bridge.cancelInspection();preflight.resolve();
  await assert.rejects(inspection,error=>error.code==='CANCELLED');
  assert.ok(e.calls.some(c=>c.path==='/release'&&c.body.jobId===jobId));
  assert.equal(e.calls.some(c=>c.path==='/start'),false);
  completed.push('cancelled native inspection never caches a late response or starts export');
}
{
  const e=environment();
  await e.bridge.inspectFiles({files});
  const result=await e.bridge.convertMany({files,formatIds:formats});
  await e.bridge.saveResult({result:result.result});
  const before=e.calls.length;
  const info=await e.bridge.inspectFiles({files});
  assert.equal(info.preflightComplete,true);
  assert.equal(e.calls.length,before,'completed/saved run retains the immutable inspection view');
  const changed=[{id:'one',file:{name:'renamed.bmp',size:1}}];
  await e.bridge.inspectFiles({files:changed});
  assert.equal(e.calls.filter(c=>c.path==='/prepare').length,2,'same metadata is not the same File');
  completed.push('inspection view survives save; changed File object requires backend validation');
}
{
  const e=environment();
  await e.bridge.inspectFiles({files});
  await e.bridge.inspectFiles({files,force:true});
  assert.equal(e.calls.filter(c=>c.path.startsWith('/input')).length,1);
  assert.equal(e.calls.filter(c=>c.path==='/preflight').length,2);
  assert.equal(e.calls.filter(c=>c.path==='/preflight').at(-1).body.recheck,true);
  completed.push('explicit preflight retry bypasses the proof cache without uploading bytes twice');
}
console.log(JSON.stringify({status:'PASS',scope:'browser adapter VM; controlled HTTP/native-save stub',
  scenarios:completed.length,checks:completed}));
