"""Last UI-only corrections on the preserved, verified Designer controller."""
from __future__ import annotations

import hashlib

EXPECTED_INPUT_SHA256 = '72455f5cf47cf7e14acad079251b5d4bbe2be30e2485999318d09df068c31f71'


def apply_native_ui(raw: bytes) -> bytes:
    if hashlib.sha256(raw).hexdigest() != EXPECTED_INPUT_SHA256:
        raise ValueError('Native UI patch requires review after upstream controller changes')
    source = raw.decode('utf-8')

    def once(old, new):
        nonlocal source
        if source.count(old) != 1:
            raise ValueError('Native UI patch: unexpected anchor: ' + old[:80])
        source = source.replace(old, new, 1)

    once('  const MAX_FILES = 100;\n', '')
    once('    enabled: value?.enabled === true,',
         "    enabled: value?.enabled === true || value?.nativeAttemptAvailable === true,\n"
         "    nativeDecision:['native_decision','native_registry_hint','native_preflight'].includes(value?.compatibility),\n"
         "    compatibility:value?.compatibility,\n"
         "    selectableFileIds:Array.isArray(value?.selectableFileIds)?value.selectableFileIds.map(String):"
         "(Array.isArray(value?.compatibleFileIds)?value.compatibleFileIds.map(String):[]),\n"
         "    incompatibleFileIds:Array.isArray(value?.incompatibleFileIds)?value.incompatibleFileIds.map(String):[],\n"
         "    undeterminedFileIds:Array.isArray(value?.undeterminedFileIds)?value.undeterminedFileIds.map(String):[],")
    once('    options: Array.isArray(value?.options) ? value.options : [],',
         '    options: Array.isArray(value?.options) ? value.options : [],\n'
         '    verifiedFileIds:Array.isArray(value?.verifiedFileIds)?value.verifiedFileIds.map(String):'
         '(Array.isArray(value?.compatibleFileIds)?value.compatibleFileIds.map(String):[]),')
    once('＋ Добавить файлы', '+ Добавить файлы')
    once('Выберите формат результата</h2>', 'Формат результата</h2>')
    once("const ext = (name) => (String(name).split('.').pop() || '').toUpperCase().slice(0, 7);",
         "const ext = (name) => (String(name).split('.').pop() || '').toUpperCase().slice(0, 4);")
    once("closePopover(); selectedFormat = null; selectedFormats.clear(); formatValues = {}; currentResult=null;retryResult=null;",
         "closePopover(); formatValues = {}; currentResult=null;retryResult=null;")
    once("if (first) selectedCategory = first.category;",
         "for(const id of selectedFormats){if(!analysis.formats.some(f=>f.id===id&&f.enabled))selectedFormats.delete(id);}\n"
         "        selectedFormat=selectedFormats.values().next().value||null;\n"
         "        if(first&&!analysis.formats.some(f=>f.category===selectedCategory))selectedCategory=first.category;")
    once("p.textContent=inputs.length+' из '+inputs.length+' файлов · '+labels.length+' '+formatWord(labels.length)+': '+summaryText;",
         "p.textContent=inputs.length+' файлов · '+labels.length+' '+formatWord(labels.length)+': '+summaryText;")
    once('p.textContent=\x60\x24{eligible.length} из \x24{inputs.length} файлов · результат: \x24{fmt.label}\x60;',
         'p.textContent=fmt.nativeDecision?\x60\x24{inputs.length} файлов · результат: \x24{fmt.label}\x60:\x60\x24{eligible.length} из \x24{inputs.length} файлов · результат: \x24{fmt.label}\x60;')

    once("button.role='tab';", "button.setAttribute('role','tab');")
    once("""        const available = Math.max(0, MAX_FILES - files.length);
        const accepted = additions.slice(0, available);
        files.push(...accepted.map(file => {const id=\x60f\x24{++nextFileId}\x60;selectedFileIds.add(id);return {id,file};}));
        if (additions.length > available) announce(\x60Можно добавить не более \x24{MAX_FILES} файлов. Остальные не добавлены.\x60);""",
         """        for(const file of additions){const id=\x60f\x24{++nextFileId}\x60;selectedFileIds.add(id);files.push({id,file});}""")
    once("root.querySelector('[data-cv-count]').textContent = \x60(\x24{files.length}/\x24{MAX_FILES})\x60;",
         "root.querySelector('[data-cv-count]').textContent = \x60(\x24{files.length})\x60;")
    once('filterInput.hidden = files.length < 2;', 'filterInput.hidden = false;')
    once('filterInput.parentElement.hidden = files.length < 2;', 'filterInput.parentElement.hidden = false;')
    once("if (!files.length) { analysis=null; status='idle'; statusMessage=''; render(); return; }", '')
    once("status = 'idle'; statusMessage='';\n        const first = analysis.formats.find(f => f.enabled);",
         "status = 'idle'; statusMessage=analysis.formats.some(f=>f.enabled)?'':analysis.message;\n        const first = analysis.formats.find(f => f.enabled);")
    once("const cats=[...new Set(all.filter(f=>f.category!=='other').map(f=>f.category))];",
         "const present=new Set(all.map(f=>f.category));\n"
         "      const cats=Object.keys(CATEGORY_LABELS).filter(category=>present.has(category));\n"
         "      categories.style.setProperty('--aio-cv-category-count',String(Math.max(1,cats.length)));")
    once("const previousScroll = fileArea.querySelector('.aio-cv__file-list')?.scrollTop || 0;",
         'const previousScroll = fileArea.scrollTop;')
    once('fileArea.append(list);list.scrollTop=previousScroll;',
         'fileArea.append(list);fileArea.scrollTop=previousScroll;')
    once('        list.append(item);',
         """        item.tabIndex=0;item.setAttribute('aria-label',file.name);
        item.addEventListener('click',event=>{if(!event.target.closest('button,a,input'))toggleFile(id);});
        item.addEventListener('keydown',event=>{if(event.target===item && (event.key===' '||event.key==='Enter')){event.preventDefault();toggleFile(id);}});
        list.append(item);""")
    once("image.style.cssText='width:100%;height:100%;object-fit:cover';",
         "image.loading='lazy';image.decoding='async';image.style.cssText='width:100%;height:100%;object-fit:cover';")
    once("      if(aioIssueView?.busy)runBtn.disabled=true;", '')
    once("      if(aioIssueView?.busy)return;", '')
    once("      if(status==='processing'||status==='checking'||saving||!selectedFileIds.size)return;",
         "      if(status==='processing' && typeof adapter.cancel==='function'){try{await adapter.cancel();}catch{statusMessage='Не удалось отменить задание. Попробуйте ещё раз.';renderSummary();}return;}\n"
         "      if(status==='processing'||status==='checking'||saving||!selectedFileIds.size)return;")
    once("      if(status==='error') statusEl.className='aio-cv__status aio-cv__error';",
         "      if(status==='error') statusEl.className='aio-cv__status aio-cv__error';\n"
         "      statusEl.title=statusMessage||statusEl.textContent;\n"
         "      if(status==='processing' && typeof adapter.cancel==='function'){runBtn.hidden=false;runBtn.disabled=false;runBtn.textContent='Отменить';}")
    once('    async function inspect() {',
         '    async function inspect() {\n      adapter.resetProgress?.();')
    once('Определяем форматы и доступные действия…', 'Проверяем направления до конвертации…')
    once('alive=false;inspectionToken++;closePopover();',
         'alive=false;inspectionToken++;adapter.resetProgress?.();closePopover();')
    once("formatValues={};currentResult=null;retryResult=null;status='idle';statusMessage='';renderFormats();renderOptions();renderSummary();",
         "adapter.resetProgress?.();formatValues={};currentResult=null;retryResult=null;status='idle';statusMessage='';renderFormats();renderOptions();renderSummary();")
    once('window.AIOConverterAddon = Object.freeze({register, mount, MAX_FILES});',
         'window.AIOConverterAddon = Object.freeze({register, mount});')
    once('    typeFilter.sync();\n    render();', '    typeFilter.sync();\n    render();\n    inspect();')
    # Eligibility is supplied by Guard, separately from filename route hints.
    # Retain the hint arrays for diagnostics; never use them to discard input.
    for name, count in (('fmt.compatibleFileIds', 4), ('f.compatibleFileIds', 3)):
        if source.count(name) != count:
            raise ValueError('Native selection eligibility requires controller review: ' + name)
        source = source.replace(name, name.replace('compatibleFileIds', 'selectableFileIds'))
    once('    const selectedFormats = new Set();',
         '    const selectedFormats = new Set();\n    let onlyVerified = false;')
    once('    function selectAllAvailableFormats() {',
         '    function selectAllAvailableFormats() {\n      onlyVerified = true;')
    anchor = 'const available=(analysis?.formats||[]).filter(formatAvailable);'
    if source.count(anchor) != 2:
        raise ValueError('Preflight Select All requires controller review')
    source = source.replace(anchor, "const available=(analysis?.formats||[]).filter(f=>f.enabled&&selectedFiles().some(x=>f.verifiedFileIds.includes(x.id)));")
    once("button.addEventListener('click',()=>{if(!enabled || saving || status==='processing')return;",
         "button.addEventListener('click',()=>{if(!enabled || saving || status==='processing')return;onlyVerified=false;")
    once('formatIds:[...selectedFormats],onProgress}', 'formatIds:[...selectedFormats],onProgress,onlyVerified}')
    once('formatId:fmt.id,options:{...formatValues},onProgress}',
         'formatId:fmt.id,options:{...formatValues},onProgress,onlyVerified}')
    once("statusMessage=analysis.formats.some(f=>f.enabled)?'':analysis.message;", 'statusMessage=analysis.message;')
    once('alive=false;inspectionToken++;adapter.resetProgress?.();closePopover();',
         "alive=false;inspectionToken++;if(adapter.dispose)adapter.dispose();else adapter.resetProgress?.();closePopover();")
    once('    async function runConversion() {',
         "    async function runConversion() {\n"
         "      if(status==='checking'&&adapter.cancelInspection){await adapter.cancelInspection();return;}\n"
         "      if(status==='error'&&!analysis&&files.length){await inspect();return;}")
    once("      if(status==='processing' && typeof adapter.cancel==='function'){runBtn.hidden=false;runBtn.disabled=false;runBtn.textContent='Отменить';}",
         "      if(status==='processing' && typeof adapter.cancel==='function'){runBtn.hidden=false;runBtn.disabled=false;runBtn.textContent='Отменить';}\n"
         "      if(status==='checking'&&adapter.cancelInspection){runBtn.hidden=false;runBtn.disabled=false;runBtn.textContent='Отменить проверку';}\n"
         "      if(status==='error'&&!analysis&&files.length){runBtn.hidden=false;runBtn.disabled=false;runBtn.textContent='Повторить проверку';}")
    return source.encode('utf-8')
