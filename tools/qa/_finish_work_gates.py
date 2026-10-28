"""One-shot 00AIO completion gates. No packaging, install, main or release."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
REPO = 'jumonja-maker/A.I.O'
ALLOWED = {'fix/native-profile-owned-job-drain-20261028': 'profile',
           'test/remaining-bag-integration-20261028': 'integration'}


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def api(path, data=None, method=None):
    body = None if data is None else json.dumps(data).encode('utf-8')
    request = urllib.request.Request('https://api.github.com/repos/' + REPO + '/' + path,
        data=body, method=method, headers={'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
            'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json',
            'User-Agent': 'AIO-owner-QA', 'X-GitHub-Api-Version': '2022-11-28'})
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.load(response)


def tree_hash(folder):
    entries = []
    for child in folder.iterdir():
        if child.name == '.git':
            continue
        key = child.name.encode('utf-8')
        if child.is_symlink():
            raise ValueError('Unexpected checkout symlink')
        if child.is_dir():
            mode, digest, sort = b'40000', tree_hash(child), key + b'/'
        elif child.is_file():
            raw = child.read_bytes()
            mode, digest, sort = b'100644', hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).digest(), key
        else:
            raise ValueError('Unknown checkout object')
        entries.append((sort, mode + b' ' + key + b'\0' + digest))
    raw = b''.join(value for _, value in sorted(entries))
    return hashlib.sha1(b'tree ' + str(len(raw)).encode() + b'\0' + raw).digest()


def verify(output):
    branch, head = os.environ['AIO_FEATURE_BRANCH'], os.environ['AIO_SOURCE_SHA']
    if branch not in ALLOWED:
        raise ValueError('Not an authorized feature branch')
    expected = api('git/commits/' + head)['tree']['sha']
    actual = tree_hash(ROOT).hex()
    if actual != expected:
        raise ValueError('Pinned checkout tree mismatch')
    output.mkdir(parents=True, exist_ok=False)
    write(output / 'source.json', {'source': head, 'tree': actual, 'branch': branch,
        'runId': int(os.environ['GITHUB_RUN_ID']), 'scope': ALLOWED[branch]})
    print('VERIFIED_SOURCE=' + head + ';TREE=' + actual, flush=True)


def run(output, engine):
    source = json.loads((output / 'source.json').read_text(encoding='utf-8'))
    report = {**source, 'status': 'RUNNING', 'stages': [], 'fulltest': 'NOT_RUN',
              'build': 'NOT_RUN', 'mainChanged': False}
    def stage(name, args):
        tick = time.monotonic()
        log = output / (name + '.log')
        print('GATE_START=' + name, flush=True)
        with log.open('wb') as stream:
            value = subprocess.run([sys.executable, *args], cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT)
        row = {'name': name, 'exitCode': value.returncode,
               'seconds': round(time.monotonic() - tick, 3)}
        report['stages'].append(row)
        write(output / 'acceptance.json', report)
        print('GATE_RESULT=' + json.dumps(row), flush=True)
        if value.returncode:
            print(log.read_text(encoding='utf-8', errors='replace')[-18000:], flush=True)
            raise RuntimeError(name + ' failed; original assertions retained')
    def native(name, script, extra=()):
        stage(name, ['tools/qa/' + script, '--output', str(output / name), *extra])
    try:
        stage('startup-original', ['-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_native_profile_safety_b27.py', '-v'])
        stage('startup-suspended', ['-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_windows_suspended_start.py', '-v'])
        if source['scope'] == 'integration':
            native('stress3', 'guard_remaining_bag_stress3.py', ['--executable', engine])
            ledger = json.loads((output / 'stress3/evidence.json').read_text(encoding='utf-8'))
            if (ledger.get('status') != 'PASS' or len(ledger.get('decisionIndex', [])) != 434
                    or ledger.get('falseRejections') != 0 or ledger.get('silentSuccesses') != 0):
                raise AssertionError('Incomplete stress3 decision ledger')
            report['stress3'] = {key: ledger[key] for key in ('status', 'falseRejections', 'silentSuccesses', 'expectedUnavailable')}
            report['stress3']['decisionRows'] = len(ledger['decisionIndex'])
        else:
            native('profile', 'guard_native_profile_acceptance.py', ['--executable', engine])
            native('dif', 'guard_b28_dif_native_acceptance.py', ['--executable', engine])
            native('ids', 'guard_b29_csv_native_acceptance.py', ['--executable', engine])
        for name, script, extra in [
            ('v20', 'guard_v20_native_acceptance.py', ['--executable', engine, '--require-mode', 'native_uno']),
            ('v21', 'guard_v21_xlsx_acceptance.py', ['--executable', engine, '--require-mode', 'native_uno']),
            ('v22', 'guard_v22_txt_native_acceptance.py', ['--executable', engine]),
            ('v08', 'guard_v08_native_acceptance.py', ['--compare-cli']),
            ('recipes', 'guard_v19_native_recipes_acceptance.py', ['--executable', engine, '--require-mode', 'native_uno']),
            ('visual', 'guard_v19_visual_acceptance.py', []),
            ('journeys', 'guard_v19_journeys_acceptance.py', [])]:
            native(name, script, extra)
        stage('windows-suite', ['tools/qa/guard_v17_selfhosted_acceptance.py', '--unit-report', str(output / 'windows-unit.json')])
        units = json.loads((output / 'windows-unit.json').read_text(encoding='utf-8-sig'))
        if units.get('status') != 'PASS' or units.get('failed') or units.get('errors'):
            raise AssertionError('Windows source gate not PASS')
        report['windowsUnit'] = {key: units.get(key) for key in ('status', 'discovered', 'passed', 'failed', 'errors', 'skipped')}
        report['status'] = 'PASS'
    except BaseException as error:
        report.update(status='FAIL', error=type(error).__name__, reason=str(error))
        raise
    finally:
        write(output / 'acceptance.json', report)
        print('FINAL_GATES=' + json.dumps(report, ensure_ascii=True), flush=True)


def publish(output):
    branch, expected = os.environ['AIO_FEATURE_BRANCH'], os.environ['AIO_SOURCE_SHA']
    if branch not in ALLOWED:
        raise ValueError('Unauthorized publication branch')
    head = api('git/ref/heads/' + branch)['object']['sha']
    if head != expected:
        raise ValueError('Feature head changed; retain local evidence, do not overwrite')
    parent = api('git/commits/' + head)
    prefix = 'docs/review/evidence/00aio-finish/' + os.environ['GITHUB_RUN_ID'] + '/'
    rows, index = [], []
    for path in sorted(output.rglob('*')):
        if not path.is_file() or path.is_symlink():
            continue
        if path.suffix not in ('.json',) and path.name != 'pair-decisions.csv':
            continue
        raw = path.read_bytes()
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError('Evidence file unexpectedly large')
        name = path.relative_to(output).as_posix()
        index.append({'file': name, 'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()})
        rows.append({'path': prefix + name, 'mode': '100644', 'type': 'blob', 'content': raw.decode('utf-8')})
    if len(rows) > 160 or not (output / 'acceptance.json').is_file():
        raise ValueError('Unexpected evidence scope')
    rows.append({'path': prefix + 'capture-index.json', 'mode': '100644', 'type': 'blob',
                 'content': json.dumps({'source': expected, 'files': index}, indent=2)})
    tree = api('git/trees', {'base_tree': parent['tree']['sha'], 'tree': rows})['sha']
    commit = api('git/commits', {'message': 'docs(qa): preserve exact 00AIO completion gate evidence ' + os.environ['GITHUB_RUN_ID'],
                              'tree': tree, 'parents': [head]})['sha']
    if api('git/ref/heads/' + branch)['object']['sha'] != head:
        raise ValueError('Feature head lease lost before evidence publication')
    api('git/refs/heads/' + branch, {'sha': commit, 'force': False}, method='PATCH')
    print('EVIDENCE_COMMIT=' + commit + ';FILES=' + str(len(rows)), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['verify', 'run', 'publish'])
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--engine')
    args = parser.parse_args()
    if args.operation == 'run':
        run(args.output, args.engine)
    elif args.operation == 'verify':
        verify(args.output)
    else:
        publish(args.output)
