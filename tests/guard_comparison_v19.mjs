// Execute production UI against a real DOM with a controlled local API.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
import vm from 'node:vm';

if(!process.env.AIO_DOM_MODULE)throw Error('AIO_DOM_MODULE must point to the pinned offline linkedom ESM entry');
const {parseHTML}=await import(pathToFileURL(process.env.AIO_DOM_MODULE).href);
const {window,document}=parseHTML('<html><body data-aio-token="unit-token"><div class="aio-cv"><div class="aio-cv__footer"><button id="save">Сохранить результат</button></div></div></body></html>');
const job='a'.repeat(32),child='b'.repeat(32),otherChild='c'.repeat(32),calls=[],saved=[],created=[],revoked=[];
window.location={href:'http://127.0.0.1:5123/',origin:'http://127.0.0.1:5123'};
window.AllInOneNativeShell=true;
window.AllInOneBridge={converter:{async saveRetained(id,mode){saved.push([id,mode]);return {saved:true};}}};
window.HTMLElement.prototype.showModal=function(){this.open=true;this.setAttribute('open','');};
window.HTMLElement.prototype.close=function(){this.open=false;this.removeAttribute('open');this.dispatchEvent(new window.Event('close'));};
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
const rows=(id)=>Array.from({length:1000},(_,i)=>({taskId:id+':'+i,fileId:'f'+i,
  name:i===0?'<img src=x onerror="unsafe">.docx':'Документ '+i+'.docx',format:'pdf',
  status:['confirmed_loss','format_limitation','uncertain','not_checked','checked'][i%5],
  sourceAvailable:true,recipeCandidates:i%10===0?['pdf_preserve_images']:[],
  findings:[{category:i===0?'confirmed_loss':'uncertain',message:i===0?'<script>untrusted</script>':'Проверена доступная часть.'},
    ...(i===0?[{code:'CELL_VALUES_CHANGED',category:'confirmed_loss',message:'Исходные значения отсутствуют в результате.',
      before:30,after:null,totalAffected:30,locations:Array.from({length:30},(_,j)=>({row:j+2,column:2,before:j===0?'001':'<script>значение</script>',after:j===0?'0':''}))},
      {category:'not_checked',message:'Неопределённое свойство',before:null,after:null}]:[])],
  unknown:['colour_accuracy','fonts','formula_evaluation','semantic_meaning','visual_layout'] }));
function packet(id,url){
  let items=rows(id);const f=url.searchParams.get('filter'),q=url.searchParams.get('q');
  if(f&&f!=='all')items=items.filter(row=>row.status===f);
  if(q)items=items.filter(row=>(row.name+' '+row.format).includes(q));
  const offset=Number(url.searchParams.get('offset'))||0;
  return {jobId:id,analysisId:'analysis-'+id,state:phase,processed:1000,total:1000,totalFiltered:items.length,
    summary:{confirmed_loss:200,format_limitation:200,uncertain:200,not_checked:200,checked:200},items:items.slice(offset,offset+50),retry};
}
async function fetchMock(path,options={}){
  const url=new URL(path,window.location.href);const body=options.body?JSON.parse(options.body):null;
  assert.equal(options.headers['X-AIO-Token'],'unit-token');
  assert.equal(url.searchParams.has('token'),false);
  calls.push({path:url.pathname,query:Object.fromEntries(url.searchParams),body,signal:options.signal});
  if(url.pathname.endsWith('/compare/start'))return response({jobId:body.jobId,state:phase});
  if(url.pathname.endsWith('/compare/control')){
    if(body.action==='cancel'&&retry&&!['completed','ready','failed','cancelled'].includes(retry.state))retry.state='cancelled';
    else phase=body.action==='pause'?'paused':body.action==='resume'?'running':'cancelled';
    return response({state:phase});
  }
  if(url.pathname.endsWith('/compare/retry')){retry={state:'completed',jobId:otherChild,jobIds:[child,otherChild],verification:{assessment:'no_confirmed_improvement'}};return response(retry);}
  if(url.pathname.endsWith('/compare/item')){
    const id=url.searchParams.get('job'),task=url.searchParams.get('task');
    const row=rows(id).find(r=>r.taskId===task);assert.ok(row);
    return response({...row,previews:{
      source:{kind:'text',name:row.name,text:'<script>source text</script>',note:'Текстовое представление; верстка не показана.'},
      result:{kind:previewKind,name:previewKind==='pdf'?'result.pdf':'result.png',url:previewWrongOrigin?'https://invalid.example/steal':'/api/converter-guard/compare/preview?job='+id+'&task='+encodeURIComponent(task)+'&side=result'}}});
  }
  if(url.pathname.endsWith('/compare/preview')){
    if(holdPreview)return await holdPreview;
    return {ok:true,status:200,headers:headers(previewMime,8,previewKind==='pdf'?{
      'X-AIO-Preview-Pages':'3','X-AIO-Preview-Page':url.searchParams.get('page')||'0'}:{}),
      async blob(){return new Blob(['fixture'],{type:previewMime});}};
  }
  if(url.pathname.endsWith('/compare'))return response(packet(url.searchParams.get('job'),url));
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
$('[data-c-retry-job]').value=child;
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
console.log(JSON.stringify({status:'PASS',scope:'production comparison UI + real DOM; controlled HTTP/native-save',
  inputs:1000,pageRows:50,selectedAcrossPages:100,automaticAnalysesOnMount:0,
  independentSave:true,escapedText:true,blobCleanup:true,stalePreviewRejected:true,
  recipeRetryPreservesParent:true,pauseResumeCancel:true,pdfNativePageControls:true}));
