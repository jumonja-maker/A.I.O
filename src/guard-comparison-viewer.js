/* Bounded, inert two-pane viewer. It never loads original HTML or document code. */
(() => {
  'use strict';
  const prefix='/api/converter-guard/compare/preview';
  function mount(root,{source,result,context,onMessage=()=>{}}){
    let destroyed=false,revision=0,syncing=false,scale='fit',fullscreenBusy=false;
    const controller=new AbortController(),urls=new Set();
    const panes=[];
    root.replaceChildren();
    const viewer=document.createElement('section');viewer.className='aio-pair-viewer';
    const toolbar=document.createElement('div');toolbar.className='aio-pair-toolbar';toolbar.setAttribute('role','toolbar');toolbar.setAttribute('aria-label','Масштаб просмотра');
    const button=(text,action)=>{const b=document.createElement('button');b.type='button';b.textContent=text;b.addEventListener('click',action);return b;};
    for(const [label,value]of [['Вписать','fit'],['100%',1],['200%',2],['400%',4]]){
      const b=button(label,()=>zoom(value));b.dataset.viewerZoom=String(value);toolbar.append(b);
    }
    const syncLabel=document.createElement('label'),sync=document.createElement('input');sync.type='checkbox';sync.checked=true;sync.dataset.viewerSync='';
    syncLabel.append(sync,document.createTextNode('Синхронно'));toolbar.append(syncLabel);
    sync.addEventListener('change',()=>zoom(scale));
    const full=button('На весь экран',async()=>{
      if(destroyed||fullscreenBusy)return;fullscreenBusy=true;full.disabled=true;
      try{
      const dialog=root.closest('dialog');if(!dialog)return;
      if(document.fullscreenElement===viewer){await document.exitFullscreen();return;}
      if(typeof viewer.requestFullscreen==='function'){
        try{await viewer.requestFullscreen();if(destroyed&&document.fullscreenElement===viewer)await document.exitFullscreen();return;}
        catch(error){if(destroyed)return;onMessage('Полноэкранный режим недоступен. Просмотр развёрнут в пределах окна.');}
      }
      const expanded=dialog.classList.toggle('aio-compare-maximized');full.setAttribute('aria-pressed',String(expanded));
      full.textContent=expanded?'Обычный размер':'На весь экран';zoom(scale);
      }finally{fullscreenBusy=false;if(!destroyed)full.disabled=false;}
    });full.dataset.viewerFullscreen='';toolbar.append(full);
    const fullscreenChanged=()=>{const expanded=document.fullscreenElement===viewer;
      root.closest('dialog')?.classList.toggle('aio-compare-maximized',expanded);
      full.setAttribute('aria-pressed',String(expanded));full.textContent=expanded?'Обычный размер':'На весь экран';zoom(scale);};
    document.addEventListener('fullscreenchange',fullscreenChanged);
    const grid=document.createElement('div');grid.className='aio-pair-grid';
    const note=document.createElement('small');note.className='aio-pair-note';note.textContent='Колесо — масштаб; перетаскивание — перемещение. Увеличение не добавляет деталей. Проверенные изменения описаны ниже.';
    viewer.append(toolbar,grid,note);root.append(viewer);
    for(const [side,label,description]of [['source','До · исходник',source],['result','После · выбранный выход',result]]){
      const section=document.createElement('section');section.className='aio-pair-pane';section.dataset.viewerSide=side;
      const head=document.createElement('div');head.className='aio-pair-head';
      const title=document.createElement('strong');title.textContent=label;head.append(title);
      const prev=button('←',()=>changePage(side,-1)),counter=document.createElement('span'),next=button('→',()=>changePage(side,1));
      prev.setAttribute('aria-label','Предыдущая страница или кадр: '+label);next.setAttribute('aria-label','Следующая страница или кадр: '+label);
      counter.textContent='1 / …';head.append(prev,counter,next);
      const viewport=document.createElement('div');viewport.className='aio-pair-viewport';viewport.tabIndex=0;
      viewport.setAttribute('aria-label',label+' · прокрутка и масштаб');
      const content=document.createElement('div');content.className='aio-pair-content';viewport.append(content);
      const status=document.createElement('small');status.className='aio-pair-status';status.textContent='Ожидаем просмотр…';
      section.append(head,viewport,status);grid.append(section);
      const pane={side,description,section,content,viewport,status,prev,next,counter,page:0,pages:null,node:null,width:0,height:0,url:null};panes.push(pane);
      let drag=null;
      viewport.addEventListener('pointerdown',e=>{if(e.button!==0)return;drag={x:e.clientX,y:e.clientY,left:viewport.scrollLeft,top:viewport.scrollTop};viewport.setPointerCapture?.(e.pointerId);});
      viewport.addEventListener('pointermove',e=>{if(!drag)return;viewport.scrollLeft=drag.left+drag.x-e.clientX;viewport.scrollTop=drag.top+drag.y-e.clientY;shareScroll(pane);});
      for(const name of ['pointerup','pointercancel','lostpointercapture'])viewport.addEventListener(name,()=>{drag=null;});
      viewport.addEventListener('scroll',()=>shareScroll(pane));
      viewport.addEventListener('wheel',e=>{if(!pane.node||pane.node.tagName!=='IMG')return;e.preventDefault();zoom(Math.max(.1,Math.min(8,(scale==='fit'?fit(pane):scale)*(e.deltaY<0?1.2:1/1.2))));},{passive:false});
    }
    function current(n){return !destroyed&&n===revision&&root.contains(viewer);}
    // Fit enlarges small originals as well as shrinking large ones. It remains
    // bounded like wheel zoom; 100% still means the original pixel dimensions.
    function fit(pane){return pane.width&&pane.height?Math.min(8,Math.max(.01,(pane.viewport.clientWidth||500)/pane.width),Math.max(.01,(pane.viewport.clientHeight||300)/pane.height)):1;}
    function zoom(value){
      scale=value;
      const imagePanes=panes.filter(p=>p.node?.tagName==='IMG');
      for(const b of toolbar.querySelectorAll('[data-viewer-zoom]'))b.setAttribute('aria-pressed',String(String(value)===b.dataset.viewerZoom));
      for(const pane of panes){
        if(pane.node?.tagName!=='IMG')continue;
        // Fit each side to its own viewport. Synchronized panning must not
        // shrink a tiny source just because the other side has 4K dimensions.
        const factor=value==='fit'?fit(pane):value;
        const cx=(pane.viewport.scrollLeft+pane.viewport.clientWidth/2)/(pane.content.scrollWidth||1);
        const cy=(pane.viewport.scrollTop+pane.viewport.clientHeight/2)/(pane.content.scrollHeight||1);
        pane.node.style.width=Math.max(1,Math.round(pane.width*factor))+'px';pane.node.style.height='auto';
        pane.viewport.scrollLeft=cx*pane.content.scrollWidth-pane.viewport.clientWidth/2;
        pane.viewport.scrollTop=cy*pane.content.scrollHeight-pane.viewport.clientHeight/2;
      }
    }
    function shareScroll(from){
      if(!sync.checked||syncing||destroyed)return;syncing=true;
      const x=from.viewport.scrollLeft/Math.max(1,from.viewport.scrollWidth-from.viewport.clientWidth);
      const y=from.viewport.scrollTop/Math.max(1,from.viewport.scrollHeight-from.viewport.clientHeight);
      for(const pane of panes)if(pane!==from){pane.viewport.scrollLeft=x*Math.max(0,pane.viewport.scrollWidth-pane.viewport.clientWidth);pane.viewport.scrollTop=y*Math.max(0,pane.viewport.scrollHeight-pane.viewport.clientHeight);}
      syncing=false;
    }
    function boundURL(pane){
      const u=new URL(pane.description.url,window.location.href);
      if(u.origin!==window.location.origin||u.pathname!==prefix||u.username||u.password)throw Error('Адрес просмотра не прошёл проверку.');
      for(const [key,value]of [['job',context.jobId],['task',context.taskId],['side',pane.side]])
        if(u.searchParams.getAll(key).length!==1||u.searchParams.get(key)!==value)throw Error('Просмотр относится к другой паре файлов.');
      if(u.searchParams.getAll('analysisId').length>1||(u.searchParams.has('analysisId')&&u.searchParams.get('analysisId')!==context.analysisId))throw Error('Просмотр относится к другому анализу.');
      if(context.analysisId)u.searchParams.set('analysisId',context.analysisId);
      if(u.searchParams.getAll('outputIndex').length>1)throw Error('Неоднозначный выходной файл.');
      if(pane.side==='result')u.searchParams.set('outputIndex',String(context.outputIndex));
      u.searchParams.set('page',String(pane.page));return u.href;
    }
    function controls(pane){pane.counter.textContent=(pane.page+1)+' / '+(pane.pages||'…');pane.prev.disabled=destroyed||!pane.page;pane.next.disabled=destroyed||!pane.pages||pane.page+1>=pane.pages;}
    async function load(pane,n){
      controls(pane);pane.prev.disabled=pane.next.disabled=true;
      pane.content.replaceChildren();pane.node=null;
      if(pane.url){URL.revokeObjectURL(pane.url);urls.delete(pane.url);pane.url=null;}
      const d=pane.description;
      if(!d||d.kind==='unavailable'||d.available===false){pane.status.textContent=d?.note||'Выход не создан или просмотр недоступен.';return;}
      try{
        let data=null,response=null;
        if(d.kind==='text'&&typeof d.text==='string')data=d;
        else {
          if(!d.url)throw Error('Нет доступного представления этого файла.');
          const url=boundURL(pane);
          for(let attempt=0;attempt<18;attempt++){
            if(!current(n))return;
            response=await fetch(url,{headers:{'X-AIO-Token':document.body.dataset.aioToken||''},cache:'no-store',signal:controller.signal});
            if(!current(n))return;
            if(response.status!==409)break;
            const reason=await response.json();if(reason.code!=='COMPARISON_PREVIEW_BUSY')throw Error(reason.error||'Просмотр недоступен.');
            await new Promise(resolve=>setTimeout(resolve,Math.min(1000,150*(attempt+1))));
          }
          if(!response?.ok)throw Error('Не удалось подготовить просмотр; результат доступен для сохранения.');
          const mime=(response.headers.get('Content-Type')||'').split(';')[0].toLowerCase();
          if(mime==='application/json')data=await response.json();
          else {
            if(!['image/png','image/jpeg','image/gif','image/webp','image/bmp'].includes(mime))throw Error('Этот тип не показывается встроенным просмотром.');
            if(Number(response.headers.get('Content-Length'))>64*1024*1024)throw Error('Просмотр превышает бюджет.');
            const blob=await response.blob();if(!current(n))return;
            if(blob.size>64*1024*1024)throw Error('Просмотр превышает бюджет.');
            const node=document.createElement('img');node.alt=d.name||pane.side;node.draggable=false;
            pane.url=URL.createObjectURL(blob);urls.add(pane.url);node.src=pane.url;
            const width=Number(response.headers.get('X-AIO-Preview-Width')),height=Number(response.headers.get('X-AIO-Preview-Height'));
            pane.width=width>0?width:0;pane.height=height>0?height:0;
            node.addEventListener('load',()=>{if(!current(n))return;if(!pane.width)pane.width=node.naturalWidth;if(!pane.height)pane.height=node.naturalHeight;zoom(scale);});
            pane.node=node;pane.content.append(node);
            const count=Number(response.headers.get('X-AIO-Preview-Pages'));pane.pages=Number.isSafeInteger(count)&&count>0?count:1;
            const rendered=Number(response.headers.get('X-AIO-Preview-Rendered-Width'));
            pane.status.textContent=(pane.width&&pane.height?pane.width+' × '+pane.height+' · ':'')+(rendered&&rendered<pane.width?'Просмотр уменьшен по бюджету; масштаб 100% соответствует исходным координатам.':d.kind==='pdf'?'Страница PDF через QtPdf; растровое представление.':'Исходное разрешение декодированного изображения.');
            zoom(scale);controls(pane);return;
          }
        }
        if(!current(n))return;
        if(data?.kind==='text'&&typeof data.text==='string'){
          const pre=document.createElement('pre');pre.textContent=data.text;pane.content.append(pre);pane.node=pre;
          pane.pages=1;pane.status.textContent=data.note||'Текстовое представление; оформление не показано.';
        }else pane.status.textContent=data?.note||'Просмотр недоступен; это не подтверждение потери данных.';
      }catch(error){if(current(n)&&error.name!=='AbortError'){pane.status.textContent=error.message;onMessage(error.message);}}
      if(current(n))controls(pane);
    }
    async function refresh(){const n=++revision;for(const pane of panes)pane.prev.disabled=pane.next.disabled=true;for(const pane of panes){if(!current(n))break;await load(pane,n);}}
    async function changePage(side,delta){
      const pane=panes.find(p=>p.side===side);if(!pane)return;
      const page=Math.max(0,Math.min((pane.pages||1)-1,pane.page+delta));
      if(page===pane.page)return;pane.page=page;
      if(sync.checked)for(const other of panes)if(other!==pane)other.page=Math.min(page,Math.max(0,(other.pages||1)-1));
      await refresh();
    }
    const resized=()=>{if(scale==='fit')zoom(scale);};window.addEventListener('resize',resized);
    return {ready:refresh(),destroy(){destroyed=true;revision++;controller.abort();if(document.fullscreenElement===viewer)document.exitFullscreen().catch(()=>{});document.removeEventListener('fullscreenchange',fullscreenChanged);for(const url of urls)URL.revokeObjectURL(url);urls.clear();window.removeEventListener('resize',resized);root.closest('dialog')?.classList.remove('aio-compare-maximized');viewer.remove();},zoom,changePage};
  }
  window.AIOComparisonViewer=Object.freeze({mount});
})();
