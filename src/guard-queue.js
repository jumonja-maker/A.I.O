/* Ordinary-mode queue. No automatic selection, export or recovery restart. */
(() => {
  'use strict';
  let root=null, bar=null, stateButton=null, state=null, jobs=[], warnings=[], storage=null, page=0, refreshPending=false, clock=null;
  let preview=null, previewId=null, previewRequest=0, previewController=null;
  let refreshRevision=0, refreshAgain=false, viewRevision=0;
  let historyPanel=null;
  let renderedRecoveryWarnings=null;
  let recoveryScan=null,recoveryTimer=null,refreshError=false,pageHidden=false;
  function stopRecoveryPoll(){if(recoveryTimer!==null){clearTimeout(recoveryTimer);recoveryTimer=null;}}
  function scheduleRecoveryPoll(){
    stopRecoveryPoll();
    if(pageHidden||refreshError||!bar?.querySelector('.aio-guard-popup')?.open)return;
    if(recoveryScan?.state==='scanning'||storage?.cleanupPending&&!storage?.cleanupRetryJobs)
      recoveryTimer=setTimeout(()=>{recoveryTimer=null;refresh();},1000);
  }

  function dropRows(id){
    if(id&&rowCache?.jobId!==id)return;
    rowCache=paintedRows=null;
    bar?.querySelector('.aio-guard-list')?.replaceChildren();
  }
  function cancelPreview(){
    previewRequest++;previewController?.abort();previewController=null;preview=previewId=null;
  }
  // Only the visible page owns DOM nodes. Keep one bounded view per file;
  // unchanged polls and the elapsed-time clock must never rebuild the queue.
  let rowCache=null, paintedRows=null;
  const pendingActions=new Set();
  let actionFeedback=null,feedbackRevision=null;
  const labels={queued:'В очереди',converting:'Конвертируется',ready:'Готово',saved:'Сохранено',
    error:'Ошибка',cancelled:'Отменено',skipped:'Исключено Guard'};
  const recoveryReasons={
    RECOVERY_UNFINISHED_PREPARATION:'Осталась незавершённая подготовка. Готовые результаты не подтверждены.',
    RECOVERY_MANIFEST_MISSING:'Не найдено описание задания job.json.',
    RECOVERY_LEDGER_INCOMPLETE:'Журнал результатов неполон. Полный состав результатов не подтверждён.',
    RECOVERY_METADATA_INVALID:'Служебные сведения задания не удалось прочитать или проверить.',
    RECOVERY_OUTPUT_MISSING:'Ожидаемый выходной файл отсутствует или изменён.',
    RECOVERY_OUTPUT_UNREADABLE:'Результат заблокирован или недоступен для чтения. Остальные файлы можно сохранить.',
    RECOVERY_UNTRACKED_OUTPUTS:'Найдены файлы без подтверждённой записи в журнале.',
    RECOVERY_RETIREMENT_INVALID:'Маркер удаления не прошёл проверку. Каталог не удалён.',
    RECOVERY_CLEANUP_RETRY:'Не завершилась очистка ранее удалённого задания.',
    RECOVERY_SALVAGED_UNVERIFIED:'Найдены файлы, но полнота и связь с исходниками не подтверждены.',
    RECOVERY_WRITE_FAILED:'Запись служебного журнала не завершилась.',
    RECOVERY_BUSY:'Каталог занят другим экземпляром AIO или заблокирован. Освободите его и повторите поиск.',
    RECOVERY_ACCESS_DENIED:'Нет доступа к каталогу или журналу. Восстановите доступ и повторите поиск.',
    RECOVERY_READ_FAILED:'Не удалось прочитать каталог или журнал. Файлы оставлены на диске; можно повторить поиск.'
  };
  function renderRecoveryWarnings(){
    const box=bar?.querySelector('[data-g-recovery]');
    if(!box||renderedRecoveryWarnings===warnings)return;
    renderedRecoveryWarnings=warnings;box.hidden=!warnings.length;
    const list=box.querySelector('[data-g-recovery-list]');list.replaceChildren();
    box.querySelector('summary').textContent='Подробности восстановления: '+warnings.length;
    for(const warning of warnings.slice(0,40)){
      const row=document.createElement('p');row.setAttribute('data-g-recovery-entry','');
      const code=typeof warning?.code==='string'?warning.code.slice(0,96):'RECOVERY_UNKNOWN';
      const folder=typeof warning?.folder==='string'?warning.folder.slice(0,160):'Задание не определено';
      const integrity=typeof warning?.integrity==='string'?' · '+warning.integrity.slice(0,64):'';
      row.textContent=folder+' · '+code+integrity+' — '+
        (recoveryReasons[code]||'Причина требует проверки. Сохраните технический отчёт.');
      list.append(row);
    }
    if(warnings.length>40){const note=document.createElement('p');
      note.textContent='Ещё предупреждений: '+(warnings.length-40)+'. Полный список — в техническом отчёте.';list.append(note);}
  }
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
        viewRevision++;actionFeedback=null;
        bar.hidden=false;dialog.showModal();
        stateButton.setAttribute('aria-expanded','true');
        refresh();renderRows();
      });
    }
    if(!stateButton.isConnected)
      header.insertBefore(stateButton,header.querySelector('[data-cv-select-all-formats]'));
  }
  function mount() {
    const next=document.querySelector('.aio-cv');
    if(!next){historyPanel?.dispose();historyPanel=null;if(root)refreshRevision++;stopRecoveryPoll();cancelPreview();dropRows();stateButton?.remove();stateButton=null;root=bar=null;if(clock){clearInterval(clock);clock=null;}return;}
    if(next===root&&bar?.isConnected){attachTopButton();return;}
    refreshRevision++;stopRecoveryPoll();cancelPreview();dropRows();stateButton?.remove();stateButton=null;
    historyPanel?.dispose();historyPanel=null;
    root=next;rowCache=paintedRows=null;renderedRecoveryWarnings=null;
    const footer=root.querySelector('.aio-cv__footer');if(!footer)return;
    bar=document.createElement('div');bar.className='aio-guard-queue';bar.hidden=true;
    bar.innerHTML='<div class="aio-guard-current"><span data-g-current></span><button type="button" data-g-pause>Пауза</button><button type="button" data-g-skip>Пропустить файл</button></div>'+
      '<dialog class="aio-guard-popup" aria-label="Состояние очереди Guard">'+
      '<div class="aio-guard-dialog-head"><strong>Состояние</strong><button type="button" data-g-close aria-label="Закрыть состояние">×</button></div>'+
      '<div data-g-counts></div><div class="aio-guard-warning" data-g-warning></div>'+
      '<div class="aio-guard-save"><button type="button" data-g-scan>Найти предыдущие результаты</button><button type="button" data-g-refresh>Обновить список</button></div>'+
      '<div data-g-scan-status role="status" aria-live="polite"></div>'+
      '<details data-g-recovery hidden><summary>Подробности восстановления</summary><div data-g-recovery-list style="max-height:12rem;overflow:auto;overflow-wrap:anywhere"></div></details>'+
      '<div class="aio-guard-tools"><input type="search" placeholder="Имя файла или формат" aria-label="Поиск в очереди"><select aria-label="Состояние задач"><option value="all">Все задачи</option><option value="error">Ошибки</option><option value="pending">Ожидают</option><option value="ready">Готовые</option></select></div>'+
      '<div data-g-storage></div><label class="aio-guard-quality"><input type="checkbox" data-g-quality> Проверить структуру результатов <small>Дольше; без проверки внешнего вида</small></label>'+
      '<div class="aio-guard-quality" data-g-verify>Сохранённые файлы проверяются автоматически <small>Сверяем байты с результатом AIO после записи на диск</small></div>'+
      '<div class="aio-guard-save" data-g-resources><select data-g-html-source aria-label="Исходник HTML"></select><button type="button" data-g-add-resources>Добавить папку ресурсов HTML</button><input type="file" data-g-resource-picker webkitdirectory multiple hidden></div>'+
      '<div class="aio-guard-list" role="list" aria-label="Задачи конвертации"></div>'+
      '<div class="aio-guard-pages"><button type="button" data-g-prev>Назад</button><span data-g-page></span><button type="button" data-g-next>Далее</button></div>'+
      '<div class="aio-guard-save"><select data-g-jobs size="4" aria-label="Доступные пакеты результатов"></select><button type="button" data-g-zip>Сохранить ZIP</button><button type="button" data-g-folder>Сохранить в папку</button></div>'+
      '<div class="aio-guard-save"><button type="button" data-g-retry>Повторить ошибки</button><button type="button" data-g-long>Повторить с большим временем</button><button type="button" data-g-check>Повторить проверку дольше</button><button type="button" data-g-report>Технический отчёт</button><button type="button" data-g-discard>Удалить результаты</button></div>'+
      '</dialog>';
    footer.append(bar);attachTopButton();
    historyPanel=window.AIOConversionHistory?.mount(bar.querySelector('.aio-guard-popup'));
    bar.querySelector('[data-g-folder]').hidden=!window.AllInOneNativeShell;
    const action=(selector,work)=>bar.querySelector(selector).addEventListener('click',async event=>{
      if(pendingActions.has(selector))return;
      const button=event.currentTarget,view=bar,revision=viewRevision;
      const current=()=>!pageHidden&&bar===view&&revision===viewRevision;
      pendingActions.add(selector);button.disabled=true;
      actionFeedback='Выполняется команда…';feedbackRevision=state?.revision;
      renderCurrent();
      text('[data-g-warning]','Выполняется запрос…');
      try{const result=await work();
        if(!current())return;
        actionFeedback=result?.message||(result?.cancelled?'Действие отменено.':
          result?.saved?'Сохранено файлов: '+(result.outputs??''):
          result?.downloadStarted?'Загрузка начата. Сохранение конечной копии не подтверждено; результаты остаются в AIO.':'Команда выполнена.');
        feedbackRevision=state?.revision;
        text('[data-g-warning]',result?.saved?'Сохранено файлов: '+(result.outputs??'')+
          (result.savedAll?'':(result.remainingCompanionFiles&&!result.remainingDocuments
            ?' · остался несохранённый служебный манифест листов.':' · остальные результаты остаются в AIO.')):'Запрос принят.');
        renderCurrent();
        await refresh();
      }catch(error){
        if(!current())return;
        actionFeedback=error.userMessage||error.message||'Команда не выполнена.';
        feedbackRevision=state?.revision;
        text('[data-g-warning]',actionFeedback);renderCurrent();
      }
      finally{pendingActions.delete(selector);button.disabled=false;render();}
    });
    action('[data-g-scan]',async()=>{
      if(!api()?.scanRecovery)throw Error('Поиск предыдущих результатов недоступен.');
      const found=await api().scanRecovery();
      return {message:found.recoveryScan?.state==='scanning'?'Поиск начат. Ход проверки показан ниже.':'Запрос поиска обработан.'};
    });
    bar.querySelector('[data-g-refresh]').addEventListener('click',()=>refresh());
    action('[data-g-pause]',()=>api().pause(!state?.pauseRequested));
    action('[data-g-skip]',()=>api().skipCurrent(state?.currentTask?.fileId));
    const selected=()=>bar.querySelector('[data-g-jobs]').value;
    action('[data-g-zip]',()=>api().saveRetained(selected(),'zip'));
    action('[data-g-folder]',()=>api().saveRetained(selected(),'folder'));
    action('[data-g-discard]',async()=>{
      if(!window.confirm('Удалить несохранённые результаты этого задания?'))return {cancelled:true};
      await api().discardRetained(selected());
      return {message:'Удаление принято. Состояние очистки показано ниже.'};
    });
    action('[data-g-retry]',()=>api().retryFailed());
    action('[data-g-long]',()=>api().retryFailed({timeoutSeconds:Math.max(3600,(state?.fileTimeoutSeconds||1800)*2)}));
    action('[data-g-check]',()=>new Promise(done=>root.dispatchEvent(new CustomEvent('aio-guard-recheck',
      {bubbles:true,detail:{timeoutSeconds:Math.max(600,(state?.preflightTimeoutSeconds||120)*2),done}}))));
    action('[data-g-report]',async()=>{
      const path=selected()||state?.jobId;
      const response=await fetch(path?'/api/converter-guard/report?job='+encodeURIComponent(path)+'&lookup=current':'/api/converter-guard/recoverable?mode=snapshot',
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
      // Native close is queued: an older close must not clear a reopened view.
      if(dialog.open)return;
      viewRevision++;refreshRevision++;actionFeedback=null;cancelPreview();
      stopRecoveryPoll();dropRows();
      stateButton?.setAttribute('aria-expanded','false');
      if(stateButton?.isConnected)stateButton.focus({preventScroll:true});
      render();
    });
    bar.querySelector('[data-g-jobs]').addEventListener('change',()=>{page=0;loadPreview();render();});
    render();refresh();
  }
  async function refresh() {
    if(pageHidden||!bar||!root?.isConnected||!api()?.recoverable)return;
    if(refreshPending){refreshAgain=true;return;}
    stopRecoveryPoll();refreshPending=true;const revision=refreshRevision,view=bar;
    try{
      const found=await api().recoverable({scan:false});
      if(pageHidden||revision!==refreshRevision||bar!==view)return;
      jobs=found.jobs||[];warnings=found.warnings||[];storage=found.storage||null;
      recoveryScan=found.recoveryScan||null;refreshError=false;render();
    }catch(_){
      if(!pageHidden&&revision===refreshRevision&&bar===view){refreshError=true;render();}
    }finally{
      refreshPending=false;
      if(refreshAgain){refreshAgain=false;refresh();}else scheduleRecoveryPoll();
    }
  }
  function render() {
    if(!bar)return;
    bar.hidden=!state&&!jobs.length&&!warnings.length&&!bar.querySelector('.aio-guard-popup').open;
    if(stateButton){
      // Empty current work must not lock the user out of manual recovery.
      stateButton.disabled=false;
      const checking=state?.state==='checking';
      const routing=checking&&state?.preflightPhase==='routes';
      const stopping=checking&&state?.cancellationRequested===true;
      const done=routing?(state?.preflightMatrixCompleted??0):(state?.preflightCompleted??0);
      const total=routing?(state?.preflightMatrixTotal??0):(state?.preflightTotal??0);
      const label=stopping?'Отменяем…':checking?
        (routing?'Направления ':'Проверка ')+done+' / '+total:'Состояние';
      if(stateButton.textContent!==label)stateButton.textContent=label;
      stateButton.classList.toggle('aio-guard-checking',checking);
      stateButton.title=checking
        ?stopping?'Отмена запрошена. Ожидаем остановки текущего вычисления.'
        :routing?'Расчёт доступных направлений: '+done+' / '+total+'. Отмена доступна в панели конвертера.'
        :'Предварительная проверка: '+done+' из '+total+'. Нажмите для подробностей и пропуска файла.'
        :'Открыть состояние очереди Guard';
    }
    text('[data-g-warning]',actionFeedback&&state?.revision===feedbackRevision?actionFeedback:'');
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
    const currentJobs=jobs.filter(job=>!job.recovered);
    text('[data-g-storage]',storage?'Слоты Guard: '+storage.jobs+' / '+storage.maxJobs+
      ' · текущих несохранённых: '+currentJobs.reduce((sum,job)=>sum+(job.unsavedOutputs||0),0)+
      ' · предыдущих пакетов в списке: '+jobs.filter(job=>job.recovered).length:'');
    const scanButton=bar.querySelector('[data-g-scan]');
    scanButton.hidden=!api()?.scanRecovery||recoveryScan?.available===false;
    scanButton.disabled=recoveryScan?.state==='scanning'||
      recoveryScan?.loaded===true&&!recoveryScan?.pendingFolders&&recoveryScan?.state!=='failed';
    scanButton.textContent=recoveryScan?.state==='failed'?'Повторить поиск':
      recoveryScan?.pendingFolders?'Продолжить поиск':'Найти предыдущие результаты';
    const scanMessage=refreshError?'Не удалось обновить список. Уже показанные результаты сохранены. Нажмите «Обновить список».':
      recoveryScan?.state==='scanning'?'Ищем предыдущие результаты и проверяем целостность. Можно закрыть это окно; конвертация не запускается.':
      recoveryScan?.state==='failed'?'Поиск не завершён (RECOVERY_SCAN_FAILED). Доступные файлы не удалены. Можно повторить поиск.':
      recoveryScan?.retryFolders?'Каталогов для повторной попытки: '+recoveryScan.retryFolders+'. Причины указаны ниже. Освободите файлы или восстановите доступ, затем продолжите поиск.':
      recoveryScan?.pendingFolders?'Ожидают свободных слотов: '+recoveryScan.pendingFolders+'. Сохраните или удалите ненужные пакеты и продолжите поиск.':
      recoveryScan?.loaded?'Поиск предыдущих результатов завершён. Это не проверка качества конвертации.':
      'Старые папки автоматически не загружаются. Для проверки нажмите «Найти предыдущие результаты».';
    const cleanupMessage=storage?.cleanupRetryJobs?' · Очистка требует повторной попытки: '+storage.cleanupRetryJobs+'. Нажмите «Обновить список».':
      storage?.cleanupPending?' · Удаляем служебные папки: '+storage.cleanupPending+'. Место учитывается до завершения.':'';
    text('[data-g-scan-status]',scanMessage+cleanupMessage);
    bar.querySelector('[data-g-quality]').disabled=!!state&&!state.finished&&!state.preparation;
    renderCurrent();renderRecoveryWarnings();
    text('[data-g-counts]',state?.state==='uploading'?'Подготовлено исходников: '+state.uploadedFiles+' / '+state.inputFiles:
      state?.state==='checking'?(state.cancellationRequested?'Отменяем проверку… · ':
        state.preflightPhase==='routes'?'Направления: '+(state.preflightMatrixCompleted??0)+' / '+(state.preflightMatrixTotal??0)+' · ':'')+
        'Исходников: '+state.preflightCompleted+' / '+state.preflightTotal:
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
    if(warnings.length)text('[data-g-warning]','Проблемы восстановления: '+warnings.length+' предупреждений. Причины перечислены ниже; это не подтверждение повреждения документов.');
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
    const elapsed=active?.startedMs?Math.max(0,Math.floor((Date.now()-active.startedMs)/1000)):0;
    const checking=state?.state==='checking';
    const count=checking?' · проверено '+(state.preflightCompleted??0)+' / '+(state.preflightTotal??0):'';
    text('[data-g-current]',state?.statusReadError?'Связь со статусом прервана · повторяем запрос':
      checking&&state.cancellationRequested?'Останавливаем проверку. Исходники остаются сохранными…':
      checking&&state.preflightPhase==='routes'?
        'Файлы проверены · формируем направления '+(state.preflightMatrixCompleted??0)+' / '+(state.preflightMatrixTotal??0):
      active?
      active.name+(active.stage==='preflight'?' · проверка исходника'+count:
      active.stage==='upload'?' · подготовка исходника':' → '+active.format.toUpperCase())+
      ' · '+elapsed+' с'+(active.stage==='preflight'&&elapsed>=30?
      ' · файл обрабатывается дольше обычного; можно пропустить его':''):
      state?.state==='checking'?'Проверено '+(state.preflightCompleted??0)+' / '+(state.preflightTotal??0)+
        ' · подготовка направлений…':
      state?.state==='paused'?'Очередь на паузе':state?.state==='prepared'?'Исходники проверены. Выберите форматы.':state&&!state.finished?'Подготовка результатов…':
      jobs.some(job=>!job.recovered&&job.unsavedOutputs>0)?'Есть текущие несохранённые результаты: '+jobs.filter(job=>!job.recovered).reduce((sum,job)=>sum+(job.unsavedOutputs||0),0):
      warnings.length?'Проблемы восстановления предыдущих заданий: '+warnings.length:
      jobs.some(job=>job.recovered)?'Предыдущие результаты доступны в «Состоянии».':'Текущих несохранённых результатов нет.');
  }
  async function loadPreview(){
    if(!bar)return;
    const id=bar.querySelector('[data-g-jobs]').value;
    cancelPreview();const request=previewRequest;previewId=id;
    const Controller=window.AbortController||(typeof AbortController==='function'?AbortController:null);
    const controller=Controller?new Controller():null;previewController=controller;
    if(!id||id===state?.jobId){previewController=null;renderRows();return;}
    try{
      const response=await fetch('/api/converter-guard/jobs?job='+encodeURIComponent(id)+'&lookup=current',
        {headers:{'X-AIO-Token':document.body.dataset.aioToken},cache:'no-store',signal:controller?.signal});
      if(!response.ok)throw Error('Не удалось загрузить выбранный пакет.');
      const loaded=await response.json();
      if(request!==previewRequest||!bar||bar.querySelector('[data-g-jobs]').value!==id)return;
      preview=loaded;
    }catch(error){if(request===previewRequest&&error.name!=='AbortError')text('[data-g-warning]',error.message);}
    finally{if(request===previewRequest)previewController=null;}
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
    const previousId=state?.jobId;state=event.detail;
    if(!state&&previousId)dropRows(previousId);
    mount();render();
    const job=jobs.find(j=>j.jobId===state?.jobId);
    // Discover completed outputs even when the popup was opened before the
    // first export finished. No extra refresh for unchanged status polls.
    if(state?.finished || state?.partialResult && (!job ||
        job.outputs!==state.partialResult.outputsCount || job.saveInProgress!==state.saveInProgress))refresh();
  });
  window.addEventListener('aio-guard-save-confirmed',event=>{
    const id=event.detail?.jobId;if(!id)return;
    const released=event.detail?.released!==false&&event.detail?.savedAll!==false;
    if(released){
      refreshRevision++;dropRows(id);
      if(id===state?.jobId)state=null;
      if(id===previewId)cancelPreview();
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
  window.addEventListener('aio-guard-job-released',event=>{
    const id=event.detail?.jobId;if(!id)return;
    refreshRevision++;dropRows(id);
    if(id===state?.jobId)state=null;
    if(id===previewId)cancelPreview();
    jobs=jobs.filter(job=>job.jobId!==id);
    if(bar)bar.querySelector('[data-g-jobs]').dataset.signature='';
    refresh();render();
  });
  window.addEventListener('focus',refresh);
  const observer=new MutationObserver(mount);
  observer.observe(document.body,{subtree:true,childList:true});mount();
  window.addEventListener('pagehide',()=>{
    pageHidden=true;stopRecoveryPoll();refreshRevision++;refreshAgain=false;cancelPreview();dropRows();state=null;jobs=[];warnings=[];storage=null;renderedRecoveryWarnings=null;
    if(clock){clearInterval(clock);clock=null;}observer.disconnect();
  });
})();

