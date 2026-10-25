// Execute production UI against a real DOM with a controlled local API.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
import vm from 'node:vm';

if(!process.env.AIO_DOM_MODULE)throw Error('AIO_DOM_MODULE must point to the pinned offline linkedom ESM entry');
const {parseHTML}=await import(pathToFileURL(process.env.AIO_DOM_MODULE).href);
const {window,document}=parseHTML('<html><body data-aio-token="unit-token"><div class="aio-cv"><div class="aio-cv__footer"><button id="save">Сохранить результат</button></div></div></body></html>');
const job='a'.repeat(32),child='b'.repeat(32),otherChild='c'.repeat(32),grandchild='d'.repeat(32),oldChild='e'.repeat(32),calls=[],saved=[],created=[],revoked=[];
window.location={href:'http://127.0.0.1:5123/',origin:'http://127.0.0.1:5123'};
window.AllInOneNativeShell=true;
window.AllInOneBridge={converter:{async saveRetained(id,mode){saved.push([id,mode]);return {saved:true};}}};
window.HTMLElement.prototype.showModal=function(){this.open=true;this.setAttribute('open','');};
window.HTMLElement.prototype.close=function(){
  if(!this.open)return;
  this.open=false;this.removeAttribute('open');
  // Native HTMLDialog queues close after flipping open, rather than invoking
  // its listener in the close() call stack.
  setTimeout(()=>this.dispatchEvent(new window.Event('close')),0);
};
window.HTMLElement.prototype.focus=function(){this.focused=true;};
window.HTMLElement.prototype.getBoundingClientRect=function(){return {left:10,top:10,right:900,bottom:700};};
// linkedom intentionally omits the browser select.value setter and dialog UI.
Object.defineProperty(window.HTMLSelectElement.prototype,'value',{configurable:true,
  get(){return this.querySelector('option[selected]')?.value||this.querySelector('option')?.value||'';},
  set(value){for(const option of this.querySelectorAll('option')){if(option.value===value)option.setAttribute('selected','');else option.removeAttribute('selected');}}});
class URLs extends URL {
  static createObjectURL(blob){assert.ok(blob instanceof Blob);const value='blob:unit/'+created.length;created.push(value);return value;}
  static revokeObjectURL(url){revoked.push(url);}
}
const headers=(mime='application/json',size=0,extra={})=>({get(name){return ({'Content-Type':mime,'Content-Length':String(size),...extra})[name]||null;}});
const response=value=>({ok:true,status:200,headers:headers(),async json(){return value;}});
let phase='completed',retry=null,holdPreview=null,previewWrongOrigin=false,previewMime='image/png',previewKind='image';
let history=[],multiOutput=false,missingJob=false,generation=0,holdDetail=null,failedPolls=0,previewBusyResponses=0,finalizing=false,missingFirst=false,missingOriginal=false,emptyReport=false,missingReport=null;
const parents={[child]:job,[otherChild]:job,[grandchild]:child};
const errorResponse=(code,status=404)=>({ok:false,status,headers:headers(),async json(){return {error:code,code};}});
function makeAttempt(runId,children,state='completed'){
  return {runId,analysisId:'analysis-'+job,state,jobId:children.at(-1)?.jobId,jobIds:children.map(row=>row.jobId),children};
}
const rows=(id)=>Array.from({length:1000},(_,i)=>({taskId:id+':'+i,fileId:'f'+i,
  name:i===0?'<img src=x onerror="unsafe">.docx':'Документ '+i+'.docx',format:'pdf',
  status:missingOriginal&&i===0?'not_checked':['confirmed_loss','format_limitation','uncertain','not_checked','checked'][i%5],
  sourceAvailable:!(missingOriginal&&i===0),recipeCandidates:!(missingOriginal&&i===0)&&i%10===0?['pdf_preserve_images']:[],
  findings:[{category:i===0?'confirmed_loss':'uncertain',message:i===0?'<script>untrusted</script>':'Проверена доступная часть.'},
    ...(i===0?[{code:'CELL_VALUES_CHANGED',category:'confirmed_loss',message:'Исходные значения отсутствуют в результате.',
      before:30,after:null,totalAffected:30,locations:Array.from({length:30},(_,j)=>({row:j+2,column:2,before:j===0?'001':'<script>значение</script>',after:j===0?'0':''}))},
      {category:'not_checked',message:'Неопределённое свойство',before:null,after:null}]:[])],
  unknown:['colour_accuracy','fonts','formula_evaluation','semantic_meaning','visual_layout'] }));
function packet(id,url){
  let items=emptyReport?[]:rows(id);const f=url.searchParams.get('filter'),q=url.searchParams.get('q');
  if(f&&f!=='all')items=items.filter(row=>row.status===f);
  if(q)items=items.filter(row=>(row.name+' '+row.format).includes(q));
  const offset=Number(url.searchParams.get('offset'))||0;
  let parent=parents[id],ancestors=[];for(let p=parent;p;p=parents[p])ancestors.unshift({jobId:p,label:p===job?'Исходный пакет':'Предыдущий повтор',available:true});
  return {jobId:id,analysisId:'analysis-'+id+(generation?':'+generation:''),state:phase,canRestart:true,canResume:['cancelled','interrupted'].includes(phase),processed:emptyReport?0:1000,total:emptyReport?0:1000,totalFiltered:items.length,
    canRetry:!finalizing&&phase==='completed',finalizing,navigation:{parentJobId:parent,ancestors},retryHistory:id===job?history.slice(0,20):[],historyTotal:id===job?history.length:0,
    summary:{confirmed_loss:200,format_limitation:200,uncertain:200,not_checked:200,checked:200},items:items.slice(offset,offset+50),retry};
}
async function fetchMock(path,options={}){
  const url=new URL(path,window.location.href);const body=options.body?JSON.parse(options.body):null;
  assert.equal(options.headers['X-AIO-Token'],'unit-token');
  assert.equal(url.searchParams.has('token'),false);
  calls.push({path:url.pathname,query:Object.fromEntries(url.searchParams),body,signal:options.signal});
  if(url.pathname.endsWith('/compare/start')){
    if(missingJob)return errorResponse('JOB_NOT_FOUND');
    if(missingReport&&!body.restart)return errorResponse(missingReport);
    if(body.restart)missingReport=null;
    if(body.analysisId&&body.analysisId!=='analysis-'+body.jobId+(generation?':'+generation:''))return errorResponse('COMPARISON_STALE',409);
    if(body.restart){phase='running';generation++;}
    return response({jobId:body.jobId,state:phase});
  }
  if(url.pathname.endsWith('/compare/retry-history')){
    if(missingJob)return errorResponse('JOB_NOT_FOUND');
    const offset=Number(url.searchParams.get('offset'))||0,limit=Number(url.searchParams.get('limit'))||20;
    if(url.searchParams.has('runId'))return response({offset,limit,total:2,items:[{name:'file.xlsx',format:'csv',assessment:'checked_subset_improved'},{name:'picture.docx',format:'pdf',assessment:'not_comparable'}].slice(offset,offset+limit)});
    return response({offset,limit,total:history.length,items:history.slice(offset,offset+limit)});
  }
  if(url.pathname.endsWith('/compare/control')){
    if(body.analysisId&&body.analysisId!=='analysis-'+body.jobId+(generation?':'+generation:''))return errorResponse('COMPARISON_STALE',409);
    if(body.action==='cancel'&&retry&&!['completed','ready','failed','cancelled'].includes(retry.state))retry.state='cancelled';
    else phase=body.action==='pause'?'paused':body.action==='resume'?'running':'cancelled';
    return response({state:phase});
  }
  if(url.pathname.endsWith('/compare/retry')){
    if(phase!=='completed')return errorResponse('COMPARISON_STALE',409);
    retry=makeAttempt('r'+(history.length+1),[
      {jobId:child,state:'completed',available:true,recipe:'csv_preserve_values',verification:{assessment:'checked_subset_improved'}},
      {jobId:otherChild,state:'completed',available:true,recipe:'pdf_preserve_images',verification:{assessment:'no_confirmed_improvement'}}]);
    history.unshift(retry);return response(retry);
  }
  if(url.pathname.endsWith('/compare/item')){
    const id=url.searchParams.get('job'),task=url.searchParams.get('task');
    const row=rows(id).find(r=>r.taskId===task);assert.ok(row);
    if(holdDetail)return await holdDetail;
    const outputs=multiOutput?Array.from({length:102},(_,i)=>({outputIndex:i,kind:missingFirst&&i===0?'unavailable':i===0?'pdf':'text',name:i===0?'first.pdf':'sheet-'+i+'.csv',available:!(missingFirst&&i===0),url:'/api/converter-guard/compare/preview?job='+id+'&task='+encodeURIComponent(task)+'&side=result&outputIndex='+i})):null;
    return response({...row,...(outputs?{outputPreviews:outputs}:{}),previews:{
      source:missingOriginal?{kind:'unavailable',note:'Служебный исходник недоступен.'}:{kind:'text',name:row.name,text:'<script>source text</script>',note:'Текстовое представление; верстка не показана.'},
      result:{kind:previewKind,name:previewKind==='pdf'?'result.pdf':'result.png',url:previewWrongOrigin?'https://invalid.example/steal':'/api/converter-guard/compare/preview?job='+id+'&task='+encodeURIComponent(task)+'&side=result'}}});
  }
  if(url.pathname.endsWith('/compare/preview')){
    if(previewBusyResponses-->0)return errorResponse('COMPARISON_PREVIEW_BUSY',409);
    if(url.searchParams.get('outputIndex')==='1')return response({kind:'text',text:'SECOND_SHEET',note:'Сопровождающий файл не проверен.'});
    if(url.searchParams.get('outputIndex')==='100')return response({kind:'text',text:'OUTPUT_100'});
    if(holdPreview)return await holdPreview;
    return {ok:true,status:200,headers:headers(previewMime,8,previewKind==='pdf'?{
      'X-AIO-Preview-Pages':'3','X-AIO-Preview-Page':url.searchParams.get('page')||'0'}:{}),
      async blob(){return new Blob(['fixture'],{type:previewMime});}};
  }
  if(url.pathname.endsWith('/compare')){
    if(missingJob)return errorResponse('JOB_NOT_FOUND');
    if(missingReport)return errorResponse(missingReport);
    if(failedPolls-->0)return errorResponse('TRANSIENT',503);
    return response(packet(url.searchParams.get('job'),url));
  }
  throw Error('Unexpected operation: '+url.pathname);
}
const context={window,document,MutationObserver:window.MutationObserver,fetch:fetchMock,URL:URLs,Blob,AbortController,
  setTimeout,clearTimeout,console};
vm.runInNewContext(readFileSync(new URL('../src/guard-comparison.js',import.meta.url),'utf8'),context);
const flush=async()=>{for(let i=0;i<4;i++)await new Promise(resolve=>setTimeout(resolve,0));};
const click=node=>{assert.ok(node);node.dispatchEvent(new window.Event('click',{bubbles:true}));};
const emit=(node,type)=>node.dispatchEvent(new window.Event(type,{bubbles:true,cancelable:true}));
const state=detail=>window.dispatchEvent(new window.CustomEvent('aio-guard-state',{detail}));

assert.equal(calls.length,0,'mounting/selecting files must not start comparison');
const launch=document.querySelector('[data-aio-compare-launch]');assert.ok(launch);assert.equal(launch.hidden,true);
state({jobId:job,finished:false,succeeded:0});assert.equal(launch.hidden,true);assert.equal(calls.length,0);
state({jobId:job,finished:true,succeeded:1000,result:{ticket:'retained'},comparisonAvailable:true});
assert.equal(launch.hidden,false);assert.equal(document.querySelector('#save').disabled,false);
assert.equal(calls.length,0,'completion only offers the optional button');
click(launch);await flush();
const dialog=document.querySelector('.aio-compare-dialog'),$=selector=>dialog.querySelector(selector);
assert.equal(dialog.open,true,$('[data-c-feedback]').textContent);assert.equal(calls.filter(r=>r.path.endsWith('/compare/start')).length,1);
assert.equal($('[data-c-files]').children.length,50,'1000 inputs render only one page');
assert.equal($('[data-c-files]').querySelectorAll('img,script').length,0,'untrusted filenames are literal text');
assert.equal($('[data-c-selected]').textContent,'Выбрано: 0');
assert.equal($('[data-c-retry]').disabled,true);assert.equal($('[data-c-pause]').disabled,true);
assert.equal($('[data-c-files]').children[0].dataset.status,'confirmed_loss');
assert.equal($('[data-c-files]').children[1].dataset.status,'format_limitation');
click($('[data-task]'));assert.equal($('[data-c-findings]').querySelectorAll('script').length,0);
assert.equal($('[data-c-files]').children[0].dataset.active,'true');
assert.equal($('[data-c-files]').children[0].dataset.status,'confirmed_loss','active row keeps its problem colour');
assert.equal($('[data-c-findings]').querySelector('.aio-compare-cell-details').hasAttribute('open'),false);
assert.equal($('[data-c-findings]').querySelectorAll('.aio-compare-cell-details li').length,25);
assert.equal($('[data-c-findings]').querySelector('.aio-compare-cell-details strong').textContent,'B2');
assert.match($('[data-c-findings]').textContent,/B2: 001 → 0/);
assert.match($('[data-c-findings]').textContent,/не определено/);
assert.doesNotMatch($('[data-c-findings]').textContent,/null/);
const css=readFileSync(new URL('../src/guard-comparison.css',import.meta.url),'utf8');
const activeRule=css.match(/\.aio-compare-row\[data-active="true"\]\{([^}]*)\}/)[1];
assert.doesNotMatch(activeRule,/--accent|border-color|box-shadow:(?!none)/,'selection does not paint over red/yellow outlines');
assert.match($('[data-c-findings]').textContent,/точность цветов/);
assert.match($('[data-c-findings]').textContent,/смысл содержимого/);
assert.doesNotMatch($('[data-c-findings]').textContent,/colour_accuracy|formula_evaluation|semantic_meaning|visual_layout/);
assert.equal(calls.some(r=>r.path.endsWith('/compare/preview')),false,'single click does not load a document');
emit($('[data-task]'),'dblclick');await flush();
assert.ok($('[data-c-preview] img'));assert.equal(created.length,1);
click($('[data-c-source]'));await flush();
assert.equal($('[data-c-preview] pre').textContent,'<script>source text</script>');
assert.equal($('[data-c-preview]').querySelectorAll('script').length,0);
assert.ok(revoked.includes(created[0]),'switching representations revokes the image URL');
click($('[data-c-result]'));await flush();assert.equal(created.length,2);

click($('[data-c-select-all]'));await flush();
assert.equal($('[data-c-selected]').textContent,'Выбрано: 100');
assert.match($('[data-c-retry]').textContent,/100/);
assert.equal($('[data-c-retry]').disabled,false);
const bulkPages=calls.filter(r=>r.path.endsWith('/compare')&&r.query.offset!=='0');assert.equal(bulkPages.length,19);
click($('[data-c-next]'));await flush();assert.match($('[data-c-page]').textContent,/2 \/ 20/);
assert.equal($('[data-c-files]').children.length,50);assert.equal($('[data-c-selected]').textContent,'Выбрано: 100');
click($('[data-c-retry]'));await flush();
const attempt=calls.find(r=>r.path.endsWith('/compare/retry'));
assert.equal(attempt.body.jobId,job);assert.equal(attempt.body.analysisId,'analysis-'+job);
assert.equal(attempt.body.taskIds.length,100);assert.equal(new Set(attempt.body.taskIds).size,100);
assert.equal($('[data-c-selected]').textContent,'Выбрано: 0');
assert.equal($('[data-c-show-retry]').disabled,false);
assert.match($('[data-c-retry-verdict]').textContent,/улучшение не подтверждено/);
assert.equal($('[data-c-retry-job]').querySelectorAll('option').length,2,'all mixed-recipe candidate jobs remain reachable');
$('[data-c-retry-job]').value=child;emit($('[data-c-retry-job]'),'change');
assert.match($('[data-c-retry-verdict]').textContent,/улучшились проверенные свойства/,'verdict follows selected child, not the other recipe');
click($('[data-c-save-retry]'));await flush();assert.deepEqual(saved,[[child,'folder']]);
click($('[data-c-show-retry]'));await flush();assert.equal(calls.filter(r=>r.path.endsWith('/compare/start')).at(-1).body.jobId,child);
assert.equal($('[data-c-back]').hidden,false);click($('[data-c-back]'));await flush();
assert.equal(calls.filter(r=>r.path.endsWith('/compare/start')).at(-1).body.jobId,job);

// A deferred preview for an old selection must never overwrite the next one.
let release;
holdPreview=new Promise(resolve=>{release=resolve;});
emit($('[data-task]'),'dblclick');await flush();
click($('[data-c-source]'));await flush();
release({ok:true,headers:headers('image/png',2),async blob(){return new Blob(['old']);}});holdPreview=null;await flush();
assert.ok($('[data-c-preview] pre'),'late output does not replace selected source');
previewWrongOrigin=true;emit($('[data-task]'),'dblclick');await flush();click($('[data-c-result]'));await flush();
assert.match($('[data-c-preview]').textContent,/проверку/);assert.equal(calls.some(r=>r.path.includes('steal')),false);
previewWrongOrigin=false;
previewKind='pdf';previewMime='image/png';await window.AIOGuardComparison.open(job);emit($('[data-task]'),'dblclick');await flush();
assert.ok($('[data-c-preview] img'),'PDF shows one native-rendered PNG page');
assert.equal($('[data-c-preview] iframe'),null,'no active PDF browser plugin or document script');
assert.equal($('[data-c-pdf-controls]').hidden,false);
assert.equal($('[data-c-pdf-page]').textContent,'1 / 3');
assert.equal($('[data-c-pdf-prev]').disabled,true);assert.equal($('[data-c-pdf-next]').disabled,false);
assert.equal(calls.filter(r=>r.path.endsWith('/compare/preview')).at(-1).query.page,'0');
click($('[data-c-pdf-next]'));await flush();
assert.equal(calls.filter(r=>r.path.endsWith('/compare/preview')).at(-1).query.page,'1');
assert.equal($('[data-c-pdf-page]').textContent,'2 / 3');
click($('[data-c-source]'));await flush();assert.equal($('[data-c-pdf-controls]').hidden,true);
click($('[data-c-result]'));await flush();assert.equal($('[data-c-pdf-page]').textContent,'2 / 3');
emit($('[data-c-files]').children[1].querySelector('[data-task]'),'dblclick');await flush();
assert.equal(calls.filter(r=>r.path.endsWith('/compare/preview')).at(-1).query.page,'0','each new task starts at its first PDF page');
assert.equal($('[data-c-pdf-page]').textContent,'1 / 3');

// A large automatic retry gets its own honest progress, not a stale "done".
for(const [state,label] of [['queued',/Готовим повторную/],['preparing',/Подготавливаем исходники/],
    ['running',/Повторная конвертация текущего пакета · 17 из 100/],['analyzing',/Проверяем повторный/],
    ['failed',/не завершена/],['cancelled',/остановлена/]]){
  phase='completed';retry={state,jobId:child,jobIds:[child],completed:17,total:100,
    error:state==='failed'?'Проверяемая причина отказа':null};
  await window.AIOGuardComparison.open(job);await flush();
  assert.match($('[data-c-progress]').textContent,label);
  if(state==='running'){
    assert.equal($('progress').max,100);assert.equal($('progress').value,17);
    assert.equal($('[data-c-cancel]').disabled,false);
    assert.equal($('[data-c-cancel]').textContent,'Остановить повтор');
    click($('[data-c-cancel]'));await flush();assert.match($('[data-c-progress]').textContent,/остановлена/);
  }
  if(state==='failed')assert.match($('[data-c-feedback]').textContent,/Проверяемая причина отказа/);
}

// Pause/cancel controls belong to analysis, not the conversion job.
retry=null;phase='running';await window.AIOGuardComparison.open(job);await flush();
click($('[data-c-pause]'));await flush();assert.equal($('[data-c-pause]').textContent,'Продолжить');
click($('[data-c-pause]'));await flush();assert.equal($('[data-c-pause]').textContent,'Пауза');
click($('[data-c-cancel]'));await flush();assert.match($('[data-c-progress]').textContent,/остановлен/);
assert.equal(calls.some(r=>r.path==='/api/converter-guard/cancel'),false);
emit(dialog,'cancel');await flush();assert.equal(dialog.open,false);
assert.equal(launch.getAttribute('aria-expanded'),'false');
assert.equal(new Set(created).size,new Set(revoked).size,'close releases every selected Blob');
assert.equal(document.querySelector('#save').disabled,false,'analysis never disables ordinary saving');
// Cancel is a frozen report; only explicit resume/restart can run another scan.
phase='cancelled';retry=null;history=[];await window.AIOGuardComparison.open(job);await flush();
const beforeResumeStarts=calls.filter(row=>row.path.endsWith('/compare/start')).length;
assert.equal(phase,'cancelled','opening a cancelled report never resumes automatically');
assert.equal($('[data-c-resume]').hidden,false);
const marked=$('[data-c-files] input');marked.checked=true;emit(marked,'change');
assert.equal($('[data-c-retry]').disabled,true,'cancelled partial selection cannot call completed-only retry');
click($('[data-c-resume]'));await flush();assert.equal(phase,'running');
assert.equal(calls.filter(row=>row.path.endsWith('/compare/start')).length,beforeResumeStarts);
click($('[data-c-cancel]'));await flush();
click($('[data-c-restart]'));await flush();
assert.equal(calls.filter(row=>row.path.endsWith('/compare/start')).at(-1).body.restart,true);
assert.equal($('[data-c-selected]').textContent,'Выбрано: 0');

// Every output is reachable, PDF page state belongs to each output, and option DOM is bounded.
phase='completed';multiOutput=true;previewKind='pdf';previewMime='image/png';
await window.AIOGuardComparison.open(job);emit($('[data-task]'),'dblclick');await flush();
assert.equal($('[data-c-output-controls]').hidden,false);
assert.equal($('[data-c-output]').querySelectorAll('option').length,100);
previewBusyResponses=2;
$('[data-c-output]').value='1';emit($('[data-c-output]'),'change');await new Promise(resolve=>setTimeout(resolve,500));await flush();
assert.equal($('[data-c-preview] pre').textContent,'SECOND_SHEET','a quick preview change waits for the old worker without showing a permanent busy error');
assert.equal($('[data-c-pdf-controls]').hidden,true);
$('[data-c-output]').value='0';emit($('[data-c-output]'),'change');await flush();
click($('[data-c-pdf-next]'));await flush();assert.equal($('[data-c-pdf-page]').textContent,'2 / 3');
$('[data-c-output]').value='1';emit($('[data-c-output]'),'change');await flush();
$('[data-c-output]').value='0';emit($('[data-c-output]'),'change');await flush();
assert.equal($('[data-c-pdf-page]').textContent,'2 / 3','each PDF output retains its own page');
click($('[data-c-output-next]'));await flush();assert.equal($('[data-c-output]').querySelectorAll('option').length,2);
assert.equal($('[data-c-preview] pre').textContent,'OUTPUT_100');
click($('[data-c-output-prev]'));await flush();assert.equal($('[data-c-pdf-page]').textContent,'2 / 3');

// A missing first artifact does not hide later outputs, while explicitly selecting it explains the limitation.
missingFirst=true;emit($('[data-c-files]').children[1].querySelector('[data-task]'),'dblclick');await flush();
assert.equal($('[data-c-output]').value,'1');assert.equal($('[data-c-preview] pre').textContent,'SECOND_SHEET');
$('[data-c-output]').value='0';emit($('[data-c-output]'),'change');await flush();
assert.equal($('[data-c-preview] pre'),null);assert.match($('[data-c-preview]').textContent,/недоступен/);
missingFirst=false;

// Root -> child -> grandchild -> back -> back preserves the complete route and root view.
$('[data-c-filter]').value='confirmed_loss';emit($('[data-c-filter]'),'change');await flush();
click($('[data-c-next]'));await flush();assert.match($('[data-c-page]').textContent,/2 \/ 4/);
const selectedOnPage=$('[data-c-files] input');selectedOnPage.checked=true;emit(selectedOnPage,'change');
const rootCount=$('[data-c-selected]').textContent;
await window.AIOGuardComparison.open(child,{parent:job});await flush();
await window.AIOGuardComparison.open(grandchild,{parent:child});await flush();
assert.equal($('[data-c-breadcrumbs]').querySelectorAll('button').length,2);
click($('[data-c-back]'));await flush();assert.equal(calls.filter(row=>row.path.endsWith('/compare/start')).at(-1).body.jobId,child);
click($('[data-c-back]'));await flush();assert.equal(calls.filter(row=>row.path.endsWith('/compare/start')).at(-1).body.jobId,job);
assert.equal($('[data-c-filter]').value,'confirmed_loss');assert.match($('[data-c-page]').textContent,/2 \/ 4/);
assert.equal($('[data-c-selected]').textContent,rootCount);

// History retains older attempts; an overall failed run can still expose a valid partial child.
history=[makeAttempt('new',[{jobId:otherChild,state:'completed',available:true,verification:{assessment:'not_comparable'}}]),
  makeAttempt('old',[{jobId:oldChild,state:'partial',available:true,verification:{assessment:'checked_subset_improved'}}],'failed')];retry=history[0];
await window.AIOGuardComparison.open(job);await flush();
assert.equal($('[data-c-attempt]').querySelectorAll('option').length,2);
$('[data-c-attempt]').value='old';emit($('[data-c-attempt]'),'change');
assert.equal($('[data-c-retry-job]').value,oldChild);
assert.equal($('[data-c-show-retry]').disabled,false,'completed partial child remains accessible after failed overall run');
assert.match($('[data-c-retry-verdict]').textContent,/улучшились проверенные свойства/);
click($('[data-c-save-retry]'));await flush();assert.deepEqual(saved.at(-1),[oldChild,'folder']);
$('[data-c-attempt]').value='new';emit($('[data-c-attempt]'),'change');
assert.match($('[data-c-retry-verdict]').textContent,/нельзя надёжно сопоставить/,'no improvement from the other attempt is reused');
history[0].children[0].available=false;click($('[data-c-refresh]'));await flush();
assert.equal($('[data-c-show-retry]').disabled,true,'released child cannot be opened or saved');
assert.match($('[data-c-retry-verdict]').textContent,/освобождён/);
history=Array.from({length:25},(_,i)=>makeAttempt('run-'+i,[{jobId:oldChild,state:'completed',available:true,verification:{assessment:'no_confirmed_improvement'}}]));retry=history[0];
await window.AIOGuardComparison.open(job);await flush();
assert.equal($('[data-c-attempt]').querySelectorAll('option').length,20);
click($('[data-c-history-next]'));await flush();assert.equal($('[data-c-attempt]').querySelectorAll('option').length,5);
assert.match($('[data-c-history-page]').textContent,/21–25 \/ 25/);
click($('[data-c-refresh]'));await flush();assert.equal($('[data-c-attempt]').querySelectorAll('option').length,5,'polling does not replace an older history page');
click($('[data-c-history-prev]'));await flush();
$('[data-c-verification-details]').open=true;emit($('[data-c-verification-details]'),'toggle');await flush();
assert.equal($('[data-c-verification-list]').children.length,2);
assert.match($('[data-c-verification-list]').textContent,/file.xlsx/);

// Another window restarting this job invalidates selection and preview evidence; stale controls refresh rather than act.
phase='running';history=[];retry=null;await window.AIOGuardComparison.open(job);await flush();
const staleAnalysisId='analysis-'+job+':'+generation;generation++;
click($('[data-c-pause]'));await flush();
assert.equal(calls.filter(row=>row.path.endsWith('/compare/control')).at(-1).body.analysisId,staleAnalysisId);
assert.match($('[data-c-feedback]').textContent,/COMPARISON_STALE/);
assert.equal($('[data-c-selected]').textContent,'Выбрано: 0');
phase='completed';click($('[data-c-refresh]'));await flush();
const proofCheck=$('[data-c-files] input');proofCheck.checked=true;emit(proofCheck,'change');
finalizing=true;click($('[data-c-refresh]'));await flush();assert.equal($('[data-c-retry]').disabled,true,'terminal state alone does not enable work while its lease/future finalizes');
finalizing=false;click($('[data-c-refresh]'));await flush();assert.equal($('[data-c-retry]').disabled,false);

// Saving cancellation leaves the current view usable. Explicit release aborts previews and polling.
emit($('[data-task]'),'dblclick');await flush();
window.AllInOneBridge.converter.saveRetained=async()=>({saved:false});
click($('[data-c-save-current]'));await flush();assert.match($('[data-c-feedback]').textContent,/Сохранение отменено/);
assert.equal($('[data-c-source]').disabled,false);
window.dispatchEvent(new window.CustomEvent('aio-guard-job-released',{detail:{jobId:job,discarded:true}}));await flush();
assert.equal($('[data-c-retry]').disabled,true);assert.equal($('[data-c-source]').disabled,true);
assert.match($('[data-c-feedback]').textContent,/удалён/);
assert.equal(document.querySelector('[data-aio-compare-launch]').hidden,true);
const stoppedAt=calls.length;await new Promise(resolve=>setTimeout(resolve,1100));assert.equal(calls.length,stoppedAt,'released context does not poll forever');
window.AIOGuardComparison.close();await flush();assert.equal($('[data-c-files]').children.length,0,'closing drops row/report event references');
missingJob=true;await window.AIOGuardComparison.open(job);await flush();
assert.match($('[data-c-feedback]').textContent,/освобождён|удалён/);
assert.equal($('[data-c-restart]').disabled,true,'a deleted Guard package cannot be restarted');
const missingAt=calls.length;await new Promise(resolve=>setTimeout(resolve,1100));assert.equal(calls.length,missingAt,'404 is terminal until the user selects another package');
window.AIOGuardComparison.close();missingJob=false;
assert.equal(new Set(created).size,new Set(revoked).size);
// A corrupt/missing report is not a deleted Guard package. First open remains read-only until explicit restart.
for(const code of ['COMPARISON_REPORT_UNAVAILABLE','COMPARISON_NOT_STARTED']){
  missingReport=code;phase='completed';const before=calls.length;
  await window.AIOGuardComparison.open(job);await flush();
  assert.match($('[data-c-feedback]').textContent,/Отчёт анализа недоступен/);
  for(const selector of ['[data-c-progress]','[data-c-summary]','[data-c-files]','[data-c-findings]','[data-c-preview]']){
    assert.match($(selector).textContent,/Отчёт недоступен/);
    assert.doesNotMatch($(selector).textContent,/нет проверяемых пар|нет подтверждённой связи|исходник.*недоступ/i,'a corrupt report does not imply missing task mapping or originals');
  }
  assert.equal($('[data-c-restart]').disabled,false,'existing Guard outputs permit an explicit new analysis');
  assert.equal($('[data-c-save-current]').disabled,false,'report failure leaves retained results savable');
  assert.equal($('[data-c-retry]').disabled,true);
  assert.equal(calls.length,before+1,'opening a corrupt report does not rescan or poll');
  assert.equal(calls.at(-1).body.restart,undefined);
  click($('[data-c-refresh]'));await flush();
  assert.equal(calls.filter(row=>row.path.endsWith('/compare/start')&&row.body.restart).length,
    calls.slice(0,before).filter(row=>row.path.endsWith('/compare/start')&&row.body.restart).length,'refresh must not silently restart');
  click($('[data-c-restart]'));await flush();
  assert.equal(missingReport,null);assert.equal(phase,'running');
  assert.equal(calls.filter(row=>row.path.endsWith('/compare/start')).at(-1).body.restart,true);
  window.AIOGuardComparison.close();
}
// Retained legacy outputs can still show an honest unavailable-source report; empty mappings never imply no loss.
missingOriginal=true;phase='completed';retry=null;history=[];multiOutput=false;
state({jobId:job,finished:true,succeeded:1000,result:{ticket:'retained'},comparisonAvailable:false});
assert.equal(launch.disabled,false);assert.match(launch.title,/исходники недоступны/);
click(launch);await flush();assert.equal($('[data-c-files] input').disabled,true);
emit($('[data-task]'),'dblclick');await flush();click($('[data-c-source]'));await flush();
assert.match($('[data-c-preview]').textContent,/исходник недоступен/);
window.AIOGuardComparison.close();missingOriginal=false;emptyReport=true;
await window.AIOGuardComparison.open(otherChild);await flush();
assert.match($('[data-c-summary]').textContent,/Нет проверяемых пар/);
assert.match($('[data-c-findings]').textContent,/Сравнение содержимого не выполнено/);
assert.equal($('[data-c-retry]').disabled,true);window.AIOGuardComparison.close();emptyReport=false;
// A queued close for an earlier opening must never abort or clear a newer one.
for(let round=0;round<5;round++){
  await window.AIOGuardComparison.open(job);await flush();
  window.AIOGuardComparison.close();
  const pending=window.AIOGuardComparison.open(round%2?child:otherChild);
  await flush();await pending;await flush();
  assert.equal(dialog.open,true);
  assert.equal($('[data-c-files]').children.length,50,'an earlier close cannot clear the reopened view');
  assert.match($('[data-c-progress]').textContent,/Анализ завершён/);
  assert.equal(calls.filter(row=>row.path.endsWith('/compare/start')).at(-1).signal.aborted,false,
    'an earlier close must not abort the new opening request');
}
// Closing through the native element also works before its queued event fires.
dialog.close();const nativeReopen=window.AIOGuardComparison.open(job);
await flush();await nativeReopen;await flush();
assert.equal($('[data-c-files]').children.length,50);
window.AIOGuardComparison.close();await flush();
console.log(JSON.stringify({status:'PASS',scope:'production comparison UI + real DOM; controlled HTTP/native-save',
  inputs:1000,pageRows:50,selectedAcrossPages:100,automaticAnalysesOnMount:0,
  independentSave:true,escapedText:true,blobCleanup:true,stalePreviewRejected:true,
  recipeRetryPreservesParent:true,pauseResumeCancel:true,pdfNativePageControls:true,multipleOutputs:102,historyAttempts:25,perChildVerdicts:true,nestedNavigation:true,cancelResumeRestart:true,releasedPollingStopped:true,queuedCloseReopens:5}));
