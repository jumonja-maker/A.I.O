/* Optional, local before/after inspection. Nothing starts on file selection. */
(() => {
  'use strict';
  const prefix='/api/converter-guard';
  const labels=Object.freeze({confirmed_loss:'Подтверждённая потеря',format_limitation:'Ограничение формата',
    uncertain:'Требуется проверка',not_checked:'Не удалось проверить',checked:'Проверенная часть сохранена'});
  const phases=Object.freeze({queued:'Ожидание анализа',running:'Проверяем результаты',paused:'Анализ на паузе',
    cancelled:'Анализ остановлен',interrupted:'Анализ прерван',completed:'Анализ завершён',failed:'Не удалось завершить анализ'});
  const properties=Object.freeze({visual_layout:'внешний вид и расположение элементов',fonts:'шрифты',
    colour_accuracy:'точность цветов',formula_evaluation:'пересчёт формул',semantic_meaning:'смысл содержимого',
    rendered_pixels:'совпадение пикселей',formula_results:'результаты вычисления формул',formula_recalculation:'пересчёт формул',
    semantic_equivalence:'смысловая эквивалентность',normalized_text:'извлечённый текст',
    image_dimensions:'размеры изображения',frames:'кадры',pages:'страницы',alpha:'прозрачность',
    cell_values:'значения ячеек',alpha_channel:'канал прозрачности',width:'ширина',height:'высота',identical_file_bytes:'побайтовое совпадение',all_sheet_values_unordered:'значения всех листов',
    all_sheet_values_by_index_name:'значения всех листов с проверкой их имён и индексов',
    html_css_layout:'вёрстка HTML/CSS',html_text_visibility:'видимость текста HTML',html_embedded_resource_rendering:'отображение ресурсов HTML',html_complex_table_mapping:'сложная структура таблиц HTML',html_dynamic_content:'динамическое содержимое HTML',html_hidden_content:'скрытое содержимое HTML',
    all_sheet_outputs_mapping:'соответствие файлов всем листам книги',spreadsheet_values:'значения ячеек',cell_values_single_grid:'значения одной таблицы',
    comments_and_revisions:'комментарии и история правок',hidden_text:'скрытый текст',field_evaluation:'вычисление полей',textboxes_reading_order:'порядок текста в надписях',word_table_structure:'структура таблиц Word',
    presentation_notes:'заметки к слайдам',presentation_masters_and_layouts:'образцы и макеты слайдов',presentation_animations:'анимации презентации',charts_and_smartart:'диаграммы и SmartArt',embedded_objects:'встроенные объекты',table_to_slide_mapping:'соответствие таблиц слайдам',slide_object_positions:'расположение объектов слайда',
    cell_types:'типы ячеек',cell_formatting:'форматирование ячеек',sheet_names_and_identity:'имена и соответствие листов',merged_cells:'объединённые ячейки',cell_annotations:'примечания к ячейкам',charts_and_embedded_objects:'диаграммы и встроенные объекты',
    notes_and_comments:'заметки и комментарии',tracked_changes:'исправления',supplemental_outputs_not_inspected:'содержимое сопровождающих файлов',incomplete_source_inspection:'непроверенная часть исходника',incomplete_result_inspection:'непроверенная часть результата',
    page_to_frame_timing:'соответствие страниц и кадров по времени',animation_loop_count:'число повторов анимации',frame_durations:'длительность кадров',individual_page_to_frame_dimensions:'размеры отдельных страниц и кадров',bit_depth:'глубина цвета',image_pixel_values:'пиксели изображения',colour_profile:'цветовой профиль',exif_and_other_metadata:'EXIF и другие метаданные',editable_formula_mapping:'соответствие редактируемых формул'});
  const terminal=new Set(['completed','cancelled','interrupted','failed']);
  const validJob=id=>typeof id==='string'&&/^[0-9a-f]{32}$/.test(id);
  let dialog=null,launch=null,launchRoot=null,lastState=null,jobId=null,analysis=null,selectedTask=null,detail=null;
  let side='result',offset=0,query='',filter='problems',timer=null,controller=null,previewController=null,previewURL=null;
  let pairedMode=true,pairedViewer=null;
  let epoch=0,viewEpoch=0,requestNumber=0,previewNumber=0,busyAction=false,searchTimer=null;
  let dialogGeneration=0,cleanedDialogGeneration=-1;
  let opener=null,parentJob=null,unavailable=false,refreshFailures=0,restoreTask=null;
  let outputIndex=0,outputOffset=0,outputChosen=false,historyOffset=0,historyTotal=0,historyPage=[],historyRunId=null,candidateJobId=null;
  let historyNumber=0,verificationNumber=0,verificationOffset=0,verificationTotal=0;
  const views=new Map();
  const verdicts=Object.freeze({checked_subset_improved:'Повтор проверен: улучшились проверенные свойства. Полная сохранность не подтверждается.',
    no_confirmed_improvement:'Повтор проверен: улучшение не подтверждено.',
    not_comparable:'Повторный результат нельзя надёжно сопоставить с предыдущим.'});
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
    return (location.sheetName?'['+plain(location.sheetName)+']!':'')+label+row;
  }
  const eligible=item=>item?.sourceAvailable!==false&&Array.isArray(item?.recipeCandidates)&&item.recipeCandidates.length>0;
  const attemptId=attempt=>attempt?.runId||'latest';
  const attempts=()=>historyPage.length?historyPage:analysis?.retry?[analysis.retry]:[];
  const currentAttempt=()=>attempts().find(row=>attemptId(row)===historyRunId)||attempts()[0]||null;
  function childrenOf(attempt){
    if(Array.isArray(attempt?.children))return attempt.children.filter(row=>validJob(row?.jobId));
    const ids=[...new Set((attempt?.jobIds||[attempt?.jobId]).filter(validJob))];
    return ids.map(id=>({jobId:id,state:attempt.state,available:true,
      verification:ids.length===1?attempt.verification:null}));
  }
  const currentCandidate=()=>childrenOf(currentAttempt()).find(row=>row.jobId===candidateJobId)||null;
  const pageKey=()=>side==='source'?'source':'result:'+outputIndex;
  const previewDescription=()=>side==='result'&&Array.isArray(detail?.outputPreviews)?
    detail.outputPreviews.find(row=>row.outputIndex===outputIndex)||detail.previews?.result:detail?.previews?.[side]||detail?.[side+'Preview'];
  function rememberView(closing=false){
    if(!validJob(jobId)||(!dialog?.open&&!closing)||unavailable)return;
    const indices=[...selected].map(id=>/^([0-9a-f]{32}):(\d+)$/.exec(id)).filter(m=>m&&m[1]===jobId).map(m=>Number(m[2]));
    views.delete(jobId);views.set(jobId,{offset,query,filter,selected:indices,selectedTask,side,outputIndex,outputOffset,outputChosen,
      pdfPages:{...pdfPages},pdfPageCounts:{...pdfPageCounts},historyOffset,historyPage,historyTotal,historyRunId,candidateJobId,
      analysisId:analysis?.analysisId,previewed:!!detail,scroll:$('[data-c-files]')?.scrollTop||0});
    while(views.size>16)views.delete(views.keys().next().value);
  }
  function cleanupClosedView(){
    // Native close events are queued. A previous opening can finish closing
    // after another package has already opened this same dialog element.
    if(!dialog||dialog.open||cleanedDialogGeneration===dialogGeneration)return;
    cleanedDialogGeneration=dialogGeneration;
    rememberView(true);epoch++;historyNumber++;verificationNumber++;viewEpoch++;
    clearTimeout(timer);clearTimeout(searchTimer);timer=searchTimer=null;
    controller?.abort();controller=null;clearPreview();busyAction=false;detail=analysis=null;
    selectedTask=null;selected.clear();$('[data-c-files]').replaceChildren();
    $('[data-c-findings]').replaceChildren();$('[data-c-output]').replaceChildren();
    $('[data-c-verification-list]').replaceChildren();historyPage=[];
    opener?.setAttribute('aria-expanded','false');
    if(opener?.isConnected)opener.focus({preventScroll:true});opener=null;
  }
  function closeComparison(){
    if(!dialog)return;
    dialog.close();
    // Release view resources immediately; the later native close event is an
    // idempotent notification and cannot clear a subsequently opened view.
    cleanupClosedView();
  }
  function markUnavailable(message){
    unavailable=true;busyAction=false;epoch++;historyNumber++;verificationNumber++;controller?.abort();clearTimeout(timer);timer=null;clearPreview();selected.clear();
    views.delete(jobId);feedback(message||'Пакет освобождён или удалён. Сравнение больше недоступно.');
    $('[data-c-progress]').textContent='Пакет недоступен';refreshActions();
  }

  const reportUnavailable=error=>error.status===404&&['COMPARISON_NOT_STARTED','COMPARISON_REPORT_UNAVAILABLE'].includes(error.code);
  function markReportUnavailable(){
    clearTimeout(timer);timer=null;clearPreview();selected.clear();selectedTask=null;detail=null;restoreTask=null;
    historyNumber++;historyPage=[];historyTotal=0;historyRunId=null;candidateJobId=null;clearVerifications();views.delete(jobId);
    analysis={jobId,state:'failed',processed:0,total:0,canRestart:true,reportUnavailable:true};
    placeholder('Отчёт недоступен. Запустите анализ заново.');
    feedback('Отчёт анализа недоступен. Можно начать анализ заново; результаты конвертации сохранены.');render();
  }

  const feedback=message=>{const node=$('[data-c-feedback]');if(node)node.textContent=plain(message);};

  async function request(path,body,signal=controller?.signal){
    const response=await fetch(prefix+path,{method:body===undefined?'GET':'POST',cache:'no-store',signal,
      headers:{'X-AIO-Token':token(),...(body===undefined?{}:{'Content-Type':'application/json'})},
      ...(body===undefined?{}:{body:JSON.stringify(body)})});
    const payload=await response.json();
    if(!response.ok){const error=new Error(payload.error||'Операция анализа не выполнена.');error.code=payload.code;error.status=response.status;throw error;}
    return payload;
  }

  function clearPreview(){
    pairedViewer?.destroy();pairedViewer=null;
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
    const scope=document.createElement('details');scope.className='aio-compare-scope';
    const summary=document.createElement('summary');summary.textContent='Границы проверки'+(unknown.length?' · есть непроверенные свойства':'');scope.append(summary);
    const note=document.createElement('small');
    note.textContent=unknown.length?'Не проверено: '+[...new Set(unknown.map(value=>properties[value]||'часть свойств формата'))].join(', ')+'.':'Проверена доступная часть содержимого. Полная визуальная идентичность не подтверждается.';
    scope.append(note);
    const checked=(item.checkedFeatures||[]).filter(value=>Object.hasOwn(properties,value));
    const evaluated=(item.evaluatedFeatures||item.checkedFeatures||[]).filter(value=>Object.hasOwn(properties,value));
    if(evaluated.length){const value=document.createElement('small');value.textContent='Исследовано: '+[...new Set(evaluated.map(value=>properties[value]))].join(', ')+'.';scope.append(value);}
    if(checked.length){const value=document.createElement('small');value.textContent='Совпало: '+checked.map(value=>properties[value]).join(', ')+'.';scope.append(value);}
    const outputs=item.outputScope;
    if(outputs){
      const count=value=>Number.isSafeInteger(value)&&value>=0;
      const parts=[];
      if(count(outputs.totalFiles))parts.push('Выходных файлов: '+outputs.totalFiles);
      if(count(outputs.primaryFiles)&&count(outputs.inspectedPrimaryFiles))
        parts.push('Проверено основных: '+outputs.inspectedPrimaryFiles+' из '+outputs.primaryFiles);
      if(count(outputs.supplementalFiles)&&count(outputs.inspectedSupplementalFiles))
        parts.push('Сопровождающих: '+outputs.inspectedSupplementalFiles+' из '+outputs.supplementalFiles);
      if(count(outputs.unavailableFiles)&&outputs.unavailableFiles>0)
        parts.push('Недоступно: '+outputs.unavailableFiles+'; всё преобразование не проверено');
      if(parts.length){const value=document.createElement('small');value.textContent=parts.join('. ')+'.';scope.append(value);}
    }
    const coverage={body:'основной текст',headers:'верхние колонтитулы',footers:'нижние колонтитулы',footnotes:'сноски',endnotes:'концевые сноски',slide_text:'текст слайдов',slide_table_text:'текст таблиц слайдов'};
    for(const [label,value] of [['Исходник',item.before],['Результат',item.after]])if(Array.isArray(value?.textCoverage)){
      const text=document.createElement('small');text.textContent=label+' · извлечённый текст: '+value.textCoverage.map(key=>coverage[key]||'часть документа').join(', ')+'.';scope.append(text);
    }
    report.append(scope);
  }

  function refreshActions(){
    if(!dialog)return;
    const state=analysis?.state,retry=analysis?.retry;
    const waiting=retry&&!['completed','ready','failed','cancelled','interrupted'].includes(retry.state);
    const finished=state==='completed',canContinue=analysis?.canResume===true||(['cancelled','interrupted'].includes(state)&&analysis?.canResume!==false);
    $('[data-c-retry]').textContent='Повторить с автонастройкой'+(selected.size?' — '+selected.size:'');
    $('[data-c-retry]').disabled=unavailable||busyAction||waiting||!selected.size||!finished||analysis?.canRetry===false;
    $('[data-c-selected]').textContent='Выбрано: '+selected.size;
    $('[data-c-select-all]').disabled=unavailable||busyAction||!finished;
    $('[data-c-clear]').disabled=unavailable||busyAction||!selected.size;
    $('[data-c-pause]').textContent=state==='paused'?'Продолжить':'Пауза';
    $('[data-c-pause]').disabled=unavailable||busyAction||!['queued','running','paused'].includes(state);
    $('[data-c-resume]').hidden=!canContinue;
    $('[data-c-resume]').disabled=unavailable||busyAction||waiting||analysis?.canResume===false;
    $('[data-c-restart]').hidden=!analysis||!terminal.has(state);
    $('[data-c-restart]').disabled=unavailable||busyAction||waiting||analysis?.canRestart===false;
    $('[data-c-refresh]').disabled=unavailable||busyAction;
    $('[data-c-cancel]').disabled=unavailable||busyAction||(!waiting&&!['queued','running','paused'].includes(state));
    $('[data-c-cancel]').textContent=waiting?'Остановить повтор':'Остановить анализ';
    $('[data-c-save-current]').disabled=unavailable||busyAction||!selectedTask;
    $('[data-c-save-retry]').hidden=true;
    const rows=attempts(),attempt=currentAttempt(),children=childrenOf(attempt);
    if(attempt)historyRunId=attemptId(attempt);
    const history=$('[data-c-history]');history.hidden=!rows.length&&!historyTotal;
    const selector=$('[data-c-attempt]'),signature=rows.map(row=>attemptId(row)+':'+row.state).join('|');
    if(selector.dataset.signature!==signature){
      selector.replaceChildren();rows.forEach((row,index)=>{const option=document.createElement('option');option.value=attemptId(row);
        const number=Math.max(1,historyTotal-historyOffset-index);option.textContent='Повтор '+number+' · '+(phases[row.state]||({ready:'Готов',preparing:'Подготовка',analyzing:'Проверка'})[row.state]||row.state||'');selector.append(option);});
      selector.dataset.signature=signature;
    }
    selector.value=historyRunId||'';selector.disabled=unavailable||busyAction;
    $('[data-c-history-page]').textContent=(rows.length?historyOffset+1:0)+'–'+(historyOffset+rows.length)+' / '+historyTotal;
    $('[data-c-history-prev]').disabled=unavailable||busyAction||historyOffset===0;
    $('[data-c-history-next]').disabled=unavailable||busyAction||historyOffset+rows.length>=historyTotal;
    const candidateJobs=$('[data-c-retry-job]'),previous=candidateJobId;
    const childSignature=historyRunId+'|'+children.map(row=>row.jobId+':'+row.available+':'+row.state).join('|');
    if(candidateJobs.dataset.jobs!==childSignature){
      candidateJobs.replaceChildren();children.forEach((row,index)=>{const option=document.createElement('option');option.value=row.jobId;
        option.textContent='Пакет '+(index+1)+' из '+children.length+(row.recipe?' · '+(row.recipe==='csv_preserve_values'?'CSV':'PDF'):'')+(row.available===false?' · освобождён':'');candidateJobs.append(option);});
      candidateJobs.dataset.jobs=childSignature;
    }
    candidateJobId=children.some(row=>row.jobId===previous)?previous:children.at(-1)?.jobId||null;
    candidateJobs.value=candidateJobId||'';candidateJobs.hidden=children.length<2;candidateJobs.disabled=unavailable||busyAction;
    const child=currentCandidate(),assessment=child?.verification?.assessment;
    let verdict=child?.available===false?'Этот пакет освобождён или удалён.':verdicts[assessment]||'';
    if(attempt?.analysisId&&analysis?.analysisId&&attempt.analysisId!==analysis.analysisId)verdict+=' Оценка относится к предыдущему анализу.';
    $('[data-c-retry-verdict]').textContent=verdict;
    $('[data-c-candidate]').hidden=!children.length;
    const childPending=child&&['queued','preparing','running','analyzing'].includes(child.state);
    $('[data-c-show-retry]').disabled=busyAction||!child||child.available===false||childPending;
    $('[data-c-save-retry]').disabled=$('[data-c-show-retry]').disabled;
    $('[data-c-verification-details]').hidden=!attempt?.runId;
    $('[data-c-back]').hidden=!parentJob;
    $('[data-c-back]').disabled=busyAction||!parentJob;
    $('[data-c-source]').setAttribute('aria-pressed',String(side==='source'));
    $('[data-c-result]').setAttribute('aria-pressed',String(side==='result'));
    $('[data-c-source]').disabled=unavailable||!detail;
    $('[data-c-result]').disabled=unavailable||!detail;
    $('[data-c-output]').disabled=unavailable||previewBusy||!detail;
    const pairActive=pairedMode&&!!window.AIOComparisonViewer;
    $('[data-c-pair]').hidden=!window.AIOComparisonViewer;
    $('[data-c-pair]').setAttribute('aria-pressed',String(pairActive));
    const pdf=!pairActive&&previewDescription()?.kind==='pdf',key=pageKey();
    $('[data-c-pdf-controls]').hidden=!pdf;
    $('[data-c-pdf-page]').textContent=((pdfPages[key]||0)+1)+' / '+(pdfPageCounts[key]||'…');
    $('[data-c-pdf-prev]').disabled=unavailable||!pdf||previewBusy||!pdfPages[key];
    $('[data-c-pdf-next]').disabled=unavailable||!pdf||previewBusy||!pdfPageCounts[key]||(pdfPages[key]||0)+1>=pdfPageCounts[key];
  }

  function renderNavigation(){
    const nav=$('[data-c-breadcrumbs]');nav.replaceChildren();
    const ancestors=analysis?.navigation?.ancestors||[];
    const entries=ancestors.filter(row=>validJob(row.jobId)&&row.jobId!==jobId);
    const visible=entries.length>16?[entries[0],...entries.slice(-15)]:entries;
    for(const [index,row] of visible.entries()){
      const button=document.createElement('button');button.type='button';button.dataset.cJob=row.jobId;
      button.textContent=row.label||(!index?'Исходный пакет':'Предыдущий повтор');button.title=plain(row.label||row.jobId);
      button.disabled=row.available===false;button.addEventListener('click',()=>open(row.jobId,{opener}));nav.append(button);
    }
    const current=document.createElement('span');current.textContent=entries.length?'Текущий повтор':'Исходный пакет';current.setAttribute('aria-current','page');nav.append(current);
    nav.hidden=!entries.length;
  }

  function renderOutputs(){
    const area=$('[data-c-output-controls]'),select=$('[data-c-output]');
    const outputs=Array.isArray(detail?.outputPreviews)?detail.outputPreviews:[];
    area.hidden=outputs.length<2;
    if(!outputs.length)return;
    if(!outputChosen||!outputs.some(row=>row.outputIndex===outputIndex)){
      const primary=outputs.find(row=>row.available!==false&&row.name===detail.previews?.result?.name)||outputs.find(row=>row.available!==false)||outputs[0];
      outputIndex=primary.outputIndex;
    }
    outputOffset=Math.floor(Math.max(0,outputs.findIndex(row=>row.outputIndex===outputIndex))/100)*100;
    const slice=outputs.slice(outputOffset,outputOffset+100);select.replaceChildren();
    for(const output of slice){const option=document.createElement('option');option.value=String(output.outputIndex);
      option.textContent=plain(output.name).slice(0,180)+(output.available===false?' · недоступен':'');option.title=plain(output.name);select.append(option);}
    select.value=String(outputIndex);select.disabled=unavailable||previewBusy;
    $('[data-c-output-prev]').hidden=outputs.length<=100;$('[data-c-output-next]').hidden=outputs.length<=100;
    $('[data-c-output-prev]').disabled=unavailable||outputOffset===0;
    $('[data-c-output-next]').disabled=unavailable||outputOffset+100>=outputs.length;
  }

  function render(){
    if(!dialog||!analysis)return;
    const count=Number(analysis.total)||0,done=Number(analysis.processed)||0;
    $('[data-c-progress]').textContent=analysis.reportUnavailable?'Отчёт недоступен. Запустите анализ заново.':(phases[analysis.state]||'Получаем состояние')+' · '+done+' из '+count;
    const progress=$('progress');progress.max=Math.max(1,count);progress.value=done;
    const retry=analysis.retry;
    if(retry&&retry.state!=='completed'&&retry.state!=='ready'){
      const messages={queued:'Готовим повторную конвертацию…',preparing:'Подготавливаем исходники для повторной конвертации…',
        running:'Повторная конвертация текущего пакета',analyzing:'Проверяем повторный результат…',
        cancelled:'Повторная конвертация остановлена. Первый результат сохранён.',
        interrupted:'Повторная конвертация прервана. Готовые пакеты сохранены.',
        failed:'Повторная конвертация не завершена. Первый результат сохранён.'};
      const total=Number(retry.total)||0,current=Number(retry.completed)||0;
      $('[data-c-progress]').textContent=(messages[retry.state]||'Выполняем повтор…')+
        (retry.state==='running'&&total?' · '+current+' из '+total:'');
      if(retry.state==='running'&&total){progress.max=total;progress.value=current;}
      else if(!['failed','cancelled'].includes(retry.state))progress.removeAttribute('value');
      if(retry.error)feedback(retry.error);
    }
    const summary=analysis.summary||{};
    $('[data-c-summary]').textContent=analysis.reportUnavailable?'Отчёт недоступен. Запустите анализ заново.':count===0&&terminal.has(analysis.state)?'Нет проверяемых пар в этом пакете':'Потери: '+(summary.confirmed_loss||0)+' · Ограничения: '+(summary.format_limitation||0)+' · Требуют проверки: '+((summary.uncertain||0)+(summary.not_checked||0));
    const list=$('[data-c-files]'),scroll=list.scrollTop,active=document.activeElement;
    const activeRow=active?.closest?.('.aio-compare-row'),focusedTask=activeRow?.querySelector('[data-task]')?.dataset.task;
    const focusedCheck=active?.tagName==='INPUT'&&active?.type==='checkbox';list.replaceChildren();
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
      const status=document.createElement('span');status.className='aio-compare-kind';status.textContent=item.conversionState==='error'?'Ошибка конвертации':labels[item.status]||'Проверяем';button.append(status);
      button.addEventListener('click',()=>focusItem(item,false));
      button.addEventListener('dblclick',()=>focusItem(item,true));
      button.addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();focusItem(item,true);}});
      row.append(button);list.append(row);
    }
    list.scrollTop=scroll;
    if(focusedTask){const row=[...list.children].find(node=>node.querySelector('[data-task]')?.dataset.task===focusedTask);(focusedCheck?row?.querySelector('input'):row?.querySelector('[data-task]'))?.focus({preventScroll:true});}
    if(!(analysis.items||[]).length&&terminal.has(analysis.state)){
      const filteredEmpty=!analysis.reportUnavailable&&analysis.processed>0;
      const note=document.createElement('p');note.className='aio-compare-placeholder';note.textContent=filteredEmpty?'По текущему фильтру результатов нет. Нажмите «Все результаты», чтобы открыть проверенные пары.':analysis.reportUnavailable?'Отчёт недоступен. Запустите анализ заново. Результаты можно сохранить.':'В пакете нет подтверждённой связи результатов с исходниками. Результаты можно сохранить.';list.append(note);
      $('[data-c-findings]').replaceChildren();const detailNote=document.createElement('p');detailNote.textContent=filteredEmpty?'Выберите «Все результаты» или измените фильтр.':analysis.reportUnavailable?'Отчёт недоступен. Запустите анализ заново.':'Сравнение содержимого не выполнено: нет проверяемых пар.';$('[data-c-findings]').append(detailNote);
    }
    const total=Number(analysis.totalFiltered??analysis.total)||0;
    $('[data-c-page]').textContent=(total?Math.floor(offset/50)+1:0)+' / '+Math.max(1,Math.ceil(total/50))+' · '+total;
    $('[data-c-prev]').disabled=offset===0;
    $('[data-c-next]').disabled=offset+50>=total;
    renderNavigation();renderOutputs();refreshActions();
  }

  function schedule(){
    clearTimeout(timer);timer=null;
    const pending=analysis?.retry&&!['completed','ready','failed','cancelled','interrupted'].includes(analysis.retry.state);
    if(dialog?.open&&!unavailable&&(!terminal.has(analysis?.state)||pending||analysis?.finalizing||analysis?.retryFinalizing))timer=setTimeout(()=>refresh().catch(()=>{}),1000);
  }
  async function refresh(){
    if(!dialog?.open||!jobId||unavailable)return;
    const version=epoch,view=viewEpoch,number=++requestNumber;
    try{
      const packet=await request('/compare?job='+encodeURIComponent(jobId)+'&offset='+offset+'&limit=50&filter='+encodeURIComponent(filter)+'&q='+encodeURIComponent(query));
      if(!alive(version)||view!==viewEpoch||number!==requestNumber)return;
      if(packet.jobId&&packet.jobId!==jobId)throw Error('Получен анализ другого задания.');
      const oldId=analysis?.analysisId||views.get(jobId)?.analysisId;
      if(oldId&&packet.analysisId&&oldId!==packet.analysisId){
        selected.clear();selectedTask=null;detail=null;restoreTask=null;clearPreview();setReport(null);views.delete(jobId);
        historyNumber++;historyOffset=0;historyPage=[];historyRunId=null;candidateJobId=null;clearVerifications();
        pdfPages={};pdfPageCounts={};outputIndex=outputOffset=0;outputChosen=false;$('[data-c-output-controls]').hidden=true;
      }
      analysis=packet;refreshFailures=0;
      if(packet.stale){markUnavailable('Пакет освобождён или удалён. Сравнение больше недоступно.');return;}
      parentJob=packet.navigation?.parentJobId||parentJob;
      historyTotal=Number(packet.historyTotal??packet.retryHistory?.length??(packet.retry?1:0));
      if(historyOffset===0)historyPage=packet.retryHistory||[];
      for(const row of packet.items||[])if(selected.has(row.taskId)&&!eligible(row))selected.delete(row.taskId);
      render();
      if(packet.error)feedback(packet.error);
      if(packet.retry?.error)feedback(packet.retry.error);
      if(restoreTask){const task=restoreTask;restoreTask=null;await focusItem({taskId:task,name:''},true);}
      schedule();
    }catch(error){if(alive(version)&&view===viewEpoch&&number===requestNumber&&error.name!=='AbortError'){
      // Errors are responses too: an old filter/page request must not invalidate
      // a newer successful report or restart its polling after a view change.
      if(reportUnavailable(error)){markReportUnavailable();return;}
      if(error.status===404||error.status===410){markUnavailable('Пакет или его отчёт освобождён/удалён. Сохранённые пользователем файлы не затрагиваются.');return;}
      feedback(error.message);refreshFailures++;
      if(refreshFailures<5&&![400,403].includes(error.status))timer=setTimeout(()=>refresh().catch(()=>{}),Math.min(15000,2000*refreshFailures));
      else $('[data-c-progress]').textContent='Не удалось обновить состояние. Нажмите «Обновить».';
    }}
  }

  async function loadHistory(nextOffset){
    const version=epoch,number=++historyNumber,expectedId=analysis?.analysisId;
    const packet=await request('/compare/retry-history?job='+encodeURIComponent(jobId)+'&offset='+nextOffset+'&limit=20');
    if(!alive(version)||number!==historyNumber||analysis?.analysisId!==expectedId)return;
    historyOffset=nextOffset;historyPage=packet.items||packet.retryHistory||[];historyTotal=Number(packet.total??packet.historyTotal??historyPage.length);
    historyRunId=null;candidateJobId=null;clearVerifications();refreshActions();
  }
  function clearVerifications(){
    verificationNumber++;verificationOffset=0;verificationTotal=0;
    $('[data-c-verification-list]')?.replaceChildren();
    $('[data-c-verification-details]')?.removeAttribute('open');
  }
  async function loadVerifications(nextOffset=0){
    const attempt=currentAttempt();if(!attempt?.runId||unavailable)return;
    const version=epoch,number=++verificationNumber,runId=attempt.runId,expectedId=analysis?.analysisId;
    try{
      const packet=await request('/compare/retry-history?job='+encodeURIComponent(jobId)+'&runId='+encodeURIComponent(runId)+'&offset='+nextOffset+'&limit=20');
      if(!alive(version)||number!==verificationNumber||currentAttempt()?.runId!==runId||analysis?.analysisId!==expectedId)return;
      verificationOffset=nextOffset;verificationTotal=Number(packet.total)||0;
      const list=$('[data-c-verification-list]');list.replaceChildren();
      for(const row of packet.items||[]){const line=document.createElement('li');line.textContent=plain(row.name||row.fileId||'Файл')+' → '+plain(row.format).toUpperCase()+': '+((row.available===false||row.stale)?'Пакет освобождён; историческая оценка. ':'')+(verdicts[row.assessment]||'Проверка не завершена.');list.append(line);}
      if(!list.children.length){const line=document.createElement('li');line.textContent='Подробные оценки пока не созданы.';list.append(line);}
      $('[data-c-verification-page]').textContent=(verificationTotal?nextOffset+1:0)+'–'+Math.min(nextOffset+20,verificationTotal)+' / '+verificationTotal;
      $('[data-c-verification-prev]').disabled=nextOffset===0;
      $('[data-c-verification-next]').disabled=nextOffset+20>=verificationTotal;
    }catch(error){if(alive(version)&&number===verificationNumber)feedback(error.message);}
  }

  async function showPreview(){
    if(unavailable)return;
    clearPreview();refreshActions();
    if(!detail){placeholder('Дважды нажмите на файл слева, чтобы открыть сравнение.');return;}
    if(pairedMode&&window.AIOComparisonViewer){
      const version=epoch,number=previewNumber;previewBusy=true;refreshActions();
      const output=detail.outputPreviews?.find(row=>row.outputIndex===outputIndex)||detail.previews?.result;
      pairedViewer=window.AIOComparisonViewer.mount($('[data-c-preview]'),{
        source:detail.previews?.source,result:output,
        context:{jobId,taskId:selectedTask,analysisId:analysis?.analysisId,outputIndex},onMessage:feedback});
      try{await pairedViewer.ready;}finally{if(alive(version)&&number===previewNumber){previewBusy=false;refreshActions();}}
      return;
    }
    const description=previewDescription();
    $('[data-c-preview-label]').textContent=description?.name||detail.name||'';
    if(!description||description.kind==='unavailable'||description.available===false){placeholder(description?.note||'Предпросмотр этого формата недоступен. Результаты анализа приведены ниже.');return;}
    const version=epoch,number=previewNumber;
    const displayText=(text,note)=>{const node=document.createElement('pre');node.textContent=plain(text);$('[data-c-preview]').replaceChildren(node);if(note)feedback(note);};
    if(description.kind==='text'&&typeof description.text==='string'){displayText(description.text,description.note);return;}
    if(!description.url){placeholder(description.note||'Нет доступного представления документа.');return;}
    let parsed;
    try{parsed=new URL(description.url,window.location?.href||'http://127.0.0.1/');
      if(parsed.origin!==(window.location?.origin||parsed.origin)||parsed.pathname!==prefix+'/compare/preview'||parsed.username||parsed.password||
         parsed.searchParams.getAll('job').length!==1||parsed.searchParams.get('job')!==jobId||
         parsed.searchParams.getAll('task').length!==1||parsed.searchParams.get('task')!==selectedTask||
         parsed.searchParams.getAll('side').length!==1||parsed.searchParams.get('side')!==side||
         parsed.searchParams.getAll('analysisId').length>1||
         (parsed.searchParams.has('analysisId')&&parsed.searchParams.get('analysisId')!==analysis?.analysisId))
        throw Error('Неверный адрес предпросмотра.');
    }catch(error){placeholder('Адрес предпросмотра не прошёл проверку.');return;}
    const viewingSide=pageKey(),isPdf=description.kind==='pdf';
    if(side==='result')parsed.searchParams.set('outputIndex',String(outputIndex));
    if(isPdf)parsed.searchParams.set('page',String(pdfPages[viewingSide]||0));
    previewController=new AbortController();previewBusy=true;refreshActions();placeholder('Загружаем выбранный документ…');
    try{
      let response;
      for(let attempt=0;attempt<18;attempt++){
        if(!alive(version)||number!==previewNumber)return;
        response=await fetch(parsed.href,{headers:{'X-AIO-Token':token()},cache:'no-store',signal:previewController.signal});
        if(!alive(version)||number!==previewNumber)return;
        if(response.status!==409)break;
        const reason=await response.json();
        if(!alive(version)||number!==previewNumber)return;
        if(reason.code!=='COMPARISON_PREVIEW_BUSY')throw Error(reason.error||'Предпросмотр временно недоступен.');
        if(attempt===17)throw Error('Предыдущий просмотр ещё готовится. Переключите документ через несколько секунд.');
        placeholder('Завершается предыдущий просмотр. Ожидаем выбранный документ…');
        await new Promise(resolve=>setTimeout(resolve,Math.min(1000,150*(attempt+1))));
      }
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
      outputIndex=0;outputOffset=0;outputChosen=false;detail=null;clearPreview();$('[data-c-output-controls]').hidden=true;
      $('[data-c-preview-label]').textContent=plain(item.name);
      placeholder('Двойной клик или Enter открывает выбранное преобразование.');refreshActions();
    }
    selectedTask=item.taskId;setReport(item);
    for(const row of $('[data-c-files]')?.children||[])row.dataset.active=String(row.querySelector('[data-task]')?.dataset.task===item.taskId);
    if(!preview)return;
    // A repeated open of the same pair starts a new preview generation too.
    // Retire old controls/fullscreen before fetching detail, so late responses
    // cannot replace a viewer the user is already zooming or expanding.
    clearPreview();detail=null;renderOutputs();
    placeholder('Готовим просмотр выбранной пары…');previewBusy=true;refreshActions();
    const version=epoch,number=++previewNumber,expectedId=analysis?.analysisId;
    try{
      const packet=await request('/compare/item?job='+encodeURIComponent(jobId)+'&task='+encodeURIComponent(item.taskId)+(expectedId?'&analysisId='+encodeURIComponent(expectedId):''));
      if(!alive(version)||selectedTask!==item.taskId||number!==previewNumber||analysis?.analysisId!==expectedId)return;
      const received=packet.item||packet;
      if((received.jobId&&received.jobId!==jobId)||(received.taskId&&received.taskId!==item.taskId))
        throw Error('Получены подробности другого преобразования. Обновите текущий отчёт.');
      if(received.analysisId&&received.analysisId!==expectedId){
        await refresh();if(alive(version))feedback('Анализ изменился. Выберите преобразование в текущем отчёте.');return;
      }
      detail=received;setReport(detail);renderOutputs();await showPreview();
    }catch(error){if(alive(version)&&selectedTask===item.taskId&&number===previewNumber&&analysis?.analysisId===expectedId&&error.name!=='AbortError')feedback(error.message);}
    finally{if(alive(version)&&number===previewNumber){previewBusy=false;refreshActions();}}
  }

  async function action(work){
    if(busyAction)return;
    const version=epoch;busyAction=true;refreshActions();feedback('Выполняем запрос…');
    try{await work(()=>alive(version)&&!unavailable);if(alive(version)&&!unavailable){feedback('');await refresh();}}
    catch(error){if(alive(version)&&error.name!=='AbortError'){
      if(error.status===409)await refresh();
      if(alive(version))feedback(error.message);
    }}
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
      '<nav class="aio-compare-breadcrumbs" data-c-breadcrumbs aria-label="Путь пакета" hidden></nav>'+
      '<div class="aio-compare-progress"><progress value="0" max="1" aria-label="Прогресс анализа"></progress><span data-c-progress role="status" aria-live="polite"></span><button type="button" data-c-pause>Пауза</button><button type="button" data-c-cancel>Остановить анализ</button><button type="button" data-c-resume hidden>Продолжить анализ</button><button type="button" data-c-restart hidden>Начать заново</button><button type="button" data-c-refresh>Обновить</button></div><p class="aio-compare-feedback" data-c-feedback role="status" aria-live="polite"></p>'+
      '<div class="aio-compare-body"><aside class="aio-compare-sidebar"><details class="aio-compare-history" data-c-history hidden><summary>Повторы и версии</summary><div class="aio-compare-history-body"><select data-c-attempt aria-label="Попытка повторной конвертации"></select><div class="aio-compare-pages"><button type="button" data-c-history-prev aria-label="Новые попытки">←</button><span data-c-history-page></span><button type="button" data-c-history-next aria-label="Предыдущие попытки">→</button></div><div class="aio-compare-candidate" data-c-candidate hidden><small data-c-retry-verdict></small><select data-c-retry-job aria-label="Пакет повторной конвертации"></select><button type="button" data-c-show-retry>Открыть этот пакет</button><button type="button" data-c-save-retry>Сохранить этот пакет</button><details class="aio-compare-verification" data-c-verification-details hidden><summary>Проверка всех преобразований попытки</summary><ul data-c-verification-list></ul><div class="aio-compare-pages"><button type="button" data-c-verification-prev>←</button><span data-c-verification-page></span><button type="button" data-c-verification-next>→</button></div></details></div></div></details><div class="aio-compare-tools"><input type="search" maxlength="200" data-c-search placeholder="Файл или формат" aria-label="Поиск по результатам анализа"><select data-c-filter aria-label="Фильтр результатов анализа"><option value="problems">Проблемные результаты</option><option value="all">Все результаты</option><option value="confirmed_loss">Подтверждённые потери</option><option value="format_limitation">Ограничения формата</option><option value="uncertain">Требуют проверки</option><option value="not_checked">Не удалось проверить</option><option value="checked">Проверенная часть сохранена</option></select></div>'+
      '<div class="aio-compare-selection"><button type="button" data-c-select-all>Выбрать все с рекомендацией</button><button type="button" data-c-clear>Снять выбор</button><span data-c-selected>Выбрано: 0</span></div><div class="aio-compare-files" data-c-files role="list" aria-label="Преобразования"></div><div class="aio-compare-pages"><button type="button" data-c-prev>Назад</button><span data-c-page></span><button type="button" data-c-next>Далее</button></div></aside>'+
      '<section class="aio-compare-right" aria-label="Просмотр и описание изменений"><div class="aio-compare-preview-head"><button type="button" data-c-source aria-pressed="false">Исходник</button><button type="button" data-c-result aria-pressed="true">Результат</button><button type="button" data-c-pair aria-pressed="true">Рядом</button><div class="aio-compare-pdf-controls" data-c-pdf-controls role="group" aria-label="Страницы PDF" hidden><button type="button" data-c-pdf-prev aria-label="Предыдущая страница PDF">←</button><small data-c-pdf-page>1 / …</small><button type="button" data-c-pdf-next aria-label="Следующая страница PDF">→</button></div><span data-c-preview-label></span><div class="aio-compare-output-controls" data-c-output-controls hidden><button type="button" data-c-output-prev aria-label="Предыдущие выходные файлы">←</button><select data-c-output aria-label="Выходной файл выбранного преобразования"></select><button type="button" data-c-output-next aria-label="Следующие выходные файлы">→</button></div></div><div class="aio-compare-preview" data-c-preview></div><div class="aio-compare-report" data-c-findings></div>'+
      '<footer class="aio-compare-actions"><button type="button" data-c-retry>Повторить с автонастройкой</button><details class="aio-compare-warning"><summary title="Об автонастройке" aria-label="Предупреждение об автонастройке">!</summary><p>Настройка не обязательно улучшит качество файла. Предыдущий результат будет сохранён.</p></details><label><input type="checkbox" data-c-include-source>Добавить исходник</label><button type="button" data-c-save-current>Сохранить выбранный выход и диагностику</button><button type="button" data-c-back hidden>Предыдущий пакет</button></footer></section></div></div>';
    document.body.append(dialog);
    $('[data-c-close]').addEventListener('click',closeComparison);
    dialog.addEventListener('cancel',event=>{event.preventDefault();if(document.fullscreenElement){document.exitFullscreen().catch(()=>{});return;}closeComparison();});
    let outside=false;
    const isOutside=event=>{if(event.target!==dialog)return false;const r=dialog.getBoundingClientRect();return event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom;};
    dialog.addEventListener('pointerdown',event=>{outside=event.button===0&&isOutside(event);});
    dialog.addEventListener('pointerup',event=>{if(outside&&event.button===0&&isOutside(event))closeComparison();outside=false;});
    dialog.addEventListener('pointercancel',()=>{outside=false;});
    dialog.addEventListener('close',cleanupClosedView);
    $('[data-c-search]').addEventListener('input',event=>{query=event.target.value;offset=0;viewEpoch++;clearTimeout(searchTimer);searchTimer=setTimeout(()=>refresh(),180);});
    function setFilter(value){filter=value;offset=0;viewEpoch++;$('[data-c-filter]').value=value;
      for(const button of dialog.querySelectorAll('[data-c-filter-choice]'))button.setAttribute('aria-pressed',String(button.dataset.cFilterChoice===value));refresh();}
    $('[data-c-filter]').addEventListener('change',event=>setFilter(event.target.value));
    const filterButtons=document.createElement('div');filterButtons.className='aio-compare-filter-buttons';filterButtons.setAttribute('role','group');filterButtons.setAttribute('aria-label','Фильтр анализа');
    const failedOption=document.createElement('option');failedOption.value='conversion_error';failedOption.textContent='Ошибки конвертации';$('[data-c-filter]').append(failedOption);
    for(const [value,label]of [['problems','Проблемные'],['all','Все результаты'],['conversion_error','Ошибки конвертации'],['confirmed_loss','Потери'],['format_limitation','Ограничения'],['uncertain','Требуют проверки'],['not_checked','Не проверено'],['checked','Проверенная часть']]){
      const b=document.createElement('button');b.type='button';b.textContent=label;b.dataset.cFilterChoice=value;b.setAttribute('aria-pressed',String(value===filter));b.addEventListener('click',()=>setFilter(value));filterButtons.append(b);}
    $('[data-c-filter]').hidden=true;$('[data-c-filter]').insertAdjacentElement('afterend',filterButtons);
    $('[data-c-prev]').addEventListener('click',()=>{offset=Math.max(0,offset-50);viewEpoch++;refresh();});
    $('[data-c-next]').addEventListener('click',()=>{offset+=50;viewEpoch++;refresh();});
    $('[data-c-source]').addEventListener('click',()=>{pairedMode=false;side='source';showPreview();});
    $('[data-c-result]').addEventListener('click',()=>{pairedMode=false;side='result';showPreview();});
    $('[data-c-pair]').addEventListener('click',()=>{pairedMode=true;showPreview();});
    $('[data-c-pdf-prev]').addEventListener('click',()=>{const key=pageKey();if(pdfPages[key]>0){pdfPages[key]--;showPreview();}});
    $('[data-c-pdf-next]').addEventListener('click',()=>{const key=pageKey();if(pdfPageCounts[key]&&(pdfPages[key]||0)+1<pdfPageCounts[key]){pdfPages[key]=(pdfPages[key]||0)+1;showPreview();}});
    $('[data-c-select-all]').addEventListener('click',chooseAll);
    $('[data-c-clear]').addEventListener('click',()=>{selected.clear();render();});
    $('[data-c-pause]').addEventListener('click',()=>action(()=>request('/compare/control',{jobId,analysisId:analysis?.analysisId,action:analysis?.state==='paused'?'resume':'pause'})));
    $('[data-c-cancel]').addEventListener('click',()=>action(()=>request('/compare/control',{jobId,analysisId:analysis?.analysisId,action:'cancel'})));
    $('[data-c-retry]').addEventListener('click',()=>action(async current=>{
      if(analysis?.state!=='completed')throw Error('Завершите анализ перед повторной конвертацией.');
      await request('/compare/retry',{jobId,analysisId:analysis.analysisId,taskIds:[...selected]});
      if(!current())return; // The request may finish after another package opens.
      selected.clear();historyOffset=0;historyPage=[];historyRunId=null;candidateJobId=null;clearVerifications();$('[data-c-history]').setAttribute('open','');
    }));
    $('[data-c-show-retry]').addEventListener('click',()=>{const candidate=$('[data-c-retry-job]').value;if(candidate)open(candidate,{parent:jobId,opener:$('[data-c-show-retry]')});});
    $('[data-c-save-retry]').hidden=true;
    $('[data-c-back]').addEventListener('click',()=>{const previous=parentJob;if(previous)open(previous,{opener});});
    $('[data-c-resume]').addEventListener('click',()=>action(()=>request('/compare/control',{jobId,analysisId:analysis?.analysisId,action:'resume'})));
    $('[data-c-restart]').addEventListener('click',()=>action(async current=>{
      await request('/compare/start',{jobId,analysisId:analysis?.analysisId,restart:true});
      if(!current())return;
      selected.clear();selectedTask=null;detail=null;restoreTask=null;clearPreview();setReport(null);views.delete(jobId);
    }));
    $('[data-c-refresh]').addEventListener('click',()=>action(async()=>{refreshFailures=0;if(!analysis)await request('/compare/start',{jobId});}));
    $('[data-c-save-current]').addEventListener('click',()=>action(async()=>{
      if(!selectedTask)throw Error('Выберите преобразование и конкретный выходной файл.');
      const response=await window.AllInOneBridge?.converter?.saveSelectedDiagnostic({jobId,
        analysisId:analysis?.analysisId,taskId:selectedTask,
        outputIndex:detail?.outputsTotal>0?outputIndex:null,
        includeSource:$('[data-c-include-source]').checked});
      if(response?.saved!==true)throw Error('Сохранение отменено. Результаты остаются в AIO.');
    }));
    $('[data-c-output]').addEventListener('change',event=>{outputIndex=Number(event.target.value);outputChosen=true;showPreview();});
    for(const [selector,direction] of [['[data-c-output-prev]',-1],['[data-c-output-next]',1]])$(selector).addEventListener('click',()=>{
      const list=detail?.outputPreviews||[],entry=list[outputOffset+direction*100];if(entry){outputIndex=entry.outputIndex;outputChosen=true;renderOutputs();showPreview();}
    });
    $('[data-c-attempt]').addEventListener('change',event=>{historyRunId=event.target.value;candidateJobId=null;clearVerifications();refreshActions();});
    $('[data-c-retry-job]').addEventListener('change',event=>{candidateJobId=event.target.value;refreshActions();});
    $('[data-c-history-prev]').addEventListener('click',()=>action(()=>loadHistory(Math.max(0,historyOffset-20))));
    $('[data-c-history-next]').addEventListener('click',()=>action(()=>loadHistory(historyOffset+20)));
    $('[data-c-verification-details]').addEventListener('toggle',event=>{if(event.currentTarget.open)loadVerifications(0);});
    $('[data-c-verification-prev]').addEventListener('click',()=>loadVerifications(Math.max(0,verificationOffset-20)));
    $('[data-c-verification-next]').addEventListener('click',()=>loadVerifications(verificationOffset+20));
  }

  async function open(id,options={}){
    if(!validJob(id))throw Error('Нет доступного задания для сравнения.');
    if(dialog&&!dialog.open)cleanupClosedView();
    rememberView();mount();dialogGeneration++;epoch++;viewEpoch++;requestNumber++;historyNumber++;verificationNumber++;
    clearTimeout(timer);clearTimeout(searchTimer);controller?.abort();clearPreview();
    const previous=views.get(id);
    controller=new AbortController();jobId=id;analysis=null;detail=null;selected.clear();unavailable=false;refreshFailures=0;
    selectedTask=previous?.selectedTask||null;restoreTask=previous?.previewed?selectedTask:null;
    side=previous?.side||'result';offset=previous?.offset||0;query=previous?.query||'';filter=previous?.filter||'problems';busyAction=false;
    outputIndex=previous?.outputIndex||0;outputOffset=previous?.outputOffset||0;outputChosen=previous?.outputChosen===true;
    for(const index of previous?.selected||[])selected.add(id+':'+index);
    pdfPages={...(previous?.pdfPages||{})};pdfPageCounts={...(previous?.pdfPageCounts||{})};
    historyOffset=previous?.historyOffset||0;historyPage=previous?.historyPage||[];historyTotal=previous?.historyTotal||0;
    historyRunId=previous?.historyRunId||null;candidateJobId=previous?.candidateJobId||null;
    parentJob=options.parent||null;
    const requestedOpener=options.opener||document.activeElement;
    if(requestedOpener&&(!dialog.open||!dialog.contains(requestedOpener)))opener=requestedOpener;
    $('[data-c-search]').value=query;$('[data-c-filter]').value=filter;$('[data-c-files]').replaceChildren();
    for(const button of dialog.querySelectorAll('[data-c-filter-choice]'))button.setAttribute('aria-pressed',String(button.dataset.cFilterChoice===filter));
    $('[data-c-output-controls]').hidden=true;clearVerifications();
    $('[data-c-progress]').textContent='Загружаем анализ…';$('[data-c-preview-label]').textContent='';
    setReport(null);placeholder('Дважды нажмите на файл слева, чтобы открыть сравнение.');feedback('');
    if(!dialog.open)dialog.showModal();opener?.setAttribute('aria-expanded','true');refreshActions();
    const version=epoch;
    try{
      await request('/compare/start',{jobId});if(alive(version))await refresh();
      if(alive(version)&&previous)$('[data-c-files]').scrollTop=previous.scroll;
    }catch(error){if(alive(version)&&error.name!=='AbortError'){
      if(reportUnavailable(error))markReportUnavailable();
      else if(error.status===404||error.status===410)markUnavailable('Пакет освобождён или удалён. Сравнение больше недоступно.');
      else{feedback(error.message);$('[data-c-progress]').textContent='Анализ недоступен. Результат можно сохранить; запуск можно повторить кнопкой «Обновить».';}
    }}
  }

  function attach(){
    const root=document.querySelector('.aio-cv');
    if(root!==launchRoot){launch?.remove();launch=null;launchRoot=root;}
    if(!root)return;
    const footer=root.querySelector('.aio-cv__footer');if(!footer)return;
    if(!launch){launch=root.querySelector('[data-cv-beforeafter]')||document.createElement('button');launch.type='button';launch.className='aio-compare-launch';launch.textContent='До/После';launch.setAttribute('aria-haspopup','dialog');launch.setAttribute('aria-expanded','false');launch.dataset.aioCompareLaunch='';launch.addEventListener('click',()=>{if(lastState?.jobId)open(lastState.jobId,{opener:launch}).catch(error=>feedback(error.message));});}
    if(!launch.isConnected){const save=[...footer.querySelectorAll('button')].find(b=>!b.closest('.aio-guard-queue')&&/Сохранить/.test(b.textContent));if(save)save.insertAdjacentElement('afterend',launch);else footer.append(launch);}
    launch.hidden=!(lastState?.finished&&(lastState.result?.ticket||lastState.failed>0));
    footer.classList.toggle('aio-compare-ready',!launch.hidden);
    launch.disabled=!lastState?.finished;
    launch.title=lastState?.comparisonAvailable===false?'Служебные исходники недоступны. Анализ покажет ограничения; результат можно просмотреть.':'';
  }
  window.addEventListener('aio-guard-state',event=>{lastState=event.detail||null;attach();});
  function released(detail){
    if(!validJob(detail?.jobId))return;
    const id=detail.jobId;views.delete(id);
    for(const view of views.values())for(const attempt of view.historyPage||[])for(const child of childrenOf(attempt))if(child.jobId===id)child.available=false;
    for(const attempt of attempts())for(const child of childrenOf(attempt))if(child.jobId===id)child.available=false;
    if(id===lastState?.jobId){lastState=null;attach();}
    if(id===jobId&&dialog?.open)markUnavailable(detail.discarded?'Пакет удалён. Сравнение больше недоступно.':'Пакет сохранён и освобождён. Сравнение больше недоступно.');
    else if(dialog?.open&&analysis)refresh();
  }
  window.addEventListener('aio-guard-save-confirmed',event=>{if(event.detail?.released===true)released(event.detail);});
  window.addEventListener('aio-guard-job-released',event=>released(event.detail));
  const observer=new MutationObserver(mutations=>{if(mutations.some(m=>[...m.addedNodes,...m.removedNodes].some(n=>n.nodeType===1&&(n.matches?.('.aio-cv,.aio-cv__footer')||n.querySelector?.('.aio-cv,.aio-cv__footer')))))attach();});
  observer.observe(document.body,{childList:true,subtree:true});attach();
  window.addEventListener('pagehide',()=>{controller?.abort();clearTimeout(timer);clearTimeout(searchTimer);clearPreview();views.clear();observer.disconnect();});
  window.AIOGuardComparison=Object.freeze({open,close:closeComparison});
})();
