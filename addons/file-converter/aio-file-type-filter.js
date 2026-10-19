/* Preview update: type filtering and per-type conversion selection. */
(function(global){
  'use strict';
  const extension=name=>{const base=String(name||'').replace(/^.*[\\/]/,'');const i=base.lastIndexOf('.');return i>0&&i<base.length-1?base.slice(i+1).toUpperCase():'БЕЗ ТИПА';};
  function attach({root,getFiles,onFilterChanged,getSelectedIds,onSelectFiles,isSelectionLocked}) {
    if(!root||typeof getFiles!=='function'||typeof onFilterChanged!=='function')throw new Error('Invalid type filter host');
    const head=root.querySelector('.aio-cv__pane--left .aio-cv__pane-head');
    const add=head?.querySelector('[data-cv-add]');
    if(!head||!add)throw new Error('Missing converter header');
    let filtering=false,popup=null,disposed=false;
    const dialogs=new Set();
    const group=document.createElement('div');group.className='aio-cv-filter__actions';
    const trigger=document.createElement('button');trigger.type='button';
    trigger.className='aio-cv__small-btn aio-cv-filter__trigger';trigger.dataset.cvTypeFilter='';
    trigger.setAttribute('aria-haspopup','dialog');trigger.setAttribute('aria-expanded','false');
    trigger.setAttribute('aria-label','Фильтр файлов по типу');
    trigger.innerHTML='<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5h16l-6.5 7.5v5.4l-3 1.6v-7Z"/></svg><span data-filter-label>Фильтр</span>';
    add.before(group);group.append(trigger,add);
    const nameOf=f=>f?.name??f?.file?.name??'';
    const idOf=f=>String(f?.id);
    const locked=()=>typeof isSelectionLocked==='function'&&isSelectionLocked();
    const chosenIds=()=>new Set((typeof getSelectedIds==='function'?getSelectedIds():[]).map(String));
    const allFiles=()=>getFiles()||[];
    const filesOf=type=>allFiles().filter(f=>type==='ВСЕ'||extension(nameOf(f))===type);
    function selectedTypes(){const chosen=chosenIds();return new Set(allFiles().filter(f=>chosen.has(idOf(f))).map(f=>extension(nameOf(f))));}
    function counts(){const map=new Map();for(const f of allFiles()){const type=extension(nameOf(f));map.set(type,(map.get(type)||0)+1);}return map;}
    function label(){
      const types=[...selectedTypes()].sort((a,b)=>a.localeCompare(b,'ru'));
      trigger.querySelector('[data-filter-label]').textContent=!filtering?'Фильтр':types.length===1?types[0]:'Типы: '+types.length;
      trigger.dataset.active=String(filtering);
      trigger.title=filtering?(types.length?'Показаны типы: '+types.join(', '):'Типы не выбраны'):'Показаны все типы файлов';
    }
    function selectType(type,{toggle=false,selectAll=false}={}){
      if(locked())return;
      filtering=type!=='ВСЕ';
      const entries=filesOf(type),ids=entries.map(idOf),chosen=chosenIds();
      // The all-types label restores the view without changing conversion choices.
      if(type!=='ВСЕ'||toggle||selectAll){
        const check=toggle?!ids.length||!ids.every(id=>chosen.has(id)):true;
        onSelectFiles?.(ids,check);
      }
      // Keep the dialog available for the next type choice. All chosen types
      // participate in one union, so two checked types show both groups.
      onFilterChanged();label();if(popup)fill(popup);
    }
    function fill(dialog){
      const list=dialog.querySelector('.aio-cv-filter__choices');if(!list)return;
      const previous=list.scrollTop||0;
      const focused=document.activeElement;
      const focusRow=focused?.closest?.('.aio-cv-filter__choice');
      const focusType=dialog.contains(focused)?focusRow?.dataset.type:null;
      const focusPart=focused?.classList?.contains('aio-cv-filter__select')?'.aio-cv-filter__select':'.aio-cv-filter__choice-main';
      list.replaceChildren();
      const chosen=chosenIds(),types=counts(),visibleTypes=selectedTypes();
      const items=[['ВСЕ',allFiles().length],...Array.from(types.entries()).sort((a,b)=>a[0].localeCompare(b[0],'ru'))];
      dialog.querySelector('[data-filter-select-all]').disabled=locked()||!allFiles().length;
      for(const [type,n] of items){
        const name=type==='ВСЕ'?'Все типы':type;
        const ids=filesOf(type).map(idOf),num=ids.filter(id=>chosen.has(id)).length;
        const state=num===0?'false':num===ids.length?'true':'mixed';
        const row=document.createElement('div');row.className='aio-cv-filter__choice';
        row.dataset.type=type;row.dataset.active=String(type==='ВСЕ'?!filtering:filtering&&visibleTypes.has(type));
        const main=document.createElement('button');main.type='button';main.className='aio-cv-filter__choice-main';
        main.setAttribute('aria-pressed',type==='ВСЕ'?String(!filtering):state);
        main.setAttribute('aria-label',type==='ВСЕ'?'Показать все типы':'Файлы типа '+type);
        main.disabled=locked();
        const text=document.createElement('span');text.className='aio-cv-filter__choice-label';text.textContent=name;
        main.append(text);
        const count=document.createElement('span');count.className='aio-cv-filter__choice-count';count.textContent=String(n);count.title='Файлов: '+n;
        const circle=document.createElement('button');circle.type='button';circle.className='aio-cv-filter__select';
        circle.setAttribute('role','checkbox');circle.setAttribute('aria-checked',state);
        circle.setAttribute('aria-label',(type==='ВСЕ'?'Все файлы':'Файлы '+type)+': выбрать для конвертации');
        circle.title=state==='true'?'Снять выбор '+name:'Выбрать '+name;
        circle.disabled=locked()||!ids.length;
        const ink=document.createElement('span');ink.setAttribute('aria-hidden','true');circle.append(ink);
        circle.addEventListener('click',event=>{event.stopPropagation();selectType(type,{toggle:true});});
        row.addEventListener('click',()=>selectType(type,{toggle:type!=='ВСЕ'}));
        row.append(main,count,circle);list.append(row);
      }
      if(!allFiles().length){const note=document.createElement('p');note.className='aio-cv-filter__empty';note.textContent='Нет файлов';list.append(note);}
      list.scrollTop=previous;
      if(focusType){const row=[...list.querySelectorAll('.aio-cv-filter__choice')].find(row=>row.dataset.type===focusType);row?.querySelector(focusPart)?.focus({preventScroll:true});}
    }
    function sync(){
      label();if(popup)fill(popup);
    }
    function position(){
      if(!popup)return;if(!trigger.isConnected){close();return;}
      const r=trigger.getBoundingClientRect(),width=Math.min(302,global.innerWidth-28),gap=13,safe=14;
      popup.style.width=width+'px';popup.style.visibility='hidden';
      const height=popup.offsetHeight;
      const x=Math.max(safe,Math.min(r.left,global.innerWidth-safe-width));
      const below=r.bottom+gap,useBelow=below+height<=global.innerHeight-safe||r.top-gap-height<safe;
      const y=Math.max(safe,Math.min(useBelow?below:r.top-gap-height,global.innerHeight-safe-height));
      popup.style.left=x+'px';popup.style.top=y+'px';popup.dataset.side=useBelow?'bottom':'top';
      popup.style.setProperty('--cv-tail-x',Math.max(17,Math.min(width-17,r.left+r.width/2-x))+'px');
      popup.style.setProperty('--cv-origin',Math.max(0,Math.min(width,r.left+r.width/2-x))+'px '+(useBelow?'0':'100%'));
      popup.style.visibility='visible';
    }
    function close(returnFocus=false){
      if(!popup)return;const box=popup;popup=null;trigger.setAttribute('aria-expanded','false');
      if(returnFocus&&trigger.isConnected)trigger.focus();
      if(global.matchMedia('(prefers-reduced-motion: reduce)').matches){box.remove();dialogs.delete(box);}
      else{box.classList.remove('aio-cv-flyout--float');box.classList.add('aio-cv-flyout--closing');setTimeout(()=>{box.remove();dialogs.delete(box);},170);}
    }
    function show(){
      if(popup){close(true);return;}
      const box=document.createElement('aside');box.className='aio-cv-flyout aio-cv-filter__popover';
      box.setAttribute('role','dialog');box.setAttribute('aria-label','Фильтр файлов по типу');
      const header=document.createElement('div');header.className='aio-cv-filter__header';
      const title=document.createElement('h3');title.className='aio-cv-filter__title';title.textContent='Фильтр файлов';
      const all=document.createElement('button');all.type='button';all.className='aio-cv-filter__all';all.dataset.filterSelectAll='';all.textContent='Выбрать всё';
      all.addEventListener('click',()=>selectType('ВСЕ',{selectAll:true}));
      header.append(title,all);
      const list=document.createElement('div');list.className='aio-cv-filter__choices';
      list.setAttribute('role','group');list.setAttribute('aria-label','Типы файлов');
      box.append(header,list);document.body.append(box);popup=box;dialogs.add(box);
      trigger.setAttribute('aria-expanded','true');fill(box);position();
      if(!global.matchMedia('(prefers-reduced-motion: reduce)').matches){box.addEventListener('animationend',e=>{
        if(e.animationName==='aio-cv-bloom'&&popup===box)box.classList.add('aio-cv-flyout--float');
      },{once:true});}
      box.querySelector('.aio-cv-filter__choice-main[aria-pressed="true"]')?.focus({preventScroll:true});
    }
    function pointer(e){if(popup&&!popup.contains(e.target)&&!trigger.contains(e.target))close();}
    function keyboard(e){if(e.key==='Escape'&&popup){e.preventDefault();e.stopPropagation();close(true);}}
    trigger.addEventListener('click',show);document.addEventListener('pointerdown',pointer);document.addEventListener('keydown',keyboard);
    global.addEventListener('resize',position);document.addEventListener('scroll',position,true);label();
    return Object.freeze({matches:f=>!filtering||selectedTypes().has(extension(nameOf(f))),selectedTypes:()=>[...selectedTypes()],sync,close,dispose(){
      if(disposed)return;disposed=true;close();for(const box of dialogs)box.remove();dialogs.clear();
      trigger.removeEventListener('click',show);document.removeEventListener('pointerdown',pointer);document.removeEventListener('keydown',keyboard);
      global.removeEventListener('resize',position);document.removeEventListener('scroll',position,true);group.replaceWith(add);
    }});
  }
  global.AIOTypeFilter=Object.freeze({attach,extension});
})(window);
