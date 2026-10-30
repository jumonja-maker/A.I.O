/* Explicit discovery/save/discard for the separate legacy API. No polling. */
(() => {
  'use strict';
  window.AIOLegacyRecovery={mount(host){
    const token=document.body.dataset.aioToken;
    if(!host||!token)return {dispose(){}};
    const panel=document.createElement('details');
    panel.setAttribute('data-legacy-recovery','');
    panel.innerHTML='<summary>Результаты прежнего конвертера</summary>'+
      '<p>Здесь доступны результаты прежних заданий. Сохранение копии не удаляет их из AIO.</p>'+
      '<div class="aio-guard-save"><button type="button" data-l-refresh>Обновить список</button></div>'+
      '<p data-l-status role="status" aria-live="polite"></p><p data-l-storage></p>'+
      '<div data-l-list style="max-height:16rem;overflow:auto;overflow-wrap:anywhere"></div>'+
      '<div class="aio-guard-pages"><button type="button" data-l-prev>Назад</button><span data-l-page></span><button type="button" data-l-next>Далее</button></div>'+
      '<div data-l-warnings style="overflow-wrap:anywhere"></div>';
    host.append(panel);
    let alive=true,revision=0,offset=0,total=0,more=false,pending=false,loading=false,controller=null;
    const get=selector=>panel.querySelector(selector);
    const status=message=>{get('[data-l-status]').textContent=message;};
    const current=id=>alive&&panel.isConnected&&host.open&&panel.open&&revision===id;
    const controls=()=>{
      get('[data-l-refresh]').disabled=pending||loading;
      get('[data-l-prev]').disabled=pending||loading||offset===0;
      get('[data-l-next]').disabled=pending||loading||!more;
      for(const button of panel.querySelectorAll('[data-l-action]'))button.disabled=pending||loading;
    };
    async function api(path,body,signal){
      const response=await fetch('/api/converter/'+path,{method:body?'POST':'GET',
        headers:{'X-AIO-Token':token,...(body?{'Content-Type':'application/json'}:{})},
        ...(body?{body:JSON.stringify(body)}:{}),cache:'no-store',signal});
      const packet=await response.json();
      if(!response.ok)throw Error(packet.error||'Не удалось получить прежние результаты.');
      return packet;
    }
    async function act(identity,discard){
      if(pending||loading)return;
      if(discard&&!window.confirm('Удалить этот результат из AIO? Убедитесь, что нужная копия сохранена.'))return;
      const id=revision;pending=true;controls();status(discard?'Удаляем результат…':'Открываем сохранение копии…');
      try{
        const found=await api('recover?request='+identity);
        if(!current(id))return;
        if(found.state!=='ready'||!found.result)throw Error('Результат больше недоступен. Обновите список.');
        const result=found.result;
        if(!/^[A-Za-z0-9_-]{32,128}$/.test(result.ticket)||
           result.downloadUrl!=='/api/converter/result?ticket='+result.ticket)
          throw Error('Неверное описание результата. Файлы оставлены в AIO.');
        if(discard){
          const outcome=await api('release',{ticket:result.ticket});
          if(current(id))status(outcome.ok?'Результат удалён из AIO.':'Удаление принято. Очистка ещё не завершилась.');
        }else{
          if(typeof window.AllInOneBridge?.saveExcel!=='function')throw Error('Сохранение недоступно.');
          const outcome=await window.AllInOneBridge.saveExcel(result);
          if(current(id))status(outcome?.cancelled?'Сохранение отменено. Результат остаётся в AIO.':
            outcome?.saved?'Запись копии завершена. Проверьте её перед удалением результата из AIO.':
            'Загрузка начата. Конечная копия не подтверждена; результат остаётся в AIO.');
        }
      }catch(error){if(current(id))status(error.message||'Действие не выполнено. Результат остаётся в AIO.');}
      finally{pending=false;if(alive){controls();if(discard&&current(id))refresh(false);}}
    }
    async function refresh(clear=true){
      if(!alive||!host.open||!panel.open||pending)return;
      controller?.abort();controller=new AbortController();
      const id=++revision;loading=true;controls();if(clear)status('Получаем список прежних результатов…');
      try{
        const packet=await api('recover?offset='+offset+'&limit=20',undefined,controller.signal);
        if(!current(id))return;
        if(!Array.isArray(packet.items)||packet.items.length>20||!Number.isSafeInteger(packet.total)||packet.total<0)
          throw Error('Неверный список результатов. Файлы оставлены в AIO.');
        total=packet.total;more=packet.more===true;
        if(offset&&(!total||offset>=total)){
          offset=total?Math.floor((total-1)/20)*20:0;
          return refresh(clear);
        }
        const list=get('[data-l-list]');list.replaceChildren();
        for(const item of packet.items){
          if(!/^[a-f0-9]{32}$/.test(item.requestId))continue;
          const row=document.createElement('div');row.className='aio-guard-save';
          const label=document.createElement('span');label.textContent=String(item.name||'Результат').slice(0,255)+
            ' · '+(Number.isSafeInteger(item.size)?item.size+' байт':'размер неизвестен')+
            (item.partial===true?' · есть ошибки преобразования':'');row.append(label);
          for(const [label,discard] of [['Сохранить копию',false],['Удалить из AIO',true]]){
            const button=document.createElement('button');button.type='button';button.textContent=label;
            button.setAttribute('data-l-action',discard?'discard':'save');
            button.addEventListener('click',()=>act(item.requestId,discard));row.append(button);
          }
          list.append(row);
        }
        get('[data-l-page]').textContent=total?'Показаны '+(offset+1)+'–'+Math.min(offset+packet.items.length,total)+' из '+total:'Нет доступных результатов';
        const storage=packet.storage||{};
        get('[data-l-storage]').textContent=Number.isSafeInteger(storage.maxOwnedBytes)?
          'Квота: '+storage.maxOwnedBytes+' байт. '+(storage.storageMeasurementOk===true&&Number.isSafeInteger(storage.usedBytes)?
            'Занято при последней проверке: '+storage.usedBytes+' байт.':'Занятое место пока не измерено или недоступно.') : '';
        const warnings=get('[data-l-warnings]');warnings.replaceChildren();
        for(const warning of (packet.recoveryWarnings||[]).slice(0,40)){
          const text=document.createElement('p');text.textContent=String(warning.code||'LEGACY_RECOVERY_UNVERIFIED').slice(0,96)+
            ' — эти файлы не подтверждены или заняты другим экземпляром. Они оставлены на диске.';warnings.append(text);
        }
        if(clear)status(packet.storageAvailable===false?'Хранилище недоступно. Проверьте права и диск.':
          'Найдено результатов: '+total+'. Целостность восстановленных файлов проверена; качество конвертации не подтверждено.');
      }catch(error){if(current(id)&&error.name!=='AbortError')status(error.message||'Не удалось обновить список.');}
      finally{if(alive&&revision===id){loading=false;controls();}}
    }
    const invalidate=()=>{revision++;controller?.abort();controller=null;loading=false;controls();};
    const toggle=()=>{if(panel.open)refresh();else invalidate();};
    const close=()=>{if(!host.open)invalidate();};
    panel.addEventListener('toggle',toggle);host.addEventListener('close',close);
    get('[data-l-refresh]').addEventListener('click',()=>refresh());
    get('[data-l-prev]').addEventListener('click',()=>{offset=Math.max(0,offset-20);refresh();});
    get('[data-l-next]').addEventListener('click',()=>{if(more){offset+=20;refresh();}});
    controls();
    return {refresh,dispose(){alive=false;invalidate();host.removeEventListener('close',close);panel.removeEventListener('toggle',toggle);panel.remove();}};
  }};
})();
