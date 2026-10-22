/* A.I.O + native Converter Guard v0.8 integration (QA branch only).
   Preserve the existing designer, Excel save bridge and independent LO engine. */
(() => {
  'use strict';
  const bridge = window.AllInOneBridge;
  const token = document.body.dataset.aioToken;
  if (!bridge || !token) return;

  const prefix = '/api/converter-guard';
  const headers = Object.freeze({'X-AIO-Token': token});
  const retained = new Map();
  const savingJobs = new Set();
  let active = null, progress = null, generation = 0;
  let prepared = null, preparing = null, inspectionSequence = 0;
  let inspected = null;
  let lastRun = null;
  const publish = report => {
    if (typeof CustomEvent === 'function' && typeof window.dispatchEvent === 'function')
      window.dispatchEvent(new CustomEvent('aio-guard-state', {detail:report}));
  };

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
      e.httpStatus = response.status;
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
  async function poll(operation, full = false) {
    const response = await fetch(prefix + '/jobs?job=' + encodeURIComponent(operation.jobId) + (full ? '' : '&delta=1'), {
      headers:{...headers, ...(!full && operation.etag ? {'If-None-Match':operation.etag} : {})},
      cache:'no-store'
    });
    if (response.status === 304) return null; // no JSON, paint or issues rerender
    let state = await payloadOf(response);
    if (state.jobId !== operation.jobId)
      throw errorOf('Получен статус другого задания.', 'STATUS_JOB_MISMATCH');
    // A command can finish while an earlier poll is still in flight. Never
    // roll the UI back, and recover a lost delta from a full state request.
    if (operation.snapshot && state.revision < operation.snapshot.revision) return null;
    if (state.delta) {
      if (!operation.snapshot || state.baseRevision !== operation.snapshot.revision) {
        operation.etag = null;
        if (!full) return poll(operation, true);
        throw errorOf('Не удалось восстановить состояние очереди.', 'STATUS_REVISION');
      }
      const changed = new Map(state.files.map(f=>[String(f.fileId),f]));
      state = {...state,files:operation.snapshot.files.map(f=>changed.get(String(f.fileId)) || f),
               changedFileIds:[...changed.keys()]};
    }
    operation.etag = response.headers.get('ETag');
    operation.snapshot = state;
    return state;
  }
  const check = (operation, allowCancelled = false) => {
    if ((!allowCancelled && operation.cancelled) || operation.generation !== generation)
      throw errorOf('Задание отменено.', 'CANCELLED');
  };
  async function releaseJob(id) {
    if (!id || savingJobs.has(id)) return false;
    try {
      await api('/release', {jobId: id});
      retained.delete(id);
      return true;
    } catch (_) {return false;}
  }
  async function cancel() {
    const op = active;
    if (!op) return;
    const previous = op.cancelled;
    op.cancelled = true;
    if (op.jobId) {
      try { return await control(op, '/cancel', {jobId: op.jobId}); }
      catch (error) {op.cancelled = previous; throw error;}
    }
  }
  async function control(op, path, body) {
    const state = await api(path, body);
    if (state.jobId === op.jobId && (!op.snapshot || state.revision >= op.snapshot.revision)) {
      op.snapshot = state;
      op.etag = null;
      if (op.generation === generation) {
        if (op === active) update(state, op, op.onProgress);
        else publish(state);
      }
    }
    return state;
  }
  function resetProgress() {
    generation++;
    cancel().catch(() => {});
    // Reset/dispose is not consent to discard unsaved conversion outputs.
    // Retained jobs remain available via /recoverable until explicitly handled.
    progress?.dispose();
    progress = null;
    lastRun = null;
    publish(null);
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
  const contains = (stage, inputs) => {
    if(!stage || stage.used)return false;
    const previous=new Map(stage.inputs.map(p=>[String(p.id),p.file]));
    return inputs.every(f=>previous.get(String(f.id))===f.file);
  };
  async function prepareInputs(inputs, {signal, onCreated, validate = () => {}, force = false,
                                       timeoutSeconds,onlyUndetermined=false} = {}) {
    if (!force && prepared && prepared.inputs.length === inputs.length && contains(prepared, inputs))
      return prepared;
    await cancelInspection();
    const previous=new Map((prepared?.inputs||[]).map(p=>[String(p.id),p.file]));
    const reusable = prepared && inputs.every(f=>!previous.has(String(f.id)) || previous.get(String(f.id))===f.file)
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
          uploads = inputs.filter(f=>previous.get(String(f.id))!==f.file);
        } catch (error) {if(error.code!=='JOB_NOT_FOUND')throw error;}
      }
      if (!created) created = await api('/prepare', {files:metadata(inputs)});
      request.jobId = created.jobId;
      onCreated?.(created.jobId);
      current();
      let uploaded=inputs.length-uploads.length;
      for (const file of uploads) {
        publish({jobId:request.jobId,state:'uploading',finished:false,files:[],
          total:0,completed:0,succeeded:0,failed:0,cancelled:0,filtered:0,
          uploadedFiles:uploaded,inputFiles:inputs.length,preparation:true,
          currentTask:{taskId:request.jobId+':upload:'+file.id,fileId:String(file.id),
            name:file.file.name,format:'',stage:'upload',startedMs:Date.now()}});
        await payloadOf(await fetch(prefix + '/input?job=' + encodeURIComponent(request.jobId)
          + '&file=' + encodeURIComponent(String(file.id)), {
          method:'POST',headers:{...headers,'Content-Type':'application/octet-stream'},
          body:file.file,cache:'no-store',signal
        }));
        current();
        uploaded++;
      }
      // A released older check can need a short process-tree cleanup. Do not
      // start parallel native imports while it is still shutting down.
      const deadline = Date.now() + 10000;
      let inspection;
      let observing=true;
      const monitor={jobId:request.jobId,generation,snapshot:null};
      const observation=(typeof CustomEvent==='function' && typeof window.dispatchEvent==='function') ? (async()=>{
        while(observing){
          try{const snapshot=await poll(monitor);request.snapshot=monitor.snapshot;
            if(snapshot && request.sequence===inspectionSequence)publish(snapshot);
          }catch(_){}
          if(observing)await new Promise(resolve=>setTimeout(resolve,400));
        }
      })() : Promise.resolve();
      try {
      for (;;) {
        current();
        try {inspection = await api('/preflight', {jobId:request.jobId,recheck:force,
          ...(timeoutSeconds ? {timeoutSeconds} : {}),...(onlyUndetermined ? {onlyUndetermined:true} : {})});break;}
        catch (error) {
          if (error.code !== 'PREFLIGHT_BUSY' || Date.now() >= deadline) throw error;
          await new Promise(resolve => setTimeout(resolve, 75));
        }
      }
      } finally {observing=false;await observation;}
      if(typeof CustomEvent==='function' && typeof window.dispatchEvent==='function') {
        try{publish(await api('/jobs?job='+encodeURIComponent(request.jobId),undefined,'GET'));}catch(_){}
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
  async function inspectFiles({files=[],signal,force=false,timeoutSeconds,onlyUndetermined=false}={}) {
    const inputs = Array.from(files);
    if (!inputs.length) {
      inspected = null;
      await cancelInspection();
      if (prepared) {await releaseJob(prepared.jobId);prepared = null;}
      return api('/inspect', {files:[]}, 'POST', signal);
    }
    const previous=new Map((inspected?.inputs||[]).map(p=>[String(p.id),p.file]));
    if (!force && !preparing && inspected && inputs.every(f=>previous.get(String(f.id))===f.file)) {
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
    return (await prepareInputs(inputs, {signal,force,timeoutSeconds,onlyUndetermined})).inspection;
  }
  function update(report, op, onProgress) {
    if (!report || op.generation !== generation) return;
    const changed = report.changedFileIds ? new Set(report.changedFileIds) : null;
    progress?.finishTargets({runId:op.id, partial:Boolean(changed), files:(report.files || []).filter(
      row=>!changed || changed.has(String(row.fileId))).map(row => {
      const targets = row.targets || [];
      const failed = targets.some(t => t.state === 'error');
      const successes = targets.filter(t => t.state === 'ready' || t.state === 'saved').length;
      const running = targets.some(t => t.state === 'converting');
      const queued = targets.some(t => t.state === 'queued');
      const cancelled = targets.some(t => t.state === 'cancelled');
      const skipped = targets.length && targets.every(t => t.state === 'skipped');
      const state = !targets.length ? 'skipped' : running ? 'converting' : queued ? 'queued' :
        skipped ? 'skipped' : failed || cancelled ? (successes ? 'partial' : cancelled && !failed ? 'cancelled' : 'error') : 'ready';
      return {fileId:row.fileId, targets, state, succeeded:successes, total:targets.length,
        allCopiesSaved:['ready','partial'].includes(state) && successes>0 && targets.every(t=>t.state!=='ready'),
        error:targets.find(t => t.state === 'error')?.error || ''};
    })});
    onProgress?.({percent:report.percent ?? null,
      message:'Обработано: ' + (report.completed ?? 0) + ' из ' + (report.total ?? 0)
        + '. Успешно: ' + (report.succeeded ?? 0) + ', ошибок: ' + (report.failed ?? 0)
        + ', отменено: ' + (report.cancelled ?? 0) + '.'});
    publish(report);
  }

  async function run({files, formatIds, formatId, onProgress, plan:requestedPlan, timeoutSeconds} = {}) {
    if (active) throw errorOf('Уже выполняется конвертация.', 'CLIENT_BUSY');
    const inputs = Array.from(files || []);
    const formats = formatIds || [formatId];
    if (!inputs.length || inputs.some(f => !f.file || !f.id))
      throw errorOf('Выберите исходные файлы.', 'INPUT_MISSING');
    if (!Array.isArray(formats) || !formats.length ||
        formats.some(f => typeof f !== 'string' || !/^[a-z0-9][a-z0-9_-]*$/.test(f)))
      throw errorOf('Выберите формат результата.', 'INVALID_FORMAT');
    const op = {id:Array.from(crypto.getRandomValues(new Uint8Array(16)),
      b=>b.toString(16).padStart(2,'0')).join(''), generation, cancelled:false, jobId:null,onProgress};
    active = op;
    progress?.dispose();
    const root = document.querySelector('.aio-cv');
    progress = root && window.AIOFileProgress ? window.AIOFileProgress.attach(root) : null;
    let last = null;
    try {
      // Preserve completed but unsaved jobs during retry/new conversion.
      // Only explicit save/discard may release their retained artifacts.
      check(op);
      const claim = id => {op.jobId=id;retained.set(id,{ticket:null,saved:new Set()});};
      let stage = contains(prepared,inputs) ? prepared :
        await prepareInputs(inputs,{onCreated:claim,validate:()=>check(op)});
      check(op);
      stage.used = true;prepared = null;claim(stage.jobId);
      const start = async () => {
        const eligible=new Map(stage.inspection.formats.map(t=>[t.id,new Set(t.verifiedFileIds||[])]));
        const requested=requestedPlan ? new Map(requestedPlan.map(p=>[p.fileId,new Set(p.formats)])) : null;
        const plan = inputs.map(f=>({fileId:String(f.id),formats:formats.filter(id=>
          (!requested || requested.get(String(f.id))?.has(id)) &&
          eligible.get(id)?.has(String(f.id)))})).filter(f=>f.formats.length);
        progress?.begin({runId:op.id,fileIds:inputs.map(f=>String(f.id))});
        // Files with no verified route must not remain visually queued.
        const plannedIds=new Set(plan.map(f=>f.fileId));
        const unplanned=inputs.filter(f=>!plannedIds.has(String(f.id)));
        if(unplanned.length)progress?.finishTargets({runId:op.id,partial:true,
          files:unplanned.map(f=>({fileId:String(f.id),targets:[],state:'skipped',
            succeeded:0,total:0}))});
        if (!plan.length) throw errorOf('Нет подтверждённых направлений. Повторите проверку.',
          'PREFLIGHT_ROUTE_UNDETERMINED');
        op.startRequested=true;op.startRejected=false;
        let state;
        try {
          state=await api('/start',{jobId:op.jobId,plan,fileIds:plan.map(f=>f.fileId),onlyVerified:true,
                                  archiveMode:window.AllInOneNativeShell === true ? 'on_demand' : 'auto',
                                  ...(timeoutSeconds ? {timeoutSeconds} : {})});
        } catch(error) {
          op.startRejected=error.httpStatus>=400&&error.httpStatus<500&&error.httpStatus!==408;
          throw error;
        }
        if(state.jobId){last=state;op.snapshot=state;update(state,op,onProgress);}
        return state;
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
      let statusFailures=0;
      for (;;) {
        // After explicit cancel, wait for Guard's real terminal state so that
        // already completed outputs can still be offered for saving.
        check(op, true);
        let state;
        try {state = await poll(op);}
        catch(error) {
          check(op, true);
          const transient=error instanceof TypeError||['TypeError','SyntaxError'].includes(error.name)||
            [408,429].includes(error.httpStatus)||error.httpStatus>=500||error.code==='STATUS_REVISION';
          if(!transient)throw error;
          op.etag=null;
          statusFailures++;
          const message='Не удалось обновить статус. Повторяем связь с очередью; готовые результаты остаются в AIO.';
          onProgress?.({percent:last?.percent??null,message});
          if(last)publish({...last,statusReadError:true,statusReadMessage:message});
          await new Promise(resolve=>setTimeout(resolve,Math.min(5000,500*statusFailures)));
          continue;
        }
        check(op, true);
        statusFailures=0;
        if (state) {last = state; update(last, op, onProgress);}
        if (last.finished) break;
        await new Promise(resolve => setTimeout(resolve, 400));
      }

      if (!last.result?.ticket || !last.result?.downloadUrl) {
        lastRun = {inputs:[...inputs],formats:[...formats],report:last};
        const e = errorOf('Нет готового результата. См. подробности в «Проблемах».',
          last.state === 'cancelled' ? 'CANCELLED' : 'NO_SUCCESSFUL_CONVERSIONS');
        e.batchReport = last; throw e;
      }
      const result = {...last.result, progressRunId:op.id,
        targetReport:last, packagingError:last.packagingError,
        directLibreOffice:true, converterGuard:true};
      retained.get(op.jobId).ticket = result.ticket;
      lastRun = {inputs:[...inputs],formats:[...formats],report:last};
      return {ok:true, result, targetReport:last, schemaVersion:3,
        message: (last.state === 'cancelled' ? 'Конвертация отменена. Готовые результаты можно сохранить. ' : '') + (last.packagingError
          ? 'Архив не собран. Все готовые файлы можно сохранить одной операцией в папку.'
          : '')};
    } catch (error) {
      if (!error.batchReport) error.batchReport = last;
      if (error.batchReport) update(error.batchReport, op, onProgress);
      else if(error.code!=='PREFLIGHT_ROUTE_UNDETERMINED')
        progress?.failBatch({runId:op.id,cancelled:op.cancelled});
      if (op.cancelled) {
        const cancelled = errorOf('Конвертация отменена.', 'CANCELLED');
        cancelled.batchReport = error.batchReport;
        throw cancelled;
      }
      throw error;
    } finally {
      // Reset during /jobs creation used to leave a late-created job behind.
      // Failed uploads and jobs without outputs also need explicit release.
      // A failed status read is not permission to cancel a live or ambiguously
      // started backend job. Explicit reset and confirmed empty terminal jobs
      // still release their ownership.
      if (op.jobId && (!op.startRequested || op.startRejected ||
                      last?.finished && !last.result?.downloadUrl &&
                      !last.partialResult?.outputsCount))
        await releaseJob(op.jobId);
      // Losing the UI generation is not proof that a live/cancelled
      // job has no outputs. Preserve it until explicit acknowledgement.
      if (active === op) active = null;
      if(op.generation===generation && last?.finished)publish(last);
    }
  }
  async function saveResult({result, mode} = {}) {
    const jobId = result?.jobId;
    const ownership = retained.get(jobId);
    if (!jobId || ownership?.ticket !== result.ticket)
      throw errorOf('Результат больше недоступен.', 'RESULT_NOT_FOUND');
    if (savingJobs.has(jobId)) throw errorOf('Результаты этого задания уже сохраняются.', 'SAVE_BUSY');
    if (mode === 'zip' && result.folderOnly)
      return bridge.converter.saveRetained(jobId, 'zip');
    savingJobs.add(jobId);
    const wasActive = active?.jobId === jobId;
    let saved = false;
    if (!wasActive && result.progressRunId) progress?.beginSave({runId:result.progressRunId});
    try {
      const folder = mode === 'folder' || (mode !== 'zip' && (result.folderOnly ||
        result.packagingError && result.outputs?.length > 1));
      const outcome = await bridge.saveExcel({...result, ...(folder ? {saveMode:'folder'} : {})});
      if (outcome?.saved !== true) return outcome;
      if (folder && (outcome.incompleteRecovery || outcome.savedAll !== true && !outcome.snapshotComplete))
        throw errorOf('Восстановленный журнал неполон. Доступные файлы скопированы, но в AIO могут оставаться другие результаты. Не удаляйте служебную папку.', 'SAVE_INCOMPLETE_ACK');
      const acknowledgement = folder ? outcome : await api('/saved', {jobId,ticket:result.ticket,compact:true});
      ownership.saved.add(result.ticket);
      saved = true;
      // A partial snapshot is not a request to stop conversion. An operation
      // that was still being observed also keeps its tickets until its caller
      // has consumed the terminal state, avoiding a /jobs -> 404 save race.
      const release = acknowledgement.savedAll === true && !wasActive && active?.jobId !== jobId;
      savingJobs.delete(jobId);
      const released=release ? await releaseJob(jobId) : false;
      // Notify both the preserved main controller and the queue about this
      // exact job. Saving an old job must not reset a new active conversion.
      const detail={...acknowledgement,jobId,runId:result.progressRunId||null,released};
      if (typeof CustomEvent === 'function') {
        if (released) document.querySelector('.aio-cv')?.dispatchEvent?.(
          new CustomEvent('aio-guard-saved',{detail}));
        window.dispatchEvent?.(new CustomEvent('aio-guard-save-confirmed',{detail}));
      }
      return {...acknowledgement,saved:true};
    } finally {
      savingJobs.delete(jobId);
      if (!wasActive && result.progressRunId) progress?.finishSave({runId:result.progressRunId,saved});
    }
  }
  bridge.converter = Object.freeze({
    acceptedExtensions:'', directLibreOffice:true, converterGuard:true,
    inspectFiles,convert:run,convertMany:run,saveResult,cancel,resetProgress,cancelInspection,dispose,
    async pause(paused=true) {
      if(!active?.jobId)throw errorOf('Нет выполняющегося задания для паузы.', 'NO_ACTIVE_JOB');
      return control(active,'/pause',{jobId:active.jobId,paused});
    },
    canRetry() {return Boolean(lastRun&&!active);},
    async skipCurrent(fileIdFromView) {
      const op=active?.snapshot?.currentTask ? active : preparing;
      const fileId=fileIdFromView ?? op?.snapshot?.currentTask?.fileId;
      if(!op?.jobId || !fileId)
        throw errorOf('Текущая операция ещё не определена или уже завершена. Обновите состояние очереди.', 'NO_CURRENT_TASK');
      // Verify the current backend task; a stale UI must not cancel a queued input.
      const latest=await api('/jobs?job='+encodeURIComponent(op.jobId),undefined,'GET');
      if(String(latest.currentTask?.fileId||'')!==String(fileId))
        throw errorOf('Очередь перешла к другому файлу. Обновите состояние и повторите действие.', 'STALE_CURRENT_TASK');
      const state=await control(op,'/cancel-file',{jobId:op.jobId,fileId,
        expectedTaskId:latest.currentTask.taskId});
      const file=state.files?.find(f=>String(f.fileId)===String(fileId));
      if(file && !file.cancellationRequested && file.targets?.every(t=>
        ['ready','saved','error','cancelled','skipped'].includes(t.state)))
        throw errorOf('Этот файл уже завершён. Очередь могла перейти к следующему файлу.', 'FILE_ALREADY_FINISHED');
      return state;
    },
    async recoverable() {return api('/recoverable',undefined,'GET');},
    async saveRetained(jobId,mode='folder') {
      let state=await api('/jobs?job='+encodeURIComponent(jobId),undefined,'GET');
      if(mode==='zip'&&!state.finished)
        throw errorOf('Пока очередь работает, готовые результаты можно сохранить в папку. ZIP доступен после завершения.', 'ZIP_REQUIRES_FINISHED');
      if(mode==='zip' && (state.bundleDeferred || state.packagingError)) {
        await api('/bundle',{jobId});
        const op={jobId,generation};
        do {state=await poll(op)||state;if(!state.finished)await new Promise(r=>setTimeout(r,200));}while(!state.finished);
        if(state.bundleDeferred || state.packagingError)
          throw errorOf('ZIP не собран. Сохраните все результаты в папку.','BUNDLE_UNAVAILABLE');
      }
      const result=state.result || (mode==='folder' ? state.partialResult : null);
      if(!result)throw errorOf('Нет доступных результатов.','RESULT_NOT_FOUND');
      retained.set(jobId,{ticket:result.ticket,saved:retained.get(jobId)?.saved||new Set()});
      return saveResult({result:{...result,packagingError:state.packagingError},mode});
    },
    async discardRetained(jobId) {
      if(active?.jobId===jobId)
        throw errorOf('Сначала завершите или отмените очередь. После этого можно удалить её результаты.', 'LIVE_JOB_DISCARD');
      if(savingJobs.has(jobId))throw errorOf('Дождитесь завершения сохранения.', 'SAVE_BUSY');
      await api('/release',{jobId,discard:true});retained.delete(jobId);
    },
    async retryFailed({includeCancelled=false,timeoutSeconds}={}) {
      if(active)throw errorOf('Дождитесь завершения или отмените текущую очередь перед повтором ошибок.', 'CLIENT_BUSY');
      if(!lastRun)throw errorOf('Исходники повторной попытки недоступны. Добавьте файлы.','RETRY_INPUTS');
      const plan=lastRun.report.files.map(f=>({fileId:String(f.fileId),formats:f.targets.filter(
        t=>t.state==='error'||includeCancelled&&t.state==='cancelled').map(t=>t.format)})).filter(p=>p.formats.length);
      const ids=new Set(plan.map(p=>p.fileId));
      if(!plan.length)throw errorOf('В этом задании нет выбранных ошибок для повтора.', 'NO_FAILED_TASKS');
      return run({files:lastRun.inputs.filter(f=>ids.has(String(f.id))),formatIds:lastRun.formats,plan,timeoutSeconds});
    },
    async diagnoseFiles({files=[],phase='before',result}={}) {
      const report=result?.targetReport;
      const byId=new Map((report?.files||[]).map(row=>[String(row.fileId),row]));
      return {phase,files:metadata(files).map(file=>{
        const targets=byId.get(String(file.id))?.targets||[];
        const findings=targets.flatMap(target=>(target.warnings||[]).map(warning=>({
          level:'risk',formats:[target.format],message:warning.message,advice:'Сохраните исходник.'})));
        return {...file,complete:false,facts:[],fonts:[],findings:[...findings,
          {level:'not_checked',formats:[],message:'Guard подтверждает маршруты, но не сравнивает полноту содержимого и внешний вид.',advice:''}]};
      }),result:{checks:[{state:'not_checked',
          message:'Проверка сохранности оформления и содержимого пока не выполнялась.'}]},
        scope:['Guard использует реестр LibreOffice и реальную native-модель.',
          'Статус готовности означает создание результата, а не доказанное сохранение качества.',
          'Все направления, ошибки и выбранные фильтры доступны в техническом отчёте панели «Состояние».']};
    }
  });
})();
