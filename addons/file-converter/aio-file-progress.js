/* A.I.O / Designer 11: presentation-only per-file progress, v1.0.
   Requires explicit engine events. No conversion, network, disk or timers
   that fabricate percentages. See README_INTEGRATION.md. */
(function (global) {
  'use strict';
  if (global.AIOFileProgress) return;
  const controllers = new WeakMap();
  const transitions = Object.freeze({
    queued: ['queued', 'converting', 'ready', 'error', 'cancelled'],
    converting: ['converting', 'ready', 'error', 'cancelled'],
    ready: ['ready', 'saving'],
    saving: ['saving', 'ready', 'saved'],
    saved: ['saved'],
    error: ['error'],
    cancelled: ['cancelled']
  });
  const validId = value => (typeof value === 'string' && value.length > 0) ||
    (typeof value === 'number' && Number.isFinite(value));
  function attach(root) {
    const scope = root?.matches?.('.aio-cv') ? root : root?.querySelector?.('.aio-cv');
    if (!scope) throw new Error('AIOFileProgress.attach requires a mounted converter.');
    if (controllers.has(scope)) return controllers.get(scope);
    let runId = null, disposed = false, frame = null, structureDirty = false;
    const pendingRows = new Set();
    const files = new Map();
    const decorations = new Map();
    const selector = '.aio-cv__file[data-file-id]';
    const observer = new global.MutationObserver(records => {
      if (disposed) return;
      for (const record of records) {
        const row = record.target.closest?.(selector);
        if (row) pendingRows.add(String(row.dataset.fileId));
        else for (const node of [...record.addedNodes, ...record.removedNodes]) {
          if (node.matches?.(selector) || node.querySelector?.(selector)) structureDirty = true;
        }
      }
      // Queue clocks, status dialogs and format controls are outside cards.
      // Their mutations must not repaint every source file once per second.
      if ((structureDirty || pendingRows.size) && frame === null) frame = global.requestAnimationFrame(() => {
        frame = null;
        const dirty = structureDirty ? undefined : new Set(pendingRows);
        structureDirty = false; pendingRows.clear(); render(dirty);
      });
    });
    const observe = () => { if (!disposed) observer.observe(scope, {childList:true, subtree:true}); };
    const current = event => !disposed && event && event.runId === runId && runId !== null;
    function label(data) {
      if (data.state === 'queued') return 'В очереди';
      if (data.state === 'skipped') return 'Не конвертировался: нет подтверждённых направлений Guard';
      if (data.state === 'partial-saved') return 'Часть результатов сохранена';
      if (data.state === 'saved') return 'Сохранено';
      // Stages and counts are confirmed by engine events, not estimated time.
      if (data.targetLabel && !['saving','saved'].includes(data.state)) return data.targetLabel;
      if (data.state === 'converting') return data.percent === null ? 'Конвертация…' :
        (data.percent === 100 ? 'Завершение…' : 'Конвертация · ' + Math.floor(data.percent) + '%');
      if (data.state === 'ready') return 'Готово';
      if (data.state === 'saving') return 'Сохранение…';
      if (data.state === 'error') return data.error ? 'Ошибка: ' + data.error : 'Не удалось конвертировать';
      if (data.state === 'cancelled') return 'Конвертация отменена';
      if (data.state === 'partial') return 'Часть результатов готова; см. ошибки';
      return '';
    }
    function restore(row, parts) {
      parts.label.remove(); parts.check.remove(); parts.paint.remove();
      row.classList.remove('aio-fp-row');
      row.removeAttribute('data-aio-fp-state');
      row.style.removeProperty('--aio-fp-progress');
      row.style.removeProperty('--aio-fp-cell-size');
      row.style.removeProperty('--aio-fp-gap');
      row.style.removeProperty('--aio-fp-padding');
      if (parts.oldBusy === null) row.removeAttribute('aria-busy');
      else row.setAttribute('aria-busy', parts.oldBusy);
      decorations.delete(row);
    }
    function decorate(row) {
      let parts = decorations.get(row);
      if (parts && row.contains(parts.label) && row.contains(parts.paint) && row.contains(parts.check)) return parts;
      if (parts) restore(row, parts);
      const info = row.querySelector('.aio-cv__file-info');
      const tools = row.querySelector('.aio-cv__file-tools');
      if (!info || !tools) return null;
      const make = (tag, className) => { const node = row.ownerDocument.createElement(tag); node.className = className; return node; };
      const status = make('span', 'aio-fp-label');
      const check = make('span', 'aio-fp-check');
      check.textContent = '✓'; check.setAttribute('aria-hidden', 'true'); check.hidden = true;
      const paint = make('div', 'aio-fp-paint');
      paint.hidden = true; paint.setAttribute('role', 'progressbar');
      paint.setAttribute('aria-valuemin', '0'); paint.setAttribute('aria-valuemax', '100');
      parts = {label:status, check, paint, oldBusy:row.getAttribute('aria-busy')};
      info.append(status); tools.prepend(check); row.append(paint); row.classList.add('aio-fp-row');
      decorations.set(row, parts);
      return parts;
    }
    function render(dirtyIds) {
      if (disposed) return;
      observer.disconnect();
      try {
        for (const [row, parts] of [...decorations]) {
          if (!scope.contains(row) || !files.has(String(row.dataset.fileId))) restore(row, parts);
        }
        for (const row of scope.querySelectorAll('.aio-cv__file[data-file-id]')) {
          if (dirtyIds && !dirtyIds.has(String(row.dataset.fileId))) continue;
          const data = files.get(String(row.dataset.fileId));
          if (!data) continue;
          const parts = decorate(row);
          if (!parts) continue;
          const text = label(data);
          row.dataset.aioFpState = data.state;
          row.setAttribute('aria-busy', String(data.state === 'converting' || data.state === 'saving'));
          parts.label.textContent = text; parts.label.hidden = !text;
          parts.label.title = data.targetTitle || data.error || '';
          parts.check.textContent = data.state === 'error' ? '×' :
            data.state === 'cancelled' ? '−' : data.state === 'skipped' ? '⊘' :
            ['partial','partial-saved'].includes(data.state) ? '!' : '✓';
          parts.check.hidden = !['ready','error','cancelled','skipped','partial','partial-saved'].includes(data.state) &&
            !(data.state === 'saving' && !['partial','partial-saved'].includes(data.beforeSave));
          // A single DOCX/LO operation has no measured intermediate percent.
          // No moving fake progress: show green ONLY for confirmed completed
          // targets. On completion the fill gives way to the status/check.
          const confirmedFill = data.percent !== null && data.percent > 0 &&
            data.state === 'converting';
          parts.paint.hidden = !confirmedFill;
          parts.paint.dataset.indeterminate = 'false';
          parts.paint.setAttribute('aria-label', 'Конвертация ' + (row.querySelector('.aio-cv__file-name')?.textContent || 'файла'));
          if (data.stageText) parts.paint.setAttribute('aria-valuetext',data.stageText);
          else parts.paint.removeAttribute('aria-valuetext');
          row.style.setProperty('--aio-fp-cell-size',data.cellSize || '100%');
          if (data.percent === null) parts.paint.removeAttribute('aria-valuenow');
          else parts.paint.setAttribute('aria-valuenow', String(Math.floor(data.percent)));
          row.style.setProperty('--aio-fp-progress', (data.percent ?? 0) + '%');

        }
      } finally { observe(); }
    }
    function begin(event) {
      if (disposed || !event || typeof event.runId !== 'string' || !event.runId.length ||
          !Array.isArray(event.fileIds) || !event.fileIds.length ||
          event.fileIds.some(id=>!validId(id))) return false;
      const ids = event.fileIds.map(String);
      if (new Set(ids).size !== ids.length) return false;
      runId = event.runId; files.clear();
      ids.forEach(id=>files.set(id, {state:'queued', percent:null}));
      render(); return true;
    }
    function update(event) {
      if (!current(event) || !validId(event.fileId)) return false;
      const data = files.get(String(event.fileId));
      if (!data || !transitions[data.state].includes(event.state)) return false;
      data.state = event.state;
      if (event.state === 'error') data.error = typeof event.error === 'string' ? event.error.slice(0, 1000) : '';
      if (event.state === 'converting' && Number.isFinite(event.percent) && event.percent >= 0 && event.percent <= 100)
        data.percent = Math.max(data.percent ?? 0, event.percent);
      if (event.state === 'ready') data.percent = 100;
      render(new Set([String(event.fileId)])); return true;
    }
    function completeBatch(event) {
      if (!current(event) || [...files.values()].some(data=>['error','cancelled','saving','saved'].includes(data.state))) return false;
      files.forEach(data=>{data.state='ready';data.percent=100;}); render(); return true;
    }
    function beginSave(event) {
      const states = [...files.values()].map(data=>data.state);
      if (!current(event) || !states.some(x=>['ready','partial'].includes(x)) ||
          states.some(x=>!['ready','partial','error','cancelled','skipped','partial-saved','saved'].includes(x))) return false;
      files.forEach(data=>{if(['ready','partial','partial-saved','saved'].includes(data.state)){
        data.beforeSave=data.state;data.state='saving';
      }}); render(); return true;
    }
    function failBatch(event) {
      if (!current(event) || [...files.values()].some(data=>data.state==='saving'||data.state==='saved')) return false;
      files.forEach(data=>{data.state=event.cancelled===true?'cancelled':'error';data.percent=null;});
      render(); return true;
    }
    function finishSave(event) {
      const states = [...files.values()].map(data=>data.state);
      if (!current(event) || typeof event.saved !== 'boolean' || !states.includes('saving') ||
          states.some(x=>!['saving','error','cancelled','skipped'].includes(x))) return false;
      files.forEach(data=>{if(data.state==='saving')data.state=event.saved?
        (['partial','partial-saved'].includes(data.beforeSave)?'partial-saved':'saved'):data.beforeSave;});
      render(); return true;
    }
    function finishTargets(event) {
      if (!current(event) || !Array.isArray(event.files) || (!event.partial && event.files.length !== files.size) ||
          [...files.values()].some(x=>x.state==='saving')) return false;
      const ids=event.files.map(x=>String(x.fileId));
      if (new Set(ids).size!==event.files.length || ids.some(id=>!files.has(id)) ||
          event.files.some(x=>!['queued','converting','ready','partial','error','cancelled','skipped'].includes(x.state)||
            (x.allCopiesSaved!==undefined&&(typeof x.allCopiesSaved!=='boolean'||x.allCopiesSaved&&!['ready','partial'].includes(x.state))))) return false;
      for(const row of event.files){
        const errors=row.targets.filter(x=>x.state==='error');
        const working=row.targets.find(x=>x.state==='converting');
        const skipped=row.targets.filter(x=>x.state==='skipped').length;
        const cancelled=row.targets.filter(x=>x.state==='cancelled').length;
        const summary='Готово '+row.succeeded+' из '+row.total+
          (skipped?' · пропущено Guard: '+skipped:'')+(cancelled?' · отменено: '+cancelled:'');
        const detail=errors.map(x=>x.format.toUpperCase()+': '+x.error).join('; ');
        // Completed targets (ready or error) fill full-width discrete stages.
        const completed=row.targets.filter(x=>['ready','saved','error','cancelled','skipped'].includes(x.state)).length;
        const progress=row.total>0?Math.round(100*completed/row.total):null;
        files.set(String(row.fileId),{state:row.allCopiesSaved?(row.state==='partial'?'partial-saved':'saved'):row.state,percent:progress,
          cellSize:row.total>1?(100/row.total)+'%':'100%',
          stageText:completed+' из '+row.total+' направлений завершено',
          targetLabel:row.total===0?'Нет доступных направлений':row.state==='queued'?'В очереди':summary+
            (working?' · '+working.format.toUpperCase()+'…':errors.length?' · '+errors.map(x=>x.format.toUpperCase()).join(', ')+': ошибка':''),
          targetTitle:summary+(detail?' · '+detail:'')});
      }
      render(event.partial ? new Set(ids) : undefined); return true;
    }
    function finishBatch(event) {
      if (!current(event) || !Array.isArray(event.files) || event.files.length !== files.size ||
          [...files.values()].some(data=>['saving','saved'].includes(data.state))) return false;
      const ids = event.files.map(data=>String(data.fileId));
      if (new Set(ids).size !== files.size || ids.some(id=>!files.has(id)) ||
          event.files.some(data=>!['ready','error'].includes(data.state))) return false;
      event.files.forEach(row=>files.set(String(row.fileId), {
        state:row.state, percent:row.state==='ready'?100:null,
        error:row.state==='error' && typeof row.error==='string'?row.error.slice(0,1000):''
      }));
      render(); return true;
    }
    function reset() {
      if (disposed) return;
      runId = null; files.clear(); render();
    }
    function dispose() {
      if (disposed) return;
      observer.disconnect();
      if (frame !== null) global.cancelAnimationFrame(frame);
      frame = null; disposed = true;
      for (const [row, parts] of [...decorations]) restore(row, parts);
      files.clear(); pendingRows.clear(); controllers.delete(scope);
    }
    const controller = Object.freeze({begin, update, completeBatch, finishBatch, failBatch, finishTargets, beginSave, finishSave, reset, dispose});
    controllers.set(scope, controller); observe(); return controller;
  }
  global.AIOFileProgress = Object.freeze({version:'1.0.0', attach});
})(window);
