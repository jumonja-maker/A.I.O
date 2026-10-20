// Opt-in installed LibreOffice + actual HTTP and browser adapter; no Qt dialog.
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync, writeFileSync} from 'node:fs';
import {webcrypto} from 'node:crypto';

const base=process.env.AIO_TEST_SERVER, token=process.env.AIO_TEST_TOKEN;
const counts={uploads:0,preflights:0,starts:0};
const starts=[], inspections=[];
const fetcher=async (url,options={})=>{
  const path=String(url);
  if(path.includes('/input?'))counts.uploads++;
  if(path.endsWith('/preflight'))counts.preflights++;
  if(path.endsWith('/start')) {counts.starts++;starts.push(JSON.parse(options.body));}
  const response=await fetch(new URL(url,base),options);
  if(path.endsWith('/preflight')&&response.ok)inspections.push(await response.clone().json());
  return response;
};
const window={AllInOneBridge:{}};
const document={body:{dataset:{aioToken:token}},querySelector:()=>null};
const context=vm.createContext({window,document,fetch:fetcher,crypto:webcrypto,setTimeout});
vm.runInContext(readFileSync(new URL('../../src/converter-bridge.js',import.meta.url),'utf8'),context);
const bridge=window.AllInOneBridge.converter;
const files=JSON.parse(process.env.AIO_TEST_SOURCES).map((path,i)=>({id:'f'+i,
  file:new File([readFileSync(path)],path.split(/[\\/]/).at(-1))}));
const info=await bridge.inspectFiles({files});
assert.equal(info.preflightChecks,4);
assert.equal(counts.starts,0);
assert.ok(info.files.every(f=>f.preflight?.state==='detected'&&f.preflight.exported===false));
for(const fmt of info.formats) {
  assert.deepEqual(fmt.selectableFileIds,fmt.verifiedFileIds);
  assert.equal(fmt.enabled,fmt.verifiedFileIds.length>0);
}
assert.equal(info.formats.find(f=>f.id==='docx').enabled,false,'CSV and BMP are not Writer models');
assert.deepEqual(info.formats.find(f=>f.id==='dbf').verifiedFileIds,['f0','f1','f2']);
const formats=(process.env.AIO_TEST_FORMATS || 'pdf').split(',');
const first=await bridge.convertMany({files,formatIds:formats});
const expected=files.map(f=>({fileId:f.id,formats:formats.filter(id=>
  info.formats.find(target=>target.id===id).verifiedFileIds.includes(f.id))}));
assert.deepEqual(starts[0].plan,expected);
const firstReport=first.targetReport;
assert.equal(firstReport.total,expected.reduce((n,f)=>n+f.formats.length,0));
assert.equal(firstReport.failed,0);assert.equal(firstReport.filtered,0);
assert.equal(firstReport.succeeded,firstReport.total);
window.AllInOneBridge.saveExcel=async result=>{
  const response=await fetcher(result.downloadUrl);
  assert.equal(response.ok,true);
  writeFileSync(process.env.AIO_TEST_ARCHIVE,Buffer.from(await response.arrayBuffer()));
  return {saved:true};
};
await bridge.saveResult({result:first.result});
const before={...counts};
const remaining=files.slice(0,3);
const view=await bridge.inspectFiles({files:remaining});
assert.deepEqual(counts,before,'removing BMP after save makes no backend request');
assert.equal(view.files.length,3);
assert.ok(!view.formats.find(f=>f.id==='png').verifiedFileIds.includes('f3'));
const second=await bridge.convertMany({files:remaining,formatIds:['pdf']});
assert.equal(inspections.at(-1).preflightChecks,0);
assert.equal(inspections.at(-1).preflightCacheHits,3);
assert.equal(second.targetReport.succeeded,3);
assert.equal(second.targetReport.failed,0);
await bridge.saveResult({result:second.result});
const storage=await (await fetcher('/api/converter-guard/status',{
  headers:{'X-AIO-Token':token}})).json();
assert.equal(storage.storage.jobs,0);assert.equal(storage.storage.reservedBytes,0);
bridge.dispose();
const clean=(key,value)=>['ticket','downloadUrl'].includes(key)?undefined:value;
console.log(JSON.stringify({status:'PASS',scope:'actual Linux LibreOffice + HTTP + JS bridge; save fixture',
  preflight:info.files.map(f=>({id:f.id,name:f.name,...f.preflight})),
  first:{total:firstReport.total,succeeded:firstReport.succeeded,failed:firstReport.failed,
    filtered:firstReport.filtered,outputs:firstReport.result.outputs.map(({fileId,format,archiveName})=>({fileId,format,archiveName}))},
  second:{total:second.targetReport.total,succeeded:second.targetReport.succeeded,
    nativeChecks:inspections.at(-1).preflightChecks,cacheHits:inspections.at(-1).preflightCacheHits},
  removalAfterSaveNoRequests:true,counts,storage:storage.storage},clean));
