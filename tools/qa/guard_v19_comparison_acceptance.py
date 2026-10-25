"""Real loopback/Guard/LibreOffice before-after and selective auto-retry acceptance.

Uses a generated, single-sheet XLSX and a deliberately configured first
LibreOffice export with SaveAsShown=true. This controlled degraded setting
reproduces a repairable precision difference even when a release already
exports full precision by default. No user document, global LO profile
or application installation is changed. This is targeted evidence, not a full
format matrix, Windows GUI check or universal quality certification.
"""
import argparse
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--executable', type=Path, required=True)
    parser.add_argument('--seconds', type=float, default=180)
    args = parser.parse_args(argv)
    if not args.executable.is_file() or args.seconds <= 0:
        parser.error('Existing LibreOffice executable and positive seconds required')
    args.output.mkdir(parents=True, exist_ok=False)
    from openpyxl import Workbook
    source = args.output / 'precision.xlsx'
    book = Workbook()
    sheet = book.active
    sheet.title = 'Precision'
    sheet.append(['code', 'amount', 'note'])
    sheet.append(['00123', 1.23456789012345, 'Привет'])
    sheet['B2'].number_format = '0.00'
    book.save(source)
    book.close()
    source_bytes = source.read_bytes()
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    os.environ['AIO_LIBREOFFICE'] = str(args.executable.resolve())
    from app_backend.server import start_server
    from app_backend.converter_guard.adapter import LibreOfficeAdapter
    from app_backend.converter_guard import adapter as adapter_module
    from app_backend.converter_guard.export_recipe import ExportRecipe, CALC_TEXT
    from unittest.mock import patch
    # Only the initial native export is deliberately configured differently.
    # Retrying uses the unmodified production automatic-recipe implementation.
    class ControlledInitialAdapter(LibreOfficeAdapter):
        def _convert_native(self, source, target, workspace, **kwargs):
            if target == 'csv' and getattr(self._local, 'retry_recipe', None) is None:
                original = adapter_module.native_export_recipe
                def shown(native, target, proof):
                    recipe = original(native, target, proof)
                    if recipe and recipe.filter_name == CALC_TEXT:
                        return ExportRecipe(recipe.filter_name,
                            '44,34,76,1,,0,false,true,true,false,false,-1,false,true',
                            warnings=recipe.warnings)
                    return recipe
                with patch.object(adapter_module, 'native_export_recipe', shown):
                    return super()._convert_native(source, target, workspace, **kwargs)
            return super()._convert_native(source, target, workspace, **kwargs)
    server, token = start_server(guard_recovery_parent=args.output / 'owned')
    server.guard.adapter = ControlledInitialAdapter(args.executable.resolve())
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
    thread.start()
    origin = 'http://127.0.0.1:' + str(server.server_port)
    evidence = {'schema': 1, 'status': 'FAIL', 'scope': 'actual local loopback + source retention + isolated analysis + LibreOffice CSV automatic retry',
                'fullFormatMatrix': 'NOT_RUN', 'windowsGUI': 'NOT_RUN', 'sourceSha256': source_hash,
                'initialExportControl': {'SaveAsShown': True, 'scope': 'deliberately degraded first LO export; not a claim of a current default defect'}}

    def call(path, body=None, binary=False):
        headers = {'X-AIO-Token': token}
        if body is not None:
            headers['Content-Type'] = 'application/octet-stream' if binary else 'application/json'
            body = body if binary else json.dumps(body).encode()
        with urlopen(Request(origin + '/api/converter-guard/' + path, data=body, headers=headers), timeout=15) as response:
            return json.loads(response.read())

    def wait(path, predicate):
        deadline = time.monotonic() + args.seconds
        while time.monotonic() < deadline:
            state = call(path)
            if predicate(state):
                return state
            time.sleep(.1)
        raise TimeoutError('Targeted acceptance did not complete before its own test deadline')

    try:
        created = call('prepare', {'files': [{'id': 'source', 'name': source.name, 'size': len(source_bytes)}],
                                   'retainSources': True})
        parent = created['jobId']
        call('input?' + urlencode({'job': parent, 'file': 'source'}), source_bytes, binary=True)
        inspection = call('preflight', {'jobId': parent})
        evidence['preflight'] = {'files': [{'fileId': row.get('fileId', row.get('id')),
            'state': row.get('preflight', {}).get('state')} for row in inspection.get('files', [])]}
        call('start', {'jobId': parent, 'formats': ['csv'], 'fileIds': ['source'],
                       'onlyVerified': True, 'archiveMode': 'on_demand'})
        first = wait('jobs?' + urlencode({'job': parent}), lambda s: s['finished'])
        if first['succeeded'] != 1:
            raise AssertionError('Native initial conversion failed: ' + json.dumps(first))
        with server.guard.comparison_inputs(parent) as records:
            assert records[0]['sourceAvailable']
            original_results = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in records[0]['paths']}
            first_csv = records[0]['paths'][0].read_text(encoding='utf-8-sig')
        evidence['firstCsv'] = first_csv
        call('compare/start', {'jobId': parent})
        analysis = wait('compare?' + urlencode({'job': parent}), lambda s: s['state'] in ('completed', 'failed', 'cancelled'))
        if analysis['state'] != 'completed':
            raise AssertionError('Initial analysis did not complete: ' + json.dumps(analysis))
        selected = [row for row in analysis['items'] if 'csv_preserve_values' in row.get('recipeCandidates', [])]
        evidence['firstAnalysis'] = analysis
        if len(selected) != 1:
            evidence.update(status='INCOMPLETE', reason='This engine did not expose the expected formatted-value difference or the verified recipe')
            return 2
        call('compare/retry', {'jobId': parent, 'analysisId': analysis['analysisId'], 'taskIds': [selected[0]['taskId']]})
        retried = wait('compare?' + urlencode({'job': parent}), lambda s: s.get('retry', {}).get('state') in ('completed', 'failed', 'cancelled'))
        if retried['retry']['state'] != 'completed':
            raise AssertionError('Automatic retry failed: ' + json.dumps(retried['retry']))
        evidence['retryVerification'] = retried['retry'].get('verification')
        assert retried['retry'].get('verification', {}).get('assessment') == 'checked_subset_improved'
        child = retried['retry']['jobId']
        child_state = call('jobs?' + urlencode({'job': child}))
        assert child_state['comparisonParentJobId'] == parent
        assert child_state['comparisonRecipe'] == 'csv_preserve_values'
        assert child_state['total'] == child_state['succeeded'] == 1
        with server.guard.comparison_inputs(child) as records:
            candidate = records[0]['paths'][0]
            import csv
            with candidate.open(encoding='utf-8-sig', newline='') as stream:
                rows = list(csv.reader(stream))
            assert rows[1][0] == '00123'
            assert Decimal(rows[1][1]) == Decimal('1.23456789012345')
            assert rows[1][2] == 'Привет'
            evidence['candidateRows'] = rows
            evidence['candidateSha256'] = hashlib.sha256(candidate.read_bytes()).hexdigest()
        call('compare/start', {'jobId': child})
        child_analysis = wait('compare?' + urlencode({'job': child}), lambda s: s['state'] in ('completed', 'failed', 'cancelled'))
        assert child_analysis['state'] == 'completed'
        assert not any(row['status'] == 'confirmed_loss' for row in child_analysis['items'])
        assert all(Path(path).is_file() and hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest
                   for path, digest in original_results.items())
        assert source.read_bytes() == source_bytes
        assert call('jobs?' + urlencode({'job': parent}))['result']['outputs'] == first['result']['outputs']
        evidence.update(status='PASS', childAnalysis=child_analysis, firstResultsUnchanged=True,
                        sourceUnchanged=True, selectedPairs=1,
                        automaticParameters=server.guard.report(child)['files'][0]['targets'][0]['automaticParameters'])
        return 0
    except Exception as error:
        evidence.update(error=type(error).__name__, reason=str(error))
        return 1
    finally:
        server.guard_api.close()
        server.guard.close()
        server.shutdown()
        server.server_close()
        thread.join(3)
        (args.output / 'evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print('AIO_V19_COMPARISON_ACCEPTANCE=' + json.dumps(evidence, ensure_ascii=True), flush=True)


if __name__ == '__main__':
    raise SystemExit(main())
