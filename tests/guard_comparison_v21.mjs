// Deferred HTTP completions against the production comparison UI and real DOM.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
import vm from 'node:vm';
if(!process.env.AIO_DOM_MODULE)throw Error('Pinned offline AIO_DOM_MODULE is required');
const {parseHTML}=await import(pathToFileURL(process.env.AIO_DOM_MODULE).href);
const script=readFileSync(new URL('../src/guard-comparison.js',import.meta.url),'utf8');
const job='a'.repeat(32),other='b'.repeat(32),child='c'.repeat(32);
const flush=async()=>{for(let i=0;i<5;i++)await new Promise(resolve=>setTimeout(resolve,0));};
function gate(){let resolve;return {promise:new Promise(done=>{resolve=done;}),resolve:value=>resolve(value)};}
function create(){
  const {window,document}=parseHTML('<html><body data-aio-token="t"><div class="aio-cv"><div class="aio-cv__footer"><button>Сохранить</button></div></div></body></html>');
  window.location={href:'http://127.0.0.1:5123/',origin:'http://127.0.0.1:5123'};
  window.AllInOneBridge={converter:{async saveRetained(){return {saved:true}}}};
  window.HTMLElement.prototype.showModal=function(){this.open=true;this.setAttribute('open','');};
  window.HTMLElement.prototype.close=function(){if(!this.open)return;this.open=false;this.removeAttribute('open');setTimeout(()=>this.dispatchEvent(new window.Event('close')),0);};
  window.HTMLElement.prototype.focus=function(){};
  Object.defineProperty(window.HTMLSelectElement.prototype,'value',{configurable:true,
    get(){return this.querySelector('option[selected]')?.value||this.querySelector('option')?.value||''},
    set(value){for(const option of this.querySelectorAll('option'))if(option.value===value)option.setAttribute('selected','');else option.removeAttribute('selected')}});
  const calls=[],pending=[],timers=new Set(),created=[],revoked=[];
  let override=null;
  const headers={get(name){return name==='Content-Type'?'application/json':null}};
  const response=(value,status=200)=>({ok:status<400,status,headers,async json(){return value}});
  const item=id=>({jobId:id,analysisId:'analysis-'+id,taskId:id+':0',name:id===job?'First.docx':'Second.docx',format:'pdf',status:'confirmed_loss',sourceAvailable:true,
    recipeCandidates:['pdf_preserve_images'],findings:[{category:'confirmed_loss',message:id===job?'FIRST REPORT':'SECOND REPORT'}],unknown:['visual_layout']});
  const packet=id=>({jobId:id,analysisId:'analysis-'+id,state:'completed',canRetry:true,canRestart:true,processed:1,total:1,totalFiltered:1,
    summary:{confirmed_loss:1},items:[item(id)],retryHistory:[],historyTotal:0});
  async function fetch(path,options={}){
    const url=new URL(path,window.location.href),body=options.body?JSON.parse(options.body):null;
    calls.push({url,body,signal:options.signal});
    const custom=override?.(url,body,options);if(custom!==undefined)return await custom;
    const id=url.searchParams.get('job')||body?.jobId;
    if(url.pathname.endsWith('/compare/start'))return response({jobId:id,state:'completed'});
    if(url.pathname.endsWith('/compare/retry'))return response({state:'completed',jobId:child});
    if(url.pathname.endsWith('/compare'))return response(packet(id));
    if(url.pathname.endsWith('/compare/item'))return response({...item(id),previews:{
      source:{kind:'text',name:'source.txt',text:'SOURCE '+id},result:{kind:'text',name:'result.txt',text:'RESULT '+id}}});
    if(url.pathname.endsWith('/compare/preview'))return response({kind:'text',text:'NETWORK PREVIEW'});
    throw Error('Unexpected '+url.pathname);
  }
  class URLs extends URL{static createObjectURL(blob){const id='blob:'+created.length;created.push(id);return id;}static revokeObjectURL(id){revoked.push(id)}}
  const setTimer=(fn,delay)=>{const timer=setTimeout(()=>{timers.delete(timer);fn();},delay);timers.add(timer);return timer};
  const clearTimer=timer=>{clearTimeout(timer);timers.delete(timer)};
  vm.runInNewContext(script,{window,document,MutationObserver:window.MutationObserver,fetch,URL:URLs,Blob,AbortController,setTimeout:setTimer,clearTimeout:clearTimer,console});
  const $=selector=>document.querySelector(selector),event=(node,type)=>node.dispatchEvent(new window.Event(type,{bubbles:true,cancelable:true}));
  const click=selector=>event($(selector),'click');
  return {window,document,$,event,click,calls,response,item,packet,timers,created,revoked,
    override(fn){override=fn},async close(){window.AIOGuardComparison.close();await flush();for(const timer of timers)clearTimeout(timer);timers.clear()},
    async select(){const box=$('[data-c-files] input');box.checked=true;event(box,'change');await flush()}};
}
const results=[];
async function check(name,work){const h=create();try{await work(h);results.push({name,status:'PASS'})}finally{await h.close()}}
const only=process.argv[2];
const cases=[
  ['obsolete_refresh_error',async h=>{
    await h.window.AIOGuardComparison.open(job);const delayed=gate();let used=false;
    h.override((url)=>{if(url.pathname.endsWith('/compare')&&!used){used=true;return delayed.promise}});
    h.$('[data-c-filter]').value='confirmed_loss';h.event(h.$('[data-c-filter]'),'change');await flush();
    h.$('[data-c-filter]').value='all';h.event(h.$('[data-c-filter]'),'change');await flush(); // newer successful response owns the page
    assert.equal(h.$('[data-c-save-current]').disabled,false);
    delayed.resolve(h.response({code:'JOB_NOT_FOUND',error:'obsolete 404'},404));await flush();
    assert.equal(h.$('[data-c-save-current]').disabled,false,'an older failed refresh must not invalidate a newer successful report');
    assert.match(h.$('[data-c-progress]').textContent,/Анализ завершён/);
    assert.doesNotMatch(h.$('[data-c-feedback]').textContent,/освобождён|удалён|obsolete/);
  }],
  ['obsolete_detail_error',async h=>{
    await h.window.AIOGuardComparison.open(job);const delayed=gate();let used=false;
    h.override(url=>{if(url.pathname.endsWith('/compare/item')&&!used){used=true;return delayed.promise}});
    h.event(h.$('[data-task]'),'dblclick');await flush();
    h.event(h.$('[data-task]'),'dblclick');await flush();
    assert.match(h.$('[data-c-preview]').textContent,/RESULT/);
    delayed.resolve(h.response({code:'COMPARISON_TASK',error:'obsolete detail failure'},404));await flush();
    assert.doesNotMatch(h.$('[data-c-feedback]').textContent,/obsolete detail failure/,'late detail errors must obey the selected detail request identity');
  }],
  ['retry_completion_different_job',async h=>{
    await h.window.AIOGuardComparison.open(job);await h.select();const delayed=gate();
    h.override(url=>url.pathname.endsWith('/compare/retry')?delayed.promise:undefined);
    h.click('[data-c-retry]');await flush();
    await h.window.AIOGuardComparison.open(other);await h.select();
    assert.equal(h.$('[data-c-selected]').textContent,'Выбрано: 1');
    delayed.resolve(h.response({state:'completed',jobId:child}));await flush();
    // Trigger a harmless refresh to render the actual in-memory selection.
    h.click('[data-c-refresh]');await flush();
    assert.equal(h.$('[data-c-selected]').textContent,'Выбрано: 1','completion of another package must not erase this package selection');
    assert.equal(h.$('[data-c-history]').hasAttribute('open'),false,'a stale retry must not open the new package history');
  }],
  ['restart_completion_different_job',async h=>{
    await h.window.AIOGuardComparison.open(job);const delayed=gate();
    h.override((url,body)=>url.pathname.endsWith('/compare/start')&&body.restart?delayed.promise:undefined);
    h.click('[data-c-restart]');await flush();
    await h.window.AIOGuardComparison.open(other);h.event(h.$('[data-task]'),'dblclick');await flush();
    assert.match(h.$('[data-c-preview]').textContent,/RESULT/);assert.match(h.$('[data-c-findings]').textContent,/SECOND REPORT/);
    delayed.resolve(h.response({state:'running'}));await flush();
    assert.match(h.$('[data-c-preview]').textContent,/RESULT/,'an old restart must not clear a newer package preview');
    assert.match(h.$('[data-c-findings]').textContent,/SECOND REPORT/);
  }],
  ['detail_pair_binding',async h=>{
    await h.window.AIOGuardComparison.open(job);
    h.override(url=>url.pathname.endsWith('/compare/item')?h.response({...h.item(other),analysisId:'analysis-'+job,
      previews:{result:{kind:'text',text:'UNRELATED RESULT'}}}):undefined);
    h.event(h.$('[data-task]'),'dblclick');await flush();
    assert.doesNotMatch(h.$('[data-c-preview]').textContent,/UNRELATED RESULT/,'same analysis label does not prove the task/job binding');
    assert.match(h.$('[data-c-feedback]').textContent,/другого|соответств/);
  }],
  ['obsolete_busy_preview',async h=>{
    await h.window.AIOGuardComparison.open(job);const delayed=gate();
    h.override(url=>{
      if(url.pathname.endsWith('/compare/item'))return h.response({...h.item(job),previews:{
        source:{kind:'text',text:'CURRENT SOURCE'},result:{kind:'text',url:'/api/converter-guard/compare/preview?job='+job+'&task='+job+':0&side=result'}}});
      if(url.pathname.endsWith('/compare/preview'))return {ok:false,status:409,json:()=>delayed.promise};
    });
    h.event(h.$('[data-task]'),'dblclick');await flush();
    h.click('[data-c-source]');await flush();assert.match(h.$('[data-c-preview]').textContent,/CURRENT SOURCE/);
    delayed.resolve({code:'COMPARISON_PREVIEW_BUSY'});await flush();
    assert.match(h.$('[data-c-preview]').textContent,/CURRENT SOURCE/,'a late busy reply must not replace the newer source preview with a waiting message');
  }],
  ['preview_analysis_binding',async h=>{
    await h.window.AIOGuardComparison.open(job);
    h.override(url=>url.pathname.endsWith('/compare/item')?h.response({...h.item(job),previews:{result:{kind:'text',
      url:'/api/converter-guard/compare/preview?job='+job+'&task='+job+':0&side=result&analysisId=obsolete'}}}):undefined);
    h.event(h.$('[data-task]'),'dblclick');await flush();
    assert.equal(h.calls.filter(row=>row.url.pathname.endsWith('/compare/preview')).length,0,'a stale analysis token cannot be used for the current report');
    assert.match(h.$('[data-c-preview]').textContent,/проверку/);
  }],
  ['bound_preview_token_preserved',async h=>{
    await h.window.AIOGuardComparison.open(job);
    h.override(url=>url.pathname.endsWith('/compare/item')?h.response({...h.item(job),previews:{result:{kind:'text',
      url:'/api/converter-guard/compare/preview?job='+job+'&task='+job+':0&side=result&analysisId=analysis-'+job}}}):undefined);
    h.event(h.$('[data-task]'),'dblclick');await flush();
    const request=h.calls.find(row=>row.url.pathname.endsWith('/compare/preview'));
    assert.equal(request?.url.searchParams.get('analysisId'),'analysis-'+job,'backend generation binding must survive output selection');
    assert.match(h.$('[data-c-preview]').textContent,/NETWORK PREVIEW/);
  }],
  ['unavailable_bundle_scope',async h=>{
    await h.window.AIOGuardComparison.open(job);
    h.override(url=>url.pathname.endsWith('/compare/item')?h.response({...h.item(job),status:'not_checked',
      findings:[{category:'not_checked',message:'Часть результата недоступна.'}],recipeCandidates:[],
      outputScope:{totalFiles:3,unavailableFiles:1,inspectedPrimaryFiles:0,inspectedSupplementalFiles:0}}):undefined);
    h.event(h.$('[data-task]'),'dblclick');await flush();
    assert.match(h.$('[data-c-findings]').textContent,/Выходных файлов: 3/);
    assert.match(h.$('[data-c-findings]').textContent,/Недоступно: 1/,'missing-artifact evidence must remain explicit');
    assert.doesNotMatch(h.$('[data-c-findings]').textContent,/0 из 0/,'an unclassified bundle must not display invented zero denominators');
  }],
  ['preview_pair_binding',async h=>{
    await h.window.AIOGuardComparison.open(job);
    h.override(url=>url.pathname.endsWith('/compare/item')?h.response({...h.item(job),previews:{result:{kind:'text',
      url:'/api/converter-guard/compare/preview?job='+other+'&task='+other+':0&side=result'}}}):undefined);
    h.event(h.$('[data-task]'),'dblclick');await flush();
    assert.equal(h.calls.filter(row=>row.url.pathname.endsWith('/compare/preview')).length,0,'a same-origin preview for another task must not be fetched');
    assert.match(h.$('[data-c-preview]').textContent,/проверку/);
  }],
];
for(const [name,work]of cases)if(!only||only===name)await check(name,work);
console.log(JSON.stringify({status:'PASS',scope:'production comparison UI with real DOM and reordered HTTP completions',cases:results}));
