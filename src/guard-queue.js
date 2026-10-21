/* Ordinary-mode queue. No automatic selection, export or recovery restart. */
(() => {
  'use strict';
  let root=null, bar=null, state=null, jobs=[], warnings=[], page=0, refreshPending=false, clock=null;
  let preview=null, previewId=null, previewRequest=0;
  const labels={queued:'В очереди',converting:'Конвертируется',ready:'Готово',saved:'Сохранено',
    error:'Ошибка',cancelled:'Отменено',skipped:'Исключено Guard'};
  const api=()=>window.AllInOneBridge?.converter;
  const text=(selector,value)=>{const n=bar?.querySelector(selector);if(n&&n.textContent!==String(value))n.textContent=String(value);};
  function mount() {
    const next=document.querySelector('.aio-cv');
    if(!next){root=bar=null;if(clock){clearInterval(clock);clock=null;}return;}
    if(next===root&&bar?.isConnected)return;
    root=next;
    const footer=root.querySelector('.aio-cv__footer');if(!footer)return;
    bar=document.createElement('div');bar.className='aio-guard-queue';bar.hidden=true;
    bar.innerHTML='<div class="aio-guard-current"><span data-g-current></span><button type="button" data-g-pause>Пауза</button><button type="button" data-g-skip>Пропустить файл</button></div>'+
      '<details><summary>Очередь и результаты</summary><dialog class="aio-guard-popup">'+
      '<div data-g-counts></div><div class="aio-guard-warning" data-g-warning></div>'+
      '<div class="aio-guard-tools"><input type="search" placeholder="Имя файла или формат" aria-label="Поиск в очереди"><select aria-label="Состояние задач"><option value="all">Все задачи</option><option value="error">Ошибки</option><option value="pending">Ожидают</option><option value="ready">Готовые</option></select></div>'+
      '<div class="aio-guard-list" role="list" aria-label="Задачи конвертации"></div>'+
      '<div class="aio-guard-pages"><button type="button" data-g-prev>Назад</button><span data-g-page></span><button type="button" data-g-next>Далее</button></div>'+
      '<div class="aio-guard-save"><select data-g-jobs aria-label="Несохранённое задание"></select><button type="button" data-g-zip>Сохранить ZIP</button><button type="button" data-g-folder>Сохранить в папку</button></div>'+
      '<div class="aio-guard-save"><button type="button" data-g-retry>Повторить ошибки</button><button type="button" data-g-long>Повторить с большим временем</button><button type="button" data-g-check>Повторить проверку дольше</button><button type="button" data-g-report>Технический отчёт</button><button type="button" data-g-discard>Удалить результаты</button></div>'+
      '</dialog></details>';
    footer.append(bar);
    bar.querySelector('[data-g-folder]').hidden=!window.AllInOneNativeShell;
    const action=(selector,work)=>bar.querySelector(selector).addEventListener('click',async event=>{
      const button=event.currentTarget;button.disabled=true;
      try{await work();await refresh();}catch(error){text('[data-g-warning]',error.userMessage||error.message);}
      finally{button.disabled=false;render();}
    });
    action('[data-g-pause]',()=>api().pause(!state?.pauseRequested));
    action('[data-g-skip]',()=>api().skipCurrent());
    const selected=()=>bar.querySelector('[data-g-jobs]').value;
    action('[data-g-zip]',()=>api().saveRetained(selected(),'zip'));
    action('[data-g-folder]',()=>api().saveRetained(selected(),'folder'));
    action('[data-g-discard]',async()=>{
      if(window.confirm('Удалить несохранённые результаты этого задания?'))await api().discardRetained(selected());
    });
    action('[data-g-retry]',()=>api().retryFailed());
    action('[data-g-long]',()=>api().retryFailed({timeoutSeconds:Math.max(3600,(state?.fileTimeoutSeconds||1800)*2)}));
    action('[data-g-check]',()=>new Promise(done=>root.dispatchEvent(new CustomEvent('aio-guard-recheck',
      {detail:{timeoutSeconds:Math.max(600,(state?.preflightTimeoutSeconds||120)*2),done}}))));
    action('[data-g-report]',async()=>{
      const path=selected()||state?.jobId;
      const response=await fetch(path?'/api/converter-guard/report?job='+encodeURIComponent(path):'/api/converter-guard/recoverable',
        {headers:{'X-AIO-Token':document.body.dataset.aioToken},cache:'no-store'});
      if(!response.ok)throw Error('Отчёт недоступен.');
      const url=URL.createObjectURL(await response.blob()),anchor=document.createElement('a');
      anchor.href=url;anchor.download='AIO-conversion-report.json';anchor.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
    });
    bar.querySelector('input').addEventListener('input',()=>{page=0;renderRows();});
    bar.querySelector('.aio-guard-tools select').addEventListener('change',()=>{page=0;renderRows();});
    bar.querySelector('[data-g-prev]').addEventListener('click',()=>{page=Math.max(0,page-1);renderRows();});
    bar.querySelector('[data-g-next]').addEventListener('click',()=>{page++;renderRows();});
    const details=bar.querySelector('details'),dialog=bar.querySelector('.aio-guard-popup');
    details.addEventListener('toggle',()=>{
      if(details.open&&!dialog.open){dialog.showModal();refresh();renderRows();}
      else if(!details.open&&dialog.open)dialog.close();
    });
    dialog.addEventListener('close',()=>{if(details.open)details.open=false;});
    bar.querySelector('[data-g-jobs]').addEventListener('change',()=>{page=0;loadPreview();});
    render();refresh();
  }
  async function refresh() {
    if(refreshPending||!api()?.recoverable)return;
    refreshPending=true;
    try{const found=await api().recoverable();jobs=found.jobs||[];warnings=found.warnings||[];render();}catch(_){}finally{refreshPending=false;}
  }
  function render() {
    if(!bar)return;
    bar.hidden=!state&&!jobs.length&&!warnings.length;
    bar.closest('.aio-cv__footer')?.classList.toggle('aio-guard-has-queue',!bar.hidden);
    const active=state?.currentTask;
    text('[data-g-current]',active?active.name+(active.stage==='preflight'?' · проверка исходника':
      active.stage==='upload'?' · подготовка исходника':' → '+active.format.toUpperCase())+' · '+
      Math.max(0,Math.floor((Date.now()-active.startedMs)/1000))+' с':
      state?.state==='paused'?'Очередь на паузе':state?.state==='prepared'?'Исходники проверены. Выберите форматы.':state&&!state.finished?'Подготовка результатов…':
      jobs.length?'Есть несохранённые результаты: '+jobs.length:warnings.length?'Есть результаты с повреждённым журналом':'Обработка завершена');
    text('[data-g-counts]',state?.state==='uploading'?'Подготовлено исходников: '+state.uploadedFiles+' / '+state.inputFiles:
      state?.state==='checking'?'Проверено исходников: '+state.preflightCompleted+' / '+state.preflightTotal:
      state?'Обработано '+state.completed+' / '+state.total+' · успешно '+state.succeeded+
      ' · ошибок '+state.failed+' · отменено '+state.cancelled+' · исключено при выполнении '+state.filtered:'Результаты предыдущего запуска');
    text('[data-g-pause]',state?.pauseRequested?'Продолжить':'Пауза');
    bar.querySelector('[data-g-pause]').disabled=!state||state.finished||['checking','prepared','packaging','uploading'].includes(state.state);
    bar.querySelector('[data-g-skip]').disabled=!active||active.stage==='upload';
    const select=bar.querySelector('[data-g-jobs]'),selected=select.value;
    const signature=jobs.map(j=>j.jobId+':'+j.outputs).join('|');
    if(select.dataset.signature!==signature){
      select.replaceChildren();for(const job of jobs){const option=document.createElement('option');
        option.value=job.jobId;option.textContent=(job.recovered?'Восстановлено · ':'')+(job.label||'Результаты')+' · '+job.outputs+' файлов';
        option.title=job.jobId;select.append(option);}
      if(jobs.some(j=>j.jobId===selected))select.value=selected;select.dataset.signature=signature;
      if(select.value!==previewId){preview=null;loadPreview();}
    }
    for(const key of ['zip','folder','discard'])bar.querySelector('[data-g-'+key+']').disabled=!jobs.length;
    for(const key of ['retry','long'])bar.querySelector('[data-g-'+key+']').disabled=!state?.finished||!state.failed||!api()?.canRetry();
    const recheck=bar.querySelector('[data-g-check]');
    recheck.hidden=!state?.preparation||!state.preflightUndetermined;
    recheck.disabled=state?.state!=='prepared';
    bar.querySelector('[data-g-report]').disabled=!jobs.length&&!state?.jobId&&!warnings.length;
    if(warnings.length)text('[data-g-warning]','Не удалось прочитать журнал '+warnings.length+' заданий. Файлы не удалены. Сохраните технический отчёт для восстановления.');
    if(state?.recoveryError)text('[data-g-warning]','Журнал восстановления записан не полностью. Сохраните результаты перед закрытием.');
    if(bar.querySelector('details').open)renderRows();
    if(active&&!clock)clock=setInterval(()=>render(),1000);
    if(!active&&clock){clearInterval(clock);clock=null;}
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
    const rows=[];
    for(const file of shown?.files||[])for(const task of file.targets||[]){
      if(search&&!(file.name+' '+task.format).toLowerCase().includes(search))continue;
      if(filter==='error'&&task.state!=='error'||filter==='pending'&&!['queued','converting'].includes(task.state)||
         filter==='ready'&&!['ready','saved'].includes(task.state))continue;
      rows.push({file,task});
    }
    const pages=Math.max(1,Math.ceil(rows.length/50));page=Math.min(page,pages-1);
    const list=bar.querySelector('.aio-guard-list');list.replaceChildren();
    for(const {file,task} of rows.slice(page*50,page*50+50)){
      const row=document.createElement('div');row.className='aio-guard-task';row.setAttribute('role','listitem');
      const name=document.createElement('strong'),status=document.createElement('span'),detail=document.createElement('small');
      name.textContent=file.name+' → '+task.format.toUpperCase();name.title=task.taskId||'';
      status.textContent=labels[task.state]||task.state;
      detail.textContent=task.error||task.stage||'';row.append(name,status,detail);list.append(row);
    }
    text('[data-g-page]',(shown?.jobId?'Пакет '+shown.jobId.slice(0,8)+' · ':'')+
      rows.length+' задач · '+(page+1)+' / '+pages);
    bar.querySelector('[data-g-prev]').disabled=page===0;bar.querySelector('[data-g-next]').disabled=page>=pages-1;
  }
  window.addEventListener('aio-guard-state',event=>{state=event.detail;mount();render();if(state?.finished)refresh();});
  window.addEventListener('aio-guard-save-confirmed',event=>{
    const id=event.detail?.jobId;
    if(id===state?.jobId)state=null;
    if(id===previewId){preview=null;previewId=null;}
    jobs=jobs.filter(j=>j.jobId!==id);
    if(bar)bar.querySelector('[data-g-jobs]').dataset.signature='';
    refresh();render();
  });
  window.addEventListener('focus',refresh);
  new MutationObserver(mount).observe(document.body,{subtree:true,childList:true});mount();
})();
