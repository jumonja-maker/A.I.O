/* M04: report-only history. No result tickets, recovery scan or automatic polling. */
(() => {
  'use strict';
  const prefix='/api/converter-guard/history';
  const idOK=id=>typeof id==='string'&&/^[0-9a-f]{32}$/.test(id);
  window.AIOConversionHistory={mount(dialog){
    const box=document.createElement('details');box.className='aio-guard-history';
    const summary=document.createElement('summary');summary.textContent='История завершённых конвертаций';
    const note=document.createElement('p');note.textContent='Здесь хранятся отчёты, а не файлы результатов. Статус сохранения — последний известный AIO, не проверка файлов на диске сейчас.';
    const status=document.createElement('p');status.setAttribute('role','status');status.setAttribute('aria-live','polite');
    const list=document.createElement('div');list.setAttribute('data-history-list','');list.style.cssText='max-height:20rem;overflow:auto;overflow-wrap:anywhere';
    const controls=document.createElement('div');controls.className='aio-guard-save';
    const button=(label,action)=>{const b=document.createElement('button');b.type='button';b.textContent=label;b.addEventListener('click',action);return b;};
    const refresh=button('Обновить историю',()=>load(null,'reset'));
    const previous=button('Назад',()=>{if(back.length)load(back[back.length-1],'previous');});
    const next=button('Далее',()=>{if(nextCursor!==null)load(nextCursor,'next');});
    controls.append(refresh,previous,next);box.append(summary,note,status,list,controls);dialog.append(box);
    let disposed=false,sequence=0,controller=null,busy=false,cursor=null,nextCursor=null,back=[];
    const urls=new Set(),timers=new Set();
    const active=()=>!disposed&&box.isConnected&&dialog.open&&box.open;
    function buttons(){
      for(const b of box.querySelectorAll('button'))b.disabled=busy;
      previous.disabled=busy||back.length===0;next.disabled=busy||nextCursor===null;
    }
    function stop(){sequence++;controller?.abort();controller=null;busy=false;buttons();}
    async function request(path,options={}){
      const response=await fetch(path,{...options,cache:'no-store',signal:controller.signal,
        headers:{'X-AIO-Token':document.body.dataset.aioToken,
                 ...(options.method==='POST'?{'Content-Type':'application/json'}:{})}});
      const data=await response.json();
      if(!response.ok)throw Error(data.error||'История недоступна. Повторите запрос.');
      return data;
    }
    function begin(){stop();controller=new AbortController();busy=true;buttons();status.textContent='Загружаем историю…';return sequence;}
    const current=version=>active()&&sequence===version;
    const labels={ready:'Конвертация завершена',partial:'Частично выполнено',failed:'Ошибка конвертации',cancelled:'Отменено',saved:'Сохранено'};
    function row(item){
      const entry=document.createElement('section');entry.setAttribute('data-history-job',item.jobId);
      entry.style.cssText='padding:.6rem 0;border-bottom:1px solid currentColor';
      const title=document.createElement('strong');title.textContent=item.firstInput||'Конвертация';
      const description=document.createElement('p');description.textContent=
        new Date(item.recordedMs).toLocaleString()+' · '+(labels[item.state]||item.state)+' · исходников: '+item.inputCount+
        ' · успешно: '+item.succeeded+' · ошибок: '+item.failed+' · сохранено выходов: '+item.savedOutputs+' / '+item.outputs;
      const actions=document.createElement('div');actions.className='aio-guard-save';
      actions.append(button('Скачать отчёт',()=>download(item.jobId)),button('Удалить запись',()=>remove(item.jobId)));
      entry.append(title,description,actions);return entry;
    }
    async function load(target,mode='keep'){
      if(!active()||busy)return;
      const version=begin();
      try{
        const data=await request(prefix+'?limit=25'+(target===null?'':'&before='+encodeURIComponent(target)));
        if(!current(version))return;
        if(data.enabled!==true){status.textContent='Постоянная история недоступна в этом режиме запуска.';return;}
        if(!Array.isArray(data.items)||data.items.length>25||data.items.some(v=>!idOK(v.jobId))||
           data.nextCursor!==null&&(!Number.isSafeInteger(data.nextCursor)||data.nextCursor<=0))throw Error('Некорректная страница истории.');
        const nodes=data.items.map(row);
        if(mode==='reset')back=[];else if(mode==='next')back.push(cursor);else if(mode==='previous')back.pop();
        cursor=target;nextCursor=data.nextCursor;list.replaceChildren(...nodes);
        status.textContent=(data.items.length?'Записей на странице: '+data.items.length+'.':'История пуста.')+
          (data.writeWarning?' Не все отчёты этого запуска удалось записать в историю. Результаты конвертации остаются отдельно.':'');
      }catch(error){if(current(version)&&error.name!=='AbortError')status.textContent=error.message;}
      finally{if(sequence===version){busy=false;buttons();}}
    }
    async function download(id){
      if(!active()||busy)return;
      const version=begin();
      try{
        const data=await request(prefix+'/report?job='+encodeURIComponent(id));
        if(!current(version))return;
        if(data.historySnapshot?.jobId!==id)throw Error('Получен отчёт другой записи.');
        const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json;charset=utf-8'}));urls.add(url);
        const a=document.createElement('a');a.href=url;a.download='AIO-history-'+id+'.json';a.click();
        const timer=setTimeout(()=>{URL.revokeObjectURL(url);urls.delete(url);timers.delete(timer);},1000);timers.add(timer);
        status.textContent='Отчёт передан для сохранения. Это не копия результатов конвертации.';
      }catch(error){if(current(version)&&error.name!=='AbortError')status.textContent=error.message;}
      finally{if(sequence===version){busy=false;buttons();}}
    }
    async function remove(id){
      if(!active()||busy||!window.confirm('Удалить только запись истории? Исходники и результаты конвертации не будут удалены.'))return;
      const version=begin();let removed=false;
      try{
        const data=await request(prefix+'/delete',{method:'POST',body:JSON.stringify({jobId:id,confirm:true})});
        if(!current(version))return;
        if(data.deleted!==true||data.jobId!==id)throw Error('Удаление записи не подтверждено.');
        removed=true;
      }catch(error){if(current(version)&&error.name!=='AbortError')status.textContent=error.message;}
      finally{if(sequence===version){busy=false;buttons();}}
      if(removed&&current(version))await load(cursor);
    }
    function toggle(){if(box.open)load(null,'reset');else stop();}
    function closed(){stop();box.open=false;}
    function hidden(){stop();box.open=false;}
    box.addEventListener('toggle',toggle);dialog.addEventListener('close',closed);window.addEventListener('pagehide',hidden);
    buttons();
    return {dispose(){
      if(disposed)return;disposed=true;stop();box.removeEventListener('toggle',toggle);
      dialog.removeEventListener('close',closed);window.removeEventListener('pagehide',hidden);
      for(const timer of timers)clearTimeout(timer);for(const url of urls)URL.revokeObjectURL(url);
      timers.clear();urls.clear();list.replaceChildren();box.remove();
    }};
  }};
})();
