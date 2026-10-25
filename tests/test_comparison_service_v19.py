"""Real loopback API + isolated analysis; original/result ownership regressions."""
from concurrent.futures import Future
import hashlib
import io
import json
from pathlib import Path
import tempfile
import sqlite3
from types import SimpleNamespace
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app_backend.converter_guard import ConverterGuard, GuardError
from app_backend.converter_guard.comparison_service import ComparisonService
from test_converter_guard_http_v02 import start_test_server
from test_converter_guard_v02 import wait_finished


class ControlledNativeAdapter:
    """Only the conversion boundary is controlled; HTTP/process/analyzers are real."""
    available = True
    def __init__(self):
        self.calls = []
        self.recipe = None
        self.break_csv = False

    def capabilities(self, files, proofs=None):
        return {'files': files, 'formats': []}

    def preflight(self, source, workspace, *, cancel, timeout):
        if cancel.is_set():
            raise GuardError('CANCELLED', 'cancelled')
        return {'state': 'detected', 'documentServices': ['com.sun.star.sheet.SpreadsheetDocument']}

    def route_decision(self, target, proof):
        return 'registered' if proof and proof.get('state') == 'detected' else 'undetermined'

    def prepare_retry_recipe(self, recipe):
        self.recipe = recipe

    def validate_retry_recipe(self, recipe, target, proof):
        from app_backend.converter_guard.export_recipe import validate_retry_recipe
        return validate_retry_recipe(recipe, target)

    def export_info(self):
        return {'filter': 'Text - txt - csv (StarCalc)', 'automaticRecipe': self.recipe,
                'automaticParameters': {'fullPrecision': True} if self.recipe else {}}

    def convert(self, source, target, workspace, *, cancel, timeout, on_started,
                output_limit, on_document_service=None):
        if cancel.is_set():
            raise GuardError('CANCELLED', 'cancelled')
        on_started()
        if on_document_service:
            on_document_service({'state': 'detected',
                                 'documentServices': ['com.sun.star.sheet.SpreadsheetDocument']})
        self.calls.append((source.name, target, self.recipe))
        output = workspace / 'output'
        output.mkdir(parents=True)
        destination = output / (source.stem + '.' + target)
        data = source.read_bytes()
        if self.break_csv and target == 'csv' and self.recipe is None:
            data = data.replace(b',42\n', b',0\n')
        destination.write_bytes(data)
        return [destination]


class ComparisonServiceContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.adapter = ControlledNativeAdapter()
        self.guard = ConverterGuard(adapter=self.adapter, spool_parent=self.root)
        self.server, self.thread, self.origin, self.token = start_test_server(self.guard)

    def tearDown(self):
        self.server.api.close()
        self.guard.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.temp.cleanup()

    def request(self, path, body=None, *, auth=True, headers=None):
        hdr = {'X-AIO-Token': self.token} if auth else {}
        hdr.update(headers or {})
        if body is not None:
            body = json.dumps(body).encode()
            hdr['Content-Type'] = 'application/json'
        return urlopen(Request(self.origin + '/api/converter-guard/' + path,
                               data=body, headers=hdr), timeout=8)

    def json_request(self, *args, **kwargs):
        with self.request(*args, **kwargs) as response:
            return json.loads(response.read())

    def converted(self, *, count=1, formats=None, name='source.txt', data=b'CONFIDENTIAL source paragraph', retain=True):
        rows = [{'id': str(i), 'name': (str(i)+'-'+name), 'size': len(data)} for i in range(count)]
        identity = self.guard.create(rows, formats or ['txt'], retain_sources=retain)['jobId']
        for row in rows:
            self.guard.upload(identity, row['id'], io.BytesIO(data), len(data))
        self.guard.start(identity, archive_mode='on_demand')
        return wait_finished(self.guard, identity, seconds=10)

    def wait_analysis(self, identity, seconds=12):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = self.json_request('compare?' + urlencode({'job': identity}))
            if value['state'] in ('completed', 'cancelled', 'failed'):
                self.assertNotEqual(value['state'], 'failed', value)
                return value
            time.sleep(.03)
        self.fail('analysis did not finish: ' + repr(value))

    def analyze(self, state):
        self.json_request('compare/start', {'jobId': state['jobId']})
        return self.wait_analysis(state['jobId'])

    def assert_http_code(self, code, path, body=None, **kwargs):
        with self.assertRaises(HTTPError) as error:
            self.request(path, body, **kwargs)
        self.assertEqual(error.exception.code, code)
        return json.loads(error.exception.read())

    def test_ordinary_conversion_does_not_start_analysis_or_retain_originals(self):
        state = self.converted(retain=False)
        identity = state['jobId']
        self.assertFalse(self.guard._jobs[identity].files[0].source.is_file())
        self.assertFalse(list(self.guard._jobs[identity].root.glob('comparison/*')))
        self.assertEqual(len(self.adapter.calls), 1)
        self.assert_http_code(404, 'compare?' + urlencode({'job': identity}))

    def test_real_isolated_analysis_preserves_original_and_old_result_without_exporting(self):
        state = self.converted()
        identity = state['jobId']
        before = self.guard.status(identity)['result']['outputs']
        source = self.guard._jobs[identity].files[0].source
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        analysis = self.analyze(state)
        self.assertEqual((analysis['processed'], analysis['total']), (1, 1))
        self.assertEqual(analysis['state'], 'completed')
        self.assertEqual(analysis['items'][0]['status'], 'checked')
        self.assertIn('identical_file_bytes', analysis['items'][0]['checkedFeatures'])
        self.assertEqual(self.guard.status(identity)['result']['outputs'], before)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), digest)
        self.assertEqual(len(self.adapter.calls), 1)
        public = json.dumps(analysis)
        self.assertNotIn(str(self.root), public)
        self.assertNotIn('CONFIDENTIAL source paragraph', public)
        self.assertNotIn('"_text"', public)

    def test_missing_legacy_original_is_unknown_and_outputs_remain_downloadable(self):
        state = self.converted(retain=False)
        analysis = self.analyze(state)
        row = analysis['items'][0]
        self.assertEqual(row['status'], 'not_checked')
        self.assertFalse(row['sourceAvailable'])
        self.assertEqual(row['recipeCandidates'], [])
        self.assertEqual(row['findings'][0]['code'], 'SOURCE_UNAVAILABLE')
        with self.guard.open_result(state['result']['ticket']) as (stream, _, __):
            self.assertEqual(stream.read(), b'CONFIDENTIAL source paragraph')

    def test_disk_backed_report_pages_all_targets_and_search_is_literal(self):
        state = self.converted(formats=['format'+str(i) for i in range(61)], name='literal%_name.bin')
        analysis = self.analyze(state)
        identity = state['jobId']
        self.assertEqual(analysis['totalFiltered'], 61)
        self.assertEqual(len(analysis['items']), 50)
        next_page = self.json_request('compare?' + urlencode({'job': identity, 'offset': 50, 'limit': 50}))
        self.assertEqual(len(next_page['items']), 11)
        ids = [item['taskId'] for item in analysis['items'] + next_page['items']]
        self.assertEqual(len(set(ids)), 61)
        not_found = self.json_request('compare?' + urlencode({'job': identity, 'q': "' OR 1=1--"}))
        self.assertEqual(not_found['totalFiltered'], 0)
        literal = self.json_request('compare?' + urlencode({'job': identity, 'q': '%_'}))
        self.assertEqual(literal['totalFiltered'], 61)
        self.assert_http_code(400, 'compare?' + urlencode({'job': identity, 'limit': 10001}))
        self.assert_http_code(400, 'compare?' + urlencode({'job': identity, 'offset': -1}))
        self.assert_http_code(400, 'compare?' + urlencode({'job': identity, 'filter': 'all;DELETE'}))

    def test_comparison_search_matches_cyrillic_names_without_case_dependence(self):
        state = self.converted(name='Договор_поставки.txt')
        self.analyze(state)
        for query in ('договор', 'ДОГОВОР', 'дОгОвОр'):
            with self.subTest(query=query):
                page = self.json_request('compare?' + urlencode({'job': state['jobId'], 'q': query}))
                self.assertEqual(page['totalFiltered'], 1)
                self.assertIn('Договор', page['items'][0]['name'])

    def test_preview_is_authenticated_json_for_markup_and_does_not_accept_client_paths(self):
        state = self.converted(data=b'<script>window.STOLEN=true</script>', name='markup.txt')
        analysis = self.analyze(state)
        identity, tid = state['jobId'], analysis['items'][0]['taskId']
        detail = self.json_request('compare/item?' + urlencode({'job': identity, 'task': tid}))
        self.assertNotIn(str(self.root), json.dumps(detail))
        url = detail['previews']['source']['url'].removeprefix('/api/converter-guard/')
        self.assert_http_code(403, url, auth=False)
        self.assert_http_code(403, url, headers={'Host': 'evil.invalid'})
        with self.request(url) as response:
            self.assertTrue(response.headers['Content-Type'].startswith('application/json'))
            payload = json.loads(response.read())
        self.assertEqual(payload['kind'], 'text')
        self.assertEqual(payload['text'], '<script>window.STOLEN=true</script>')
        self.assert_http_code(400, 'compare/preview?' + urlencode({'job': identity, 'task': tid, 'side': '../etc/passwd'}))
        self.assert_http_code(404, 'compare/item?' + urlencode({'job': identity, 'task': identity+':999999'}))
        self.assert_http_code(404, 'compare/preview?' + urlencode({'job': identity, 'task': 'other:0', 'side': 'source'}))

    def test_output_symlink_is_not_a_preview_escape_from_owned_storage(self):
        state = self.converted()
        analysis = self.analyze(state)
        identity, tid = state['jobId'], analysis['items'][0]['taskId']
        artifact = self.guard._jobs[identity].artifacts[0]
        external = self.root / 'outside-owned-job.txt'
        secret = b'Z' * artifact.path.stat().st_size
        external.write_bytes(secret)
        artifact.path.unlink()
        try:
            artifact.path.symlink_to(external)
        except (NotImplementedError, OSError):
            self.skipTest('This platform/account cannot create the controlled link fixture')
        preview = self.json_request('compare/preview?' + urlencode({'job': identity, 'task': tid, 'side': 'result'}))
        self.assertEqual(preview['kind'], 'unavailable')
        self.assertNotIn(secret.decode(), json.dumps(preview))
        self.assertTrue(external.is_file())
        self.assertEqual(external.read_bytes(), secret)

    def test_mutated_source_is_not_a_confirmed_loss_or_successful_comparison(self):
        state = self.converted(data=b'original value')
        self.guard._jobs[state['jobId']].files[0].source.write_bytes(b'tampered value')
        analysis = self.analyze(state)
        row = analysis['items'][0]
        self.assertEqual(row['status'], 'not_checked')
        self.assertFalse(row['recipeCandidates'])
        self.assertFalse(any(f.get('category') == 'confirmed_loss' for f in row['findings']))
        self.assertTrue(any(f.get('code') in ('COMPARISON_SOURCE_CHANGED', 'SOURCE_CHANGED', 'SOURCE_DIGEST_MISMATCH')
                            for f in row['findings']), row)

    def test_retry_rejects_stale_unknown_and_malformed_selection_without_new_jobs(self):
        state = self.converted()
        analysis = self.analyze(state)
        before = set(self.guard._jobs)
        base = {'jobId': state['jobId'], 'analysisId': analysis['analysisId']}
        for tasks in ([], [{}], [1], ['x', 'x'], ['x'*32+':0']):
            with self.subTest(tasks=tasks):
                self.assert_http_code(400, 'compare/retry', {**base, 'taskIds': tasks})
                self.assertEqual(set(self.guard._jobs), before)
        self.assert_http_code(409, 'compare/retry', {**base, 'analysisId': 'stale', 'taskIds': [state['jobId']+':0']})
        self.assertEqual(set(self.guard._jobs), before)

    def test_retry_only_selected_pair_produces_new_candidate_and_preserves_old_tickets(self):
        self.adapter.break_csv = True
        state = self.converted(count=2, name='values.csv', data=b'key,value\nA,42\n', formats=['csv'])
        identity = state['jobId']
        before = self.guard.status(identity)['result']['outputs']
        contents = {a.ticket: a.path.read_bytes() for a in self.guard._jobs[identity].artifacts}
        analysis = self.analyze(state)
        self.assertTrue(all(row['status'] == 'confirmed_loss' for row in analysis['items']))
        tid = analysis['items'][1]['taskId']
        self.assertEqual(analysis['items'][1]['recipeCandidates'], ['csv_preserve_values'])
        self.json_request('compare/retry', {'jobId': identity, 'analysisId': analysis['analysisId'], 'taskIds': [tid]})
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            status = self.json_request('compare?' + urlencode({'job': identity}))
            if status['retry'] and status['retry']['state'] in ('completed', 'failed', 'cancelled'):
                break
            time.sleep(.03)
        self.assertEqual(status['retry']['state'], 'completed', status)
        child = self.guard.status(status['retry']['jobId'])
        self.assertEqual((child['total'], child['succeeded']), (1, 1))
        self.assertEqual(child['comparisonParentJobId'], identity)
        self.assertEqual(self.guard.status(identity)['result']['outputs'], before)
        self.assertTrue(all(self.guard._tickets[ticket].path.read_bytes() == data for ticket, data in contents.items()))
        child_analysis = self.analyze(child)
        self.assertFalse(any(row['status'] == 'confirmed_loss' for row in child_analysis['items']))
        with self.guard.open_result(child['result']['ticket']) as (stream, _, __):
            self.assertEqual(stream.read(), b'key,value\nA,42\n')

    def test_pause_cancel_and_discard_keep_leased_originals_until_worker_stops(self):
        state = self.converted()
        identity = state['jobId']
        service = self.server.api.comparison
        entered, proceed = threading.Event(), threading.Event()
        original_worker = service._worker
        def held(item, records, root):
            entered.set()
            while not proceed.wait(.01):
                if item.cancel.is_set():
                    return
            return original_worker(item, records, root)
        with patch.object(service, '_worker', side_effect=held):
            self.json_request('compare/start', {'jobId': identity})
            self.assertTrue(entered.wait(2))
            source = self.guard._jobs[identity].files[0].source
            self.assertGreater(self.guard._jobs[identity].io, 0)
            paused = self.json_request('compare/control', {'jobId': identity, 'action': 'pause'})
            self.assertEqual(paused['state'], 'paused')
            resumed = self.json_request('compare/control', {'jobId': identity, 'action': 'resume'})
            self.assertEqual(resumed['state'], 'running')
            self.guard.release(identity, discard=True)
            self.assertTrue(source.is_file())
            self.json_request('compare/control', {'jobId': identity, 'action': 'cancel'})
            proceed.set()
        deadline = time.monotonic() + 3
        while source.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertFalse(source.exists())

    def test_cancelled_before_executor_start_releases_the_transferred_input_lease(self):
        state = self.converted()
        identity = state['jobId']
        service = self.server.api.comparison
        pending = Future()
        with patch.object(service.executor, 'submit', return_value=pending):
            accepted = self.json_request('compare/start', {'jobId': identity})
        self.assertEqual(accepted['state'], 'queued')
        self.assertGreater(self.guard._jobs[identity].io, 0)
        self.assertTrue(pending.cancel())
        deadline = time.monotonic() + 2
        while self.guard._jobs[identity].io and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(self.guard._jobs[identity].io, 0,
                         'A queued Future cancelled before _run must not retain job.io forever')
        self.assertTrue(self.guard._jobs[identity].files[0].source.is_file())

    def test_unknown_retry_evidence_is_not_promoted_to_confirmed_improvement(self):
        before = {'taskId': 'parent:0', 'fileId': 'file', 'format': 'csv',
                  'status': 'confirmed_loss', 'checkedFeatures': [],
                  'findings': [{'category': 'confirmed_loss', 'feature': 'cell_values',
                                'code': 'CELL_VALUES_CHANGED'}]}
        after = {'taskId': 'child:0', 'fileId': 'file', 'format': 'csv',
                 'status': 'uncertain', 'checkedFeatures': [],
                 'findings': [{'category': 'uncertain', 'feature': 'cell_values',
                               'code': 'CELL_EXTRACTION_UNCERTAIN'}]}
        objects = []
        for name, payload in [('before.sqlite', before), ('after.sqlite', after)]:
            database = self.root / name
            with sqlite3.connect(database) as db:
                db.execute('CREATE TABLE results(task TEXT PRIMARY KEY, payload TEXT)')
                db.execute('INSERT INTO results VALUES (?,?)', (payload['taskId'], json.dumps(payload)))
            objects.append(SimpleNamespace(db=database, state='completed'))
        assessment = ComparisonService._verify_retry(objects[0], objects[1], ['parent:0'])
        self.assertNotEqual(assessment['assessment'], 'checked_subset_improved',
                            'Disappearance of evidence is not evidence that a previous loss was fixed')

    def test_report_requests_after_release_are_not_sqlite_server_errors(self):
        state = self.converted()
        analysis = self.analyze(state)
        identity = state['jobId']
        self.guard.release(identity, discard=True)
        self.assert_http_code(404, 'compare?' + urlencode({'job': identity}))
        self.assert_http_code(404, 'compare/item?' + urlencode({'job': identity, 'task': analysis['items'][0]['taskId']}))


if __name__ == '__main__':
    unittest.main()
