"""One-shot, SHA-locked source edit. Removed from the candidate commit."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import urllib.request

REPO = 'jumonja-maker/A.I.O'
BRANCH = 'fix/fulltest-critical-hosted-lo-20261029'
assert os.environ['GITHUB_REPOSITORY'] == REPO
assert os.environ['GITHUB_REF'] == 'refs/heads/' + BRANCH
EXPECTED = {
    'app_backend/converter_guard/native_fidelity.py': 'a030e9492d13c5094430aff6d363c07c76e46f51',
    'app_backend/converter_guard/uno_session.py': '66ccbfec4678b81d55e024e0f66746528b0b8ae0',
    'app_backend/converter_guard/adapter.py': '089c1f8394fe483d55825301f49f373a91093c33',
    'tools/qa/fulltest_batch1_native.py': '01480f491088ab05623ec49fc576373145d62988',
}
texts = {}
for name, sha in EXPECTED.items():
    data = Path(name).read_bytes()
    observed = hashlib.sha1(('blob %d\0' % len(data)).encode() + data).hexdigest()
    if observed != sha:
        raise RuntimeError('Source changed: ' + name)
    texts[name] = data.decode('utf-8')

def change(name, before, after):
    if texts[name].count(before) != 1:
        raise RuntimeError('Non-unique edit in ' + name + ': ' + before[:100])
    texts[name] = texts[name].replace(before, after, 1)

native = 'app_backend/converter_guard/native_fidelity.py'
change(native, "NATIVE_FIDELITY_SOURCE = r'''", "from .preservation_contract import PRESERVATION_POLICY_SOURCE\n\nNATIVE_FIDELITY_SOURCE = PRESERVATION_POLICY_SOURCE + r'''")
helper = '''def aio_native_preservation_plan(model, services, target):
    """Read one count only for a contracted export from the already-open model."""
    scope = aio_preservation_scope(services, target)
    if scope is None:
        return None
    try:
        if scope[1] == 'sheets':
            count = model.getSheets().getCount()
        elif scope[2] == 'loaded_writer_layout':
            count = model.getCurrentController().PageCount
        else:
            count = model.getDrawPages().getCount()
    except Exception as error:
        raise AioPreservationIncomplete('native_unit_count_unavailable') from error
    return aio_plan_preservation(services, target, count)


'''
change(native, 'def aio_normal_text(value):', helper + 'def aio_normal_text(value):')
change(native, 'def aio_export_writer_pages(model, prop, target, export_options, filter_name):',
    'def aio_export_writer_pages(model, prop, target, export_options, filter_name, preservation=None):')
change(native, '        count = controller.PageCount\n',
    '        count = preservation.expected if preservation is not None else controller.PageCount\n')
change(native, 'def aio_export_graphic_pages(model, remote, prop, target):',
    'def aio_export_graphic_pages(model, remote, prop, target, preservation=None):')
change(native, "    count = pages.getCount()\n    mime =", "    count = preservation.expected if preservation is not None else pages.getCount()\n    mime =")

worker = 'app_backend/converter_guard/uno_session.py'
change(worker,
    'def export_table_sheets(model, desktop, prop, target, export_options, *, csv_format=None, raw_values=False):',
    'def export_table_sheets(model, desktop, prop, target, export_options, *, csv_format=None, raw_values=False, preservation=None):')
change(worker, '    sheets = model.getSheets()\n    count = sheets.getCount()\n',
    '    sheets = model.getSheets()\n    count = preservation.expected if preservation is not None else sheets.getCount()\n')
change(worker, '                    export_tick = time.monotonic()\n                    exported_paths = [target]',
    "                    export_tick = time.monotonic()\n                    preservation_plan = aio_native_preservation_plan(model, services, req['format'])\n                    exported_paths = [target]")
change(worker, 'graphic_pages = (aio_export_graphic_pages(model, remote, prop, target)',
    'graphic_pages = (aio_export_graphic_pages(model, remote, prop, target, preservation_plan)')
change(worker, "model, prop, target, export_options, req['exportFilter'])",
    "model, prop, target, export_options, req['exportFilter'], preservation_plan)")
change(worker, "                                raw_values=req.get('automaticRecipe') == 'csv_preserve_values')",
    "                                raw_values=req.get('automaticRecipe') == 'csv_preserve_values',\n                                preservation=preservation_plan)")
change(worker, "                    # Result paths stay on disk. Thousands of HTML sidecars",
    "                    preservation_check = (aio_verify_preservation(\n                        preservation_plan, [p.name for p in exported_paths])\n                        if present and not export_handler.blocked and not handler.blocked else None)\n                    # Result paths stay on disk. Thousands of HTML sidecars")
change(worker, "                    warnings = []\n                    if writer_pages is not None:",
    "                    warnings = []\n                    if preservation_check is not None:\n                        result['preservationContract'] = preservation_check\n                    elif (result['ok'] and 'com.sun.star.sheet.SpreadsheetDocument' in services\n                            and req['format'] in _AIO_PAGE_TARGETS):\n                        # This block does not implement Calc print pagination.\n                        # Do not present a successful image encoder as verified coverage.\n                        result['preservationContract'] = {\n                            'check': 'native_preservation_contract', 'status': 'not_checked',\n                            'reason': 'calc_graphic_page_scope_not_implemented',\n                            'scope': 'loaded_calc_print_layout', 'qualityCompared': False}\n                        warnings.append('NATIVE_OUTPUT_NOT_CHECKED')\n                    if writer_pages is not None:")
change(worker, 'isinstance(error, (SheetOutputMismatch, AioPresentationTextLoss))',
    'isinstance(error, (SheetOutputMismatch, AioPresentationTextLoss, AioPreservationLoss))')
change(worker, 'isinstance(error, (AioPresentationUnverified, AioWriterPagesUnverified))',
    'isinstance(error, (AioPresentationUnverified, AioWriterPagesUnverified, AioPreservationIncomplete))')

adapter = 'app_backend/converter_guard/adapter.py'
change(adapter, "                                                     'NATIVE_WRITER_RASTER_PAGES'))",
    "                                                     'NATIVE_WRITER_RASTER_PAGES',\n                                                     'NATIVE_OUTPUT_NOT_CHECKED'))")
change(adapter, "                        if result.get('nativeCloseInterrupted'):",
    "                        receipt = result.get('preservationContract')\n                        if result.get('ok') and isinstance(receipt, dict):\n                            self._local.export_info['resultChecks'] = list(\n                                self._local.export_info.get('resultChecks', ())) + [receipt]\n                        if result.get('nativeCloseInterrupted'):")
change(adapter, "            prepare_profile(workspace / 'profile')\n            self._local.execution_mode = 'native_cli'",
    "            # A CLI fallback has no loaded-model receipt. Keep its existing\n            # conversion route, but never label it as a checked native contract.\n            from .preservation_contract import aio_preservation_scope\n            scope = aio_preservation_scope(proof.get('documentServices', ()), target)\n            if scope is not None:\n                self._local.export_info['resultChecks'] = list(\n                    self._local.export_info.get('resultChecks', ())) + [{\n                    'check': 'native_preservation_contract', 'status': 'not_checked',\n                    'policyId': scope[0], 'scope': scope[2],\n                    'reason': 'native_cli_has_no_model_receipt', 'qualityCompared': False}]\n                self._local.export_info['warnings'] = tuple(dict.fromkeys((\n                    *self._local.export_info.get('warnings', ()), 'NATIVE_OUTPUT_NOT_CHECKED')))\n            prepare_profile(workspace / 'profile')\n            self._local.execution_mode = 'native_cli'")

qa = 'tools/qa/fulltest_batch1_native.py'
change(qa, '    results=[];counter=0\n', '    results=[];counter=0\n    contract_receipts=[]\n')
change(qa, '    def proof(path):', '''    def require_contract(unit, expected):
        checks = [item for item in adapter.export_info().get('resultChecks', ())
                  if item.get('check') == 'native_preservation_contract']
        assert len(checks) == 1, checks
        check = checks[0]
        assert check['status'] == 'checked_subset' and check['unit'] == unit, check
        assert check['expectedUnits'] == check['observedOutputs'] == expected, check
        assert check['qualityCompared'] is False, check
        contract_receipts.append(check)

    def proof(path):''')
change(qa, '                files=convert(writer,target,wp)\n',
    "                files=convert(writer,target,wp)\n                require_contract('pages', 3)\n")
change(qa, "                assert len(values)==4,(target,len(values))\n",
    "                require_contract('sheets', 4)\n                assert len(values)==4,(target,len(values))\n")
change(qa, "            files=convert(source,'dbf',bk)\n",
    "            files=convert(source,'dbf',bk)\n            require_contract('sheets', 4)\n")
change(qa, "                assert len(pages)==3,(target,len(pages))\n",
    "                require_contract('pages', 3)\n                assert len(pages)==3,(target,len(pages))\n")
change(qa, '        assert len(results)==16,results\n',
    '        assert len(results)==16,results\n        assert len(contract_receipts)==7,contract_receipts\n')
change(qa, "                'cases':results,'sourceUnchanged':True,'csvDecimalAcceptance':csv_report}",
    "                'cases':results,'sourceUnchanged':True,'csvDecimalAcceptance':csv_report,\n                'preservationContracts':contract_receipts}")

for name, text in texts.items():
    compile(text, name, 'exec')
    Path(name).write_text(text, encoding='utf-8', newline='\n')
subprocess.run(['python', '-m', 'unittest', 'discover', '-s', 'tests',
                '-p', 'test_preservation_contract.py', '-v'], check=True)
import ast
context = {}
exec(Path('app_backend/converter_guard/preservation_contract.py').read_text(), context)
for node in ast.parse(texts[native]).body:
    if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id=='NATIVE_FIDELITY_SOURCE' for t in node.targets):
        context['NATIVE_FIDELITY_SOURCE'] = eval(compile(ast.Expression(node.value), native, 'eval'), context)
compile(context['NATIVE_FIDELITY_SOURCE'], '<native fidelity>', 'exec')
for node in ast.parse(texts[worker]).body:
    if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id=='WORKER_SOURCE' for t in node.targets):
        compile(context['NATIVE_FIDELITY_SOURCE'] + ast.literal_eval(node.value.right), '<native worker>', 'exec')

# Candidate only. The authenticated connector performs the lease-checked branch
# update after inspecting this run; this job never changes a ref or MAIN.
def api(path, payload):
    request = urllib.request.Request('https://api.github.com/repos/' + REPO + '/' + path,
        data=json.dumps(payload).encode(), method='POST', headers={
            'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
            'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json',
            'X-GitHub-Api-Version': '2022-11-28'})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)
parent = os.environ['GITHUB_SHA']
base = subprocess.check_output(['git', 'rev-parse', 'HEAD^{tree}'], text=True).strip()
entries = [{'path': name, 'mode': '100644', 'type': 'blob', 'content': text}
           for name, text in texts.items()]
entries += [{'path': path, 'mode': '100644', 'type': 'blob', 'sha': None} for path in (
    'tools/qa/_apply_preservation_contract_once.py', '.github/workflows/aio-contract-apply-once.yml')]
tree = api('git/trees', {'base_tree': base, 'tree': entries})['sha']
commit = api('git/commits', {'message': 'feat(guard): enforce lightweight native page and sheet preservation contracts',
                            'tree': tree, 'parents': [parent]})['sha']
print('AIO_CANDIDATE_COMMIT=' + commit)
print('AIO_CANDIDATE_PARENT=' + parent)
print('AIO_REFS_UNCHANGED=true')
