// Real browser bridge with a controlled transport. No conversion is measured.
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import {webcrypto} from 'node:crypto';
import {performance} from 'node:perf_hooks';
const source=readFileSync(new URL('../../src/converter-bridge.js',import.meta.url),'utf8');
const inputs=Array.from({length:1000},(_,i)=>({id:'f'+i,file:{name:'file'+i+'.csv',size:1}}));
const formats=Array.from({length:83},(_,i)=>({id:'fmt'+i,
  verifiedFileIds:inputs.filter((_,n)=>(n+i)%4!==0).map(f=>f.id)}));
const ids=formats.map(f=>f.id),referenceTimes=[],actualTimes=[];
for(let trial=0;trial<4;trial++){
  const tick=performance.now();
  const reference=inputs.map(f=>({fileId:f.id,formats:ids.filter(id=>formats.some(
    target=>target.id===id&&target.verifiedFileIds.includes(f.id)))})).filter(f=>f.formats.length);
  const referenceMs=performance.now()-tick;
  let plan,preflightAt,planMs;
  const job='a'.repeat(32),ticket='b'.repeat(43);
  const fetch=async(url,options={})=>{
    const path=String(url).replace('/api/converter-guard','');
    let value={jobId:job};
    if(path==='/preflight')value={available:true,files:[],formats};
    else if(path==='/start'){
      plan=JSON.parse(options.body).plan;planMs=performance.now()-preflightAt;value={ok:true};
    }else if(path.startsWith('/jobs?'))value={jobId:job,revision:1,finished:true,state:'ready',files:[],
      result:{jobId:job,ticket,downloadUrl:'/api/converter-guard/result?ticket='+ticket}};
    return{ok:true,status:200,headers:{get:()=>null},json:async()=>{
      if(path==='/preflight')preflightAt=performance.now();return value;}};
  };
  const window={AllInOneBridge:{},location:{href:'http://127.0.0.1/'}};
  const document={body:{dataset:{aioToken:'qa'}},querySelector:()=>null};
  vm.runInNewContext(source,{window,document,fetch,crypto:webcrypto,setTimeout});
  await window.AllInOneBridge.converter.convertMany({files:inputs,formatIds:ids});
  assert.deepEqual(JSON.parse(JSON.stringify(plan)),reference);
  if(trial){referenceTimes.push(referenceMs);actualTimes.push(planMs);}
}
const median=values=>[...values].sort((a,b)=>a-b)[Math.floor(values.length/2)];
console.log(JSON.stringify({status:'PASS',scope:'plan + JSON serialization only; no native preflight/export',
  files:1000,formats:83,plannedPairs:62250,equivalentPlan:true,
  previousMedianMs:Number(median(referenceTimes).toFixed(3)),
  actualBridgeMedianMs:Number(median(actualTimes).toFixed(3)),
  samples:{previous:referenceTimes,actual:actualTimes}}));
