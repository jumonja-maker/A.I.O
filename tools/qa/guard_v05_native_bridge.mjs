// Explicit QA only: actual Guard bridge + HTTP + installed native LO.
// Native Qt Save is replaced with a declared host-acknowledgement fixture.
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync,writeFileSync} from 'node:fs';
import {webcrypto} from 'node:crypto';

const base=process.env.AIO_TEST_SERVER,token=process.env.AIO_TEST_TOKEN;
const nativeFetch=globalThis.fetch.bind(globalThis);
let unchanged=0,polls=0;
const apiFetch=async (url,options)=>{
  const response=await nativeFetch(new URL(url,base),options);
  if(String(url).includes('/jobs?')) {polls++;if(response.status===304)unchanged++;}
  return response;
};
const window={AllInOneBridge:{}};
const document={body:{dataset:{aioToken:token}},querySelector:()=>null};
const context=vm.createContext({window,document,fetch:apiFetch,crypto:webcrypto,setTimeout});
vm.runInContext(readFileSync(new URL('../../src/converter-bridge.js',import.meta.url),'utf8'),context);
const bridge=window.AllInOneBridge.converter;
const files=JSON.parse(process.env.AIO_TEST_SOURCES).map((path,i)=>({id:'f'+i,
  file:new File([readFileSync(path)],path.split('/').at(-1))}));
const formatIds=['pdf','docx','odt','dbf','png'];
const inspection=await bridge.inspectFiles({files});
const dbf=inspection.formats.find(f=>f.id==='dbf');
// v0.6 imports first, so known Draw/Writer exclusions are already authoritative.
assert.equal(dbf.nativeAttemptAvailable,!inspection.preflightComplete);
assert.deepEqual(dbf.selectableFileIds,inspection.preflightComplete?[]:files.map(f=>f.id));
const outcome=await bridge.convertMany({files,formatIds});
const report=outcome.targetReport;
writeFileSync(process.env.AIO_TEST_ARCHIVE.replace(/\.zip$/,'.status.json'),JSON.stringify(report,
  (key,value)=>['ticket','downloadUrl'].includes(key)?undefined:value,2));
assert.equal(report.total,15);assert.ok(report.succeeded>0);
assert.equal(report.attempted,report.succeeded);assert.ok(report.filtered>0);assert.equal(report.failed,0);
assert.equal(report.succeeded+report.filtered,15);
assert.equal(report.files[1].targets.find(t=>t.format==='docx').state,'ready');
assert.equal(report.files[2].targets.find(t=>t.format==='docx').state,'ready');
assert.equal(report.files[0].targets.find(t=>t.format==='dbf').state,'skipped');
assert.equal(report.completed,15);assert.equal(report.pending,0);
assert.deepEqual(report.files.map(f=>f.targets.map(t=>t.format)),files.map(()=>formatIds));
assert.ok(unchanged>0,'long native work must use actual ETag/304 responses');
let cancelled=true;
window.AllInOneBridge.saveExcel=async result=>{
  if(cancelled){cancelled=false;return {cancelled:true};}
  const response=await apiFetch(result.downloadUrl);
  assert.equal(response.ok,true);
  const bytes=Buffer.from(await response.arrayBuffer());
  assert.equal(bytes.subarray(0,2).toString(),'PK');
  writeFileSync(process.env.AIO_TEST_ARCHIVE,bytes);
  return {saved:true};
};
assert.equal((await bridge.saveResult({result:outcome.result})).cancelled,true);
const status=async()=>await (await apiFetch('/api/converter-guard/status',{
  headers:{'X-AIO-Token':token}})).json();
assert.equal((await status()).storage.jobs,1,'cancelled save retains native outputs');
assert.equal((await bridge.saveResult({result:outcome.result})).saved,true);
assert.equal((await status()).storage.jobs,0);
assert.equal((await status()).storage.reservedBytes,0);
console.log(JSON.stringify({status:'PASS',scope:'native LibreOffice + real HTTP/browser bridge; Qt save fixture',
  total:report.total,succeeded:report.succeeded,attempted:report.attempted,
  filtered:report.filtered,failed:report.failed,polls,unchangedPolls:unchanged,
  explicitTicket:true,saveCancellationRetry:true,releasedSpool:true,
  outputs:report.result.outputs.map(({fileId,format,archiveName})=>({fileId,format,archiveName}))}));
