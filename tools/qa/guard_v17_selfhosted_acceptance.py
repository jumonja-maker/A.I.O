"""Bounded, offline acceptance for the owner's existing Windows/Linux runners.

No packages are installed, no artifacts are uploaded, and input corpora are read
only. Exit 2 means incomplete coverage; a missing dependency or skipped native
route can never be turned into an acceptance PASS by a successful subprocess.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import time
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# These tests run on the Linux runner. They cannot execute on Windows because
# their fixtures are scripts with POSIX shebangs/process-group semantics.
WINDOWS_INAPPLICABLE_REASONS = frozenset({
    'POSIX test fixture; real Windows Job Object needs Windows acceptance',
    'Executable generated fixture uses a POSIX shebang',
    'Owned POSIX worker lifecycle; not Windows Job Object QA',
})
SOURCE_ONLY_NATIVE_REASONS = frozenset({
    'Opt in with AIO_QT_SAVE_QA=1 and existing Qt bindings',
    'PySide6 required for native raster frame tests',
    'Application Qt dependency required',
    'LibreOffice not installed',
})


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def suite_summary(result, system=None, *, source_only=False):
    system = system or platform.system()
    skipped = [{'test': test.id(), 'reason': reason,
                'status': ('not_applicable' if system == 'Windows' and
                           reason in WINDOWS_INAPPLICABLE_REASONS else
                           'platform_not_applicable' if source_only and system == 'Linux' and
                           reason in SOURCE_ONLY_NATIVE_REASONS else 'INCOMPLETE')}
               for test, reason in result.skipped]
    failed = [{'test': test.id(), 'traceback': trace}
              for test, trace in result.failures + result.errors]
    expected_failures = [{'test': test.id(), 'traceback': trace}
                         for test, trace in getattr(result, 'expectedFailures', [])]
    incomplete = bool(expected_failures) or any(row['status'] == 'INCOMPLETE' for row in skipped)
    status = 'FAIL' if failed or result.unexpectedSuccesses else 'INCOMPLETE' if incomplete else 'PASS'
    return {'status': status, 'discovered': result.testsRun,
            'passed': result.testsRun - len(result.failures) - len(result.errors) - len(result.skipped) - len(expected_failures),
            'failed': len(result.failures), 'errors': len(result.errors),
            'skipped': skipped, 'failureDetails': failed, 'expectedFailures': expected_failures,
            'unexpectedSuccesses': [test.id() for test in result.unexpectedSuccesses]}


def unit_child(path, *, source_only=False):
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    summary = suite_summary(result, source_only=source_only or os.environ.get('AIO_QA_SOURCE_ONLY') == '1')
    write_json(path, summary)
    print('AIO_UNIT_EVIDENCE=' + json.dumps(summary, ensure_ascii=True), flush=True)
    return 1 if summary['status'] == 'FAIL' else 2 if summary['status'] == 'INCOMPLETE' else 0


def run_command(name, command, output, *, timeout=1800):
    """Contain and reap only this stage's children, including timed-out LO."""
    from app_backend.native_process import ProcessTree
    logfile = output / (name + '.log')
    started = time.monotonic()
    timed_out = False
    print('AIO_QA_STAGE_START=' + name, flush=True)
    heartbeat = started
    with logfile.open('wb') as stream:
        with ProcessTree([str(arg) for arg in command], cwd=ROOT,
                         stdout=stream, stderr=subprocess.STDOUT, env=dict(os.environ)) as tree:
            until = time.monotonic() + timeout
            while tree.process.poll() is None:
                if time.monotonic() >= until:
                    timed_out = True
                    if os.name != 'nt':
                        # Python's SIGINT unwinds native acceptance finally
                        # blocks, closing LO children that own separate sessions.
                        try:
                            os.kill(tree.process.pid, signal.SIGINT)
                            tree.process.wait(timeout=10)
                        except (ProcessLookupError, subprocess.TimeoutExpired):
                            pass
                    break
                if time.monotonic() - heartbeat >= 30:
                    print('AIO_QA_STAGE_RUNNING=' + json.dumps({'name': name,
                        'elapsedSeconds': round(time.monotonic() - started)}), flush=True)
                    heartbeat = time.monotonic()
                time.sleep(.1)
            returncode = tree.process.poll()
    tail = logfile.read_bytes()[-14000:].decode('utf-8', errors='replace')
    if tail:
        print(tail, flush=True)
    row = {'status': 'FAIL' if timed_out or returncode not in (0, 2) else
                       'INCOMPLETE' if returncode == 2 else 'PASS',
           'returncode': returncode, 'timedOut': timed_out,
           'durationMs': round((time.monotonic() - started) * 1000), 'log': logfile.name}
    print('AIO_QA_STAGE=' + json.dumps({'name': name, **row}, ensure_ascii=True), flush=True)
    return row


def corpus_candidates(system=None):
    if (system or platform.system()) == 'Windows':
        return [Path(p) for p in (
            r'D:\AIO-Lab\FULLTEST', r'D:\AIO-Lab\AIO-QA\FULLTEST',
            r'D:\AIO-Lab\AIO-QA-Env\FULLTEST', r'D:\AIO-QA\FULLTEST')]
    return [Path(p) for p in (
        '/home/aio-runner/AIO-Lab/FULLTEST', '/home/aio-runner/AIO-QA/FULLTEST',
        '/home/aio-runner/FULLTEST')]


def corpus_inventory(folder):
    """Inspect a resolved corpus only, without following directory symlinks."""
    rows = []
    for current, dirs, names in os.walk(folder, followlinks=False):
        dirs[:] = sorted(name for name in dirs if not (Path(current) / name).is_symlink())
        for name in sorted(names):
            path = Path(current) / name
            if path.is_symlink() or not path.is_file():
                continue
            with path.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            rows.append({'name': path.relative_to(folder).as_posix(),
                         'bytes': path.stat().st_size, 'sha256': digest})
    rows.sort(key=lambda row: row['name'])
    encoded = json.dumps(rows, ensure_ascii=True, separators=(',', ':')).encode()
    return {'files': rows, 'count': len(rows),
            'bytes': sum(row['bytes'] for row in rows),
            'extensions': dict(sorted(Counter(Path(row['name']).suffix.lower() for row in rows).items())),
            'inventorySha256': hashlib.sha256(encoded).hexdigest()}


def bundled_corpus(output):
    """Verify all immutable ZIP entries before extracting an owned subset."""
    bundle = ROOT / 'tests/data/guard_v17_fulltest_subset.zip'
    if not bundle.is_file():
        return None
    destination = output / 'targeted-corpus'
    with zipfile.ZipFile(bundle) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError('Duplicate bundled corpus archive entries')
        manifest = json.loads(archive.read('__manifest__.json'))
        if manifest.get('scope') != 'targeted_original_FULLTEST_subset':
            raise ValueError('Unexpected bundled corpus scope')
        rows = manifest['files']
        wanted = [row['name'] for row in rows]
        if len(wanted) != len(set(wanted)) or set(names) != set(wanted) | {'__manifest__.json'}:
            raise ValueError('Bundled corpus manifest/entries differ')
        verified = []
        for row in rows:
            name = row['name']
            if (not isinstance(name, str) or name in ('', '.', '..') or
                    any(char in name for char in '/\\\0:') or any(ord(char) < 32 for char in name)):
                raise ValueError('Unsafe bundled corpus filename')
            raw = archive.read(name)
            if len(raw) != row['bytes'] or hashlib.sha256(raw).hexdigest() != row['sha256']:
                raise ValueError('Bundled corpus bytes differ from manifest')
            verified.append((name, raw))
        destination.mkdir()
        for name, raw in verified:
            (destination / name).write_bytes(raw)
        write_json(output / 'bundled-corpus-manifest.json', manifest)
    return destination


def native_summary(evidence):
    generated = evidence.get('generatedFixtures', [])
    acceptance = evidence.get('guardAcceptance', {})
    negative = evidence.get('negativeControls', [])
    failed = (any(row.get('status') == 'fail' for row in generated + negative) or
              acceptance.get('status') == 'fail' or
              any(row.get('originalUnchanged') is False for row in evidence.get('originalFixtures', [])))
    unsupported = [row for row in generated if row.get('status') == 'not_run' and
                   row.get('reason') == 'installed_native_export_filter_unavailable']
    not_run = [{'kind': kind, **row} for kind, rows in (
        ('generatedFixture', generated), ('negativeControl', negative))
        for row in rows if row.get('status') != 'pass' and row not in unsupported]
    incomplete = (not generated or bool(not_run) or acceptance.get('status') != 'pass' or
                  not acceptance.get('report', {}).get('succeeded'))
    return {'status': 'FAIL' if failed else 'INCOMPLETE' if incomplete else 'PASS',
            'generated': dict(Counter(row.get('status', 'missing') for row in generated)),
            'guard': acceptance.get('status', 'missing'),
            'counts': {key: acceptance.get('report', {}).get(key) for key in
                       ('total', 'succeeded', 'failed', 'cancelled')},
            'unplannedPairs': acceptance.get('unplannedPairs', []),
            'notRun': not_run, 'unsupportedFixtureGeneration': unsupported,
            'negativeControls': dict(Counter(row.get('status', 'missing') for row in negative)),
            'fulltest510': evidence.get('fulltest510', 'NOT_RUN'),
            'windowsGuiTested': evidence.get('windowsGuiTested', False)}


def completion_status(stages):
    statuses = [row.get('status') for row in stages.values()]
    return 'FAIL' if 'FAIL' in statuses else 'INCOMPLETE' if any(s != 'PASS' for s in statuses) else 'PASS'


def attach_evidence(stage, details):
    """A good-looking report cannot hide a crashed/timed-out supervisor."""
    status = completion_status({'process': stage, 'evidence': details})
    stage.update(details)
    stage['status'] = status


def dependency_summary(deps, *, source_only=False, system=None):
    system = system or platform.system()
    inapplicable = ['PySide6'] if source_only and system == 'Linux' and not deps.get('PySide6') else []
    missing = [name for name, present in deps.items() if not present and name not in inapplicable]
    return {'status': 'INCOMPLETE' if missing else 'PASS', 'missing': missing,
            'platformNotApplicable': inapplicable}


def emit_summary(evidence, output):
    write_json(output / 'selfhosted-evidence.json', evidence)
    compact = {key: value for key, value in evidence.items() if key != 'corpusFiles'}
    print('AIO_SELFHOSTED_EVIDENCE=' + json.dumps(compact, ensure_ascii=True, separators=(',', ':')), flush=True)
    summary_path = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary_path:
        with open(summary_path, 'a', encoding='utf-8') as stream:
            stream.write('### AIO PR40 self-hosted acceptance\n\n')
            stream.write('```json\n' + json.dumps(compact, ensure_ascii=False, indent=2) + '\n```\n')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--executable', type=Path)
    parser.add_argument('--corpus', type=Path)
    parser.add_argument('--require-corpus', action='store_true')
    parser.add_argument('--source-only', action='store_true',
                        help='Supplemental Linux source/POSIX/DOM checks; native acceptance remains NOT_RUN')
    parser.add_argument('--seconds', type=float, default=60)
    parser.add_argument('--unit-report', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.unit_report:
        return unit_child(args.unit_report, source_only=args.source_only)
    if args.source_only and platform.system() != 'Linux':
        parser.error('--source-only is supplemental Linux coverage; Windows requires native acceptance')
    if (args.output is None or args.seconds <= 0 or
            (not args.source_only and (args.executable is None or not args.executable.is_file()))):
        parser.error('An existing native executable, new --output and positive --seconds are required')
    output = args.output.resolve()
    if output == ROOT or output.is_relative_to(ROOT):
        parser.error('Evidence output must be outside the immutable source checkout')
    if args.corpus and (not args.corpus.is_dir() or output.is_relative_to(args.corpus.resolve())):
        parser.error('Corpus must exist and output must be outside it')
    output.mkdir(parents=True, exist_ok=False)
    os.environ.update(PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1', PYTHONIOENCODING='utf-8',
                      AIO_QT_SAVE_QA='1', AIO_QA_SOURCE_ONLY='1' if args.source_only else '0',
                      AIO_LO_QA_MUST_HAVE_ENGINE='0' if args.source_only else '1',
                      AIO_LIBREOFFICE=(str(output / 'native-disabled-in-source-only') if args.source_only else
                                       str(args.executable.resolve())),
                      QT_QPA_PLATFORM='offscreen', OMP_NUM_THREADS='2', OPENBLAS_NUM_THREADS='2')
    evidence = {'schema': 1, 'sourceSha': os.environ.get('AIO_QA_SOURCE_SHA', 'unspecified'),
                'workflowSha': os.environ.get('GITHUB_SHA', 'local'), 'platform': platform.system(),
                'python': platform.python_version(), 'stages': {},
                'fulltest510': 'NOT_RUN', 'visibleGui': 'NOT_RUN',
                'scope': 'source_only' if args.source_only else
                         'offline full source regressions and bounded native conversion; no installation or packaging'}
    if args.source_only:
        evidence['native'] = {'status': 'NOT_RUN', 'reason': 'explicit_source_only_mode',
                              'requiredCoverage': 'Full native acceptance on NIKITA Windows job'}
    stages = evidence['stages']
    try:
        deps = {name: importlib.util.find_spec(name) is not None
                for name in ('openpyxl', 'PySide6', 'pypdf')}
        deps['node'] = bool(shutil.which('node'))
        deps['DOM'] = bool(os.environ.get('AIO_DOM_MODULE') and Path(os.environ['AIO_DOM_MODULE']).is_file())
        evidence['dependencies'] = deps
        stages['dependencies'] = dependency_summary(deps, source_only=args.source_only)
        corpus = args.corpus.resolve() if args.corpus else next((p for p in corpus_candidates() if p.is_dir()), None)
        bundled = False
        if corpus is None:
            corpus = bundled_corpus(output)
            bundled = corpus is not None
        before = corpus_inventory(corpus) if corpus else None
        if before is not None:
            write_json(output / 'corpus-inventory-before.json', before)
            evidence['corpus'] = {key: value for key, value in before.items() if key != 'files'}
            evidence['corpus']['status'] = 'enumerated_read_only'
            evidence['corpus']['scope'] = 'targeted_original_FULLTEST_subset' if bundled else 'existing_local_corpus'
        else:
            evidence['corpus'] = {'status': 'INCOMPLETE', 'reason': 'No corpus in explicit controlled candidate folders'}
        if args.require_corpus:
            stages['requiredCorpus'] = {'status': 'PASS' if before and before['count'] else 'INCOMPLETE'}
        unit_path = output / 'unittest-evidence.json'
        stages['unittest'] = run_command('unittest', [sys.executable, __file__, '--unit-report', unit_path], output)
        if unit_path.is_file():
            attach_evidence(stages['unittest'], json.loads(unit_path.read_text(encoding='utf-8')))
        else:
            stages['unittest'].update(status='FAIL', reason='No completed unittest evidence')
        if not args.source_only and deps['pypdf']:
            native_path = output / 'native-v16'
            command = [sys.executable, ROOT / 'tools/qa/guard_v16_bugbag_acceptance.py',
                       '--output', native_path, '--executable', args.executable.resolve(), '--seconds', str(args.seconds)]
            # The v16 tool expects a flat corpus. A nested corpus is inventoried
            # but must not be silently treated as a completed original test set.
            if corpus and any(p.is_file() and not p.is_symlink() for p in corpus.iterdir()):
                command.extend(['--corpus', corpus])
            stages['nativeV16'] = run_command('native-v16', command, output)
            native_json = native_path / 'evidence.json'
            if native_json.is_file():
                attach_evidence(stages['nativeV16'], native_summary(json.loads(native_json.read_text(encoding='utf-8'))))
            else:
                stages['nativeV16'].update(status='FAIL', reason='No completed native evidence')
            smoke = output / 'native-v08'
            stages['native11Routes'] = run_command('native-v08', [sys.executable,
                ROOT / 'tools/qa/guard_v08_native_acceptance.py', '--output', smoke, '--compare-cli'], output)
            smoke_json = smoke / 'evidence.json'
            if smoke_json.is_file():
                smoke_evidence = json.loads(smoke_json.read_text(encoding='utf-8'))
                stages['native11Routes']['quality'] = smoke_evidence.get('quality', {})
                if any(value != 'PASS' for value in smoke_evidence.get('quality', {}).values()):
                    stages['native11Routes']['status'] = 'INCOMPLETE'
            else:
                stages['native11Routes'].update(status='FAIL', reason='No completed 11-route evidence')
            v17_output = output / 'native-v17'
            stages['nativeV17'] = run_command('native-v17', [sys.executable,
                ROOT / 'tools/qa/guard_v17_native_acceptance.py', '--output', v17_output,
                '--executable', args.executable.resolve(), '--seconds', str(args.seconds),
                *(['--corpus', corpus] if corpus else [])], output)
            v17_json = v17_output / 'evidence.json'
            if v17_json.is_file():
                native_v17 = json.loads(v17_json.read_text(encoding='utf-8'))
                attach_evidence(stages['nativeV17'], native_v17)
            else:
                stages['nativeV17'].update(status='FAIL', reason='No completed v17 native evidence')
        elif not args.source_only:
            stages['nativeV16'] = {'status': 'INCOMPLETE', 'reason': 'Existing pypdf is required for independent PDF checks'}
        if before is not None:
            after = corpus_inventory(corpus)
            write_json(output / 'corpus-inventory-after.json', after)
            stages['originalsUnchanged'] = {'status': 'PASS' if before == after else 'FAIL',
                                           'before': before['inventorySha256'], 'after': after['inventorySha256']}
    except Exception as error:
        import traceback
        traceback.print_exc()
        stages['supervisor'] = {'status': 'FAIL', 'error': type(error).__name__, 'message': str(error)}
    evidence['status'] = completion_status(stages)
    emit_summary(evidence, output)
    return 1 if evidence['status'] == 'FAIL' else 2 if evidence['status'] == 'INCOMPLETE' else 0


if __name__ == '__main__':
    sys.exit(main())
