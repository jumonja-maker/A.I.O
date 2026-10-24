/* Ordinary-mode queue. No automatic selection, export or recovery restart. */
(() => {
  'use strict';
  let root=null, bar=null, stateButton=null, state=null, jobs=[], warnings=[], storage=null, page=0, refreshPending=false, clock=null;
  let preview=null, previewId=null, previewRequest=0;
  // Only the visible page owns DOM nodes. Keep one bounded view per file;
  // unchanged polls and the elapsed-time clock must never rebuild the queue.
  let rowCache=null, paintedRows=null;
  const pendingActions=new Set();
  let actionFeedback=null,feedbackRevision=null;
  const labels={queued:'В очереди',converting:'Конвертируется',ready:'Готово',saved:'Сохранено',
    error:'Ошибка',cancelled:'Отменено',skipped:'Исключено Guard'};
  const api=()=>window.AllInOneBridge?.converter;
  const text=(selector,value)=>{const n=bar?.querySelector(selector);if(n&&n.textContent!==String(value))n.textContent=String(value);};
  function attachTopButton() {
    const header=root?.querySelector('.aio-cv__format-head') ||
      root?.querySelector('.aio-cv__pane--right .aio-cv__pane-head');
    if(!header || !bar)return;
    if(!stateButton){
      stateButton=document.createElement('button');
      stateButton.type='button';
      stateButton.className='aio-guard-state-button';
      stateButton.textContent='Состояние';
      stateButton.setAttribute('aria-haspopup','dialog');
      stateButton.setAttribute('aria-expanded','false');
      stateButton.addEventListener('click',()=>{
        const dialog=bar?.querySelector('.aio-guard-popup');
        if(!dialog||dialog.open)return;
        dialog.showModal();
        stateButton.setAttribute('aria-expanded','true');
        refresh();renderRows();
      });
    }
    if(!stateButton.isConnected)
      header.insertBefore(stateButton,header.querySelector('[data-cv-select-all-formats]'));
  }
  function mount() {
    const next=document.querySelector('.aio-cv');
    if(!next){stateButton?.remove();stateButton=null;root=bar=null;rowCache=paintedRows=null;if(clock){clearInterval(clock);clock=null;}return;}
    if(next===root&&bar?.isConnected){attachTopButton();return;}
    stateButton?.remove();stateButton=null;
    root=next;rowCache=paintedRows=null;
    const footer=root.querySelector('.aio-cv__footer');if(!footer)return;
    bar=document.createElement('div');bar.className='aio-guard-queue';bar.hidden=true;
    bar.innerHTML='<div class="aio-guard-current"><span data-g-current></span><button type="button" data-g-pause>Пауза</button><button type="button" data-g-skip>Пропустить файл</button></div>'+
      '<dialog class="aio-guard-popup" aria-label="Состояние очереди Guard">'+
      '<div class="aio-guard-dialog-head"><strong>Состояние</strong><button type="button" data-g-close aria-label="Закрыть состояние">×</button></div>'+
      '<div data-g-counts></div><div class="aio-guard-warning" data-g-warning></div>'+
      '<div class="aio-guard-tools"><input type="search" placeholder="Имя файла или формат" aria-label="Поиск в очереди"><select aria-label="Состояние задач"><option value="all">Все задачи</option><option value="error">Ошибки</option><option value="pending">Ожидают</option><option value="ready">Готовые</option></select></div>'+
      '<div data-g-storage></div><label class="aio-guard-quality"><input type="checkbox" data-g-quality> Проверить структуру результатов <small>Дольше; без проверки внешнего вида</small></label>'+
      '<div class="aio-guard-quality" data-g-verify>Сохранённые файлы проверяются автоматически <small>Сверяем байты с результатом AIO после записи на диск</small></div>'+
      '<div class="aio-guard-save" data-g-resources><select data-g-html-source aria-label="Исходник HTML"></select><button type="button" data-g-add-resources>Добавить папку ресурсов HTML</button><input type="file" data-g-resource-picker webkitdirectory multiple hidden></div>'+
      '<div class="aio-guard-list" role="list" aria-label="Задачи конвертации"></div>'+
      '<div class="aio-guard-pages"><button type="button" data-g-prev>Назад</button><span data-g-page></span><button type="button" data-g-next>Далее</button></div>'+
      '<div class="aio-guard-save"><select data-g-jobs aria-label="Несохранённое задание"></select><button type="button" data-g-zip>Сохранить ZIP</button><button type="button" data-g-folder>Сохранить в папку</button></div>'+
      '<div class="aio-guard-save"><button type="button" data-g-retry>Повторить ошибки</button><button type="button" data-g-long>Повторить с большим временем</button><button type="button" data-g-check>Повторить проверку дольше</button><button type="button" data-g-report>Технический отчёт</button><button type="button" data-g-discard>Удалить результаты</button></div>'+
      '</dialog>';
    footer.append(bar);attachTopButton();
    bar.querySelector('[data-g-folder]').hidden=!window.AllInOneNativeShell;
    const action=(selector,work)=>bar.querySelector(selector).addEventListener('click',async event=>{
      if(pendingActions.has(selector))return;
      const button=event.currentTarget;pendingActions.add(selector);button.disabled=true;
      actionFeedback='Выполняется команда…';feedbackRevision=state?.revision;
      renderCurrent();
      text('[data-g-warning]','Выполняется запрос…');
      try{const result=await work();
        actionFeedback=result?.saved?'Сохранено файлов: '+(result.outputs??''):
          'Команда принята. Обновляем состояние…';
        feedbackRevision=state?.revision;
        text('[data-g-warning]',result?.saved?'Сохранено файлов: '+(result.outputs??'')+
          (result.savedAll?'':' · остальные результаты остаются в AIO.'):'Запрос принят.');
        renderCurrent();
        await refresh();
      }catch(error){
        actionFeedback=error.userMessage||error.message||'Команда не выполнена.';
        feedbackRevision=state?.revision;
        text('[data-g-warning]',actionFeedback);renderCurrent();
      }
      finally{pendingActions.delete(selector);button.disabled=false;render();}
    });
    action('[data-g-pause]',()=>api().pause(!state?.pauseRequested));
    action('[data-g-skip]',()=>api().skipCurrent(state?.currentTask?.fileId));
    const selected=()=>bar.querySelector('[data-g-jobs]').value;
    action('[data-g-zip]',()=>api().saveRetained(selected(),'zip'));
    action('[data-g-folder]',()=>api().saveRetained(selected(),'folder'));
    action('[data-g-discard]',async()=>{
      if(window.confirm('Удалить несохранённые результаты этого задания?'))await api().discardRetained(selected());
    });
    action('[data-g-retry]',()=>api().retryFailed());
    action('[data-g-long]',()=>api().retryFailed({timeoutSeconds:Math.max(3600,(state?.fileTimeoutSeconds||1800)*2)}));
    action('[data-g-check]',()=>new Promise(done=>root.dispatchEvent(new CustomEvent('aio-guard-recheck',
      {bubbles:true,detail:{timeoutSeconds:Math.max(600,(state?.preflightTimeoutSeconds||120)*2),done}}))));
    action('[data-g-report]',async()=>{
      const path=selected()||state?.jobId;
      const response=await fetch(path?'/api/converter-guard/report?job='+encodeURIComponent(path):'/api/converter-guard/recoverable',
        {headers:{'X-AIO-Token':document.body.dataset.aioToken},cache:'no-store'});
      if(!response.ok)throw Error('Отчёт недоступен.');
      const url=URL.createObjectURL(await response.blob()),anchor=document.createElement('a');
      anchor.href=url;anchor.download='AIO-conversion-report.json';anchor.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
    });
    const qualityInput=bar.querySelector('[data-g-quality]');
    qualityInput.checked=api()?.getQualityCheck?.()===true;
    qualityInput.addEventListener('change',()=>{
      try{api()?.setQualityCheck?.(qualityInput.checked);}catch(error){
        qualityInput.checked=api()?.getQualityCheck?.()===true;text('[data-g-warning]',error.message);}
    });
    bar.querySelector('[data-g-verify]').hidden=!window.AllInOneNativeShell;
    const picker=bar.querySelector('[data-g-resource-picker]');
    bar.querySelector('[data-g-add-resources]').addEventListener('click',()=>picker.click());
    picker.addEventListener('change',async()=>{
      const button=bar.querySelector('[data-g-add-resources]');
      if(!picker.files?.length||button.disabled)return;
      const fileId=bar.querySelector('[data-g-html-source]').value;
      button.disabled=true;
      try{
        await api().attachHtmlResources({fileId,files:picker.files});
        root.dispatchEvent(new CustomEvent('aio-guard-resources-ready',{bubbles:true}));
        text('[data-g-warning]','Ресурсы загружены. Выбор форматов остаётся за вами.');
      }catch(error){text('[data-g-warning]',error.userMessage||error.message);}
      finally{picker.value='';button.disabled=false;render();}
    });
    bar.querySelector('input').addEventListener('input',()=>{page=0;renderRows();});
    bar.querySelector('.aio-guard-tools select').addEventListener('change',()=>{page=0;renderRows();});
    bar.querySelector('[data-g-prev]').addEventListener('click',()=>{page=Math.max(0,page-1);renderRows();});
    bar.querySelector('[data-g-next]').addEventListener('click',()=>{page++;renderRows();});
    const dialog=bar.querySelector('.aio-guard-popup');
    bar.querySelector('[data-g-close]').addEventListener('click',()=>dialog.close());
    const outsideDialog=event=>{
      if(event.target!==dialog||!Number.isFinite(event.clientX)||!Number.isFinite(event.clientY))return false;
      const bounds=dialog.getBoundingClientRect();
      return event.clientX<bounds.left||event.clientX>bounds.right||
        event.clientY<bounds.top||event.clientY>bounds.bottom;
    };
    let backdropDown=false;
    dialog.addEventListener('pointerdown',event=>{backdropDown=event.button===0&&outsideDialog(event);});
    dialog.addEventListener('pointerup',event=>{
      if(backdropDown&&event.button===0&&outsideDialog(event))dialog.close();
      backdropDown=false;
    });
    dialog.addEventListener('pointercancel',()=>{backdropDown=false;});
    dialog.addEventListener('cancel',event=>{
      event.preventDefault();
      dialog.close();
    });
    dialog.addEventListener('close',()=>{
      stateButton?.setAttribute('aria-expanded','false');
      if(stateButton?.isConnected)stateButton.focus({preventScroll:true});
    });
    bar.querySelector('[data-g-jobs]').addEventListener('change',()=>{page=0;loadPreview();});
    render();refresh();
  }
  async function refresh() {
    if(refreshPending||!api()?.recoverable)return;
    refreshPending=true;
    try{const found=await api().recoverable();jobs=found.jobs||[];warnings=found.warnings||[];storage=found.storage||null;render();}catch(_){}finally{refreshPending=false;}
  }
  function render() {
    if(!bar)return;
    bar.hidden=!state&&!jobs.length&&!warnings.length;
    if(stateButton)stateButton.disabled=bar.hidden;
    if(bar.hidden&&bar.querySelector('.aio-guard-popup').open)bar.querySelector('.aio-guard-popup').close();
    const htmlSources=api()?.getHtmlSources?.()||[],htmlSelect=bar.querySelector('[data-g-html-source]');
    bar.querySelector('[data-g-resources]').hidden=!htmlSources.length;
    const htmlSignature=htmlSources.map(f=>f.id+':'+f.name).join('|');
    if(htmlSelect.dataset.signature!==htmlSignature){
      const previous=htmlSelect.value;htmlSelect.replaceChildren();
      const selectedId=htmlSources.some(f=>f.id===previous)?previous:htmlSources[0]?.id;
      for(const source of htmlSources){const option=document.createElement('option');option.value=source.id;
        if(source.id===selectedId)option.setAttribute('selected','');
        option.textContent=source.name;htmlSelect.append(option);}
      htmlSelect.dataset.signature=htmlSignature;
    }
    bar.closest('.aio-cv__footer')?.classList.toggle('aio-guard-has-queue',!bar.hidden);
    const active=state?.currentTask;
    text('[data-g-storage]',storage?'Слоты Guard: '+storage.jobs+' / '+storage.maxJobs+
      ' · несохранено: '+storage.unsavedOutputs+' файлов · '+Math.ceil(storage.unsavedBytes/1048576)+' МБ':'');
    bar.querySelector('[data-g-quality]').disabled=!!state&&!state.finished&&!state.preparation;
    renderCurrent();
    text('[data-g-counts]',state?.state==='uploading'?'Подготовлено исходников: '+state.uploadedFiles+' / '+state.inputFiles:
      state?.state==='checking'?'Проверено исходников: '+state.preflightCompleted+' / '+state.preflightTotal:
      state?'Обработано '+state.completed+' / '+state.total+' · успешно '+state.succeeded+
      ' · ошибок '+state.failed+' · отменено '+state.cancelled+' · исключено при выполнении '+state.filtered:'Результаты предыдущего запуска');
    text('[data-g-pause]',state?.pauseRequested?'Продолжить':'Пауза');
    bar.querySelector('[data-g-pause]').disabled=!state||state.finished||['checking','prepared','packaging','uploading'].includes(state.state);
    bar.querySelector('[data-g-skip]').disabled=!active||active.stage==='upload';
    const select=bar.querySelector('[data-g-jobs]'),selected=select.value;
    const signature=jobs.map(j=>j.jobId+':'+j.outputs+':'+j.unsavedOutputs+':'+j.state+':'+j.saveInProgress).join('|');
    if(select.dataset.signature!==signature){
      select.replaceChildren();for(const job of jobs){const option=document.createElement('option');
        option.value=job.jobId;option.textContent=(job.salvaged?'Спасено без журнала · ':job.recovered?'Восстановлено · ':'')+(job.label||'Результаты')+' · '+job.outputs+' файлов';
        option.title=job.jobId;select.append(option);}
      if(jobs.some(j=>j.jobId===selected))select.value=selected;select.dataset.signature=signature;
      if(select.value!==previewId){preview=null;loadPreview();}
    }
    const chosen=jobs.find(job=>job.jobId===select.value);
    bar.querySelector('[data-g-folder]').disabled=!chosen||chosen.saveInProgress;
    bar.querySelector('[data-g-zip]').disabled=!chosen||chosen.running||chosen.saveInProgress;
    bar.querySelector('[data-g-zip]').title=chosen?.running?'ZIP доступен после завершения очереди. Сейчас можно сохранить готовые файлы в папку.':'';
    bar.querySelector('[data-g-discard]').disabled=!chosen||chosen.running||chosen.saveInProgress;
    bar.querySelector('[data-g-discard]').title=chosen?.running?'Сначала завершите или отмените очередь. Готовые результаты сейчас можно сохранить.':'';
    for(const key of ['retry','long'])bar.querySelector('[data-g-'+key+']').disabled=!state?.finished||!state.failed||!api()?.canRetry();
    const recheck=bar.querySelector('[data-g-check]');
    recheck.hidden=!state?.preparation||!state.preflightUndetermined;
    recheck.disabled=state?.state!=='prepared';
    bar.querySelector('[data-g-report]').disabled=!jobs.length&&!state?.jobId&&!warnings.length;
    for(const selector of pendingActions)bar.querySelector(selector).disabled=true;
    if(state?.files?.some(f=>f.nativeRoute?.csvImportSampleOnly))
      text('[data-g-warning]','CSV: настройки импорта определены по образцу. Полная сверка ячеек и локали не выполнялась.');
    if(storage?.pendingRecoveryFolders)text('[data-g-warning]','Ещё папок для восстановления: '+storage.pendingRecoveryFolders+'. Сохраните или удалите завершённые пакеты, чтобы освободить слоты.');
    if(warnings.length)text('[data-g-warning]','Не удалось прочитать журнал '+warnings.length+' заданий. Файлы не удалены. Сохраните технический отчёт для восстановления.');
    if(jobs.some(j=>j.salvaged))text('[data-g-warning]','Найдены файлы без надёжного журнала. Их можно сохранить; полнота и связь с исходниками не подтверждены. Оригинальная служебная папка останется до явного удаления.');
    if(state?.files?.some(f=>f.nativeRoute?.csvInspection?.raggedRows))
      text('[data-g-warning]','В CSV найдены строки с разным числом столбцов. Проверьте разделитель и результат; конвертация не запрещена.');
    if(state?.recoveryError)text('[data-g-warning]','Журнал восстановления записан не полностью. Сохраните результаты перед закрытием.');
    if(state?.statusReadError)text('[data-g-warning]',state.statusReadMessage);
    if(bar.querySelector('.aio-guard-popup').open)renderRows();
    if(active&&!clock)clock=setInterval(renderCurrent,1000);
    if(!active&&clock){clearInterval(clock);clock=null;}
  }
  function renderCurrent(){
    if(actionFeedback && state?.revision===feedbackRevision){
      text('[data-g-current]',actionFeedback);return;
    }
    actionFeedback=null;
    const active=state?.currentTask;
    text('[data-g-current]',state?.statusReadError?'Связь со статусом прервана · повторяем запрос':active?active.name+(active.stage==='preflight'?' · проверка исходника':
      active.stage==='upload'?' · подготовка исходника':' → '+active.format.toUpperCase())+' · '+
      Math.max(0,Math.floor((Date.now()-active.startedMs)/1000))+' с':
      state?.state==='paused'?'Очередь на паузе':state?.state==='prepared'?'Исходники проверены. Выберите форматы.':state&&!state.finished?'Подготовка результатов…':
      jobs.length?'Есть несохранённые результаты: '+jobs.length:warnings.length?'Есть результаты с повреждённым журналом':'Обработка завершена');
  }
  async function loadPreview(){
    if(!bar)return;
    const id=bar.querySelector('[data-g-jobs]').value,request=++previewRequest;
    previewId=id;preview=null;
    if(!id||id===state?.jobId){renderRows();return;}
    try{
      const response=await fetch('/api/converter-guard/jobs?job='+encodeURIComponent(id),
        {headers:{'X-AIO-Token':document.body.dataset.aioToken},cache:'no-store'});
      if(!response.ok)throw Error('Не удалось загрузить выбранный пакет.');
      const loaded=await response.json();
      if(request!==previewRequest||bar.querySelector('[data-g-jobs]').value!==id)return;
      preview=loaded;
    }catch(error){if(request===previewRequest)text('[data-g-warning]',error.message);}
    if(request===previewRequest)renderRows();
  }
  function renderRows() {
    if(!bar)return;
    const selected=bar.querySelector('[data-g-jobs]').value;
    const shown=selected&&selected!==state?.jobId?previewId===selected?preview:null:state;
    const search=bar.querySelector('input').value.toLowerCase(),filter=bar.querySelector('.aio-guard-tools select').value;
    const files=shown?.files||[];
    const sameFiles=rowCache&&rowCache.files.length===files.length&&rowCache.files.every((file,i)=>file===files[i]);
    if(!sameFiles||rowCache.search!==search||rowCache.filter!==filter||rowCache.jobId!==shown?.jobId){
      const reusable=rowCache&&rowCache.search===search&&rowCache.filter===filter&&rowCache.jobId===shown?.jobId;
      const previous=new Map(reusable?rowCache.chunks.map(chunk=>[chunk.file,chunk]):[]);
      const chunks=[];let count=0;
      for(const file of files){
        let chunk=previous.get(file);
        if(!chunk){
          const rows=[];
          for(const task of file.targets||[]){
            if(search&&!(file.name+' '+task.format).toLowerCase().includes(search))continue;
            if(filter==='error'&&task.state!=='error'||filter==='pending'&&!['queued','converting'].includes(task.state)||
               filter==='ready'&&!['ready','saved'].includes(task.state))continue;
            rows.push({file,task});
          }
          chunk={file,rows};
        }
        chunks.push(chunk);count+=chunk.rows.length;
      }
      // A one-file status delta must not allocate/walk every other task in
      // an 83,000-task queue. Retain unchanged chunks and page over them.
      rowCache={files,chunks,count,search,filter,jobId:shown?.jobId};
    }
    const pages=Math.max(1,Math.ceil(rowCache.count/50));page=Math.min(page,pages-1);
    const visible=[];let skip=page*50;
    for(const chunk of rowCache.chunks){
      if(skip>=chunk.rows.length){skip-=chunk.rows.length;continue;}
      visible.push(...chunk.rows.slice(skip,skip+50-visible.length));skip=0;
      if(visible.length===50)break;
    }
    const unchanged=paintedRows&&paintedRows.length===visible.length&&paintedRows.every((row,i)=>
      row.file===visible[i].file&&row.task===visible[i].task);
    if(!unchanged){
      const list=bar.querySelector('.aio-guard-list');list.replaceChildren();
      for(const {file,task} of visible){
        const row=document.createElement('div');row.className='aio-guard-task';row.setAttribute('role','listitem');
        const name=document.createElement('strong'),status=document.createElement('span'),detail=document.createElement('small');
        name.textContent=file.name+' → '+task.format.toUpperCase();name.title=task.taskId||'';
        status.textContent=labels[task.state]||task.state;
        const quality=task.quality;
        const qualityText=quality?quality.status==='risks_found'?'Возможны потери структуры · подробности в отчёте':
          quality.qualityCompared?'Часть структуры проверена · внешний вид не проверен':'Структуру сопоставить не удалось':'';
        detail.textContent=task.error||qualityText||(task.warnings||[]).map(w=>w.message).join(' ')||task.stage||'';
        row.append(name,status,detail);list.append(row);
      }
      paintedRows=visible;
    }
    text('[data-g-page]',(shown?.jobId?'Пакет '+shown.jobId.slice(0,8)+' · ':'')+
      rowCache.count+' задач · '+(page+1)+' / '+pages);
    bar.querySelector('[data-g-prev]').disabled=page===0;bar.querySelector('[data-g-next]').disabled=page>=pages-1;
  }
  window.addEventListener('aio-guard-state',event=>{
    state=event.detail;mount();render();
    const job=jobs.find(j=>j.jobId===state?.jobId);
    // Discover completed outputs even when the popup was opened before the
    // first export finished. No extra refresh for unchanged status polls.
    if(state?.finished || state?.partialResult && (!job ||
        job.outputs!==state.partialResult.outputsCount || job.saveInProgress!==state.saveInProgress))refresh();
  });
  window.addEventListener('aio-guard-save-confirmed',event=>{
    const id=event.detail?.jobId;
    const released=event.detail?.released!==false&&event.detail?.savedAll!==false;
    if(released){
      if(id===rowCache?.jobId)rowCache=paintedRows=null;
      if(id===state?.jobId)state=null;
      if(id===previewId){preview=null;previewId=null;}
      jobs=jobs.filter(j=>j.jobId!==id);
    }else if(id===previewId){
      // A partial save changes ready/saved states without changing output
      // count. Reload the selected retained job instead of keeping its old
      // full snapshot indefinitely while another queue is being observed.
      preview=null;rowCache=paintedRows=null;loadPreview();
    }
    if(bar)bar.querySelector('[data-g-jobs]').dataset.signature='';
    refresh();render();
  });
  window.addEventListener('focus',refresh);
  new MutationObserver(mount).observe(document.body,{subtree:true,childList:true});mount();
})();

