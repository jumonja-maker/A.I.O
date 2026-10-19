/* A.I.O + native Converter Guard v0.5 integration (QA branch only).
   Preserve the existing designer, Excel save bridge and independent LO engine. */
(() => {
  'use strict';
  const bridge = window.AllInOneBridge;
  const token = document.body.dataset.aioToken;
  if (!bridge || !token) return;

  const prefix = '/api/converter-guard';
  const headers = Object.freeze({'X-AIO-Token': token});
  const retained = new Map();
  let active = null, progress = null, generation = 0;

  const errorOf = (message, code) => {
    const e = new Error(message);
    e.userMessage = message;
    e.code = code;
    return e;
  };
  const metadata = files => Array.from(files || [], f => ({
    id: String(f.id || ''), name: String(f.file?.name || f.name || ''),
    size: f.file?.size ?? f.size
  }));
  async function payloadOf(response) {
    const payload = await response.json();
    if (!response.ok) {
      const e = errorOf(payload.error || 'Операция Converter Guard не выполнена.', payload.code);
      e.stage = payload.stage;
      throw e;
    }
    return payload;
  }
  async function api(path, body, method = 'POST', signal) {
    const response = await fetch(prefix + path, {
      method, headers: {...headers, ...(method === 'POST' ? {'Content-Type':'application/json'} : {})},
      ...(method === 'POST' ? {body: JSON.stringify(body)} : {}), cache: 'no-store', signal
    });
    return payloadOf(response);
  }
  async function poll(operation) {
    const response = await fetch(prefix + '/jobs?job=' + encodeURIComponent(operation.jobId), {
      headers:{...headers, ...(operation.etag ? {'If-None-Match':operation.etag} : {})},
      cache:'no-store'
    });
    if (response.status === 304) return null; // no JSON, paint or issues rerender
    const state = await payloadOf(response);
    operation.etag = response.headers.get('ETag');
    return state;
  }
  const check = (operation, allowCancelled = false) => {
    if ((!allowCancelled && operation.cancelled) || operation.generation !== generation)
      throw errorOf('Задание отменено.', 'CANCELLED');
  };
  async function releaseJob(id) {
    if (!id) return;
    try {
      await api('/release', {jobId: id});
      retained.delete(id);
    } catch (_) {}
  }
  async function cancel() {
    const op = active;
    if (!op) return;
    op.cancelled = true;
    if (op.jobId) {
      try { await api('/cancel', {jobId: op.jobId}); } catch (_) {}
    }
  }
  function resetProgress() {
    generation++;
    cancel().catch(() => {});
    for (const id of retained.keys()) releaseJob(id).catch(() => {});
    progress?.dispose();
    progress = null;
  }
  function update(report, op, onProgress) {
    if (!report || op.generation !== generation) return;
    progress?.finishTargets({runId:op.id, files:(report.files || []).map(row => {
      const targets = row.targets || [];
      const failed = targets.some(t => t.state === 'error');
      const successes = targets.filter(t => t.state === 'ready' || t.state === 'saved').length;
      const running = targets.some(t => t.state === 'converting');
      const queued = targets.some(t => t.state === 'queued');
      const cancelled = targets.some(t => t.state === 'cancelled');
      const state = running ? 'converting' : queued ? 'queued' :
        failed || cancelled ? (successes ? 'partial' : cancelled && !failed ? 'cancelled' : 'error') : 'ready';
      return {fileId:row.fileId, targets, state, succeeded:successes, total:targets.length,
        error:targets.find(t => t.state === 'error')?.error || ''};
    })});
    onProgress?.({percent:report.percent ?? null,
      message:'Обработано: ' + (report.completed ?? 0) + ' из ' + (report.total ?? 0)
        + '. Запусков LibreOffice: ' + (report.attempted ?? 0)
        + ', пропущено Guard: ' + (report.filtered ?? 0) + '.'});
  }

  // Preserve the entire choice and its order. Only Guard's confirmed native
  // model may skip a route; filename hints never remove selected work here.
  function makePlan(inputs, formats) {
    return inputs.map(file => ({fileId:String(file.id), formats:[...formats]}));
  }

  async function run({files, formatIds, formatId, onProgress} = {}) {
    if (active) throw errorOf('Уже выполняется конвертация.', 'CLIENT_BUSY');
    const inputs = Array.from(files || []);
    const formats = formatIds || [formatId];
    if (!inputs.length || inputs.some(f => !f.file || !f.id))
      throw errorOf('Выберите исходные файлы.', 'INPUT_MISSING');
    if (!Array.isArray(formats) || !formats.length ||
        formats.some(f => typeof f !== 'string' || !/^[a-z0-9][a-z0-9_-]*$/.test(f)))
      throw errorOf('Выберите формат результата.', 'INVALID_FORMAT');
    const op = {id:Array.from(crypto.getRandomValues(new Uint8Array(16)),
      b=>b.toString(16).padStart(2,'0')).join(''), generation, cancelled:false, jobId:null};
    active = op;
    progress?.dispose();
    const root = document.querySelector('.aio-cv');
    progress = root && window.AIOFileProgress ? window.AIOFileProgress.attach(root) : null;
    progress?.begin({runId:op.id, fileIds:inputs.map(f => String(f.id))});
    let last = null;
    try {
      for (const id of [...retained.keys()]) await releaseJob(id);
      check(op);
      const info = metadata(inputs);
      const plan = makePlan(info, formats);
      const created = await api('/jobs', {files:info, plan});
      op.jobId = created.jobId;
      retained.set(op.jobId, {ticket:null, saved:new Set()});
      check(op);

      for (const file of inputs) {
        check(op);
        const response = await fetch(prefix + '/input?job=' + encodeURIComponent(op.jobId)
          + '&file=' + encodeURIComponent(String(file.id)), {
          method:'POST',headers:{...headers,'Content-Type':'application/octet-stream'},
          body:file.file,cache:'no-store'
        });
        const payload = await response.json();
        if (!response.ok) throw errorOf(payload.error || 'Не удалось передать файл Guard.',payload.code);
      }
      check(op);
      await api('/start', {jobId:op.jobId});
      for (;;) {
        // After explicit cancel, wait for Guard's real terminal state so that
        // already completed outputs can still be offered for saving.
        check(op, true);
        const state = await poll(op);
        check(op, true);
        if (state) {last = state; update(last, op, onProgress);}
        if (last.finished) break;
        await new Promise(resolve => setTimeout(resolve, 400));
      }

      if (!last.result?.ticket || !last.result?.downloadUrl) {
        const e = errorOf('Нет готового результата. См. подробности в «Проблемах».',
          last.state === 'cancelled' ? 'CANCELLED' : 'NO_SUCCESSFUL_CONVERSIONS');
        e.batchReport = last; throw e;
      }
      const result = {...last.result, progressRunId:op.id,
        targetReport:last, packagingError:last.packagingError,
        directLibreOffice:true, converterGuard:true};
      retained.get(op.jobId).ticket = result.ticket;
      return {ok:true, result, targetReport:last, schemaVersion:3,
        message: (last.state === 'cancelled' ? 'Конвертация отменена. Готовые результаты можно сохранить. ' : '') + (last.packagingError
          ? 'Архив не собран. Готовые файлы сохранены в Guard; при сохранении будут предложены отдельно.'
          : '')};
    } catch (error) {
      if (!error.batchReport) error.batchReport = last;
      if (error.batchReport) update(error.batchReport, op, onProgress);
      else progress?.failBatch({runId:op.id,cancelled:op.cancelled});
      if (op.cancelled) {
        const cancelled = errorOf('Конвертация отменена.', 'CANCELLED');
        cancelled.batchReport = error.batchReport;
        throw cancelled;
      }
      throw error;
    } finally {
      // Reset during /jobs creation used to leave a late-created job behind.
      // Failed uploads and jobs without outputs also need explicit release.
      if (op.jobId && (op.generation !== generation || !last?.result?.downloadUrl))
        await releaseJob(op.jobId);
      if (active === op) active = null;
    }
  }
  async function saveResult({result} = {}) {
    const jobId = result?.jobId;
    const ownership = retained.get(jobId);
    if (!jobId || ownership?.ticket !== result.ticket)
      throw errorOf('Результат больше недоступен.', 'RESULT_NOT_FOUND');
    let saved = false;
    progress?.beginSave({runId:result.progressRunId});
    try {
      const copies = result.packagingError && result.outputs?.length > 1 ? result.outputs : [result];
      for (const copy of copies) {
        if (ownership.saved.has(copy.ticket)) continue;
        const outcome = await bridge.saveExcel(copy);
        if (outcome?.saved !== true) return outcome;
        await api('/saved', {jobId,ticket:copy.ticket});
        ownership.saved.add(copy.ticket);
      }
      saved = true;
      await releaseJob(jobId);
      return {saved:true};
    } finally {
      progress?.finishSave({runId:result.progressRunId,saved});
    }
  }
  bridge.converter = Object.freeze({
    acceptedExtensions:'', directLibreOffice:true, converterGuard:true,
    inspectFiles({files=[],signal}={}) {return api('/inspect',{files:metadata(files)},'POST',signal);},
    convert:run,convertMany:run,saveResult,cancel,resetProgress,
    async diagnoseFiles({files=[],phase='before',result}={}) {
      const report=result?.targetReport;
      return {phase,files:metadata(files).map(file=>({...file,complete:false,
        facts:[],fonts:[],findings:[{level:'not_checked',formats:[],
          message:'Guard фильтрует направления, но не сравнивает качество содержимого.',advice:''}]})),
        result:{checks:[{state:'not_checked',
          message:'Проверка сохранности оформления и содержимого пока не выполнялась.'}]},
        scope:['Guard использует реестр LibreOffice и реальную native-модель.',
          ...(report ? report.files.flatMap(row=>row.targets.map(target=>
            target.format.toUpperCase()+': '+
            (target.state==='skipped'?'Пропущено Guard — ': '')+
            (target.error || target.state)+
            ' · LibreOffice: '+(target.libreOfficeStarted?'запускался':'не запускался'))) : [])]};
    }
  });
})();
