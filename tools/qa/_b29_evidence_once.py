"""Temporary one-shot B29 evidence publication; no product/main mutation."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
REPO = 'jumonja-maker/A.I.O'
BRANCH = 'fix/guard-v23-b29-csv-text-ids-20261027'
BASE = 'e627c0fe3b8165ff6a5dd24d89845d8949ffb1b4'
FILES = (
 'app_backend/converter_guard/csv_id_contract.py',
 'app_backend/converter_guard/csv_import.py',
 'app_backend/converter_guard/legacy_import.py',
 'app_backend/converter_guard/adapter.py',
 'app_backend/converter_guard/export_recipe.py',
 'tests/test_csv_text_ids_b29.py', 'tests/b29_csv_fixtures.py',
 'tools/qa/guard_b29_csv_native_acceptance.py',
 'docs/review/AIO_B29_CSV_IDS_2026-10-27_RU.md',
)


def fingerprints():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in FILES}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['seal', 'publish'])
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    path = args.evidence / 'source-seal.json'
    if args.action == 'seal':
        value = {'base': BASE, 'testedHead': os.environ['B29_EXPECTED_HEAD'], 'files': fingerprints()}
        path.write_text(json.dumps(value, indent=2), encoding='utf-8')
        print('B29_SOURCE_SEAL=' + json.dumps(value), flush=True)
        return
    seal = json.loads(path.read_text(encoding='utf-8'))
    if seal['files'] != fingerprints():
        raise RuntimeError('Product or test bytes changed during acceptance')
    unit = json.loads((args.evidence / 'unit.json').read_text(encoding='utf-8-sig'))
    native = json.loads((args.evidence / 'native/evidence.json').read_text(encoding='utf-8-sig'))
    dif = json.loads((args.evidence / 'dif/evidence.json').read_text(encoding='utf-8-sig'))
    if unit.get('status') != 'PASS' or unit.get('failed') or unit.get('errors'):
        raise RuntimeError('Full Windows source gate failed')
    if (native.get('status') != 'PASS' or native.get('passed') != 108 or native.get('failed') or
            not native.get('sourceBytesUnchanged') or len(native.get('baseline', ())) != 24 or
            native.get('numericControl', {}).get('status') != 'PASS'):
        raise RuntimeError('Exact B29 native gate failed')
    if dif.get('status') != 'PASS' or dif.get('passed') != 48 or not dif.get('sourceBytesUnchanged'):
        raise RuntimeError('B28 DIF native regression failed')
    if (args.evidence / 'other-gates-pass.txt').read_text(encoding='utf-8-sig').strip() != 'PASS':
        raise RuntimeError('Native v20/v21/v22 and queue/save gate missing')
    expected = os.environ['B29_EXPECTED_HEAD']
    if seal['testedHead'] != expected or os.environ.get('GITHUB_REPOSITORY') != REPO:
        raise RuntimeError('Wrong tested branch identity')
    acceptance = {'schema': 1, 'status': 'PASS_TARGETED_QA', 'base': BASE, 'testedHead': expected,
        'windowsRun': int(os.environ['GITHUB_RUN_ID']), 'sourceSeal': seal['files'],
        'nativeCases': 108, 'legacyImportControls': 24, 'difRegression': 48,
        'nativeGates': ['v20', 'v21-xlsx', 'v22-txt', 'v08-queue-save'],
        'fulltest': 'NOT_RUN', 'exe': 'NOT_BUILT', 'mainChanged': False}
    docs = {'source-seal.json': seal, 'acceptance.json': acceptance, 'windows-unit.json': unit,
            'windows-b29-native.json': native, 'windows-b28-regression.json': dif}
    token = os.environ['B29_GITHUB_TOKEN']
    endpoint = 'https://api.github.com/repos/' + REPO + '/'
    def api(path, value=None, method=None):
        req = urllib.request.Request(endpoint + path,
            data=None if value is None else json.dumps(value).encode('utf-8'),
            method=method or ('GET' if value is None else 'POST'), headers={
                'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
                'Content-Type': 'application/json', 'X-GitHub-Api-Version': '2022-11-28',
                'User-Agent': 'AIO-B29-QA'})
        with urllib.request.urlopen(req, timeout=60) as response:
            return json.load(response)
    ref = 'git/refs/heads/' + BRANCH
    if api(ref)['object']['sha'] != expected:
        raise RuntimeError('Feature branch advanced; refusing publication')
    parent_tree = api('git/commits/' + expected)['tree']['sha']
    entries = []
    for name, data in docs.items():
        blob = api('git/blobs', {'content': json.dumps(data, ensure_ascii=False, indent=2) + '\n',
                                 'encoding': 'utf-8'})['sha']
        entries.append({'path': 'docs/review/evidence/b29/' + name,
                        'mode': '100644', 'type': 'blob', 'sha': blob})
    tree = api('git/trees', {'base_tree': parent_tree, 'tree': entries})['sha']
    commit = api('git/commits', {'message': 'docs(qa): exact B29 Windows source and 108-case native acceptance',
                               'tree': tree, 'parents': [expected]})['sha']
    if api(ref)['object']['sha'] != expected:
        raise RuntimeError('Feature branch advanced during publication')
    api(ref, {'sha': commit, 'force': False}, method='PATCH')
    print('B29_EVIDENCE_COMMIT=' + commit, flush=True)


if __name__ == '__main__':
    main()
