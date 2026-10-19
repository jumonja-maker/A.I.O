/* A.I.O + native Converter Guard v0.4 integration (QA branch only).
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
  async function api(path, body, method = 'POST') {
    const response = await fetch(prefix + path, {
      method, headers: {...headers, ...(method === 'POST' ? {'Content-Type':'application/json'} : {})},
      ...(method === 'POST' ? {body: JSON.stringify(body)} : {}), cache: 'no-store'
    });
    const payload = await response.json();
    if (!response.ok) {
      const e = errorOf(payload.error || 'Операция Converter Guard не выполнена.', payload.code);
      e.stage = payload.stage;
      throw e;
    }
    return payload;
  }
  const check = operation => {
    if (operation.cancelled || operation.generation !== generation)
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
  // Guard's 'skipped' is a recorded, deliberately unattempted conversion,
  // NOT a success. Display why it was excluded in the problems panel.
  const visibleTarget = t => t.state === 'skipped'
    ? {...t, state: 'error', error: t.error || 'Направление недоступно для этого типа документа.'}
    : t;
  function update(report, op, onProgress, finished = false) {
    if (!report || op.generation !== generation) return;
    for (const row of report.files || []) {
      const targets = (row.targets || []).map(visibleTarget);
      const failed = targets.some(t => t.state === 'error');
      const ready = targets.some(t => t.state === 'ready' || t.state === 'saved');
      const running = targets.some(t => t.state === 'converting');
      const queued = targets.some(t => t.state === 'queued');
      const cancelled = targets.some(t => t.state === 'cancelled');
      const state = running ? 'converting' : queued ? 'queued' :
        failed ? (ready ? 'partial' : 'error') : cancelled ? 'cancelled' : 'ready';
      // A.I.O progress widget expects its existing state vocabulary.
      progress?.update({runId:op.id, fileId:row.fileId,
        state:state === 'partial' ? 'error' : state,
        percent:null, error:targets.find(t => t.error)?.error || ''});
    }
    if (finished) {
      progress?.finishTargets({runId:op.id, files:(report.files || []).map(row => {
        const targets = (row.targets || []).map(visibleTarget);
        const successes = targets.filter(t => t.state === 'ready' || t.state === 'saved').length;
        const errors = targets.filter(t => t.state === 'error' || t.state === 'cancelled').length;
        return {fileId:row.fileId, targets,
          state: errors ? (successes ? 'partial' : 'error') : 'ready',
          succeeded:successes, total:targets.length,
          error:targets.find(t => t.error)?.error || ''};
      })});
    }
    onProgress?.({percent:report.percent ?? null,
      message:'Обработано: ' + (report.completed ?? 0) + ' из ' + (report.total ?? 0)
        + '. Запусков LibreOffice: ' + (report.attempted ?? 0)
        + ', пропущено Guard: ' + (report.filtered ?? 0) + '.'});
  }

  // A route hint comes only from the installed LibreOffice registry. It must
  // not veto all of a source's explicit options: an extension may be renamed.
  function makePlan(inputs, formats, inspection) {
    const byFormat = new Map((inspection.formats || []).map(f => [f.id, f]));
    return inputs.map(file => {
      let options = formats.filter(format => {
        const info = byFormat.get(format);
        return !info || !Array.isArray(info.incompatibleFileIds) ||
          !info.incompatibleFileIds.includes(String(file.id));
      });
      if (!options.length) options = [formats[0]]; // user-selected native fallback
      return {fileId:String(file.id), formats:options};
    });
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
      const inspection = await api('/inspect', {files:info});
      check(op);
      const plan = makePlan(info, formats, inspection);
      const created = await api('/jobs', {files:info, plan});
      op.jobId = created.jobId;
      retained.set(op.jobId, null);
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
        check(op);
        last = await api('/jobs?job=' + encodeURIComponent(op.jobId), undefined, 'GET');
        update(last, op, onProgress, Boolean(last.finished));
        if (last.finished) break;
        await new Promise(resolve => setTimeout(resolve, 400));
      }

      if (!last.result?.ticket || !last.result?.downloadUrl) {
        const e = errorOf('Нет готового результата. См. подробности в «Проблемах».',
          last.state === 'cancelled' ? 'CANCELLED' : 'NO_SUCCESSFUL_CONVERSIONS');
        e.batchReport = last; throw e;
      }
      const result = {...last.result, progressRunId:op.id,
        targetReport:last, directLibreOffice:true, converterGuard:true};
      retained.set(op.jobId, result.ticket);
      return {ok:true, result, targetReport:last, schemaVersion:3};
    } catch (error) {
      if (!error.batchReport) error.batchReport = last;
      if (error.batchReport) update(error.batchReport, op, onProgress, true);
      else progress?.failBatch({runId:op.id,cancelled:op.cancelled});
      if (op.cancelled) {
        const cancelled = errorOf('Конвертация отменена.', 'CANCELLED');
        cancelled.batchReport = error.batchReport;
        throw cancelled;
      }
      throw error;
    } finally {
      if (active === op) active = null;
    }
  }
  async function saveResult({result} = {}) {
    const jobId = result?.jobId;
    if (!jobId || retained.get(jobId) !== result.ticket)
      throw errorOf('Результат больше недоступен.', 'RESULT_NOT_FOUND');
    let saved = false;
    progress?.beginSave({runId:result.progressRunId});
    try {
      const outcome = await bridge.saveExcel(result);
      saved = outcome?.saved === true;
      if (saved) {
        await api('/saved', {jobId,ticket:result.ticket});
        await releaseJob(jobId);
      }
      return outcome;
    } finally {
      progress?.finishSave({runId:result.progressRunId,saved});
    }
  }
  bridge.converter = Object.freeze({
    acceptedExtensions:'', directLibreOffice:true, converterGuard:true,
    inspectFiles({files=[]}={}) {return api('/inspect',{files:metadata(files)});},
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
