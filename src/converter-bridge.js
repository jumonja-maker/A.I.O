/* Direct LibreOffice transport. Source/target and size policies belong to Guard. */
(() => {
  'use strict';
  const bridge = window.AllInOneBridge;
  const token = document.body.dataset.aioToken;
  if (!bridge || !token) return;
  const headers = {'X-AIO-Token': token};
  const retained = new Set();
  let active = null, progress = null, generation = 0;

  function failure(message, code) {
    const error = new Error(message);
    error.userMessage = message; error.code = code;
    return error;
  }
  const metadata = files => Array.from(files || [], f => ({
    id: String(f.id || ''), name: String(f.file?.name || f.name || ''),
    size: f.file?.size ?? f.size
  }));
  async function json(path, body) {
    const response = await fetch('/api/converter/' + path, {
      headers: {...headers, ...(body === undefined ? {} : {'Content-Type': 'application/json'})},
      ...(body === undefined ? {} : {method: 'POST', body: JSON.stringify(body)}),
      cache: 'no-store'
    });
    const payload = await response.json();
    if (!response.ok) {
      const error = failure(payload.error || 'Не удалось выполнить задание LibreOffice.', payload.code);
      error.batchReport = payload.batchReport;
      throw error;
    }
    return payload;
  }
  async function release(ticket) {
    try {
      const outcome = await json('release', {ticket});
      if (outcome.ok) retained.delete(ticket);
    } catch {}
  }
  async function cancel() {
    if (!active) return;
    active.cancelled = true;
    await json('cancel', {requestId: active.id});
  }
  function resetProgress() {
    generation++;
    cancel().catch(() => {});
    for (const ticket of retained) release(ticket);
    progress?.dispose(); progress = null;
  }
  function update(report, operation, onProgress, finished = false) {
    if (operation.generation !== generation) return;
    for (const row of report.files || []) {
      const targets = row.targets || [];
      const running = targets.some(t => t.state === 'running');
      const queued = targets.some(t => t.state === 'queued');
      const ready = targets.some(t => t.state === 'ready');
      const failed = targets.some(t => t.state === 'error');
      progress?.update({runId: operation.id, fileId: row.fileId,
        state: running ? 'converting' : queued ? 'queued' : failed ? (ready ? 'partial' : 'error') : 'ready',
        percent: null, error: targets.find(t => t.error)?.error || ''});
    }
    if (finished) progress?.finishTargets({runId: operation.id,
      files: (report.files || []).map(row => ({
        fileId: row.fileId, targets: row.targets,
        state: row.targets.some(t=>t.state==='error') ? (row.targets.some(t=>t.state==='ready') ? 'partial' : 'error') : 'ready',
        succeeded: row.targets.filter(t => t.state === 'ready').length,
        total: row.targets.length,
        error: row.targets.find(t => t.error)?.error || ''
      }))});
    onProgress?.({percent: report.percent,
      message: report.total ? 'Обработано преобразований: ' + report.completed + ' из ' + report.total + '.' :
        'Передаём файлы LibreOffice…'});
  }
  async function run({files, formatIds, formatId, onProgress} = {}) {
    if (active) throw failure('Уже выполняется конвертация.', 'CLIENT_BUSY');
    const inputs = Array.from(files || []);
    const formats = formatIds || [formatId];
    if (!inputs.length || inputs.some(f => !f.file || !f.id))
      throw failure('Выберите исходные файлы.', 'INPUT_MISSING');
    if (!Array.isArray(formats) || !formats.length ||
        formats.some(f => typeof f !== 'string' || !/^[a-z0-9][a-z0-9_-]*$/.test(f)))
      throw failure('Выберите формат результата.', 'INVALID_FORMAT');
    const operation = {id: Array.from(crypto.getRandomValues(new Uint8Array(16)),
      b => b.toString(16).padStart(2, '0')).join(''), generation,
      cancelled: false, done: false, timer: null};
    active = operation;
    progress?.dispose();
    const root = document.querySelector('.aio-cv');
    progress = root && window.AIOFileProgress ? window.AIOFileProgress.attach(root) : null;
    progress?.begin({runId: operation.id, fileIds: inputs.map(f => String(f.id))});
    async function poll() {
      if (operation.done || operation.generation !== generation) return;
      try {
        const report = await json('progress?request=' + operation.id);
        if (!operation.done) update(report, operation, onProgress);
      } catch {}
      if (!operation.done && operation.generation === generation)
        operation.timer = setTimeout(poll, 400);
    }
    try {
      for (const ticket of [...retained]) await release(ticket);
      const payload = new FormData();
      payload.append('ids', JSON.stringify(inputs.map(f => String(f.id))));
      for (const file of inputs) payload.append('files', file.file, file.file.name || file.name);
      payload.append('formats', JSON.stringify(formats));
      onProgress?.({percent: null, message: 'Передаём файлы LibreOffice…'});
      const pending = fetch('/api/converter/convert?request=' + operation.id, {
        method: 'POST', headers, body: payload
      });
      operation.timer = setTimeout(poll, 100);
      const response = await pending;
      const outcome = await response.json();
      if (operation.generation !== generation) {
        if (outcome.result?.ticket) await release(outcome.result.ticket);
        throw failure('Задание закрыто.', 'CANCELLED');
      }
      if (!response.ok) {
        if (outcome.batchReport) update(outcome.batchReport, operation, onProgress, true);
        const error = failure(outcome.error || 'LibreOffice не создал результат.', outcome.code);
        error.batchReport = outcome.batchReport; throw error;
      }
      if (outcome.ok !== true || !outcome.result?.downloadUrl || !outcome.result?.ticket)
        throw failure('Не получен готовый результат LibreOffice.', 'INVALID_RESULT');
      retained.add(outcome.result.ticket);
      outcome.result.progressRunId = operation.id;
      outcome.result.directLibreOffice = true;
      update(outcome.result.targetReport, operation, onProgress, true);
      return outcome;
    } catch (error) {
      if (!error.batchReport) progress?.failBatch({runId: operation.id, cancelled: operation.cancelled});
      if (operation.cancelled) {
        const cancelled = failure('Конвертация отменена.', 'CANCELLED');
        cancelled.batchReport = error.batchReport;
        throw cancelled;
      }
      throw error;
    } finally {
      operation.done = true; clearTimeout(operation.timer);
      if (active === operation) active = null;
    }
  }
  async function saveResult({result} = {}) {
    if (!result?.ticket || !retained.has(result.ticket))
      throw failure('Результат больше недоступен. Повторите конвертацию.', 'RESULT_NOT_FOUND');
    let saved = false;
    progress?.beginSave({runId: result.progressRunId});
    try {
      const outcome = await bridge.saveExcel(result);
      saved = outcome?.saved === true;
      if (saved) await release(result.ticket);
      return outcome;
    } finally {
      progress?.finishSave({runId: result.progressRunId, saved});
    }
  }
  bridge.converter = Object.freeze({
    acceptedExtensions: '', directLibreOffice: true,
    inspectFiles({files = []} = {}) { return json('inspect', {files: metadata(files)}); },
    convert: run, convertMany: run, saveResult, cancel, resetProgress,
    async diagnoseFiles({files = [], phase = 'before', result} = {}) {
      const report = result?.targetReport;
      return {phase, files: metadata(files).map(file => ({...file, complete: false,
        facts: [], fonts: [], findings: [{level: 'not_checked', formats: [],
          message: 'Содержимое и оформление автоматически не сравнивались.', advice: ''}]})),
        result: {checks: [{state: 'not_checked',
          message: 'Готовый файл создан LibreOffice. Сохранность всех элементов автоматически не проверялась.'}]},
        scope: ['Проверка доступности направления выполняется самим LibreOffice.',
          ...(report ? report.files.flatMap(row => row.targets.map(target =>
            target.format.toUpperCase() + ': ' + (target.error || 'Результат создан') +
            ' · LibreOffice: ' + (target.libreOfficeStarted ? 'запускался' : 'не запускался') +
            (target.warnings?.includes('SOURCE_NAME_NORMALIZED') ? ' · Имя временной копии изменено из-за ограничений ОС.' : ''))) : [])]};
    }
  });
})();
