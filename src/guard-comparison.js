/* Optional, local before/after inspection. Nothing starts on file selection. */
(() => {
  'use strict';
  const prefix='/api/converter-guard';
  const labels=Object.freeze({confirmed_loss:'Подтверждённая потеря',format_limitation:'Ограничение формата',
    uncertain:'Требуется проверка',not_checked:'Не удалось проверить',checked:'Проверенная часть сохранена'});
  const phases=Object.freeze({queued:'Ожидание анализа',running:'Проверяем результаты',paused:'Анализ на паузе',
    cancelled:'Анализ остановлен',completed:'Анализ завершён',failed:'Не удалось завершить анализ'});
  const properties=Object.freeze({visual_layout:'внешний вид и расположение элементов',fonts:'шрифты',
    colour_accuracy:'точность цветов',formula_evaluation:'пересчёт формул',semantic_meaning:'смысл содержимого',
    rendered_pixels:'совпадение пикселей',formula_results:'результаты вычисления формул',formula_recalculation:'пересчёт формул',
    semantic_equivalence:'смысловая эквивалентность',normalized_text:'извлечённый текст',
    image_dimensions:'размеры изображения',frames:'кадры',pages:'страницы',alpha:'прозрачность',
    identical_file_bytes:'побайтовое совпадение',all_sheet_values_unordered:'значения всех листов',
    all_sheet_outputs_mapping:'соответствие файлов всем листам книги',spreadsheet_values:'значения ячеек'});
  const terminal=new Set(['completed','cancelled','failed']);
  let dialog=null,launch=null,launchRoot=null,lastState=null,jobId=null,analysis=null,selectedTask=null,detail=null;
  let side='result',offset=0,query='',filter='all',timer=null,controller=null,previewController=null,previewURL=null;
  let epoch=0,viewEpoch=0,requestNumber=0,previewNumber=0,busyAction=false,searchTimer=null;
  let opener=null,parentJob=null;
  let previewBusy=false;
  let pdfPages={source:0,result:0},pdfPageCounts={source:null,result:null};
  const selected=new Set();
  const $=selector=>dialog?.querySelector(selector);
  const token=()=>document.body.dataset.aioToken||'';
  const alive=version=>version===epoch&&dialog?.open===true;
  const plain=value=>typeof value==='string'?value:value==null?'':String(value);
  const describe=value=>{
    if(value==null)return 'не определено';
    if(value==='')return 'пусто';
    if(typeof value==='boolean')return value?'да':'нет';
    if(Array.isArray(value))return value.slice(0,8).map(describe).join(' × ');
    if(typeof value==='object')return 'подробное значение недоступно';
    const text=plain(value);return text.length>300?text.slice(0,300)+'…':text;
  };
  function cellAddress(location){
    let column=Number(location.column),label='';const row=Number(location.row);
    if(!Number.isSafeInteger(column)||column<1||column>1000000||!Number.isSafeInteger(row)||row<1)return 'Ячейка';
    while(column>0){column--;label=String.fromCharCode(65+column%26)+label;column=Math.floor(column/26);}
    return label+row;
  }
  const eligible=item=>item?.sourceAvailable!==false&&Array.isArray(item?.recipeCandidates)&&item.recipeCandidates.length>0;
  const feedback=message=>{const node=$('[data-c-feedback]');if(node)node.textContent=plain(message);};

  async function request(path,body,signal=controller?.signal){
    const response=await fetch(prefix+path,{method:body===undefined?'GET':'POST',cache:'no-store',signal,
      headers:{'X-AIO-Token':token(),...(body===undefined?{}:{'Content-Type':'application/json'})},
      ...(body===undefined?{}:{body:JSON.stringify(body)})});
    const payload=await response.json();
    if(!response.ok){const error=new Error(payload.error||'Операция анализа не выполнена.');error.code=payload.code;throw error;}
    return payload;
  }

  function clearPreview(){
    previewNumber++;
    previewBusy=false;
    previewController?.abort();previewController=null;
    $('[data-c-preview]')?.replaceChildren();
    if(previewURL){URL.revokeObjectURL(previewURL);previewURL=null;}
  }
  function placeholder(message){
    const area=$('[data-c-preview]');if(!area)return;
    area.replaceChildren();const node=document.createElement('p');node.className='aio-compare-placeholder';node.textContent=message;area.append(node);
  }
  function setReport(item){
    const report=$('[data-c-findings]');if(!report)return;
    report.replaceChildren();
    const title=document.createElement('h3');title.textContent='Что изменилось';report.append(title);
    if(!item){const note=document.createElement('small');note.textContent='Выберите преобразование. Двойной клик или Enter открывает предпросмотр.';report.append(note);return;}
    const findings=Array.isArray(item.findings)?item.findings:[];
    if(findings.length){const list=document.createElement('ul');for(const finding of findings){
      const row=document.createElement('li');
      const category=labels[finding.category]||'';
      row.textContent=(category?category+': ':'')+plain(finding.message||finding.code||'Есть расхождение.');
      if(Array.isArray(finding.locations)&&finding.locations.length){
        const details=document.createElement('details');details.className='aio-compare-cell-details';
        const summary=document.createElement('summary');
        const total=Number.isSafeInteger(finding.totalAffected)?finding.totalAffected:finding.locations.length;
        summary.textContent='Ячейки с расхождениями — '+total;
        details.append(summary);const cells=document.createElement('ul');
        for(const location of finding.locations.slice(0,25)){
          if(!location||typeof location!=='object')continue;
          const cell=document.createElement('li'),address=document.createElement('strong');address.textContent=cellAddress(location);cell.append(address);
          cell.append(document.createTextNode(': '+describe(location.before)+' → '+describe(location.after)));cells.append(cell);
        }
        details.append(cells);
        if(total>25){const more=document.createElement('small');more.textContent='Показаны первые 25 ячеек.';details.append(more);}
        row.append(details);
      }else if(finding.before!==undefined||finding.after!==undefined){
        const values=document.createElement('small');
        values.textContent='До: '+describe(finding.before)+' · После: '+describe(finding.after);row.append(values);
      }
      list.append(row);
    }report.append(list);}
    else{const note=document.createElement('p');note.textContent=labels[item.status]||'Подробности ещё не получены.';report.append(note);}
    const unknown=Array.isArray(item.unknown)?item.unknown:[];
    const scope=document.createElement('small');
    scope.textContent=unknown.length?'Не проверено: '+[...new Set(unknown.map(value=>properties[value]||'часть свойств формата'))].join(', ')+'.':'Проверена доступная часть содержимого. Полная визуальная идентичность не подтверждается.';
    report.append(scope);
    const checked=(item.checkedFeatures||[]).filter(value=>Object.hasOwn(properties,value));
    if(checked.length){const note=document.createElement('small');note.textContent='Проверено: '+checked.map(value=>properties[value]).join(', ')+'.';report.append(note);}
  }

  function refreshActions(){
    if(!dialog)return;
    const state=analysis?.state;
    const retry=analysis?.retry;
    const waiting=retry&&!['completed','ready','failed','cancelled'].includes(retry.state);
    $('[data-c-retry]').textContent='Повторить с автонастройкой'+(selected.size?' — '+selected.size:'');
    $('[data-c-retry]').disabled=busyAction||waiting||!selected.size||!terminal.has(state)||state==='failed';
    $('[data-c-selected]').textContent='Выбрано: '+selected.size;
    $('[data-c-select-all]').disabled=busyAction||state!=='completed';
    $('[data-c-clear]').disabled=busyAction||!selected.size;
    $('[data-c-pause]').textContent=state==='paused'?'Продолжить':'Пауза';
    $('[data-c-pause]').disabled=busyAction||!['queued','running','paused'].includes(state);
    $('[data-c-cancel]').disabled=busyAction||(!waiting&&!['queued','running','paused'].includes(state));
    $('[data-c-cancel]').textContent=waiting?'Остановить повтор':'Остановить анализ';
    const candidate=$('[data-c-candidate]');
    candidate.hidden=!retry?.jobId;
    const assessment=retry?.verification?.assessment;
    const verdicts={checked_subset_improved:'Повтор проверен: улучшились проверенные свойства. Полная сохранность не подтверждается.',
      no_confirmed_improvement:'Повтор проверен: улучшение не подтверждено.',
      not_comparable:'Повторный результат нельзя надёжно сопоставить с предыдущим.'};
    $('[data-c-retry-verdict]').textContent=Object.hasOwn(verdicts,assessment)?verdicts[assessment]:'';
    const candidateJobs=$('[data-c-retry-job]');
    const childIds=[...new Set((retry?.jobIds||[retry?.jobId]).filter(id=>typeof id==='string'&&/^[0-9a-f]{32}$/.test(id)))];
    const childSignature=childIds.join('|');
    if(candidateJobs.dataset.jobs!==childSignature){
      const previous=candidateJobs.value;candidateJobs.replaceChildren();
      childIds.forEach((id,index)=>{const option=document.createElement('option');option.value=id;option.textContent='Повторный пакет '+(index+1)+' из '+childIds.length;candidateJobs.append(option);});
      candidateJobs.value=childIds.includes(previous)?previous:retry?.jobId||childIds[0]||'';
      candidateJobs.dataset.jobs=childSignature;
    }
    candidateJobs.hidden=childIds.length<2;
    $('[data-c-show-retry]').disabled=busyAction||!retry?.jobId||!['completed','ready'].includes(retry.state);
    $('[data-c-save-retry]').disabled=$('[data-c-show-retry]').disabled;
    $('[data-c-back]').hidden=!parentJob;
    $('[data-c-source]').setAttribute('aria-pressed',String(side==='source'));
    $('[data-c-result]').setAttribute('aria-pressed',String(side==='result'));
    $('[data-c-source]').disabled=!detail;
    $('[data-c-result]').disabled=!detail;
    const pdf=detail?.previews?.[side]?.kind==='pdf'||detail?.[side+'Preview']?.kind==='pdf';
    $('[data-c-pdf-controls]').hidden=!pdf;
    $('[data-c-pdf-page]').textContent=(pdfPages[side]+1)+' / '+(pdfPageCounts[side]||'…');
    $('[data-c-pdf-prev]').disabled=!pdf||previewBusy||pdfPages[side]===0;
    $('[data-c-pdf-next]').disabled=!pdf||previewBusy||!pdfPageCounts[side]||pdfPages[side]+1>=pdfPageCounts[side];
  }

  function render(){
    if(!dialog||!analysis)return;
    const count=Number(analysis.total)||0,done=Number(analysis.processed)||0;
    $('[data-c-progress]').textContent=(phases[analysis.state]||'Получаем состояние')+' · '+done+' из '+count;
    const progress=$('progress');progress.max=Math.max(1,count);progress.value=done;
    const retry=analysis.retry;
    if(retry&&retry.state!=='completed'&&retry.state!=='ready'){
      const messages={queued:'Готовим повторную конвертацию…',preparing:'Подготавливаем исходники для повторной конвертации…',
        running:'Повторная конвертация текущего пакета',analyzing:'Проверяем повторный результат…',
        cancelled:'Повторная конвертация остановлена. Первый результат сохранён.',
        failed:'Повторная конвертация не завершена. Первый результат сохранён.'};
      const total=Number(retry.total)||0,current=Number(retry.completed)||0;
      $('[data-c-progress]').textContent=(messages[retry.state]||'Выполняем повтор…')+
        (retry.state==='running'&&total?' · '+current+' из '+total:'');
      if(retry.state==='running'&&total){progress.max=total;progress.value=current;}
      else if(!['failed','cancelled'].includes(retry.state))progress.removeAttribute('value');
      if(retry.error)feedback(retry.error);
    }
    const summary=analysis.summary||{};
    $('[data-c-summary]').textContent='Потери: '+(summary.confirmed_loss||0)+' · Ограничения: '+(summary.format_limitation||0)+' · Требуют проверки: '+((summary.uncertain||0)+(summary.not_checked||0));
    const list=$('[data-c-files]');list.replaceChildren();
    for(const item of (analysis.items||[]).slice(0,50)){
      if(typeof item.taskId!=='string')continue;
      const row=document.createElement('div');row.className='aio-compare-row';row.setAttribute('role','listitem');
      row.dataset.status=Object.hasOwn(labels,item.status)?item.status:'not_checked';
      row.dataset.active=String(item.taskId===selectedTask);
      const label=document.createElement('label'),check=document.createElement('input');check.type='checkbox';
      check.checked=selected.has(item.taskId);check.disabled=!eligible(item)||busyAction;
      check.setAttribute('aria-label','Выбрать '+plain(item.name)+' → '+plain(item.format)+' для повторной конвертации');
      check.title=eligible(item)?'Добавить в повторную конвертацию':'Нет проверенной автоматической настройки для этого результата';
      check.addEventListener('change',()=>{if(check.checked)selected.add(item.taskId);else selected.delete(item.taskId);refreshActions();});
      label.append(check);row.append(label);
      const button=document.createElement('button');button.type='button';button.dataset.task=item.taskId;
      const name=document.createElement('span');name.className='aio-compare-name';name.textContent=plain(item.name);name.title=plain(item.name);button.append(name);
      const format=document.createElement('span');format.textContent='→ '+plain(item.format).toUpperCase();button.append(format);
      const status=document.createElement('span');status.className='aio-compare-kind';status.textContent=labels[item.status]||'Проверяем';button.append(status);
      button.addEventListener('click',()=>focusItem(item,false));
      button.addEventListener('dblclick',()=>focusItem(item,true));
      button.addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();focusItem(item,true);}});
      row.append(button);list.append(row);
    }
    const total=Number(analysis.totalFiltered??analysis.total)||0;
    $('[data-c-page]').textContent=(total?Math.floor(offset/50)+1:0)+' / '+Math.max(1,Math.ceil(total/50))+' · '+total;
    $('[data-c-prev]').disabled=offset===0;
    $('[data-c-next]').disabled=offset+50>=total;
    refreshActions();
  }

  function schedule(){
    clearTimeout(timer);timer=null;
    const pending=analysis?.retry&&!['completed','ready','failed','cancelled'].includes(analysis.retry.state);
    if(dialog?.open&&(!terminal.has(analysis?.state)||pending))timer=setTimeout(()=>refresh().catch(()=>{}),1000);
  }
  async function refresh(){
    if(!dialog?.open||!jobId)return;
    const version=epoch,view=viewEpoch,number=++requestNumber;
    try{
      const packet=await request('/compare?job='+encodeURIComponent(jobId)+'&offset='+offset+'&limit=50&filter='+encodeURIComponent(filter)+'&q='+encodeURIComponent(query));
      if(!alive(version)||view!==viewEpoch||number!==requestNumber)return;
      if(packet.jobId&&packet.jobId!==jobId)throw Error('Получен анализ другого задания.');
      analysis=packet;render();
      if(packet.error)feedback(packet.error);
      if(packet.retry?.error)feedback(packet.retry.error);
      schedule();
    }catch(error){if(alive(version)&&error.name!=='AbortError'){feedback(error.message);timer=setTimeout(()=>refresh().catch(()=>{}),2000);}}
  }

  async function showPreview(){
    clearPreview();refreshActions();
    if(!detail){placeholder('Дважды нажмите на файл слева, чтобы открыть сравнение.');return;}
    const description=detail.previews?.[side]||detail[side+'Preview'];
    $('[data-c-preview-label]').textContent=description?.name||detail.name||'';
    if(!description||description.kind==='unavailable'||description.available===false){placeholder(description?.note||'Предпросмотр этого формата недоступен. Результаты анализа приведены ниже.');return;}
    const version=epoch,number=previewNumber;
    const displayText=(text,note)=>{const node=document.createElement('pre');node.textContent=plain(text);$('[data-c-preview]').replaceChildren(node);if(note)feedback(note);};
    if(description.kind==='text'&&typeof description.text==='string'){displayText(description.text,description.note);return;}
    if(!description.url){placeholder(description.note||'Нет доступного представления документа.');return;}
    let parsed;
    try{parsed=new URL(description.url,window.location?.href||'http://127.0.0.1/');
      if(parsed.origin!==(window.location?.origin||parsed.origin)||parsed.pathname!==prefix+'/compare/preview'||parsed.username||parsed.password)
        throw Error('Неверный адрес предпросмотра.');
    }catch(error){placeholder('Адрес предпросмотра не прошёл проверку.');return;}
    const viewingSide=side,isPdf=description.kind==='pdf';
    if(isPdf)parsed.searchParams.set('page',String(pdfPages[viewingSide]));
    previewController=new AbortController();previewBusy=true;refreshActions();placeholder('Загружаем выбранный документ…');
    try{
      const response=await fetch(parsed.href,{headers:{'X-AIO-Token':token()},cache:'no-store',signal:previewController.signal});
      if(!alive(version)||number!==previewNumber)return;
      if(!response.ok)throw Error('Предпросмотр недоступен. Файлы остаются в AIO.');
      const mime=(response.headers.get('Content-Type')||'').split(';')[0].toLowerCase();
      if(isPdf){
        const rawCount=response.headers.get('X-AIO-Preview-Pages'),rawPage=response.headers.get('X-AIO-Preview-Page');
        const count=rawCount==null||rawCount===''?NaN:Number(rawCount);
        const page=rawPage==null||rawPage===''?NaN:Number(rawPage);
        if(Number.isSafeInteger(count)&&count>0)pdfPageCounts[viewingSide]=count;
        if(Number.isSafeInteger(page)&&page>=0&&page<1000000)pdfPages[viewingSide]=page;
      }
      if(mime==='application/json'){
        const data=await response.json();if(!alive(version)||number!==previewNumber)return;
        if(data.kind==='text'&&typeof data.text==='string')displayText(data.text,data.note);
        else placeholder(data.note||'Этот формат можно проверить по отчёту.');
        return;
      }
      if(Number(response.headers.get('Content-Length'))>64*1024*1024)throw Error('Документ слишком большой для встроенного предпросмотра. Конвертация и сохранение доступны.');
      if(!['image/png','image/jpeg','image/gif','image/webp','image/bmp'].includes(mime))throw Error('Этот тип файла не поддерживается встроенным предпросмотром.');
      const blob=await response.blob();if(!alive(version)||number!==previewNumber)return;
      if(blob.size>64*1024*1024)throw Error('Документ превышает размер встроенного предпросмотра.');
      previewURL=URL.createObjectURL(blob);
      // PDFs arrive as one native QtPdf-rendered PNG page. No active PDF
      // plugin, iframe, document scripts or all-pages decoding is required.
      const node=document.createElement('img');node.alt=description.name||'Предпросмотр документа';
      node.src=previewURL;$('[data-c-preview]').replaceChildren(node);
      if(description.note)feedback(description.note);
    }catch(error){if(alive(version)&&number===previewNumber&&error.name!=='AbortError')placeholder(error.message);}
    finally{if(alive(version)&&number===previewNumber){previewBusy=false;refreshActions();}}
  }

  async function focusItem(item,preview){
    if(selectedTask!==item.taskId){
      pdfPages={source:0,result:0};pdfPageCounts={source:null,result:null};
      detail=null;clearPreview();
      $('[data-c-preview-label]').textContent=plain(item.name);
      placeholder('Двойной клик или Enter открывает выбранное преобразование.');refreshActions();
    }
    selectedTask=item.taskId;setReport(item);
    for(const row of $('[data-c-files]')?.children||[])row.dataset.active=String(row.querySelector('[data-task]')?.dataset.task===item.taskId);
    if(!preview)return;
    const version=epoch,number=++previewNumber;
    try{
      const packet=await request('/compare/item?job='+encodeURIComponent(jobId)+'&task='+encodeURIComponent(item.taskId));
      if(!alive(version)||selectedTask!==item.taskId||number!==previewNumber)return;
      detail=packet.item||packet;setReport(detail);await showPreview();
    }catch(error){if(alive(version)&&error.name!=='AbortError')feedback(error.message);}
  }

  async function action(work){
    if(busyAction)return;
    const version=epoch;busyAction=true;refreshActions();feedback('Выполняем запрос…');
    try{await work();if(alive(version)){feedback('');await refresh();}}
    catch(error){if(alive(version)&&error.name!=='AbortError')feedback(error.message);}
    finally{if(alive(version)){busyAction=false;refreshActions();}}
  }

  async function chooseAll(){
    const version=epoch,id=analysis?.analysisId,view=viewEpoch;
    await action(async()=>{
      const total=Number(analysis?.totalFiltered??analysis?.total)||0;
      const ids=[];
      for(let start=0;start<total;start+=50){
        const packet=await request('/compare?job='+encodeURIComponent(jobId)+'&offset='+start+'&limit=50&filter='+encodeURIComponent(filter)+'&q='+encodeURIComponent(query));
        if(!alive(version)||view!==viewEpoch||packet.analysisId!==id)throw Error('Состояние анализа изменилось. Повторите выбор.');
        for(const item of packet.items||[])if(eligible(item))ids.push(item.taskId);
      }
      for(const id of ids)selected.add(id);
    });
  }

  function mount(){
    if(dialog)return;
    dialog=document.createElement('dialog');dialog.className='aio-compare-dialog';dialog.setAttribute('aria-labelledby','aio-compare-title');
    dialog.innerHTML='<div class="aio-compare-shell"><header class="aio-compare-head"><div><h2 id="aio-compare-title">До / после конвертации</h2><small data-c-summary>Анализ запускается по вашему запросу</small></div><button type="button" data-c-close aria-label="Закрыть сравнение">×</button></header>'+
      '<div class="aio-compare-progress"><progress value="0" max="1" aria-label="Прогресс анализа"></progress><span data-c-progress role="status" aria-live="polite"></span><button type="button" data-c-pause>Пауза</button><button type="button" data-c-cancel>Остановить анализ</button></div><p class="aio-compare-feedback" data-c-feedback role="status" aria-live="polite"></p>'+
      '<div class="aio-compare-body"><aside class="aio-compare-sidebar"><div class="aio-compare-tools"><input type="search" data-c-search placeholder="Файл или формат" aria-label="Поиск по результатам анализа"><select data-c-filter aria-label="Фильтр результатов анализа"><option value="all">Все результаты</option><option value="confirmed_loss">Подтверждённые потери</option><option value="format_limitation">Ограничения формата</option><option value="uncertain">Требуют проверки</option><option value="not_checked">Не удалось проверить</option><option value="checked">Проверенная часть сохранена</option></select></div>'+
      '<div class="aio-compare-selection"><button type="button" data-c-select-all>Выбрать все с рекомендацией</button><button type="button" data-c-clear>Снять выбор</button><span data-c-selected>Выбрано: 0</span></div><div class="aio-compare-files" data-c-files role="list" aria-label="Преобразования"></div><div class="aio-compare-pages"><button type="button" data-c-prev>Назад</button><span data-c-page></span><button type="button" data-c-next>Далее</button></div></aside>'+
      '<section class="aio-compare-right" aria-label="Просмотр и описание изменений"><div class="aio-compare-preview-head"><button type="button" data-c-source aria-pressed="false">Исходник</button><button type="button" data-c-result aria-pressed="true">Результат</button><div class="aio-compare-pdf-controls" data-c-pdf-controls role="group" aria-label="Страницы PDF" hidden><button type="button" data-c-pdf-prev aria-label="Предыдущая страница PDF">←</button><small data-c-pdf-page>1 / …</small><button type="button" data-c-pdf-next aria-label="Следующая страница PDF">→</button></div><span data-c-preview-label></span></div><div class="aio-compare-preview" data-c-preview></div><div class="aio-compare-report" data-c-findings></div>'+
      '<footer class="aio-compare-actions"><button type="button" data-c-retry>Повторить с автонастройкой</button><details><summary title="Об автонастройке" aria-label="Предупреждение об автонастройке">!</summary><p>Настройка не обязательно улучшит качество файла. Предыдущий результат будет сохранён.</p></details><div class="aio-compare-candidate" data-c-candidate hidden><small data-c-retry-verdict></small><select data-c-retry-job aria-label="Пакет повторной конвертации"></select><button type="button" data-c-show-retry>Проверить повторный результат</button><button type="button" data-c-save-retry>Сохранить повторный результат</button></div><button type="button" data-c-back hidden>Вернуться к первому результату</button></footer></section></div></div>';
    document.body.append(dialog);
    $('[data-c-close]').addEventListener('click',()=>dialog.close());
    dialog.addEventListener('cancel',event=>{event.preventDefault();dialog.close();});
    let outside=false;
    const isOutside=event=>{if(event.target!==dialog)return false;const r=dialog.getBoundingClientRect();return event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom;};
    dialog.addEventListener('pointerdown',event=>{outside=event.button===0&&isOutside(event);});
    dialog.addEventListener('pointerup',event=>{if(outside&&event.button===0&&isOutside(event))dialog.close();outside=false;});
    dialog.addEventListener('pointercancel',()=>{outside=false;});
    dialog.addEventListener('close',()=>{epoch++;viewEpoch++;clearTimeout(timer);clearTimeout(searchTimer);timer=searchTimer=null;controller?.abort();controller=null;clearPreview();busyAction=false;detail=analysis=null;selectedTask=null;selected.clear();opener?.setAttribute('aria-expanded','false');if(opener?.isConnected)opener.focus({preventScroll:true});opener=null;});
    $('[data-c-search]').addEventListener('input',event=>{query=event.target.value;offset=0;viewEpoch++;clearTimeout(searchTimer);searchTimer=setTimeout(()=>refresh(),180);});
    $('[data-c-filter]').addEventListener('change',event=>{filter=event.target.value;offset=0;viewEpoch++;refresh();});
    $('[data-c-prev]').addEventListener('click',()=>{offset=Math.max(0,offset-50);viewEpoch++;refresh();});
    $('[data-c-next]').addEventListener('click',()=>{offset+=50;viewEpoch++;refresh();});
    $('[data-c-source]').addEventListener('click',()=>{side='source';showPreview();});
    $('[data-c-result]').addEventListener('click',()=>{side='result';showPreview();});
    $('[data-c-pdf-prev]').addEventListener('click',()=>{if(pdfPages[side]>0){pdfPages[side]--;showPreview();}});
    $('[data-c-pdf-next]').addEventListener('click',()=>{if(pdfPageCounts[side]&&pdfPages[side]+1<pdfPageCounts[side]){pdfPages[side]++;showPreview();}});
    $('[data-c-select-all]').addEventListener('click',chooseAll);
    $('[data-c-clear]').addEventListener('click',()=>{selected.clear();render();});
    $('[data-c-pause]').addEventListener('click',()=>action(()=>request('/compare/control',{jobId,action:analysis?.state==='paused'?'resume':'pause'})));
    $('[data-c-cancel]').addEventListener('click',()=>action(()=>request('/compare/control',{jobId,action:'cancel'})));
    $('[data-c-retry]').addEventListener('click',()=>action(async()=>{await request('/compare/retry',{jobId,analysisId:analysis.analysisId,taskIds:[...selected]});selected.clear();}));
    $('[data-c-show-retry]').addEventListener('click',()=>{const candidate=$('[data-c-retry-job]').value;if(candidate)open(candidate,{parent:jobId,opener:$('[data-c-show-retry]')});});
    $('[data-c-save-retry]').addEventListener('click',()=>action(async()=>{const candidate=$('[data-c-retry-job]').value;if(!candidate)throw Error('Повторный результат ещё не готов.');const response=await window.AllInOneBridge?.converter?.saveRetained(candidate,window.AllInOneNativeShell?'folder':'zip');if(response?.saved!==true)throw Error('Сохранение отменено. Оба результата остаются в AIO.');}));
    $('[data-c-back]').addEventListener('click',()=>{const previous=parentJob;if(previous)open(previous,{opener:$('[data-c-back]')});});
  }

  async function open(id,options={}){
    if(typeof id!=='string'||!/^[0-9a-f]{32}$/.test(id))throw Error('Нет доступного задания для сравнения.');
    mount();epoch++;viewEpoch++;requestNumber++;clearTimeout(timer);controller?.abort();clearPreview();
    controller=new AbortController();jobId=id;analysis=null;selectedTask=null;detail=null;selected.clear();side='result';offset=0;query='';filter='all';busyAction=false;
    pdfPages={source:0,result:0};pdfPageCounts={source:null,result:null};
    parentJob=options.parent||null;
    const requestedOpener=options.opener||document.activeElement;
    // Switching parent/candidate inside the same dialog keeps its external
    // launcher as the focus destination and aria-expanded owner.
    if(requestedOpener&&(!dialog.open||!dialog.contains(requestedOpener)))opener=requestedOpener;
    $('[data-c-search]').value='';$('[data-c-filter]').value='all';$('[data-c-files]').replaceChildren();
    $('[data-c-progress]').textContent='Подготавливаем анализ…';$('[data-c-preview-label]').textContent='';
    setReport(null);placeholder('Дважды нажмите на файл слева, чтобы открыть сравнение.');feedback('');
    if(!dialog.open)dialog.showModal();opener?.setAttribute('aria-expanded','true');refreshActions();
    const version=epoch;
    try{await request('/compare/start',{jobId});if(alive(version))await refresh();}
    catch(error){if(alive(version)&&error.name!=='AbortError'){feedback(error.message);$('[data-c-progress]').textContent='Анализ недоступен. Результат можно сохранить.';}}
  }

  function attach(){
    const root=document.querySelector('.aio-cv');
    if(root!==launchRoot){launch?.remove();launch=null;launchRoot=root;}
    if(!root)return;
    const footer=root.querySelector('.aio-cv__footer');if(!footer)return;
    if(!launch){launch=document.createElement('button');launch.type='button';launch.className='aio-compare-launch';launch.textContent='Анализ до/после';launch.setAttribute('aria-haspopup','dialog');launch.setAttribute('aria-expanded','false');launch.dataset.aioCompareLaunch='';launch.addEventListener('click',()=>{if(lastState?.jobId)open(lastState.jobId,{opener:launch}).catch(error=>feedback(error.message));});}
    if(!launch.isConnected){const save=[...footer.querySelectorAll('button')].find(b=>!b.closest('.aio-guard-queue')&&/Сохранить/.test(b.textContent));if(save)save.insertAdjacentElement('afterend',launch);else footer.append(launch);}
    launch.hidden=!(lastState?.finished&&lastState.result?.ticket&&lastState.succeeded>0);
    footer.classList.toggle('aio-compare-ready',!launch.hidden);
    launch.disabled=lastState?.comparisonAvailable===false;
    launch.title=launch.disabled?'Исходные копии этого задания уже недоступны для сравнения':'';
  }
  window.addEventListener('aio-guard-state',event=>{lastState=event.detail||null;attach();});
  window.addEventListener('aio-guard-save-confirmed',event=>{if(event.detail?.released&&event.detail.jobId===lastState?.jobId){lastState=null;attach();}if(event.detail?.released&&event.detail.jobId===jobId&&dialog?.open){feedback('Задание сохранено и освобождено. Предпросмотр можно закрыть.');clearTimeout(timer);}});
  const observer=new MutationObserver(mutations=>{if(mutations.some(m=>[...m.addedNodes,...m.removedNodes].some(n=>n.nodeType===1&&(n.matches?.('.aio-cv,.aio-cv__footer')||n.querySelector?.('.aio-cv,.aio-cv__footer')))))attach();});
  observer.observe(document.body,{childList:true,subtree:true});attach();
  window.addEventListener('pagehide',()=>{controller?.abort();clearTimeout(timer);clearTimeout(searchTimer);clearPreview();observer.disconnect();});
  window.AIOGuardComparison=Object.freeze({open,close:()=>dialog?.close()});
})();
