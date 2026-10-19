"""Scoped interaction upgrade on top of SHA-verified designer 11 addon v1.4.

The original design asset is kept byte-for-byte and checked independently.
Only explicit event/state handlers are modified. Never patch anonymous JS:
every exact replacement is fail-closed, with unit and browser tests.
"""
from __future__ import annotations


def apply_multiselect(raw: bytes) -> bytes:
    source = raw.decode('utf-8')

    def replace_once(old: str, new: str):
        nonlocal source
        count = source.count(old)
        if count != 1:
            raise ValueError('Unexpected designer add-on structure for multi-output patch '
                             + str(count) + ': ' + old[:65])
        source = source.replace(old, new, 1)

    replace_once(
        'let replaceId = null;',
        'let replaceId = null;\n'
        '    // Selection persists when switching Documents/Images/Tables tabs.\n'
        '    const selectedFormats = new Set();')

    replace_once(
        'closePopover(); selectedFormat = null; formatValues = {}; currentResult = null;',
        'closePopover(); selectedFormat = null; selectedFormats.clear(); '
        'formatValues = {}; currentResult = null;')

    replace_once(
        "wrapper.setAttribute('aria-selected',String(selectedFormat===fmt.id));",
        "wrapper.setAttribute('aria-selected',String(selectedFormats.has(fmt.id)));")

    replace_once(
        "button.setAttribute('aria-label',fmt.enabled?",
        "button.setAttribute('aria-pressed',String(selectedFormats.has(fmt.id)));\n"
        "        button.setAttribute('aria-label',fmt.enabled?")

    replace_once(
        "button.addEventListener('click',()=>{selectedFormat=fmt.id;formatValues={};currentResult=null;status='idle';statusMessage='';renderFormats();renderOptions();renderSummary();});",
        "button.addEventListener('click',()=>{"
        "if(selectedFormats.has(fmt.id))selectedFormats.delete(fmt.id);"
        "else selectedFormats.add(fmt.id);"
        "selectedFormat=selectedFormats.has(fmt.id)?fmt.id:(selectedFormats.values().next().value||null);"
        "formatValues={};currentResult=null;status='idle';statusMessage='';"
        "renderFormats();renderOptions();renderSummary();});")

    # Category tabs are navigation, not edits to selected formats or output.
    # Preserve the completed Blob until the user saves it or deliberately
    # begins another conversion; this is critical for expensive 100-page jobs.
    replace_once(
        "selectedCategory=category;selectedFormat=null;formatValues={};currentResult=null;status='idle';statusMessage='';closePopover();",
        "selectedCategory=category;closePopover();")

    replace_once(
        "optionsArea.replaceChildren();\n      const current=analysis?.formats.find(f=>f.id===selectedFormat);",
        "optionsArea.replaceChildren();\n"
        "      optionsArea.hidden=true;\n"
        "      if(selectedFormats.size>1)return;\n"
        "      const current=analysis?.formats.find(f=>f.id===selectedFormat);")

    replace_once(
        "function renderSummary() {\n      const fmt=analysis?.formats.find(f=>f.id===selectedFormat);",
        "function formatWord(n){const h=n%100;if(h>=11&&h<=14)return 'форматов';"
        "const k=n%10;return k===1?'формат':(k>=2&&k<=4?'формата':'форматов');}\n"
        "    function renderSummary() {\n"
        "      const chosen=(analysis?.formats||[]).filter(f=>selectedFormats.has(f.id)&&f.enabled);\n"
        "      const fmt=chosen.length===1?chosen[0]:null;")

    replace_once(
        "if (fmt) {\n        const eligible=fmt.compatibleFileIds",
        "if(chosen.length>1){\n"
        "        const labels=[...selectedFormats].map(id=>(analysis?.formats||[]).find(f=>f.id===id)?.label).filter(Boolean);\n"
        "        const summaryText=labels.slice(0,4).join(', ')+(labels.length>4?' и ещё '+(labels.length-4):'');\n"
        "        p.textContent=files.length+' из '+files.length+' файлов · '+"
        "labels.length+' '+formatWord(labels.length)+': '+summaryText;\n"
        "        const allEligible=chosen.every(f=>files.every(x=>f.compatibleFileIds.includes(x.id)));\n"
        "        if(!allEligible){const warn=document.createElement('div');"
        "warn.className='aio-cv__warning';"
        "warn.textContent='Не все исходные файлы поддерживают каждый выбранный формат.';"
        "summary.append(warn);}\n"
        "      }else if (fmt) {\n        const eligible=fmt.compatibleFileIds")

    replace_once(
        "const ready=Boolean(fmt?.enabled && fmt.compatibleFileIds.some(id=>files.some(f=>f.id===id)) && typeof adapter.convert==='function' && status!=='processing' && status!=='checking');",
        "const ready=chosen.length>1?\n"
        "        Boolean(typeof adapter.convertMany==='function' && files.length &&\n"
        "          chosen.every(f=>files.every(x=>f.compatibleFileIds.includes(x.id))) &&\n"
        "          status!=='processing' && status!=='checking'):\n"
        "        Boolean(fmt?.enabled && fmt.compatibleFileIds.some(id=>files.some(f=>f.id===id)) &&\n"
        "          typeof adapter.convert==='function' && status!=='processing' && status!=='checking');")

    # A single selected output must apply to EVERY input file. Previously
    # the Designer single-output path silently filtered incompatible inputs.
    replace_once(
        "Boolean(fmt?.enabled && fmt.compatibleFileIds.some(id=>files.some(f=>f.id===id)) &&",
        "Boolean(fmt?.enabled && files.length && "
        "files.every(f=>fmt.compatibleFileIds.includes(f.id)) &&")

    replace_once(
        "runBtn.disabled=!ready;runBtn.hidden=Boolean(currentResult && status!=='error');",
        "runBtn.disabled=!ready;runBtn.hidden=Boolean(currentResult && status!=='error');\n"
        "      runBtn.textContent=chosen.length>1?'Конвертировать в '+chosen.length+' '+formatWord(chosen.length):'Конвертировать';")

    replace_once(
        "async function runConversion() {\n      const fmt=analysis?.formats.find(f=>f.id===selectedFormat);\n      if (!fmt?.enabled || !fmt.compatibleFileIds.length || typeof adapter.convert!=='function') return;",
        "async function runConversion() {\n"
        "      const chosen=(analysis?.formats||[]).filter(f=>selectedFormats.has(f.id)&&f.enabled);\n"
        "      const fmt=chosen.length===1?chosen[0]:null;\n"
        "      if(!chosen.length)return;\n"
        "      if(chosen.length>1 && (!files.length || typeof adapter.convertMany!=='function' ||\n"
        "          !chosen.every(f=>files.every(x=>f.compatibleFileIds.includes(x.id)))))return;\n"
        "      if(chosen.length===1 && (!files.length || !files.every(f=>fmt.compatibleFileIds.includes(f.id)) || typeof adapter.convert!=='function'))return;")

    replace_once(
        "const currentFiles=filePayload().filter(f=>fmt.compatibleFileIds.includes(f.id));",
        "const currentFiles=filePayload();")

    replace_once(
        "const output=await adapter.convert({files:currentFiles,formatId:fmt.id,options:{...formatValues},onProgress:(value)=>{",
        "const onProgress=(value)=>{")

    replace_once(
        "          renderSummary();\n        }});\n        if(!alive)return;",
        "          renderSummary();\n        };\n"
        "        const output=chosen.length>1?\n"
        "          await adapter.convertMany({files:currentFiles,formatIds:[...selectedFormats],onProgress}):\n"
        "          await adapter.convert({files:currentFiles,formatId:fmt.id,"
        "options:{...formatValues},onProgress});\n"
        "        if(!alive)return;")

    # Owner selected continuation after a refused source (2026-10-16).
    # Submit every file so none disappears silently; the backend reports
    # each failure. At least one source must support all chosen outputs.
    if source.count('chosen.every(f=>files.every(x=>f.compatibleFileIds.includes(x.id)))') != 3:
        raise ValueError('Unexpected multi-format continuation anchors')
    source = source.replace(
        'chosen.every(f=>files.every(x=>f.compatibleFileIds.includes(x.id)))',
        'files.some(x=>chosen.every(f=>f.compatibleFileIds.includes(x.id)))')
    replace_once('const allEligible=files.some(x=>chosen.every(f=>f.compatibleFileIds.includes(x.id)));',
                 'const allEligible=chosen.every(f=>files.every(x=>f.compatibleFileIds.includes(x.id)));')
    if source.count('files.every(f=>fmt.compatibleFileIds.includes(f.id))') != 2:
        raise ValueError('Unexpected single-format continuation anchors')
    source = source.replace('files.every(f=>fmt.compatibleFileIds.includes(f.id))',
                            'files.some(f=>fmt.compatibleFileIds.includes(f.id))')
    replace_once("currentResult=output.result;status='ready';statusMessage='';render();",
                 "currentResult=output.result;status='ready';"
                 "statusMessage=typeof output.message==='string'?output.message:'';render();")
    replace_once("statusEl.textContent='Преобразование завершено. Файлы ещё не сохранены.';",
                 "statusEl.textContent=statusMessage||'Преобразование завершено. Файлы ещё не сохранены.';")
    replace_once("statusEl.textContent='Файлы сохранены.';",
                 "statusEl.textContent=currentResult?.batchReport?.failed?"
                 "'Успешные результаты сохранены. Ошибок: '+currentResult.batchReport.failed+'.':'Файлы сохранены.';")
    return source.encode('utf-8')
