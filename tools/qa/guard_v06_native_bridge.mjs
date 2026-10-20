// Opt-in QA: real bridge + HTTP + installed LibreOffice. No Qt dialog is driven.
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync,writeFileSync} from 'node:fs';
import {webcrypto} from 'node:crypto';

const base=process.env.AIO_TEST_SERVER,token=process.env.AIO_TEST_TOKEN;
const nativeFetch=globalThis.fetch.bind(globalThis);
const counts={uploads:0,preflights:0,starts:0,polls:0,unchanged:0};
let preparedId,firstStart;
const apiFetch=async (url,options)=>{
  const path=String(url);
  if(path.includes('/input?'))counts.uploads++;
  if(path.endsWith('/preflight'))counts.preflights++;
  if(path.endsWith('/start'))counts.starts++;
  const response=await nativeFetch(new URL(url,base),options);
  if(path.endsWith('/prepare')&&response.ok)preparedId=(await response.clone().json()).jobId;
  if(path.endsWith('/start')&&response.ok&&!firstStart)firstStart=await response.clone().json();
  if(path.includes('/jobs?')){counts.polls++;if(response.status===304)counts.unchanged++;}
  return response;
};
const window={AllInOneBridge:{}};
const document={body:{dataset:{aioToken:token}},querySelector:()=>null};
const context=vm.createContext({window,document,fetch:apiFetch,crypto:webcrypto,setTimeout});
vm.runInContext(readFileSync(new URL('../../src/converter-bridge.js',import.meta.url),'utf8'),context);
const bridge=window.AllInOneBridge.converter;
const files=JSON.parse(process.env.AIO_TEST_SOURCES).map((path,i)=>({id:'f'+i,
  file:new File([readFileSync(path)],path.split('/').at(-1))}));
const get=async path=>await(await apiFetch(path,{headers:{'X-AIO-Token':token}})).json();
const inspection=await bridge.inspectFiles({files});
assert.equal(inspection.preflightComplete,true);
assert.equal(inspection.preflightChecks,3);
const before=await get('/api/converter-guard/jobs?job='+preparedId+'&trace=1');
assert.equal(before.state,'prepared');assert.equal(before.total,0);assert.equal(before.attempted,0);
assert.equal(counts.starts,0);
assert.deepEqual(inspection.formats.find(f=>f.id==='dbf').verifiedFileIds,['f2']);
assert.deepEqual(inspection.formats.find(f=>f.id==='docx').verifiedFileIds,['f1']);
assert.deepEqual(inspection.formats.find(f=>f.id==='pdf').verifiedFileIds,['f0','f1','f2']);
// The DOCX deliberately has a BMP suffix: the native model must win.
assert.equal(inspection.files[1].preflight.nativeKind,0);
assert.ok(inspection.files.every(f=>f.preflight.exported===false));
const cached=await bridge.inspectFiles({files});
assert.equal(cached,inspection);
bridge.resetProgress();
await bridge.inspectFiles({files});
assert.equal(counts.uploads,3);assert.equal(counts.preflights,1);
const formatIds=['dbf','pdf','docx','odt','png'];
const outcome=await bridge.convertMany({files,formatIds,onlyVerified:true});
const report=outcome.targetReport;
writeFileSync(process.env.AIO_TEST_ARCHIVE.replace(/\.zip$/,'.status.json'),JSON.stringify(report,
  (key,value)=>['ticket','downloadUrl'].includes(key)?undefined:value,2));
assert.equal(report.total,15);assert.equal(report.preflightChecks,3);
assert.equal(report.attempted,report.succeeded);assert.ok(report.filtered>0);assert.equal(report.failed,0);
assert.equal(report.succeeded+report.filtered,15);
assert.equal(report.completed,15);assert.equal(report.pending,0);
assert.deepEqual(report.files.map(f=>f.targets.map(t=>t.format)),files.map(()=>formatIds));
const excluded=report.files.flatMap(f=>f.targets.filter(t=>t.state==='skipped')
  .map(t=>({fileId:f.fileId,format:t.format,stage:t.stage,started:t.libreOfficeStarted})));
assert.ok(excluded.length>0);
for(const row of excluded){
  assert.equal(row.stage,'preflight');assert.equal(row.started,false);
  const initial=firstStart.files.find(f=>f.fileId===row.fileId).targets.find(t=>t.format===row.format);
  assert.equal(initial.state,'skipped');assert.equal(initial.libreOfficeStarted,false);
}
assert.equal(counts.uploads,3);assert.equal(counts.preflights,1);assert.equal(counts.starts,1);
assert.ok(counts.unchanged>0,'native work must use actual ETag/304 responses');
let cancelSave=true;
window.AllInOneBridge.saveExcel=async result=>{
  if(cancelSave){cancelSave=false;return {cancelled:true};}
  const response=await apiFetch(result.downloadUrl);
  assert.equal(response.ok,true);
  const bytes=Buffer.from(await response.arrayBuffer());
  assert.equal(bytes.subarray(0,2).toString(),'PK');
  writeFileSync(process.env.AIO_TEST_ARCHIVE,bytes);
  return {saved:true};
};
assert.equal((await bridge.saveResult({result:outcome.result})).cancelled,true);
assert.equal((await get('/api/converter-guard/status')).storage.jobs,1);
assert.equal((await bridge.saveResult({result:outcome.result})).saved,true);
const storage=(await get('/api/converter-guard/status')).storage;
assert.equal(storage.jobs,0);assert.equal(storage.reservedBytes,0);
bridge.dispose();
console.log(JSON.stringify({status:'PASS',scope:'native LibreOffice + real HTTP/browser bridge; Qt save fixture',
  total:report.total,succeeded:report.succeeded,attempted:report.attempted,
  filtered:report.filtered,failed:report.failed,preflightChecks:report.preflightChecks,requests:counts,
  beforeExport:{state:before.state,total:before.total,attempted:before.attempted},
  preflight:inspection.files.map(f=>({id:f.id,name:f.name,...f.preflight})),
  confirmedRoutes:inspection.files.map(f=>({id:f.id,count:inspection.formats.filter(
    fmt=>fmt.verifiedFileIds.includes(f.id)).length})),
  excluded,saveCancellationRetry:true,releasedSpool:true,cachedInspection:true,
  outputs:report.result.outputs.map(({fileId,format,archiveName})=>({fileId,format,archiveName}))}));
