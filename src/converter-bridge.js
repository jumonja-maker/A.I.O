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
  const htmlCompanions = new WeakMap();
  let active = null, progress = null, generation = 0;
  let prepared = null, preparing = null, inspectionSequence = 0;
  let inspected = null;
  let lastRun = null;
  let qualityCheck = false;
  // Native saves are acknowledged only after an independent read of disk bytes.
  const verifySaved = true;
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
    const response = await fetch(prefix + '/jobs?job=' + encodeURIComponent(operation.jobId) + (full ? '' : '&delta=1&resultDelta=1'), {
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
      if (state.resultOutputsDelta === true && state.result) {
        if (!operation.snapshot.result || !Array.isArray(operation.snapshot.result.outputs)) {
          // A result wrapper can appear during packaging, after its files
          // already appeared in earlier snapshots. An incremental inventory
          // cannot establish that initial complete set; resync it once.
          operation.etag = null;
          if (!full) return poll(operation, true);
          throw errorOf('Не удалось восстановить список результатов.', 'STATUS_REVISION');
        }
        // Outputs grow throughout a large batch. Guard sends only changed
        // tickets when this client explicitly negotiates the delta contract.
        // Keep old tickets (including saved ones), replace updated metadata,
        // and never merge into the anchor-only partialResult descriptor.
        const previous = operation.snapshot.result.outputs;
        const additions = state.result.outputs || [];
        if (additions.length) {
          const outputs = previous.slice();
          // Index only once per authoritative inventory. A 14,000-output
          // batch otherwise rebuilds all ticket entries for each small delta.
          // The copied array still preserves every previously published view.
          let indexes=operation.outputIndex;
          if(operation.outputIndexAnchor!==previous||!indexes)
            indexes=new Map(previous.map((output,i)=>[output.ticket,i]));
          for (const output of additions) {
            const index = indexes.get(output.ticket);
            if (index === undefined) {indexes.set(output.ticket,outputs.length);outputs.push(output);}
            else outputs[index] = output;
          }
          operation.outputIndex=indexes;operation.outputIndexAnchor=outputs;
          state = {...state,result:{...state.result,outputs}};
        } else state = {...state,result:{...state.result,outputs:previous}};
      }
      state = {...state,files:operation.snapshot.files.map(f=>changed.get(String(f.fileId)) || f),
               changedFileIds:[...changed.keys()]};
    }
    if(!state.result||operation.outputIndexAnchor!==state.result.outputs){
      operation.outputIndex=null;operation.outputIndexAnchor=null;
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
    if (!request) return;
    request.cancelled = true;
    // /release rejects a live checking job (JOB_BUSY) and leaves its native
    // LibreOffice process running. Cancel it first; prepareInputs.finally owns
    // release after the blocked /preflight request has unwound. A cancelled
    // inspection must not keep the global preflight gate for 510 more files.
    if (request.jobId) {
      try { await api('/cancel', {jobId:request.jobId}); }
      catch (error) {
        if (error.code !== 'JOB_NOT_FOUND') throw error;
      }
    }
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
  async function uploadCompanions(jobId, input, entries, {signal, current=()=>{}}={}) {
    await api('/resources', {jobId,fileId:String(input.id),
      resources:entries.map(e=>({path:e.path,size:e.file.size}))});
    let next=0, failed=null;
    const worker=async()=>{
      while(!failed) {
        current();
        const entry=entries[next++];if(!entry)return;
        try{
          await payloadOf(await fetch(prefix+'/resource?job='+encodeURIComponent(jobId)+
            '&file='+encodeURIComponent(String(input.id))+'&path='+encodeURIComponent(entry.path), {
              method:'POST',headers:{...headers,'Content-Type':'application/octet-stream'},
              body:entry.file,cache:'no-store',signal}));
        }catch(error){failed=error;return;}
      }
    };
    const transfers=await Promise.allSettled(Array.from({length:Math.min(4,entries.length)},worker));
    const rejected=transfers.find(r=>r.status==='rejected');
    if(failed||rejected)throw failed||rejected.reason;
    current();
  }

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
      if (!created) created = await api('/prepare', {files:metadata(inputs),retainSources:true});
      request.jobId = created.jobId;
      onCreated?.(created.jobId);
      current();
      let uploaded=inputs.length-uploads.length, nextUpload=0, transferError=null, lastUploadPaint=0;
      const showUpload=file=>{
        const now=Date.now();
        if(now-lastUploadPaint<100 && uploaded<inputs.length)return;
        lastUploadPaint=now;
        publish({jobId:request.jobId,state:'uploading',finished:false,files:[],
          total:0,completed:0,succeeded:0,failed:0,cancelled:0,filtered:0,
          uploadedFiles:uploaded,inputFiles:inputs.length,preparation:true,
          currentTask:{taskId:request.jobId+':upload:'+file.id,fileId:String(file.id),
            name:file.file.name,format:'',stage:'upload',startedMs:now}});
      };
      // Bounded local transfers, not parallel LibreOffice operations. Wait for
      // every in-flight upload before releasing a failed/cancelled preparation.
      const uploadWorker=async()=>{
        while(!transferError){
          current();
          const file=uploads[nextUpload++];if(!file)return;
          showUpload(file);
          try{
            await payloadOf(await fetch(prefix + '/input?job=' + encodeURIComponent(request.jobId)
              + '&file=' + encodeURIComponent(String(file.id)), {
              method:'POST',headers:{...headers,'Content-Type':'application/octet-stream'},
              body:file.file,cache:'no-store',signal
            }));
            current();uploaded++;showUpload(file);
          }catch(error){transferError=transferError||error;return;}
        }
      };
      const transfers=await Promise.allSettled(Array.from({length:Math.min(4,uploads.length)},uploadWorker));
      const rejected=transfers.find(result=>result.status==='rejected');
      if(transferError||rejected)throw transferError||rejected.reason;
      for(const input of uploads) {
        const companions=htmlCompanions.get(input.file);
        if(companions)await uploadCompanions(request.jobId,input,companions,{signal,current});
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
      let finalState=null;
      if(typeof CustomEvent==='function' && typeof window.dispatchEvent==='function') {
        try{finalState=await api('/jobs?job='+encodeURIComponent(request.jobId),undefined,'GET');}catch(_){}
      }
      current();
      prepared = {jobId:request.jobId,inputs:[...inputs],inspection,used:false};
      inspected = {inputs:[...inputs],inspection};
      accepted = true;
      if(preparing===request)preparing=null;
      if(finalState)publish(finalState);
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
    // Backend snapshots contain planned inputs only; unplanned UI files have
    // already been marked skipped. Apply this subset without requiring a
    // full-card count match, including the first and terminal snapshots.
    progress?.finishTargets({runId:op.id, partial:true, files:(report.files || []).filter(
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
                                  qualityCheck,
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
      const outcome = await bridge.saveExcel({...result, verifySaved, ...(folder ? {saveMode:'folder'} : {})});
      if (outcome?.saved !== true) return outcome;
      if (verifySaved && window.AllInOneNativeShell && outcome.readbackVerified !== true)
        throw errorOf('Приложение не подтвердило проверку сохранённых байтов. Результаты оставлены в Guard.', 'SAVE_VERIFY_MISSING');
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
          new CustomEvent('aio-guard-saved',{detail,bubbles:true}));
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
    getQualityCheck() {return qualityCheck;},
    setQualityCheck(enabled) {
      if(typeof enabled!=='boolean')throw errorOf('Некорректный режим проверки.','QUALITY_POLICY');
      if(active?.startRequested)throw errorOf('Режим проверки можно изменить перед следующей конвертацией.','QUALITY_BUSY');
      qualityCheck=enabled;
    },
    setSaveVerification() { return verifySaved; },
    getSaveVerification() { return verifySaved; },
    getHtmlSources() {
      if(active||preparing)return [];
      return (prepared?.inputs||[]).filter(f=>/\.(x?html?|htm)$/i.test(f.file.name))
        .map(f=>({id:String(f.id),name:f.file.name}));
    },
    async attachHtmlResources({fileId, files}={}) {
      if(active||preparing||!prepared)throw errorOf('Дождитесь проверки исходников.', 'RESOURCE_STATE');
      const input=prepared.inputs.find(f=>String(f.id)===String(fileId));
      if(!input||!/\.(x?html?|htm)$/i.test(input.file.name))throw errorOf('Выберите исходник HTML.', 'RESOURCE_SOURCE');
      const picked=Array.from(files||[]);
      const matches=picked.filter(f=>f.name===input.file.name&&f.size===input.file.size&&f.webkitRelativePath);
      if(matches.length!==1)throw errorOf('Выберите папку, содержащую исходный HTML и его изображения/стили.', 'RESOURCE_SOURCE');
      const original=matches[0], base=original.webkitRelativePath.slice(0,-original.name.length);
      const entries=picked.filter(f=>f!==original&&f.webkitRelativePath.startsWith(base))
        .map(file=>({path:file.webkitRelativePath.slice(base.length),file}));
      if(!entries.length)throw errorOf('В папке не найдены дополнительные файлы.', 'RESOURCE_EMPTY');
      const stage=prepared;
      htmlCompanions.set(input.file,entries);
      try {
        await uploadCompanions(stage.jobId,input,entries);
        const inspection=await api('/preflight',{jobId:stage.jobId});
        if(prepared!==stage)throw errorOf('Выбор исходников изменён.', 'CANCELLED');
        stage.inspection=inspection;inspected={inputs:[...stage.inputs],inspection};
        publish(await api('/jobs?job='+encodeURIComponent(stage.jobId),undefined,'GET'));
        return inspection;
      } catch(error) {
        await releaseJob(stage.jobId);if(prepared===stage)prepared=null;inspected=null;
        throw error;
      }
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
      if(typeof CustomEvent==='function')
        window.dispatchEvent?.(new CustomEvent('aio-guard-job-released',{detail:{jobId,discarded:true}}));
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
        for(const target of targets){
          const q=target.quality;
          if(target.checkedFeatures?.length)findings.push({level:'info',formats:[target.format],
            message:'Проверены отдельные свойства: '+target.checkedFeatures.join(', ')+'.',
            advice:'Это не проверка полной сохранности содержимого и оформления.'});
          if(target.saveStatus)findings.push({level:'info',formats:[target.format],
            message:'Сохранение: '+({saved:'сохранено',partial:'сохранено частично',unsaved:'не сохранено',
              not_applicable:'нет готовых файлов'}[target.saveStatus]||target.saveStatus)+
              (target.readbackVerified?' · сохранённые байты прочитаны и сверены.':'. Чтение сохранённой копии не подтверждено.'),advice:''});
          if(target.unknownReasons?.length)findings.push({level:'not_checked',formats:[target.format],
            message:'Границы проверки: '+target.unknownReasons.join(', ')+'.',advice:'Причины сохранены в отчёте.'});
          for(const risk of q?.risks||[])findings.push({level:'risk',formats:[target.format],
            message:'Проверка структуры: '+({TEXT_DIFFERS:'текст отличается',CELL_VALUES_DIFFER:'значения ячеек отличаются',
              STRUCTURE_COUNT_DECREASED:'меньше элементов ('+risk.feature+')'}[risk.code]||risk.code)+'. Требуется проверка.',advice:'Сравните с исходником.'});
          if(q)findings.push({level:q.qualityCompared?'info':'not_checked',formats:[target.format],
            message:q.qualityCompared?'Проверена часть структуры. Внешний вид и шрифты не проверены.':
              'Структуру этих форматов не удалось сопоставить.',advice:'Подробности в техническом отчёте.'});
        }
        return {...file,complete:false,facts:[],fonts:[],findings:[...findings,
          {level:'not_checked',formats:[],message:'Готовность результата не гарантирует сохранение оформления. Проверка структуры включается перед конвертацией в «Состояние».',advice:''}]};
      }),result:{checks:[{state:'not_checked',
          message:'Полная сохранность оформления и содержимого не подтверждена. Выполненные проверки перечислены отдельно.'}]},
        scope:['Guard использует реестр LibreOffice и реальную native-модель.',
          'Статус готовности означает создание результата, а не доказанное сохранение качества.',
          'Все направления, ошибки и выбранные фильтры доступны в техническом отчёте панели «Состояние».']};
    }
  });
})();
