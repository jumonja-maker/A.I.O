"""Actual 510-input Guard native preflight on an isolated Windows QA runner.

Fixture-only benchmark: no conversion, install, network, test-corpus download,
release or changes to the original owner's files. A hard deadline requests
normal Guard cancellation and records incomplete verification as failure.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app_backend.converter_guard import ConverterGuard, GuardLimits, LibreOfficeAdapter
from tools.qa.guard_v08_native_acceptance import fixtures


def _digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as source:
        while chunk := source.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--engine', type=Path, required=True)
    parser.add_argument('--count', type=int, default=510)
    parser.add_argument('--deadline-seconds', type=float, default=600)
    args = parser.parse_args()
    if args.count < 32 or args.count > 510 or not 120 <= args.deadline_seconds <= 900:
        parser.error('Count must be 32..510, deadline 120..900s')
    args.output.mkdir(parents=True, exist_ok=False)
    patterns = fixtures(args.output / 'templates')
    roots = args.output / 'inputs'
    roots.mkdir()
    names = [('docx', 'document'), ('xlsx', 'sheet'), ('csv', 'table'), ('bmp', 'picture')]
    entries = []
    for index in range(args.count):
        suffix, prefix = names[index % len(names)]
        owned = roots / ('%04d_%s.%s' % (index + 1, prefix, suffix))
        owned.write_bytes(patterns[suffix].read_bytes())
        entries.append((f'f{index:04d}', owned))
    checks = {str(path): _digest(path) for _, path in entries}
    start = time.monotonic()
    adapter = LibreOfficeAdapter(args.engine)
    if not adapter.available:
        raise SystemExit('Trusted native LibreOffice binary unavailable')
    guard = ConverterGuard(adapter=adapter, limits=GuardLimits(preflight_timeout=120),
                           recovery_parent=args.output / 'owned-jobs')
    report = {
        'schema': 1, 'scope': 'real Windows native 510-file Guard preflight only',
        'count': args.count, 'deadlineSeconds': args.deadline_seconds,
        'types': dict(Counter(path.suffix.lower() for _, path in entries)),
        'status': 'NOT_STARTED', 'samples': [], 'negativeSamples': 0,
        'originalsPreserved': False,
    }
    try:
        rows = [{'id': identity, 'name': path.name, 'size': path.stat().st_size}
                for identity, path in entries]
        job = guard.create(rows, prepare_only=True)['jobId']
        upload_start = time.monotonic()
        for identity, path in entries:
            with path.open('rb') as stream:
                guard.upload(job, identity, stream, path.stat().st_size)
        report['uploadSeconds'] = round(time.monotonic() - upload_start, 3)
        status_before = guard.status(job)
        if status_before['state'] != 'uploading':
            raise RuntimeError('Unexpected initial preparation state')

        errors, returned = [], []
        def preflight():
            try:
                returned.append(guard.preflight(job))
            except BaseException as error:
                errors.append({'type': type(error).__name__,
                               'code': getattr(error, 'code', None),
                               'message': str(error)[-250:]})

        thread = threading.Thread(target=preflight, name='aio-510-native-preflight', daemon=True)
        thread.start()
        phase_start = time.monotonic()
        last_done = -1
        last_update = time.monotonic()
        saw_checking = False
        stopped_for_deadline = False
        while thread.is_alive():
            state = guard.status(job)
            saw_checking = saw_checking or state['state'] == 'checking'
            done = state.get('preflightCompleted', 0)
            if done != last_done:
                last_done, last_update = done, time.monotonic()
                if done == args.count or done == 0 or done % 25 == 0:
                    sample = {'completed': done, 'total': args.count,
                              'elapsedSeconds': round(time.monotonic()-phase_start, 2)}
                    task = state.get('currentTask')
                    if task is not None:
                        sample['activeSuffix'] = Path(task.get('name', '')).suffix.lower()
                    report['samples'].append(sample)
                    print('AIO_510_PREFLIGHT_PROGRESS=' +
                          json.dumps(sample, ensure_ascii=True), flush=True)
            age = time.monotonic() - last_update
            if age > 80 or time.monotonic()-phase_start > args.deadline_seconds:
                stopped_for_deadline = True
                report['stallSeconds'] = round(age, 2)
                report['timeoutAt'] = done
                guard.cancel(job)
                print('AIO_510_PREFLIGHT_CANCEL=WATCHDOG_DEADLINE', flush=True)
                break
            time.sleep(.25)
        thread.join(timeout=35)
        if thread.is_alive():
            report['status'] = 'FAILED_CANCEL_DID_NOT_TERMINATE'
            report['threadAlive'] = True
        else:
            state = guard.status(job)
            report['nativePreflightSeconds'] = round(time.monotonic()-phase_start, 3)
            report['completed'] = state.get('preflightCompleted')
            report['preflightTotal'] = state.get('preflightTotal')
            report['budgetPerFileSeconds'] = state.get('preflightTimeoutSeconds')
            report['officeStarts'] = state.get('officeStarts')
            report['sawChecking'] = saw_checking
            report['errors'] = errors
            report['deadlineTriggered'] = stopped_for_deadline
            if returned:
                result = returned[0]
                states = Counter((row.get('preflight') or {}).get('state', 'no_proof')
                                 for row in result['files'])
                report['proofStates'] = dict(states)
                report['verifiedRouteCounts'] = {
                    row['id']: len(row.get('verifiedFileIds', ()))
                    for row in result['formats'] if row.get('verifiedFileIds')}
            report['status'] = ('PASS' if not errors and not stopped_for_deadline
                and state.get('state') == 'prepared' and saw_checking
                and state.get('preflightCompleted') == args.count
                and report.get('proofStates') == {'detected': args.count}
                else 'INCOMPLETE')
        report['originalsPreserved'] = all(_digest(path) == checks[str(path)]
                                           for _, path in entries)
        if not report['originalsPreserved']:
            report['status'] = 'SOURCE_MUTATED'
    except BaseException as error:
        report['status'] = 'ERROR'
        report['error'] = {'type': type(error).__name__, 'message': str(error)[-300:]}
        raise
    finally:
        report['totalSeconds'] = round(time.monotonic()-start, 3)
        (args.output / 'evidence.json').write_text(json.dumps(report, ensure_ascii=True, indent=2),
                                                   encoding='utf-8')
        print('AIO_510_PREFLIGHT_SUMMARY=' +
              json.dumps({key: report.get(key) for key in
                  ('status','count','uploadSeconds','nativePreflightSeconds',
                   'completed','budgetPerFileSeconds','officeStarts',
                   'proofStates','errors','stallSeconds','originalsPreserved','totalSeconds')}),
              flush=True)
        guard.close()
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
