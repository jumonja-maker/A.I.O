/* Existing two-phase issues control. Content checks run only on explicit click. */
(() => {
  'use strict';
  const terminal = new Set(['ready', 'saved', 'error']);
  const limitText = (value, max = 1600) => String(value || '').slice(0, max);
  const riskPattern = /экспериментально|только текст|не восстанавливаются|могут .*сохраниться|могут измениться|не остаются редактируемыми|один слайд|извлечение таблиц/i;
  const preflightReasons = {
    SOURCE_CONTAINER_INCONSISTENT:'Объявления типа и содержимое контейнера противоречат друг другу. Сохраните исходник и пересоздайте его в родной программе; переименование расширения проблему не исправляет.',
    SOURCE_MODEL_MISMATCH:'LibreOffice открыл содержимое как другой тип документа. Конвертация остановлена, чтобы не выдать неверно импортированные данные за готовый результат.',
    NATIVE_IMPORT_FILTER_CHANGED:'При повторном открытии LibreOffice выбрал другой импортирующий фильтр. Повторите проверку исходника; результат по изменившемуся маршруту не подтверждён.',
    EPUB_NATIVE_IMPORT_UNAVAILABLE:'Установленный LibreOffice умеет экспортировать EPUB, но не подтвердил импорт этого EPUB. Для него пока нет рабочего нативного маршрута.',
    HTML_LOCAL_RESOURCES_NOT_UPLOADED:'HTML ссылается на отсутствующие локальные файлы. Добавьте папку ресурсов HTML через «Состояние».',
    HTML_PREFLIGHT_UNDETERMINED:'Проверка зависимостей HTML не завершена из-за синтаксиса или времени анализа.',
    NATIVE_IMPORT_UNAVAILABLE:'Эта установка LibreOffice не подтвердила нативный импорт содержимого.',
    PREFLIGHT_TIMEOUT:'LibreOffice не завершил распознавание за отведённое время.',
    ENGINE_NOT_FOUND:'LibreOffice не найден.',
    PREFLIGHT_REGISTRY_UNAVAILABLE:'Не удалось прочитать реестр фильтров LibreOffice.',
    PREFLIGHT_UNO_API_UNAVAILABLE:'Не удалось выполнить проверку через UNO установленного LibreOffice.',
    PREFLIGHT_UNO_TYPE_UNKNOWN:'LibreOffice не определил тип содержимого.',
    PREFLIGHT_UNO_TYPE_UNMAPPED:'Тип распознан, но однозначный импортирующий фильтр не подтверждён.',
    PREFLIGHT_IMPORT_UNDETERMINED:'LibreOffice не подтвердил импорт содержимого.',
    PREFLIGHT_KIND_UNDETERMINED:'Нативный тип не удалось сопоставить с модулем LibreOffice. Это не доказывает повреждение файла.',
    PREFLIGHT_IMPORT_FAILED:'LibreOffice не подтвердил открытие этого содержимого без диалога.',
    PREFLIGHT_INTERACTION_REQUIRED:'Для импорта LibreOffice запросил дополнительные сведения. Автоматический импорт не подтверждён.'
  };
  const routeReasons = {
    NATIVE_FORMAT_IMPORT_ONLY:'Установленный LibreOffice умеет читать этот формат, но не имеет фильтра для записи в него.',
    NATIVE_REPORT_EXPORT_UNAVAILABLE:'Для ORP не найден установленный экспортёр отчётов Base. Наличие формата в каталоге не подтверждает рабочий экспорт.',
    NATIVE_DOCUMENT_SERVICE_MISMATCH:'Экспортёр установлен, но не поддерживает фактически открытый тип этого документа.',
    NATIVE_SVG_SELECTION_ONLY:'Нативный SVG-фильтр Writer/Calc экспортирует выбранную графику. Экспорт всего документа этим маршрутом не поддерживается.'
  };
  let sequence = 0;

  function batchOutcome(snapshot) {
    const report = snapshot.result?.batchReport || snapshot.failedBatchReport;
    if (!report || report.schemaVersion !== 1 || !Array.isArray(report.files) ||
        report.files.length !== snapshot.files.length || report.total !== snapshot.files.length ||
        !Number.isSafeInteger(report.succeeded) || !Number.isSafeInteger(report.failed) ||
        report.succeeded < 0 || report.failed < 0 || report.succeeded + report.failed !== report.total ||
        report.files.some((row, i) => row.fileId !== snapshot.files[i].id ||
          !['ready', 'error'].includes(row.state) || (row.state === 'error' &&
            (typeof row.error !== 'string' || !row.error.length || row.error.length > 1000))) ||
        report.files.filter(row => row.state === 'ready').length !== report.succeeded) return null;
    return report;
  }

  function brief(snapshot, after) {
    const rows = [];
    const selected = snapshot.formats.filter(f => snapshot.selectedFormats.includes(f.id));
    const owners = new Map(snapshot.allFiles.map(f=>[String(f.id),f]));
    const proofs = new Map((snapshot.preflightFiles||[]).map(f=>[String(f.id),f.preflight]));
    if (after) {
      const report = snapshot.result?.targetReport || snapshot.retryResult?.targetReport ||
        (snapshot.failedBatchReport?.files?.some(f=>Array.isArray(f.targets)) ? snapshot.failedBatchReport : null);
      for (const file of (Array.isArray(report?.files) ? report.files : [])) {
        const owner = owners.get(String(file.fileId));
        for (const target of (Array.isArray(file.targets) ? file.targets : [])) {
          for(const warning of target.warnings||[])rows.push({
            source:owner?.file?.name||owner?.name||file.name||'Исходный файл',
            target:limitText(target.format,12).toUpperCase(),kind:'Предупреждение экспорта',reason:warning.message});
          if (target.state !== 'error' && target.state !== 'skipped') continue;
          rows.push({source: owner?.file?.name || owner?.name || 'Исходный файл',
            target: limitText(target.format, 12).toUpperCase(),
            kind: target.state === 'skipped' ? 'Направление пропущено Guard' : 'Ошибка конвертации',
            reason: target.error || (target.state === 'skipped'
              ? 'После подтверждения native-типа LibreOffice не имеет такого экспорта.'
              : 'Движок не завершил преобразование.')});
        }
      }
      const reported = new Set((report?.files||[]).map(f=>String(f.fileId)));
      if (report?.schemaVersion === 3) for (const file of snapshot.files) {
        if (reported.has(String(file.id))) continue;
        const proof = proofs.get(String(file.id));
        if (proof?.state === 'undetermined') rows.push({source:file.name,target:'',
          kind:'Файл не включён в план',reason:(preflightReasons[proof.code] ||
            'Направления не подтверждены.') + ' Экспорт этого файла не запускался.'});
      }
      if (!report) for (const file of (batchOutcome(snapshot)?.files || [])) {
        if (file.state !== 'error') continue;
        const owner = owners.get(String(file.fileId));
        rows.push({source: owner?.file?.name || owner?.name || 'Исходный файл',
          target: '', kind: 'Ошибка обработки файла', reason: file.error});
      }
      if (snapshot.status === 'error' && !rows.length) rows.push({source: 'Задание',
        target: '', kind: 'Ошибка задания', reason: snapshot.statusMessage || 'Не удалось завершить задание. Нет подтверждения, что каждый файл завершился ошибкой.'});
      return rows;
    }
    if (snapshot.status === 'checking') return rows;
    const pending = new Set();
    for (const file of snapshot.files) {
      const proof = proofs.get(String(file.id));
      if (!proof || proof.state === 'detected') continue;
      pending.add(String(file.id));
      rows.push({source:file.name,target:'',kind:'Проверка файла не завершена',
        reason:(preflightReasons[proof.code] || 'Направления конвертации пока не подтверждены.') +
          (proof.code ? ' Код: '+limitText(proof.code,80)+'.' : '') +
          (proof.diagnosticStage ? ' Этап: '+limitText(proof.diagnosticStage,64)+'.' : '') +
          (proof.missingResources?.length ? ' Не найдены: '+proof.missingResources.slice(0,8).map(p=>limitText(p,160)).join(', ')+'.' : '') +
          ' Форматы пока недоступны. Можно повторить проверку направлений.'});
    }
    const candidates = selected.length ? selected : snapshot.formats.filter(f => !f.enabled || f.undeterminedFileIds?.length);
    for (const fmt of candidates) {
      const ids = new Set((Array.isArray(fmt.compatibleFileIds)?fmt.compatibleFileIds:[]).map(String));
      const undetermined = new Set((Array.isArray(fmt.undeterminedFileIds)?fmt.undeterminedFileIds:[]).map(String));
      const incompatible = new Set((Array.isArray(fmt.incompatibleFileIds)?fmt.incompatibleFileIds:[]).map(String));
      const compatible = [];
      for (const file of snapshot.files) {
        const id = String(file.id);
        if (pending.has(id)) continue;
        if (ids.has(id) && fmt.enabled) {
          compatible.push(file.name);
        } else if (incompatible.has(id)) {
          rows.push({source: file.name, target: fmt.label || fmt.id,
            kind: 'Нет зарегистрированного маршрута',
            reason: routeReasons[fmt.unavailableReasons?.[id]] || (fmt.compatibility==='native_preflight'
              ? 'Предварительная проверка определила тип содержимого. У этого типа в реестре LibreOffice нет выбранного экспорта.'
              : 'Для расширения исходника реестр LibreOffice не показывает экспорт в этот формат. '
                + 'Это предварительная подсказка, а не проверка содержимого.')});
        } else if (undetermined.has(id)) {
          rows.push({source:file.name,target:fmt.label||fmt.id,kind:'Направление не подтверждено',
            reason:'Направление не подтверждено и пока недоступно. Можно повторить проверку направлений.'});
        }
      }
      const risk = fmt.help?.[2]?.text || '';
      if (compatible.length && riskPattern.test(risk)) rows.push({source: compatible.join(', '),
        target: fmt.label || fmt.id, kind: 'Риск изменения качества', reason: risk});
    }
    return rows;
  }

  function groupedIssues(rows) {
    // One rejected Office package can block 20 output routes. These are not
    // 20 independent root failures: retain every target in the group.
    const groups = new Map();
    for (const row of rows) {
      const key = [row.source, row.kind, row.reason].join('\\u0000');
      let group = groups.get(key);
      if (!group) {
        group = {...row, targets: new Set()};
        groups.set(key, group);
      }
      if (row.target) group.targets.add(row.target);
    }
    return [...groups.values()].map(group => ({
      source: group.source, kind: group.kind, reason: group.reason,
      target: [...group.targets].join(', ')
    }));
  }

  function attach({root, adapter, onBusyChanged = () => {}, onRetryPreflight}) {
    const pane = root.querySelector('.aio-cv__pane--right');
    const formatHeader = pane?.querySelector('.aio-cv__format-head');
    let header = formatHeader || pane?.querySelector('.aio-cv__pane-head');
    let createdHeader = null;
    if (!header) {
      // Main still has a bare title; PR #43 already has the fixed header.
      // Reuse either shape without replacing format controls or help glyphs.
      const title = pane?.querySelector('.aio-cv__pane-title');
      if (title?.parentElement === pane) {
        header = document.createElement('div');
        header.className = 'aio-cv__pane-head';
        pane.insertBefore(header, title); header.append(title); createdHeader = header;
      }
    }
    if (!header) return null;
    const node = (tag, className, text) => {
      const el = document.createElement(tag);
      if (className) el.className = className;
      if (text !== undefined) el.textContent = limitText(text, 4000);
      return el;
    };
    const slot = node('div', 'aio-cv-issues-slot');
    const button = node('button', 'aio-cv-issues-button');
    button.type = 'button';
    button.setAttribute('aria-haspopup', 'dialog');
    button.setAttribute('aria-expanded', 'false');
    const pop = node('div', 'aio-cv-issues-popover');
    pop.id = 'aio-cv-issues-details-' + (++sequence);
    pop.setAttribute('role', 'dialog');
    pop.setAttribute('aria-label', 'Риски и результаты конвертации');
    pop.tabIndex = -1;
    pop.hidden = true;
    button.setAttribute('aria-controls', pop.id);
    const top = node('div', 'aio-cv-issues-top');
    const heading = node('strong', 'aio-cv-issues-heading');
    const closeButton = node('button', 'aio-cv-issues-close', 'Закрыть');
    closeButton.type = 'button';
    const note = node('p', 'aio-cv-issues-note');
    const preflightRetry = node('button', 'aio-cv-issues-retry', 'Повторить проверку направлений');
    preflightRetry.type = 'button'; preflightRetry.hidden = true;
    const list = node('div', 'aio-cv-issues-list');
    const more = node('button', 'aio-cv-issues-more', 'Проверить подробнее');
    more.type = 'button';
    more.setAttribute('aria-expanded', 'false');
    const details = node('section', 'aio-cv-issues-details');
    details.id = pop.id + '-content';
    details.hidden = true;
    more.setAttribute('aria-controls', details.id);
    const activity = node('p', 'aio-cv-issues-activity');
    activity.setAttribute('role', 'status');
    activity.setAttribute('aria-live', 'polite');
    const reportArea = node('div', 'aio-cv-issues-report');
    const retry = node('button', 'aio-cv-issues-retry', 'Повторить проверку');
    retry.type = 'button'; retry.hidden = true;
    details.append(activity, reportArea, retry);
    top.append(heading, closeButton);
    pop.append(top, note, list, preflightRetry, more, details);
    slot.append(button);
    // AIO-UI-001: title, diagnostics, and Select All share ONE fixed row.
    if (formatHeader) formatHeader.insertBefore(slot, formatHeader.querySelector('[data-cv-select-all-formats]'));
    else header.append(slot);
    document.body.append(pop);
    let disposed = false, open = false, expanded = false, snapshot = null;
    let after = false, busy = false, abort = null, generation = 0;
    let key = null, renderedBrief = '', cached = null, error = '';
    let briefCache = null;
    let attemptKey = null;

    function selectionKey(s) {
      return {files: s.files.map(f => [String(f.id), f.file, f.name]),
        formats: [...s.selectedFormats].sort()};
    }
    function sameSelection(a, b) {
      return Boolean(a && b && a.files.length === b.files.length &&
        a.formats.join('\0') === b.formats.join('\0') &&
        a.files.every((f, i) => f[0] === b.files[i][0] && f[1] === b.files[i][1] && f[2] === b.files[i][2]));
    }
    function sameKey(a, b) {
      return sameSelection(a, b) && a.after === b.after && a.result === b.result;
    }
    function setBusy(value) {
      if (busy === value) return;
      busy = value;
      onBusyChanged(value);
    }
    function cancel() {
      generation++;
      abort?.abort(); abort = null;
      // Sync sets busy directly to avoid a recursive render on invalidation.
      busy = false;
    }
    function position() {
      if (!open || disposed) return;
      const rect = button.getBoundingClientRect();
      const vw = document.documentElement.clientWidth || window.innerWidth;
      const vh = document.documentElement.clientHeight || window.innerHeight;
      const margin = 12, width = Math.max(0, Math.min(520, vw - margin * 2));
      const below = vh - rect.bottom - margin - 8;
      const above = rect.top - margin - 8;
      const upward = below < Math.min(300, above);
      pop.style.width = width + 'px';
      pop.style.left = Math.max(margin, Math.min(rect.right - width, vw - width - margin)) + 'px';
      pop.style.top = upward ? 'auto' : Math.max(margin, rect.bottom + 8) + 'px';
      pop.style.bottom = upward ? Math.max(margin, vh - rect.top + 8) + 'px' : 'auto';
      pop.style.maxHeight = Math.max(0, Math.min(600, upward ? above : below)) + 'px';
      pop.dataset.placement = upward ? 'up' : 'down';
    }
    function close(restore = false) {
      open = false; pop.hidden = true;
      button.setAttribute('aria-expanded', 'false');
      if (restore && !disposed) button.focus({preventScroll: true});
    }
    function item(parent, source, kind, message, advice = '') {
      const row = node('div', 'aio-cv-issues-item');
      if (source) row.append(node('strong', '', source));
      if (kind) row.append(node('span', 'aio-cv-issues-kind', kind));
      row.append(node('p', '', message));
      if (advice) row.append(node('p', 'aio-cv-issues-advice', advice));
      parent.append(row);
    }
    const levels = {risk: 'Возможный риск', not_checked: 'Не проверено',
      difference: 'Обнаружено отличие', error: 'Ошибка', info: 'Наблюдение'};
    function renderDetails() {
      details.hidden = !expanded;
      more.textContent = expanded ? 'Скрыть подробности' : 'Проверить подробнее';
      more.setAttribute('aria-expanded', String(expanded));
      details.setAttribute('aria-busy', String(busy));
      more.disabled = !snapshot?.files.length || !snapshot?.selectedFormats.length ||
        snapshot.status === 'processing' || snapshot.status === 'checking' || snapshot.saving === true;
      retry.disabled = busy || more.disabled;
      retry.hidden = !error;
      activity.textContent = busy ? 'Проверяем структуру локально… Можно закрыть окно.' : error ||
        (cached ? 'Проверка завершена в указанном объёме.' : 'Выберите файлы и формат, затем запросите проверку.');
      // Do not rebuild the report on progress/status updates: native details,
      // keyboard focus and scroll position must survive those updates.
      if (reportArea._report === cached) return;
      reportArea._report = cached;
      reportArea.replaceChildren();
      if (!cached) return;
      for (const file of cached.files) {
        const group = node('details', 'aio-cv-issues-file');
        const summary = node('summary', '', file.name + (file.complete ? '' : ' · проверено частично'));
        group.append(summary);
        if (file.facts.length) {
          const facts = node('dl', 'aio-cv-issues-facts');
          for (const fact of file.facts) facts.append(node('dt', '', fact.label), node('dd', '', String(fact.value)));
          group.append(facts);
        }
        if (file.fonts.length) item(group, '', 'Объявления шрифтов', file.fonts.join(', '));
        for (const row of file.findings) item(group, '', levels[row.level] || 'Наблюдение',
          row.message + (row.formats.length ? ' → ' + row.formats.map(f => f.toUpperCase()).join(', ') : ''), row.advice);
        if (!file.findings.length) item(group, '', 'Объём проверки', 'В реализованных структурных проверках риски не обнаружены. Внешний вид и полнота результата не подтверждены.');
        reportArea.append(group);
      }
      if (cached.phase === 'after') {
        const output = node('section', 'aio-cv-issues-output');
        output.append(node('strong', '', 'Проверка содержимого результата'));
        for (const check of cached.result.checks) item(output, '',
          check.state === 'matched' ? 'Совпало в указанном объёме' : check.state === 'different' ? 'Обнаружено отличие' : 'Не проверено', check.message);
        reportArea.append(output);
      }
      const scope = node('details', 'aio-cv-issues-scope');
      scope.append(node('summary', '', 'Границы проверки'));
      for (const line of cached.scope) scope.append(node('p', '', line));
      reportArea.append(scope);
    }
    async function requestDetails(force = false) {
      if (disposed || busy || !snapshot || more.disabled || (cached && !force)) return;
      error = '';
      if (typeof adapter?.diagnoseFiles !== 'function') {
        error = 'Подробная проверка недоступна в этой версии адаптера. Конвертация доступна отдельно.';
        renderDetails(); return;
      }
      const ticket = ++generation;
      abort = new AbortController();
      const captured = snapshot;
      const capturedAfter = after;
      setBusy(true); renderDetails();
      try {
        const report = await adapter.diagnoseFiles({files: captured.files,
          formatIds: captured.selectedFormats, phase: capturedAfter ? 'after' : 'before',
          result: captured.result, signal: abort.signal});
        if (disposed || ticket !== generation) return;
        cached = report;
      } catch (err) {
        if (disposed || ticket !== generation) return;
        error = limitText(err?.userMessage || 'Проверка не завершена. Это не означает ошибку конвертации.');
      } finally {
        if (!disposed && ticket === generation) {
          abort = null; setBusy(false); renderDetails(); position();
        }
      }
    }
    const toggle = () => {
      if (open) {close(); return;}
      open = true; pop.hidden = false; button.setAttribute('aria-expanded', 'true');
      position(); pop.focus({preventScroll: true});
    };
    const outside = e => {if (open && !slot.contains(e.target) && !pop.contains(e.target)) close();};
    const onKey = e => {if (open && e.key === 'Escape') {e.preventDefault(); e.stopPropagation(); close(true);}};
    const onMore = () => {
      expanded = !expanded; renderDetails(); position();
      if (expanded) requestDetails();
    };
    const onRetry = () => requestDetails(true);
    const onPreflightRetry = () => {if(!preflightRetry.disabled)onRetryPreflight?.();};
    const onClose = () => close(true);
    button.addEventListener('click', toggle);
    closeButton.addEventListener('click', onClose);
    more.addEventListener('click', onMore);
    retry.addEventListener('click', onRetry);
    preflightRetry.addEventListener('click', onPreflightRetry);
    document.addEventListener('pointerdown', outside, true);
    document.addEventListener('keydown', onKey, true);
    window.addEventListener('resize', position);
    document.addEventListener('scroll', position, true);
    return {
      get busy() {return busy;},
      sync(value) {
        if (disposed) return;
        snapshot = value;
        const selection = selectionKey(value);
        if (value.status === 'processing') attemptKey = selection;
        after = terminal.has(value.status) && Boolean(value.result || value.retryResult?.targetReport ||
          (value.status === 'error' && sameSelection(attemptKey, selection)));
        const nextKey = {...selection, after, result: after ? value.result || value.retryResult : null};
        if (!sameKey(key, nextKey) || value.status === 'processing' && busy) {
          cancel(); cached = null; error = ''; expanded = false; key = nextKey;
        }
        button.disabled = !value.files.length;
        // Progress renders are frequent; do not rebuild the same potentially
        // 100-source compatibility matrix on every engine message.
        let rows;
        const report=value.result?.targetReport||value.retryResult?.targetReport||value.failedBatchReport;
        if (briefCache?.after===after && briefCache.formats === value.formats && briefCache.status === value.status &&
            briefCache.report===report && briefCache.message===value.statusMessage &&
            briefCache.preflightFiles === value.preflightFiles && sameSelection(briefCache.selection, selection)) {
          rows = briefCache.rows;
        } else {
          const raw = brief(value, after);
          rows = groupedIssues(raw);
          briefCache = {formats: value.formats, preflightFiles:value.preflightFiles,
            status:value.status, selection, rows,rawCount:raw.length,after,report,message:value.statusMessage};
        }
        // Root-cause groups, not one scary item for every blocked output.
        const rawCount = briefCache.rawCount;
        // Keep all vertical space for formats until there is a real
        // issue. After a successful run, offer opt-in result inspection even
        // when the engine reports zero errors.
        const hidden = !value.files.length || (!after && rows.length === 0);
        slot.classList.toggle('aio-cv-issues-empty', hidden);
        if (hidden) close();
        button.textContent = after ? (rows.length ? 'Итоги (' + rows.length + ')' : 'Результат') :
          rows.length ? 'Проблемы (' + rows.length + ')' : 'Риски конвертации';
        button.title = button.textContent;
        heading.textContent = after ? 'После конвертации' : 'Перед конвертацией';
        preflightRetry.hidden = after || !onRetryPreflight ||
          !value.preflightFiles?.some(f=>f.preflight?.state==='undetermined');
        preflightRetry.disabled = ['checking','processing'].includes(value.status) || value.saving === true;
        note.textContent = after ? 'Отчёт задания показывает ошибки движка. Отсутствие ошибок не подтверждает сохранность всего содержимого.' :
          'Краткая сводка по выбранным форматам. Подробный анализ документа запускается только по вашему запросу.';
        if (rawCount > rows.length)
          note.textContent += ' Сгруппировано ' + rawCount + ' сочетаний по ' +
            rows.length + ' исходным причинам; затронутые форматы перечислены ниже.';
        const targetReport = value.result?.targetReport || value.retryResult?.targetReport || batchOutcome(value);
        if (after && targetReport?.schemaVersion === 3 && Number.isSafeInteger(targetReport?.filtered) &&
            Number.isSafeInteger(targetReport?.succeeded) && Number.isSafeInteger(targetReport?.failed)) {
          note.textContent = 'Готово: ' + targetReport.succeeded + ' из ' + targetReport.total +
            '. Пропущено Guard: ' + targetReport.filtered + '. Ошибок: ' + targetReport.failed +
            '. Отменено: ' + targetReport.cancelled + '. Экспортов LibreOffice: ' + targetReport.attempted +
            '. Сохранность содержимого пока не проверялась.';
        } else if (after && Number.isSafeInteger(targetReport?.succeeded) && targetReport.succeeded >= 0 &&
            Number.isSafeInteger(targetReport?.total) && targetReport.total >= targetReport.succeeded &&
            Number.isSafeInteger(targetReport?.failed) && targetReport.failed >= 0) {
          note.textContent = 'По отчёту движка готово: ' + targetReport.succeeded + ' из ' + targetReport.total +
            '. Ошибок: ' + targetReport.failed + '. Сохранность содержимого проверяется отдельно.';
        }
        const signature = JSON.stringify(rows);
        if (signature !== renderedBrief) {
          renderedBrief = signature; list.replaceChildren();
          for (const row of rows.slice(0, 100)) item(list, row.source + (row.target ? ' → ' + row.target : ''), row.kind, row.reason);
          if (rows.length > 100) item(list, '', '', 'Показаны первые 100 записей; ещё ' + (rows.length - 100) + '.');
        }
        renderDetails(); position();
        if (!value.files.length) close();
      },
      dispose() {
        disposed = true; cancel(); close();
        button.removeEventListener('click', toggle);
        closeButton.removeEventListener('click', onClose);
        more.removeEventListener('click', onMore);
        retry.removeEventListener('click', onRetry);
        preflightRetry.removeEventListener('click', onPreflightRetry);
        document.removeEventListener('pointerdown', outside, true);
        document.removeEventListener('keydown', onKey, true);
        window.removeEventListener('resize', position);
        document.removeEventListener('scroll', position, true);
        slot.remove(); pop.remove(); snapshot = null; cached = null; key = null; attemptKey = null; briefCache = null;
        if (createdHeader?.parentNode) {
          for (const child of [...createdHeader.childNodes]) pane.insertBefore(child, createdHeader);
          createdHeader.remove();
        }
      }
    };
  }
  window.AIOConverterIssues = Object.freeze({attach});
})();

