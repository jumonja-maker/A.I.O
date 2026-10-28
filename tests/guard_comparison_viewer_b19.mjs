import assert from 'node:assert/strict';
import vm from 'node:vm';
import {createRequire}from'node:module';
import {readFileSync}from'node:fs';
const require=createRequire(import.meta.url),{parseHTML}=require(process.env.AIO_DOM_MODULE);
const{window,document}=parseHTML('<html><body data-aio-token="unit-token"><dialog open><div id="preview"></div></dialog></body></html>');
window.location=new URL('http://127.0.0.1:1234/');
const created=[],revoked=[],requests=[];
class URLs extends URL{static createObjectURL(blob){const url='blob:viewer/'+created.length;created.push(url);return url;}static revokeObjectURL(url){revoked.push(url);}}
let text=false,hold=null,asymmetric=false;
async function fetchMock(path,options){
 const u=new URL(path);requests.push({u,options});assert.equal(options.headers['X-AIO-Token'],'unit-token');assert.equal(u.searchParams.has('token'),false);
 if(hold)await hold;
 const page=Number(u.searchParams.get('page'))||0;
 const side=u.searchParams.get('side'),width=asymmetric?(side==='source'?140:3840):2400,height=asymmetric?(side==='source'?105:2160):1200;
 return{ok:true,status:200,headers:{get:k=>({'Content-Type':text?'application/json':'image/png','Content-Length':'10','X-AIO-Preview-Pages':'3','X-AIO-Preview-Page':String(page),'X-AIO-Preview-Width':String(width),'X-AIO-Preview-Height':String(height),'X-AIO-Preview-Rendered-Width':String(width)})[k]||null},
  blob:async()=>new Blob(['inert PNG boundary']),json:async()=>({kind:'text',text:'<script>never execute</script>',note:'Текст'})};
}
const context=vm.createContext({window,document,URL:URLs,Blob,fetch:fetchMock,AbortController,setTimeout,clearTimeout,console});
vm.runInContext(readFileSync(new URL('../src/guard-comparison-viewer.js',import.meta.url),'utf8'),context);
const job='a'.repeat(32),task=job+':0',analysisId='analysis';
const descriptor=side=>({kind:'image',name:side+'.png',url:'/api/converter-guard/compare/preview?'+new URLSearchParams({job,task,side,analysisId})});
const root=document.querySelector('#preview'),mount=extra=>window.AIOComparisonViewer.mount(root,{source:descriptor('source'),result:descriptor('result'),context:{jobId:job,taskId:task,analysisId,outputIndex:2},...extra});
const viewer=mount();await viewer.ready;
assert.equal(root.querySelectorAll('.aio-pair-pane').length,2);assert.equal(root.querySelectorAll('img').length,2);
assert.equal(requests[1].u.searchParams.get('outputIndex'),'2');
viewer.zoom(1);assert.ok([...root.querySelectorAll('img')].every(n=>n.style.width==='2400px'));
viewer.zoom(2);assert.ok([...root.querySelectorAll('img')].every(n=>n.style.width==='4800px'));
viewer.zoom(4);assert.ok([...root.querySelectorAll('img')].every(n=>n.style.width==='9600px'));
await viewer.changePage('source',1);
assert.ok(requests.slice(-2).every(r=>r.u.searchParams.get('page')==='1'));
assert.ok([...root.querySelectorAll('.aio-pair-head span')].every(n=>n.textContent==='2 / 3'));
root.querySelector('[data-viewer-fullscreen]').click();assert.equal(document.querySelector('dialog').classList.contains('aio-compare-maximized'),true);
root.querySelector('[data-viewer-fullscreen]').click();assert.equal(document.querySelector('dialog').classList.contains('aio-compare-maximized'),false);
const panes=[...root.querySelectorAll('.aio-pair-viewport')];
for(const node of panes)for(const[k,v]of Object.entries({clientWidth:400,clientHeight:200,scrollWidth:2000,scrollHeight:1000}))Object.defineProperty(node,k,{configurable:true,value:v});
panes[0].scrollLeft=800;panes[0].scrollTop=400;panes[0].dispatchEvent(new window.Event('scroll'));
assert.equal(panes[1].scrollLeft,800);assert.equal(panes[1].scrollTop,400);
Object.defineProperty(panes[1],'clientWidth',{configurable:true,value:800});
Object.defineProperty(panes[1],'clientHeight',{configurable:true,value:400});
viewer.zoom('fit');assert.deepEqual([...root.querySelectorAll('img')].map(n=>n.style.width),['400px','800px'],'fit uses independent viewport scales');
viewer.destroy();assert.equal(root.children.length,0);
asymmetric=true;
const mismatched=mount();await mismatched.ready;
for(const p of root.querySelectorAll('.aio-pair-viewport'))for(const[k,v]of Object.entries({clientWidth:400,clientHeight:200,scrollWidth:2000,scrollHeight:1000}))
  Object.defineProperty(p,k,{configurable:true,value:v});
mismatched.zoom('fit');
assert.deepEqual([...root.querySelectorAll('img')].map(n=>n.style.width),['267px','356px'],
  '140x105 source must not shrink with 3840x2160 target and synced pan');
mismatched.destroy();asymmetric=false;assert.equal(root.children.length,0);assert.deepEqual([...created].sort(),[...revoked].sort());
const before=requests.length;
const invalid=mount({source:{...descriptor('source'),url:'https://invalid.example/preview'}});await invalid.ready;
assert.equal(requests.length,before+1);assert.match(root.textContent,/Адрес просмотра/);invalid.destroy();
text=true;const inert=mount();await inert.ready;assert.equal(root.querySelectorAll('script').length,0);assert.match(root.textContent,/<script>never execute/);inert.destroy();text=false;
let resolve;hold=new Promise(r=>resolve=r);const delayed=mount();delayed.destroy();resolve();await delayed.ready;
assert.equal(requests.at(-1).options.signal.aborted,true);assert.equal(root.children.length,0);
assert.deepEqual([...created].sort(),[...revoked].sort());
const fullscreen=mount();await fullscreen.ready;
const section=root.querySelector('.aio-pair-viewer');let fullscreenResolve,exits=0;
section.requestFullscreen=()=>new Promise(resolve=>fullscreenResolve=resolve);
document.exitFullscreen=async()=>{exits++;document.fullscreenElement=null;};
root.querySelector('[data-viewer-fullscreen]').click();
assert.equal(root.querySelector('[data-viewer-fullscreen]').disabled,true);
fullscreen.destroy();document.fullscreenElement=section;fullscreenResolve();
await new Promise(resolve=>setTimeout(resolve,0));
assert.equal(exits,1);assert.equal(document.fullscreenElement,null);assert.equal(root.children.length,0);
assert.deepEqual([...created].sort(),[...revoked].sort());
console.log(JSON.stringify({status:'PASS',scope:'real viewer + DOM, controlled image transport',twoPanes:true,zoom:[100,200,400],synchronizedPages:true,synchronizedPan:true,fullscreen:true,inertText:true,boundURLs:true,stalePreviewDiscarded:true,allBlobURLsRevoked:true}));
