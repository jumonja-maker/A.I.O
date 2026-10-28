"""Owner-requested result controls, selection count and safe list clearing."""
import hashlib

EXPECTED_INPUT_SHA256='775ece031bd5cb67ca6d40a34b5f08968c226dee8820f117e7e988343e5a4b24'


def apply_lifecycle_ui(raw):
    if hashlib.sha256(raw).hexdigest()!=EXPECTED_INPUT_SHA256:
        raise ValueError('Lifecycle UI patch needs review after upstream changes')
    code=raw.decode('utf-8')
    def once(old,new):
        nonlocal code
        if code.count(old)!=1:raise ValueError('Lifecycle UI anchor changed: '+old[:90])
        code=code.replace(old,new,1)
    once('    const selectedFileIds = new Set();',
         '    const selectedFileIds = new Set();\n    let lifecycleGeneration=0,lifecycleBusy=false;')
    once('Файлы <span data-cv-count></span>',
         '<span data-cv-count aria-label="Выбрано / всего">0/0</span>')
    once('data-cv-add>+ Добавить файлы</button></div>',
         'data-cv-add>+ Добавить файлы</button><button type="button" class="aio-cv__small-btn aio-cv__clear-inputs" data-cv-clear-inputs title="Очистить список исходников" aria-label="Очистить список исходников">🗑</button></div>')
    once('<button class="aio-cv__small-btn" type="button" data-cv-save hidden>Сохранить результат</button>',
         '<div class="aio-cv__result-actions" data-cv-result-actions hidden><button class="aio-cv__small-btn" type="button" data-cv-close-result>Закрыть</button><button class="aio-cv__small-btn" type="button" data-cv-save>Сохранить результаты</button><button class="aio-cv__small-btn" type="button" data-cv-beforeafter>До/После</button></div>')
    once("root.querySelector('[data-cv-count]').textContent = `(${files.length})`;",
         "root.querySelector('[data-cv-count]').textContent = `${selectedFiles().length}/${files.length}`;")
    once("      saveBtn.hidden=!(currentResult && status==='ready');saveBtn.disabled=!(currentResult && status==='ready' && typeof adapter.saveResult==='function');\n      saveBtn.textContent = 'Сохранить результат';",
         "      root.querySelector('[data-cv-result-actions]').hidden=!(currentResult||aioFailedBatchReport?.jobId||status==='processing');\n"
         "      root.querySelector('[data-cv-close-result]').disabled=saving||lifecycleBusy;\n"
         "      root.querySelector('[data-cv-clear-inputs]').disabled=!files.length||saving||lifecycleBusy;\n"
         "      saveBtn.hidden=false;saveBtn.disabled=saving||lifecycleBusy||!currentResult||!['ready','saved'].includes(status);\n"
         "      saveBtn.textContent='Сохранить результаты';")
    once("if(result?.saved===true){status='saved';statusMessage='';renderSummary();}",
         "if(result?.saved===true){status=result.savedAll===false?'ready':'saved';statusMessage=result.savedAll===false?'Часть результатов сохранена. Остальные остаются в AIO.':'';renderSummary();}")
    # A confirmed close/list clear invalidates late completion/progress of
    # the earlier conversion, without changing state when the user chooses No.
    start=code.index('    async function runConversion() {')
    end=code.index('    async function saveOutput() {',start)
    part=code[start:end].replace('    async function runConversion() {',
        '    async function runConversion() {\n      const runGeneration=lifecycleGeneration;')
    part=part.replace('if(!alive||status', 'if(!alive||runGeneration!==lifecycleGeneration||status')
    part=part.replace('if(!alive)return;', 'if(!alive||runGeneration!==lifecycleGeneration)return;')
    code=code[:start]+part+code[end:]
    once("    local(runBtn,'click',runConversion);local(saveBtn,'click',saveOutput);",
         """    function clearCurrentConversion() {
      lifecycleGeneration++;currentResult=null;retryResult=null;aioFailedBatchReport=null;
      status='idle';statusMessage='';realProgress=null;render();
    }
    async function closeCurrentConversion() {
      if(lifecycleBusy||saving)return;
      lifecycleBusy=true;renderSummary();
      try {
        const response=await adapter.closeConversion?.(currentResult?.jobId||aioFailedBatchReport?.jobId);
        if(response?.closed&&alive)clearCurrentConversion();
      }catch(error){if(alive){statusMessage=error.userMessage||error.message;renderSummary();}}
      finally{lifecycleBusy=false;if(alive)renderSummary();}
    }
    async function clearLoadedInputs() {
      if(lifecycleBusy||saving)return;
      lifecycleBusy=true;renderSummary();
      try {
        const response=await adapter.clearInputList?.();
        if(response?.cleared&&alive){lifecycleGeneration++;files=[];selectedFileIds.clear();
          selectedFormats.clear();selectedFormat=null;formatValues={};analysis=null;
          filters='';filterInput.value='';
          if(!currentResult&&!aioFailedBatchReport)status='idle';
          statusMessage='';realProgress=null;render();typeFilter?.sync();}
      }catch(error){if(alive){statusMessage=error.userMessage||error.message;renderSummary();}}
      finally{lifecycleBusy=false;if(alive)renderSummary();}
    }
    local(root.querySelector('[data-cv-close-result]'),'click',closeCurrentConversion);
    local(root.querySelector('[data-cv-clear-inputs]'),'click',clearLoadedInputs);
    local(window,'aio-guard-job-released',event=>{
      if(alive&&event.detail?.jobId&&(event.detail.jobId===currentResult?.jobId||event.detail.jobId===aioFailedBatchReport?.jobId))clearCurrentConversion();
    });
    local(runBtn,'click',runConversion);local(saveBtn,'click',saveOutput);""")
    return code.encode('utf-8')
