/* Explicit discovery/save/discard for the separate legacy API. No polling. */
(() => {
  'use strict';
  window.AIOLegacyRecovery={mount(host){
    const token=document.body.dataset.aioToken;
    if(!host||!token)return {dispose(){}};
    const panel=document.createElement('details');
    panel.setAttribute('data-legacy-recovery','');
    panel.innerHTML='<summary>Результаты прежнего конвертера</summary>'+
      '<p>Здесь доступны результаты прежних заданий. Они не входят в счётчик текущих несохранённых. Сохранение копии не удаляет результат из AIO.</p>'+
      '<div class="aio-guard-save"><button type="button" data-l-refresh>Обновить список</button><button type="button" data-l-decline-all hidden>Не восстанавливать все предыдущие результаты</button></div>'+
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
      for(const button of panel.querySelectorAll('[data-l-action],[data-l-decline],[data-l-salvage]'))button.disabled=pending||loading||button.dataset.unavailable==='true';
      get('[data-l-decline-all]').disabled=pending||loading;
    };
    async function api(path,body,signal){
      const response=await fetch('/api/converter/'+path,{method:body?'POST':'GET',
        headers:{'X-AIO-Token':token,...(body?{'Content-Type':'application/json'}:{})},
        ...(body?{body:JSON.stringify(body)}:{}),cache:'no-store',signal});
      const packet=await response.json();
      if(!response.ok)throw Error(packet.error||'Не удалось получить прежние результаты.');
      return packet;
    }
    async function decline(target,all=false){
      if(pending||loading)return;
      const question=all?
        'Отказаться от восстановления ВСЕХ предыдущих результатов? Временные файлы будут удалены. История конвертаций останется. Отменить удаление нельзя.':
        'Не восстанавливать эти результаты и удалить их временные файлы? История конвертации останется. Отменить удаление нельзя.';
      if(!window.confirm(question))return;
      const id=revision;pending=true;controls();
      status('Записываем отказ от восстановления…');
      let finished=false;
      try{
        const outcome=await api('decline',{confirm:true,...target});
        if(!current(id))return;
        if(all){
          status(outcome.notDeclined?
            'Отказ записан для '+outcome.declined+' пакетов. '+outcome.notDeclined+
            ' каталогов заняты или недоступны; их не удаляли. Повторите после освобождения.':
            'Отказ от восстановления записан. Эти результаты больше не появятся после перезапуска.'+
            (outcome.cleanupPending?' Очистка части временных файлов ещё продолжается.':''));
        }else{
          if(outcome.declined!==true)throw Error('Отказ не подтверждён. Файлы оставлены без изменений.');
          status('Отказ записан. Эти результаты больше не будут предлагаться.'+
            (outcome.cleanupPending?' Очистка временных файлов ещё не завершена.':''));
        }
        finished=true;
      }catch(error){if(current(id))status(error.message||'Отказ не записан. Никакие результаты не скрыты.');}
      finally{
        pending=false;
        if(alive){controls();if(finished&&current(id))refresh(false);}
      }
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
    async function salvage(workspaceId){
      if(pending||loading)return;
      const id=revision;pending=true;controls();
      status('Собираем уцелевшие файлы. Полнота и качество конвертации не подтверждены…');
      try{
        const found=await api('salvage',{workspaceId});
        if(!current(id))return;
        const result=found?.result;
        if(found.state!=='ready'||result?.salvagedUnverified!==true||
           !/^[A-Za-z0-9_-]{32,128}$/.test(result.ticket)||
           result.downloadUrl!=='/api/converter/result?ticket='+result.ticket)
          throw Error('Непроверенный ZIP не создан. Оригинальные файлы оставлены на диске.');
        if(typeof window.AllInOneBridge?.saveExcel!=='function')
          throw Error('Сохранение недоступно. Исходные файлы оставлены на диске.');
        const outcome=await window.AllInOneBridge.saveExcel(result);
        if(current(id))status(outcome?.saved?
          'Копия уцелевших файлов сохранена. Это НЕ подтверждение успешной конвертации. Оригиналы оставлены в AIO.':
          'Сохранение копии не подтверждено. Оригинальные файлы оставлены в AIO.');
      }catch(error){if(current(id))status(error.message||'Не удалось сохранить найденные файлы. Оригиналы не удалены.');}
      finally{pending=false;if(alive)controls();}
    }
    async function refresh(clear=true,forceScan=false){
      if(!alive||!host.open||!panel.open||pending)return;
      controller?.abort();controller=new AbortController();
      const id=++revision;loading=true;controls();if(clear)status('Получаем список прежних результатов…');
      try{
        if(forceScan)await api('rescan',{},controller.signal);
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
            (item.available===false?' · ФАЙЛ НЕДОСТУПЕН':item.salvagedUnverified===true?' · НЕПРОВЕРЕННЫЕ файлы':item.partial===true?' · есть ошибки преобразования':'');row.append(label);
          if(item.restoredFromJournal===true){
            const button=document.createElement('button');button.type='button';
            button.textContent='Не восстанавливать';button.setAttribute('data-l-decline',item.requestId);
            button.addEventListener('click',()=>decline({requestId:item.requestId}));row.append(button);
          }
          for(const [label,discard] of [['Сохранить копию',false],['Удалить из AIO',true]]){
            const button=document.createElement('button');button.type='button';button.textContent=label;
            button.setAttribute('data-l-action',discard?'discard':'save');
            if(item.available===false){button.dataset.unavailable='true';button.disabled=true;}
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
          const line=document.createElement('div');line.className='aio-guard-save';
          const code=String(warning.code||'LEGACY_RECOVERY_UNVERIFIED').slice(0,96);
          const text=document.createElement('p');text.textContent=code+
            ' — файлы не подтверждены или заняты другим экземпляром. Оригиналы оставлены на диске.';
          line.append(text);
          if(/^aio-legacy-[a-f0-9]{32}$/.test(warning.workspaceId||'')){
            const button=document.createElement('button');button.type='button';
            button.textContent='Не восстанавливать';
            button.setAttribute('data-l-decline',warning.workspaceId);
            button.addEventListener('click',()=>decline({workspaceId:warning.workspaceId}));
            line.append(button);
          }
          if(code==='LEGACY_RECOVERY_UNVERIFIED'&&
             /^aio-legacy-[a-f0-9]{32}$/.test(warning.workspaceId||'')){
            const button=document.createElement('button');button.type='button';
            button.textContent='Сохранить уцелевшие файлы (непроверенные)';
            button.setAttribute('data-l-salvage',warning.workspaceId);
            button.addEventListener('click',()=>salvage(warning.workspaceId));line.append(button);
          }
          warnings.append(line);
        }
        get('[data-l-decline-all]').hidden=!(packet.restoredCount>0||packet.salvagedCount>0||
          (packet.recoveryWarnings||[]).some(w=>/^aio-legacy-[a-f0-9]{32}$/.test(w.workspaceId||'')));
        if(clear)status(packet.storageAvailable===false?'Хранилище недоступно. Проверьте права и диск.':
          'Найдено записей: '+total+'. Недоступных файлов: '+(packet.unavailableResults||0)+'. Качество конвертации не подтверждено.');
      }catch(error){if(current(id)&&error.name!=='AbortError')status(error.message||'Не удалось обновить список.');}
      finally{if(alive&&revision===id){loading=false;controls();}}
    }
    const invalidate=()=>{revision++;controller?.abort();controller=null;loading=false;controls();};
    const toggle=()=>{if(panel.open)refresh();else invalidate();};
    const close=()=>{if(!host.open)invalidate();};
    panel.addEventListener('toggle',toggle);host.addEventListener('close',close);
    get('[data-l-refresh]').addEventListener('click',()=>refresh(true,true));
    get('[data-l-decline-all]').addEventListener('click',()=>decline({scope:'all_previous'},true));
    get('[data-l-prev]').addEventListener('click',()=>{offset=Math.max(0,offset-20);refresh();});
    get('[data-l-next]').addEventListener('click',()=>{if(more){offset+=20;refresh();}});
    controls();
    return {refresh,dispose(){alive=false;invalidate();host.removeEventListener('close',close);panel.removeEventListener('toggle',toggle);panel.remove();}};
  }};
})();
