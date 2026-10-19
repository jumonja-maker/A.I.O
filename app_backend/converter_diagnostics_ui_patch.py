"""Upgrade the existing issues view AFTER all pinned UI11 selection patches.

Preserve original payloads and their earlier transformation hashes. Unknown
upstream controller bytes are refused, requiring an explicit rebase/review.
"""
import hashlib

EXPECTED_INPUT_SHA256 = '57efa31587770d9564cbac43cc35f69a3ff81707267056b5225419223e79b155'
EXPECTED_INPUT_BYTES = 47118
EXPECTED_OUTPUT_SHA256 = '72455f5cf47cf7e14acad079251b5d4bbe2be30e2485999318d09df068c31f71'
EXPECTED_OUTPUT_BYTES = 39480


def _transform(raw: bytes) -> bytes:
    source = raw.decode('utf-8')
    start = '    let aioIssueView = null;'
    end = '    function renderSummary() {'
    if source.count(start) != 1 or source.count(end) != 1:
        raise ValueError('Missing existing converter issues lifecycle')
    left, right = source.index(start), source.index(end)
    if right <= left:
        raise ValueError('Unexpected converter issues lifecycle order')
    hook = '''    let aioIssueView = null;
    let aioFailedBatchReport = null;
    function updateAioIssues() {
      if(!aioIssueView && window.AIOConverterIssues?.attach) {
        aioIssueView=window.AIOConverterIssues.attach({root, adapter,
          onBusyChanged:()=>{if(alive)renderSummary();}});
      }
      aioIssueView?.sync({files:selectedPayload(), allFiles:files,
        formats:(analysis?.formats||[]), selectedFormats:[...selectedFormats],
        result:currentResult,retryResult,status,statusMessage,saving,
        failedBatchReport:aioFailedBatchReport});
    }
'''
    source = source[:left] + hook + source[right:]
    tail = "if(status==='error') statusEl.className='aio-cv__status aio-cv__error';"
    run = 'async function runConversion() {'
    if source.count(tail) != 1 or source.count(run) != 1:
        raise ValueError('Missing existing converter summary/action anchor')
    source = source.replace(tail, tail + '\n      if(aioIssueView?.busy)runBtn.disabled=true;', 1)
    source = source.replace(run, run + '\n      if(aioIssueView?.busy)return;', 1)
    begin = "closePopover();status='processing';realProgress=null;"
    failure = '} catch(error) {if(!alive)return;if(error?.resetRetry)'
    if source.count(begin) != 1 or source.count(failure) != 1:
        raise ValueError('Missing converter batch outcome anchor')
    source = source.replace(begin, 'aioFailedBatchReport=null;' + begin, 1)
    source = source.replace(failure, '} catch(error) {if(!alive)return;aioFailedBatchReport=error?.batchReport||null;if(error?.resetRetry)', 1)
    return source.encode('utf-8')


def apply_diagnostics_ui(raw: bytes) -> bytes:
    if len(raw) != EXPECTED_INPUT_BYTES or hashlib.sha256(raw).hexdigest() != EXPECTED_INPUT_SHA256:
        raise ValueError('Converter diagnostics: upstream UI changed; review and rebase the patch')
    result = _transform(raw)
    if len(result) != EXPECTED_OUTPUT_BYTES or hashlib.sha256(result).hexdigest() != EXPECTED_OUTPUT_SHA256:
        raise ValueError('Converter diagnostics: unexpected patched UI output')
    return result
