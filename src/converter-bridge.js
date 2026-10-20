/* A.I.O + native Converter Guard v0.7 integration (QA branch only).
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
  let prepared = null, preparing = null, inspectionSequence = 0;
  let inspected = null;

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
  async function cancelInspection() {
    inspectionSequence++;
    const request = preparing;
    if (request) {request.cancelled = true; await releaseJob(request.jobId);}
  }
  function dispose() {
    inspected = null;
    resetProgress();
    cancelInspection().catch(() => {});
    if (prepared) {releaseJob(prepared.jobId).catch(() => {});prepared = null;}
  }
  const contains = (stage, inputs) => stage && !stage.used && inputs.every(f =>
    stage.inputs.some(p => String(p.id) === String(f.id) && p.file === f.file));
  async function prepareInputs(inputs, {signal, onCreated, validate = () => {}, force = false} = {}) {
    if (!force && prepared && prepared.inputs.length === inputs.length && contains(prepared, inputs))
      return prepared;
    await cancelInspection();
    const reusable = prepared && inputs.every(f=>!prepared.inputs.some(p=>String(p.id)===String(f.id)&&p.file!==f.file))
      ? prepared : null;
    if (prepared && !reusable) await releaseJob(prepared.jobId);
    prepared = null;
    const request = {sequence:++inspectionSequence,jobId:null,cancelled:false};
    preparing = request;
    const current = () => {
      validate();
      if (request.cancelled || signal?.aborted || request.sequence !== inspectionSequence)
        throw errorOf('Предварительная проверка отменена.', 'CANCELLED');
    };
    let accepted = false;
    try {
      current();
      let created, uploads = inputs;
      if (reusable) {
        request.jobId = reusable.jobId;
        try {
          created = await api('/prepare-update',{jobId:reusable.jobId,files:metadata(inputs)});
          uploads = inputs.filter(f=>!reusable.inputs.some(p=>String(p.id)===String(f.id)&&p.file===f.file));
        } catch (error) {if(error.code!=='JOB_NOT_FOUND')throw error;}
      }
      if (!created) created = await api('/prepare', {files:metadata(inputs)});
      request.jobId = created.jobId;
      onCreated?.(created.jobId);
      current();
      for (const file of uploads) {
        await payloadOf(await fetch(prefix + '/input?job=' + encodeURIComponent(request.jobId)
          + '&file=' + encodeURIComponent(String(file.id)), {
          method:'POST',headers:{...headers,'Content-Type':'application/octet-stream'},
          body:file.file,cache:'no-store',signal
        }));
        current();
      }
      // A released older check can need a short process-tree cleanup. Do not
      // start parallel native imports while it is still shutting down.
      const deadline = Date.now() + 10000;
      let inspection;
      for (;;) {
        current();
        try {inspection = await api('/preflight', {jobId:request.jobId,recheck:force});break;}
        catch (error) {
          if (error.code !== 'PREFLIGHT_BUSY' || Date.now() >= deadline) throw error;
          await new Promise(resolve => setTimeout(resolve, 75));
        }
      }
      current();
      prepared = {jobId:request.jobId,inputs:[...inputs],inspection,used:false};
      inspected = {inputs:[...inputs],inspection};
      accepted = true;
      return prepared;
    } finally {
      if (preparing === request) preparing = null;
      if (!accepted && request.jobId) await releaseJob(request.jobId);
    }
  }
  function projectInspection(inspection, inputs) {
    const ids = new Set(inputs.map(f=>String(f.id)));
    const files = inspection.files.filter(f=>ids.has(String(f.id)));
    const formats = inspection.formats.map(f=>{
      const value = {...f};
      for (const key of ['selectableFileIds','verifiedFileIds','compatibleFileIds',
                         'undeterminedFileIds','incompatibleFileIds'])
        value[key] = (f[key] || []).filter(id=>ids.has(String(id)));
      value.enabled = Boolean(inspection.available && value.verifiedFileIds.length);
      return value;
    });
    return {...inspection,files,formats,
      message:files.some(f=>f.preflight?.state!=='detected') ? inspection.message : ''};
  }
  async function inspectFiles({files=[],signal,force=false}={}) {
    const inputs = Array.from(files);
    if (!inputs.length) {
      inspected = null;
      await cancelInspection();
      if (prepared) {await releaseJob(prepared.jobId);prepared = null;}
      return api('/inspect', {files:[]}, 'POST', signal);
    }
    if (!force && !preparing && inspected && inputs.every(f=>inspected.inputs.some(
      p=>String(p.id)===String(f.id) && p.file===f.file))) {
      if (inputs.length===inspected.inputs.length && inputs.every((f,i)=>
        String(f.id)===String(inspected.inputs[i].id) && f.file===inspected.inputs[i].file))
        return inspected.inspection;
      // Removal only: immutable File references, no upload/native restart.
      // This view is never accepted as authority by the backend at start().
      const inspection = projectInspection(inspected.inspection,inputs);
      inspected = {inputs:[...inputs],inspection};
      if (prepared) {
        const ids=new Set(inputs.map(f=>String(f.id)));
        prepared.inputs=prepared.inputs.filter(f=>ids.has(String(f.id)));
        prepared.inspection=inspection;
      }
      return inspection;
    }
    return (await prepareInputs(inputs, {signal,force})).inspection;
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
      const skipped = targets.length && targets.every(t => t.state === 'skipped');
      const state = running ? 'converting' : queued ? 'queued' :
        skipped ? 'skipped' : failed || cancelled ? (successes ? 'partial' : cancelled && !failed ? 'cancelled' : 'error') : 'ready';
      return {fileId:row.fileId, targets, state, succeeded:successes, total:targets.length,
        error:targets.find(t => t.state === 'error')?.error || ''};
    })});
    onProgress?.({percent:report.percent ?? null,
      message:'Обработано: ' + (report.completed ?? 0) + ' из ' + (report.total ?? 0)
        + '. Экспортов LibreOffice: ' + (report.attempted ?? 0)
        + ', пропущено Guard: ' + (report.filtered ?? 0) + '.'});
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
    let last = null;
    try {
      for (const id of [...retained.keys()]) await releaseJob(id);
      check(op);
      const claim = id => {op.jobId=id;retained.set(id,{ticket:null,saved:new Set()});};
      let stage = contains(prepared,inputs) ? prepared :
        await prepareInputs(inputs,{onCreated:claim,validate:()=>check(op)});
      check(op);
      stage.used = true;prepared = null;claim(stage.jobId);
      const start = () => {
        const plan = inputs.map(f=>({fileId:String(f.id),formats:formats.filter(id=>
          stage.inspection.formats.some(target=>target.id===id &&
            target.verifiedFileIds?.includes(String(f.id))))})).filter(f=>f.formats.length);
        if (!plan.length) throw errorOf('Нет подтверждённых направлений. Повторите проверку.',
          'PREFLIGHT_ROUTE_UNDETERMINED');
        progress?.begin({runId:op.id,fileIds:plan.map(f=>f.fileId)});
        return api('/start',{jobId:op.jobId,plan,fileIds:plan.map(f=>f.fileId),onlyVerified:true});
      };
      try {await start();}
      catch (error) {
        if (error.code !== 'JOB_NOT_FOUND') throw error;
        // A long-idle prepared job may expire. Revalidate the same File
        // objects instead of treating expiry as an unsupported document.
        retained.delete(op.jobId);
        stage = await prepareInputs(inputs,{onCreated:claim,validate:()=>check(op)});
        check(op);stage.used=true;prepared=null;claim(stage.jobId);await start();
      }
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
    inspectFiles,convert:run,convertMany:run,saveResult,cancel,resetProgress,cancelInspection,dispose,
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
