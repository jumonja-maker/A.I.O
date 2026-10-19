"""Scoped, fail-closed last-stage patch on the signed Designer 11 controller.

The signed payload and native help/format controls stay intact. The old
warning nodes are not appended; the existing backend guards are unchanged.
"""
from __future__ import annotations


def apply_issues_ui(raw: bytes) -> bytes:
    source = raw.decode('utf-8')
    warning_anchor = 'summary.append(warn);'
    warnings = source.count(warning_anchor)
    if not 1 <= warnings <= 3:
        raise ValueError('Unexpected Designer warning count: ' + str(warnings))
    source = source.replace(warning_anchor, 'void warn;')

    anchor = '    function renderSummary() {'
    if source.count(anchor) != 1:
        raise ValueError('Missing Designer summary render anchor')

    implementation = r'''
    // Owner QA: one stable two-phase issues control, no clipped warning banner.
    // This is deliberately scoped to the converter mount; Excel is untouched.
    let aioIssueView = null;
    function aioEnsureIssuesView() {
      if(aioIssueView) return aioIssueView;
      const pane=root.querySelector('.aio-cv__pane--right');
      const header=pane?.querySelector('.aio-cv__pane-head');
      if(!header) return null;
      const slot=document.createElement('div');
      slot.className='aio-cv-issues-slot';
      const button=document.createElement('button');
      button.className='aio-cv-issues-button';
      button.type='button';
      button.setAttribute('aria-haspopup','dialog');
      button.setAttribute('aria-expanded','false');
      const pop=document.createElement('div');
      pop.className='aio-cv-issues-popover';
      pop.setAttribute('role','dialog');
      pop.setAttribute('aria-label','Подробности о форматах и ошибках конвертации');
      pop.tabIndex=-1;
      pop.hidden=true;
      pop.id='aio-cv-issues-details';
      button.setAttribute('aria-controls',pop.id);
      const heading=document.createElement('div');
      heading.className='aio-cv-issues-heading';
      const list=document.createElement('div');
      list.className='aio-cv-issues-list';
      pop.append(heading,list);slot.append(button);header.append(slot);
      // Keep this dialog outside the intentionally clipped converter panel.
      document.body.append(pop);
      let open=false;
      function positionPopover() {
        if(!open)return;
        const rect=button.getBoundingClientRect();
        const vw=document.documentElement.clientWidth||window.innerWidth;
        const vh=document.documentElement.clientHeight||window.innerHeight;
        const width=Math.min(420,Math.max(0,vw-24));
        const below=vh-rect.bottom-12;
        const above=rect.top-12;
        const upward=below<160 && above>below;
        const space=upward?above:below;
        pop.style.width=width+'px';
        pop.style.left=Math.max(12,Math.min(rect.right-width,vw-width-12))+'px';
        pop.style.right='auto';
        pop.style.top=upward?'auto':Math.max(12,rect.bottom+9)+'px';
        pop.style.bottom=upward?Math.max(12,vh-rect.top+9)+'px':'auto';
        pop.style.maxHeight=Math.max(60,Math.min(360,space-9))+'px';
        pop.dataset.placement=upward?'up':'down';
      }
      function close() {
        if(!open)return;
        open=false;button.setAttribute('aria-expanded','false');
        pop.dataset.open='false';
        setTimeout(()=>{if(!open)pop.hidden=true;},190);
      }
      button.addEventListener('click',()=>{
        if(open){close();return;}
        open=true;pop.hidden=false;
        button.setAttribute('aria-expanded','true');
        positionPopover();
        requestAnimationFrame(()=>{
          if(open){pop.dataset.open='true';pop.focus({preventScroll:true});}
        });
      });
      const outside=event=>{
        if(open && !slot.contains(event.target) && !pop.contains(event.target))close();
      };
      const onKey=event=>{
        if(open && event.key==='Escape'){close();button.focus();}
      };
      document.addEventListener('pointerdown',outside,true);
      document.addEventListener('keydown',onKey,true);
      window.addEventListener('resize',positionPopover);
      document.addEventListener('scroll',positionPopover,true);
      aioIssueView={slot,button,pop,heading,list,close,dispose(){
        close();document.removeEventListener('pointerdown',outside,true);
        document.removeEventListener('keydown',onKey,true);
        window.removeEventListener('resize',positionPopover);
        document.removeEventListener('scroll',positionPopover,true);
        pop.remove();slot.remove();
      }};
      return aioIssueView;
    }

    function updateAioIssues() {
      const view=aioEnsureIssuesView();
      if(!view)return;
      const all=(analysis?.formats||[]);
      const selected=all.filter(fmt=>selectedFormats.has(fmt.id));
      const report=currentResult?.targetReport || retryResult?.targetReport;
      const after=Boolean(report && (status==='ready'||status==='saved'||status==='error'))
                  || status==='error';
      const issues=[];
      if(after){
        if(report?.files){
          for(const row of report.files){
            const owner=files.find(f=>String(f.id)===String(row.fileId));
            for(const output of (row.targets||[])){
              if(output.state!=='error')continue;
              issues.push({
                source:owner?.file?.name||owner?.name||'Исходный файл',
                target:String(output.format||'').toUpperCase(),
                kind:'Ошибка конвертации',
                reason:output.error||'Движок не завершил преобразование.'
              });
            }
          }
        }
        if(!issues.length && status==='error'){
          issues.push({
            source:files.map(f=>f.file?.name||f.name).filter(Boolean).join(', ')||'Исходный файл',
            target:selected.map(f=>f.label).join(', ')||'Выбранный формат',
            kind:'Ошибка задания',
            reason:statusMessage||'Не удалось завершить преобразование.'
          });
        }
      } else if(files.length && all.length){
        // Until a format is selected, show only unsupported catalog routes.
        // For selected formats report their actual per-source eligibility.
        const candidates=selected.length?selected:all.filter(fmt=>!fmt.enabled);
        for(const fmt of candidates){
          const eligible=Array.isArray(fmt.compatibleFileIds)?fmt.compatibleFileIds:[];
          for(const file of files){
            if(eligible.includes(file.id) && fmt.enabled)continue;
            issues.push({
              source:file.file?.name||file.name||'Исходный файл',
              target:fmt.label||String(fmt.id).toUpperCase(),
              kind:'Формат недоступен',
              reason:fmt.incompatibleReasons?.[String(file.id)]||file.error||'Этот маршрут не подтверждён как безопасный.'
            });
          }
          if(!fmt.enabled)continue;
          const risk=fmt.help?.[2]?.text || '';
          if(/экспериментально|только текст|не восстанавливаются|могут .*сохраниться|могут измениться|не остаются редактируемыми|один слайд|извлечение таблиц/i.test(risk)){
            issues.push({
              source:files.map(f=>f.file?.name||f.name).filter(Boolean).join(', '),
              target:fmt.label||String(fmt.id).toUpperCase(),
              kind:'Риск изменения качества',
              reason:risk
            });
          }
        }
      }
      view.button.textContent=(after?'Ошибки конвертации (':'Проблемы с форматами (')+issues.length+')';
      view.button.title=view.button.textContent;
      view.heading.textContent=after?'Ошибки после конвертации':'Совместимость и сохранность данных';
      view.list.replaceChildren();
      for(const issue of issues.slice(0,100)){
        const item=document.createElement('div');
        item.className='aio-cv-issues-item';
        const line=document.createElement('strong');
        line.textContent=issue.source+' → '+issue.target;
        const category=document.createElement('span');
        category.className='aio-cv-issues-kind';
        category.textContent=issue.kind;
        const explanation=document.createElement('p');
        explanation.textContent=issue.reason;
        item.append(line,category,explanation);view.list.append(item);
      }
      if(issues.length>100){
        const remainder=document.createElement('p');
        remainder.textContent='Показаны первые 100 записей; ещё '+(issues.length-100)+' проблем.';
        view.list.append(remainder);
      }
      if(!issues.length)view.close();
      // Visibility reserves the same dimensions in either phase. The neighboring
      // help/format buttons never move as status or counter changes.
      view.slot.classList.toggle('aio-cv-issues-empty',issues.length===0);
    }

'''
    cleanup = 'alive=false;inspectionToken++;closePopover();typeFilter?.dispose();'
    if source.count(cleanup) != 1:
        raise ValueError('Missing Designer mount cleanup anchor')
    source = source.replace(cleanup, cleanup + 'aioIssueView?.dispose?.();', 1)
    return source.replace(anchor, implementation + anchor.replace('{',
        '{\n      updateAioIssues();'), 1).encode('utf-8')
