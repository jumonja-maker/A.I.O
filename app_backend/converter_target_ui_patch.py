"""Exact, fail-closed interaction patch after the existing designer patches."""
from __future__ import annotations


def apply_target_recovery(raw: bytes) -> bytes:
    source = raw.decode('utf-8')

    def replace(old, new):
        nonlocal source
        if source.count(old) != 1:
            raise ValueError('Unexpected target recovery UI anchor: ' + old[:70])
        source = source.replace(old, new, 1)

    replace('let replaceId = null;', 'let replaceId = null;\n    let retryResult = null;')
    # Every existing selection reset also invalidates the retry handle.
    import re
    source, count = re.subn(r'currentResult\s*=\s*null;',
                           'currentResult=null;retryResult=null;', source)
    if count != 2:
        raise ValueError('Unexpected target recovery selection resets: ' + str(count))
    replace("      root.querySelector('[data-cv-add]').disabled = status==='processing';",
        "      const retryReady=retryResult?.sessionToken && retryResult?.targetReport?.failed>0;\n"
        "      if(retryReady){runBtn.hidden=false;runBtn.disabled=status==='processing'||status==='checking';"
        "runBtn.textContent='Повторить';}\n"
        "      else if(status==='saved' && currentResult){runBtn.hidden=false;"
        "runBtn.disabled=false;runBtn.textContent='Повторить конвертацию';}\n"
        "      root.querySelector('[data-cv-add]').disabled = status==='processing';")
    replace('async function runConversion() {',
        "async function runConversion() {\n"
        "      if(status==='processing'||status==='checking')return;\n"
        "      const retrying=Boolean(retryResult?.sessionToken && retryResult?.targetReport?.failed>0);\n"
        "      if(!retrying){")
    replace("      closePopover();status='processing';realProgress=null;",
        "      }\n      closePopover();status='processing';realProgress=null;")
    # chosen/fmt were originally scoped to the checks above.
    replace('      const output=chosen.length>1?',
        "      const chosen=(analysis?.formats||[]).filter(f=>selectedFormats.has(f.id)&&f.enabled);\n"
        "      const fmt=chosen.length===1?chosen[0]:null;\n"
        "      const output=retrying?await adapter.retryFailed({result:retryResult,files:currentFiles,onProgress}):chosen.length>1?")
    replace('currentResult=output.result;',
        'currentResult=output.result;retryResult=output.result?.targetReport?.failed?output.result:null;')
    replace("} catch(error) {if(!alive)return;status='error';",
        "} catch(error) {if(!alive)return;if(error?.resetRetry){currentResult=null;retryResult=null;}if(error?.retryResult)retryResult=error.retryResult;"
        "status=currentResult?'ready':'error';")
    replace("} catch(error){if(!alive)return;status='ready';",
        "} catch(error){if(!alive)return;if(error?.resetRetry){currentResult=null;retryResult=null;}"
        "status=currentResult?'ready':'error';")
    # Both main and the per-file QA patch are valid upstreams.
    original = "statusEl.textContent='Преобразование завершено. Файлы ещё не сохранены.';"
    if original in source:
        replace(original, "statusEl.textContent=statusMessage||'Преобразование завершено. Файлы ещё не сохранены.';")
    original = "statusMessage='';render();\n      } catch(error)"
    if original in source:
        replace(original, "statusMessage=typeof output.message==='string'?output.message:'';render();\n      } catch(error)")
    replace("      if(status==='error') statusEl.className='aio-cv__status aio-cv__error';",
        "      if(status==='saved' && currentResult?.targetReport?.failed)statusEl.textContent="
        "'Успешные результаты сохранены. Ошибок: '+currentResult.targetReport.failed+'.';\n"
        "      if(status==='error') statusEl.className='aio-cv__status aio-cv__error';")
    return source.encode('utf-8')
