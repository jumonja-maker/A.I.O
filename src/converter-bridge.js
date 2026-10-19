/* A.I.O Converter Guard v0.1: metadata → disk upload → native LO → save. */
(() => {
  'use strict';
  const bridge = window.AllInOneBridge;
  const token = document.body.dataset.aioToken;
  if (!bridge || !token) return;
  const headers = {'X-AIO-Token': token};
  const retained = new Set();
  let active = null, progress = null;

  function failure(message, code) {
    const error = new Error(message);
    error.userMessage = message; error.code = code;
    return error;
  }
  async function request(path, body, options = {}) {
    const response = await fetch('/api/converter/' + path, {
      ...options, headers: {...headers, ...(body === undefined ? {} : {'Content-Type': 'application/json'}), ...options.headers},
      ...(body === undefined ? {} : {method: 'POST', body: JSON.stringify(body)})
    });
    const payload = await response.json();
    if (!response.ok) throw failure(payload.error || 'Не удалось выполнить операцию Guard.', payload.code);
    return payload;
  }
  const metadata = files => Array.from(files || [], f => ({
    id: String(f.id || ''), name: String(f.file?.name || f.name || ''), size: f.file?.size ?? f.size
  }));
  async function release(id) {
    try { await request('release', {jobId: id}, {keepalive: true}); }
    catch (error) { if (error.code !== 'JOB_NOT_FOUND') return false; }
    retained.delete(id); return true;
  }
  function attachProgress() {
    progress?.dispose();
    const scope = document.querySelector('.aio-cv');
    progress = scope && window.AIOFileProgress ? window.AIOFileProgress.attach(scope) : null;
  }
  async function cancel() {
    if (!active) return;
    active.cancelled = true;
    active.upload.abort();
    if (active.id) await request('cancel', {jobId: active.id});
  }
  function resetProgress() {
    cancel().catch(() => {});
    for (const id of retained) release(id);
    progress?.dispose(); progress = null;
  }
  function targetReport(state) {
    const files = state.files.map(f => ({fileId: f.fileId, targets: [{format: 'pdf',
      state: f.state === 'ready' ? 'ready' : 'error', error: f.error,
      code: f.code, libreOfficeStarted: f.libreOfficeStarted}]}));
    const succeeded = state.files.filter(f => f.state === 'ready').length;
    return {files, succeeded, failed: files.length - succeeded, total: files.length};
  }
  function update(state, runId, onProgress) {
    for (const file of state.files) progress?.update({runId, fileId: file.fileId,
      state: file.state === 'awaiting_upload' ? 'queued' : file.state,
      percent: file.percent, error: file.error || ''});
    if (state.finished) progress?.finishTargets?.({runId, files: state.files.map(file => ({
      fileId: file.fileId, state: file.state === 'ready' ? 'ready' : 'error',
      error: file.error || '', succeeded: file.state === 'ready' ? 1 : 0, total: 1,
      targets: [{format: 'pdf', state: file.state === 'ready' ? 'ready' : 'error', error: file.error || ''}]
    }))});
    onProgress?.({percent: state.percent,
      message: 'Обработано файлов: ' + state.completed + ' из ' + state.total + '.'});
  }
  async function run({files, formatId = 'pdf', options = {}, onProgress} = {}) {
    if (active) throw failure('Уже выполняется конвертация.', 'CLIENT_BUSY');
    if (formatId !== 'pdf' || Object.keys(options).length)
      throw failure('В первой версии Guard подключён экспорт в PDF.', 'OUTPUT_NOT_CONNECTED');
    const input = Array.from(files || []);
    if (!input.length || input.some(f => !f.file)) throw failure('Выберите исходные файлы.', 'INPUT_MISSING');
    const operation = {id: null, cancelled: false, upload: new AbortController()};
    active = operation;
    attachProgress();
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'aio-cv__small-btn'; button.textContent = 'Отменить';
    button.dataset.guardCancel = '';
    button.addEventListener('click', () => {
      button.disabled = true; button.textContent = 'Отменяем…';
      cancel().catch(() => {button.disabled = false; button.textContent = 'Повторить отмену';});
    });
    document.querySelector('.aio-cv__footer')?.append(button);
    try {
      for (const id of [...retained]) await release(id);
      const created = await request('jobs', {files: metadata(input), formatId, options});
      operation.id = created.jobId; retained.add(created.jobId);
      progress?.begin({runId: created.jobId, fileIds: input.map(f => String(f.id))});
      if (operation.cancelled) {
        await request('cancel', {jobId: created.jobId});
        throw failure('Конвертация отменена.', 'CANCELLED');
      }
      for (let i = 0; i < input.length; i++) {
        onProgress?.({percent: null, message: 'Загрузка файла ' + (i + 1) + ' из ' + input.length + '.'});
        const response = await fetch('/api/converter/input?job=' + created.jobId + '&file=' + encodeURIComponent(input[i].id), {
          method: 'POST', headers: {...headers, 'Content-Type': 'application/octet-stream'},
          body: input[i].file, signal: operation.upload.signal
        });
        if (!response.ok) {
          const error = await response.json();
          throw failure(error.error || 'Загрузка не завершена.', error.code);
        }
      }
      await request('start', {jobId: created.jobId});
      let state;
      do {
        state = await request('jobs?job=' + created.jobId);
        update(state, created.jobId, onProgress);
        if (!state.finished) await new Promise(resolve => setTimeout(resolve, 400));
      } while (!state.finished);
      const report = targetReport(state);
      if (!state.result) {
        const first = state.files.find(f => f.error);
        const error = failure(first?.error || 'Ни один файл не был преобразован.', first?.code || 'CONVERSION_FAILED');
        error.batchReport = report; throw error;
      }
      const result = {...state.result, targetReport: report, guardTrace: state.trace, progressRunId: created.jobId};
      return {ok: true, result, message: report.failed ?
        'Готово: ' + report.succeeded + '. Не преобразовано: ' + report.failed + '. Подробности в «Проблемы».' :
        'PDF создан LibreOffice. Можно сохранить результат.'};
    } catch (error) {
      if (operation.id) await release(operation.id);
      const cancelled = operation.cancelled || error.name === 'AbortError' || error.code === 'CANCELLED';
      if (cancelled) error = failure('Конвертация отменена.', 'CANCELLED');
      if (!error.batchReport) progress?.failBatch({runId: operation.id, cancelled});
      throw error;
    } finally {
      button.remove(); active = null;
    }
  }
  async function saveResult({result} = {}) {
    if (!result?.jobId || !retained.has(result.jobId))
      throw failure('Результат больше недоступен. Повторите конвертацию.', 'RESULT_NOT_FOUND');
    const downloads = result.downloadUrl ? [result] : result.outputs;
    if (!Array.isArray(downloads) || !downloads.length) throw failure('Нет готовых результатов.', 'RESULT_NOT_FOUND');
    progress?.beginSave({runId: result.progressRunId});
    let saved = true;
    try {
      for (const output of downloads) {
        const outcome = await bridge.saveExcel({name: output.name, downloadUrl: output.downloadUrl});
        if (outcome?.saved !== true) {saved = false; return outcome;}
      }
      await release(result.jobId);
      return {saved: true};
    } catch (error) {saved = false; throw error;}
    finally {progress?.finishSave({runId: result.progressRunId, saved});}
  }
  bridge.converter = Object.freeze({
    acceptedExtensions: '.docx,.doc,.odt,.rtf,.xlsx,.xls,.ods,.pptx,.ppt,.odp',
    inspectFiles({files} = {}) { return request('inspect', {files: metadata(files)}); },
    convert: run,
    convertMany({formatIds, ...details} = {}) {
      if (!Array.isArray(formatIds) || formatIds.length !== 1 || formatIds[0] !== 'pdf')
        return Promise.reject(failure('Подключён только экспорт в PDF.', 'OUTPUT_NOT_CONNECTED'));
      return run({...details, formatId: 'pdf'});
    },
    saveResult, cancel, resetProgress,
    async diagnoseFiles({files = [], phase = 'before', result} = {}) {
      const trace = result?.guardTrace;
      return {phase, files: metadata(files).map(f => ({...f, complete: false,
        facts: [], fonts: [], findings: [{level: 'not_checked', formats: ['pdf'],
          message: 'Глубокий анализ документа в базовой версии не выполняется.', advice: ''}]})),
        result: {checks: [{state: 'not_checked', message: 'Полнота содержимого и внешний вид автоматически не сравнивались.'}]},
        scope: ['Guard проверяет создание PDF и завершение процесса. Это не подтверждение отсутствия потерь.',
          ...(trace ? trace.events.map(e => e.sequence + ': ' + e.stage +
            (e.code ? ' [' + e.code + ']' : '') + ' · LibreOffice: ' + (e.libreOfficeStarted ? 'запускался' : 'ещё не запускался')) : [])]};
    }
  });
})();
