"""Owner-requested converter UI update. Applied after existing signed-asset patches.

Input and output hashes are pinned; engine code and the signed payload are unchanged.
"""
import re


def apply_selection(code: str) -> str:
    def once(old, new):
        nonlocal code
        assert code.count(old) == 1, (old[:100], code.count(old))
        code = code.replace(old, new, 1)

    once("let currentResult = null, status = 'idle', statusMessage = '', realProgress = null;",
         "let currentResult = null, status = 'idle', statusMessage = '', realProgress = null;\n"
         "    const selectedFileIds = new Set();\n    let saving = false;")
    once("const filePayload = () => files.map(({id,file}) => ({id, file, name:file.name, size:file.size}));",
         "const filePayload = () => files.map(({id,file}) => ({id, file, name:file.name, size:file.size}));\n"
         "    const selectedFiles = () => files.filter(f => selectedFileIds.has(f.id));\n"
         "    const selectedPayload = () => filePayload().filter(f => selectedFileIds.has(f.id));\n"
         "    const formatAvailable = fmt => fmt.enabled && selectedFiles().some(f => fmt.compatibleFileIds.includes(f.id));\n"
         "    function selectFileGroup(ids,checked) {\n"
         "      if(status==='processing'||status==='checking'||saving)return;\n"
         "      const valid=new Set(files.map(f=>f.id));\n"
         "      for(const id of ids){if(!valid.has(String(id)))continue;if(checked)selectedFileIds.add(String(id));else selectedFileIds.delete(String(id));}\n"
         "      filters='';filterInput.value='';\n"
         "      currentResult=null;retryResult=null;status='idle';statusMessage='';realProgress=null;\n"
         "      adapter.resetProgress?.();render();\n"
         "      announce('Выбрано файлов: '+selectedFileIds.size);\n"
         "    }\n"
         "    function toggleFile(id) {\n"
         "      if (status==='processing' || status==='checking' || saving) return;\n"
         "      if(selectedFileIds.has(id))selectedFileIds.delete(id);else selectedFileIds.add(id);\n"
         "      currentResult=null;retryResult=null;status='idle';statusMessage='';realProgress=null;\n"
         "      adapter.resetProgress?.();render();\n"
         "      fileArea.querySelector(`[data-file-id=\"${id}\"] [data-op=\"select\"]`)?.focus({preventScroll:true});\n"
         "      announce(selectedFileIds.has(id)?'Файл выбран для конвертации.':'Выбор файла снят.');\n"
         "    }")
    once("files.push(...accepted.map(file => ({id: `f${++nextFileId}`, file})));",
         "files.push(...accepted.map(file => {const id=`f${++nextFileId}`;selectedFileIds.add(id);return {id,file};}));")
    once("files=files.filter(x=>x.id!==id); inspect();", "selectedFileIds.delete(id);files=files.filter(x=>x.id!==id); inspect();")

    # Remove just the controls and caption circled by the owner.
    old = """<button type="button" class="aio-cv__icon-btn" data-op="up" aria-label="Переместить ${esc(file.name)} выше" title="Выше">↑</button><button type="button" class="aio-cv__icon-btn" data-op="down" aria-label="Переместить ${esc(file.name)} ниже" title="Ниже">↓</button>"""
    new = """<button type="button" class="aio-cv__select-file" data-op="select" role="checkbox" aria-checked="${selectedFileIds.has(id)}" aria-label="Выбрать для конвертации: ${esc(file.name)}" title="${selectedFileIds.has(id)?'Снять выбор':'Выбрать для конвертации'}"><span aria-hidden="true"></span></button>"""
    once(old,new)
    once("item.dataset.fileId=id;", "item.dataset.fileId=id;item.dataset.selected=String(selectedFileIds.has(id));")
    once("if (status==='processing') item.querySelectorAll('[data-op]').forEach(b=>b.disabled=true);",
         "if (status==='processing' || status==='checking' || saving) item.querySelectorAll('[data-op]').forEach(b=>b.disabled=true);")
    once("if (button.dataset.op==='up') reorderFile(id,-1);\n          if (button.dataset.op==='down') reorderFile(id,1);",
         "if (button.dataset.op==='select') toggleFile(id);")
    once("root.querySelector('[data-cv-left-status]').textContent = files.length ? `Файлов в списке: ${files.length}. Порядок можно менять стрелками ↑ ↓ или перетаскиванием.` : 'Файлы остаются на вашем компьютере.';",
         "root.querySelector('[data-cv-left-status]').textContent='';\n      root.querySelector('[data-cv-left-status]').hidden=true;")
    # Reordering by dragging remains available; the removed arrow function is unused.
    start=code.index('    function reorderFile(id, delta) {')
    end=code.index('    let typeFilter = null;',start)
    code=code[:start]+code[end:]

    # Catalog and warnings reflect the selected inputs, not the search result.
    start=code.index('    function renderFormats() {')
    end=code.index('    function renderOptions() {',start)
    part=code[start:end]
    part=part.replace("for (const fmt of visible) {", "for (const fmt of visible) {\n        const enabled=formatAvailable(fmt);")
    part=part.replace('fmt.enabled','enabled')
    part=part.replace("status==='processing' || status==='checking'", "status==='processing' || status==='checking' || saving")
    part=part.replace("button.addEventListener('click',()=>{", "button.addEventListener('click',()=>{if(!enabled || saving || status==='processing')return;")
    code=code[:start]+part+code[end:]
    start=code.index('    function updateAioIssues() {')
    end=code.index('    function renderSummary() {',start)
    part=code[start:end]
    part=part.replace('const all=(analysis?.formats||[]);','const all=(analysis?.formats||[]);\n      const inputs=selectedFiles();')
    part=part.replace('files.map(', 'inputs.map(').replace('if(files.length', 'if(inputs.length')
    part=part.replace('else if(files.length', 'else if(inputs.length').replace('for(const file of files)', 'for(const file of inputs)')
    code=code[:start]+part+code[end:]
    start=code.index('    function renderSummary() {')
    end=code.index('    function render() {',start)
    part=code[start:end]
    part=part.replace('updateAioIssues();','updateAioIssues();\n      const inputs=selectedFiles();')
    # Only counts/eligibility use selected files; add-files visibility uses the full list.
    pos=part.index("root.querySelector('[data-cv-add]').disabled")
    head,tail=part[:pos],part[pos:]
    head=re.sub(r'\bfiles\b','inputs',head)
    head=head.replace("else p.textContent = inputs.length ?", "else p.textContent = inputs.length ?")
    head=head.replace("'Добавьте один или несколько файлов.';", "(files.length?'Выберите файлы кружками в списке.':'Добавьте один или несколько файлов.');")
    head=head.replace("runBtn.disabled=!ready;", "runBtn.disabled=!ready || saving;")
    head=head.replace("status==='processing'||status==='checking';runBtn.textContent='Повторить'", "status==='processing'||status==='checking'||saving||!inputs.length;runBtn.textContent='Повторить'")
    head=head.replace("runBtn.hidden=false;runBtn.disabled=false;runBtn.textContent='Повторить конвертацию';", "runBtn.hidden=false;runBtn.disabled=saving||!inputs.length;runBtn.textContent='Повторить конвертацию';")
    code=code[:start]+head+tail+code[end:]

    start=code.index('    async function runConversion() {')
    end=code.index('    async function saveOutput() {',start)
    part=code[start:end]
    part=part.replace("if(status==='processing'||status==='checking')return;", "if(status==='processing'||status==='checking'||saving||!selectedFileIds.size)return;\n      const inputs=selectedFiles();")
    part=part.replace('!files.length','!inputs.length').replace('files.some(','inputs.some(')
    part=part.replace('const currentFiles=filePayload();', 'const currentFiles=selectedPayload();')
    code=code[:start]+part+code[end:]
    once("if(!currentResult || typeof adapter.saveResult!=='function')return;\n      saveBtn.disabled=true;",
         "if(!currentResult || saving || typeof adapter.saveResult!=='function')return;\n      saving=true;renderFileList();saveBtn.disabled=true;")
    once("} catch(error){if(!alive)return;if(error?.resetRetry){currentResult=null;retryResult=null;}status=currentResult?'ready':'error';statusMessage=String(error?.userMessage||'Не удалось сохранить файл. Вы можете повторить сохранение.');renderSummary();}",
         "} catch(error){if(!alive)return;if(error?.resetRetry){currentResult=null;retryResult=null;}status=currentResult?'ready':'error';statusMessage=String(error?.userMessage||'Не удалось сохранить файл. Вы можете повторить сохранение.');renderSummary();}\n"
         "      finally {saving=false;if(alive){renderFileList();renderSummary();}}")
    assert 'data-op="up"' not in code and 'data-op="down"' not in code
    assert 'Порядок можно менять' not in code
    once("p.textContent='Поиск не дал результатов. Очистите строку поиска.';",
         "p.textContent=filters?'Поиск не дал результатов. Очистите строку поиска.':'Типы файлов не выбраны.';")
    once("typeFilter=window.AIOTypeFilter.attach({root,getFiles:()=>files,onFilterChanged:renderFileList});",
         "typeFilter=window.AIOTypeFilter.attach({root,getFiles:()=>files,\n"
         "      onFilterChanged:()=>{filters='';filterInput.value='';renderFileList();},\n"
         "      getSelectedIds:()=>[...selectedFileIds],onSelectFiles:selectFileGroup,\n"
         "      isSelectionLocked:()=>status==='processing'||status==='checking'||saving});")
    once('<h2 class="aio-cv__pane-title">Выберите формат результата</h2>',
         '<div class="aio-cv__format-head"><h2 class="aio-cv__pane-title">Выберите формат результата</h2>'
         '<button type="button" class="aio-cv__small-btn aio-cv__select-all-formats" data-cv-select-all-formats '
         'title="Выбрать доступные форматы во всех категориях">Выбрать всё</button></div>')
    once("const formatsArea = root.querySelector('[data-cv-formats]');",
         "const formatsArea = root.querySelector('[data-cv-formats]');\n"
         "    const selectAllFormatsBtn=root.querySelector('[data-cv-select-all-formats]');")
    once('    function renderFormats() {',
         "    function selectAllAvailableFormats() {\n"
         "      if(status==='processing'||status==='checking'||saving)return;\n"
         "      const available=(analysis?.formats||[]).filter(formatAvailable);\n"
         "      if(!available.length)return;\n"
         "      if(available.length===selectedFormats.size&&available.every(f=>selectedFormats.has(f.id)))return;\n"
         "      selectedFormats.clear();available.forEach(f=>selectedFormats.add(f.id));\n"
         "      selectedFormat=available[0].id;formatValues={};currentResult=null;retryResult=null;\n"
         "      status='idle';statusMessage='';realProgress=null;adapter.resetProgress?.();render();\n"
         "      announce('Выбрано форматов: '+available.length);\n"
         "    }\n"
         "    function renderFormats() {")
    once('    function renderSummary() {\n      updateAioIssues();',
         "    function renderSummary() {\n"
         "      const available=(analysis?.formats||[]).filter(formatAvailable);\n"
         "      selectAllFormatsBtn.disabled=!available.length||status==='processing'||status==='checking'||saving;\n"
         "      selectAllFormatsBtn.setAttribute('aria-pressed',String(available.length>0&&available.every(f=>selectedFormats.has(f.id))));\n"
         "      updateAioIssues();")
    once("local(runBtn,'click',runConversion);local(saveBtn,'click',saveOutput);",
         "local(runBtn,'click',runConversion);local(saveBtn,'click',saveOutput);\n"
         "    local(selectAllFormatsBtn,'click',selectAllAvailableFormats);")
    # A mixed set may have different valid targets per input. Keep valid pairs
    # runnable; the existing adapter reports unsupported pairs independently.
    route_guard='inputs.some(x=>chosen.every(f=>f.compatibleFileIds.includes(x.id)))'
    assert code.count(route_guard)==2
    code=code.replace(route_guard,'inputs.some(x=>chosen.some(f=>f.compatibleFileIds.includes(x.id)))')
    # 00AIO integration review: maintain immutable in-flight selections and
    # mount the previously unreachable issues control outside the title row.
    # These edits are pinned by the final output digest below as well.
    once("const header=pane?.querySelector('.aio-cv__pane-head');",
         "const header=pane?.querySelector('.aio-cv__format-head');")
    once('pop.append(heading,list);slot.append(button);header.append(slot);',
         'pop.append(heading,list);slot.append(button);header.after(slot);')
    once("if (!incoming.length || status === 'processing') return;",
         "if (!incoming.length || status === 'processing' || status === 'checking' || saving) return;")
    once("if(status==='processing')return;",
         "if(status==='processing'||status==='checking'||saving)return;")
    once("item.addEventListener('dragstart', event=>{if(status==='processing'){",
         "item.addEventListener('dragstart', event=>{if(status==='processing'||status==='checking'||saving){")
    once("if (!objectDragId || objectDragId===id) return;",
         "if (!objectDragId || objectDragId===id || status==='checking' || status==='processing' || saving) return;")
    once("root.querySelector('[data-cv-add]').disabled = status==='processing';",
         "root.querySelector('[data-cv-add]').disabled = status==='processing'||status==='checking'||saving;")
    return code


EXPECTED_INPUT_SHA256 = '55cecf687124408b55152717298b8538ae63f9313124d5d584b8307a8a5f7146'
EXPECTED_OUTPUT_SHA256 = '57efa31587770d9564cbac43cc35f69a3ff81707267056b5225419223e79b155'
EXPECTED_INPUT_BYTES = 43935
EXPECTED_OUTPUT_BYTES = 47118

def apply_selection_design(raw: bytes) -> bytes:
    import hashlib
    if len(raw) != EXPECTED_INPUT_BYTES or hashlib.sha256(raw).hexdigest() != EXPECTED_INPUT_SHA256:
        raise ValueError('AIO design update: upstream runtime changed; review and rebase the UI patch')
    result = apply_selection(raw.decode('utf-8')).encode('utf-8')
    if len(result) != EXPECTED_OUTPUT_BYTES or hashlib.sha256(result).hexdigest() != EXPECTED_OUTPUT_SHA256:
        raise ValueError('AIO design update: unexpected patched UI output')
    return result
