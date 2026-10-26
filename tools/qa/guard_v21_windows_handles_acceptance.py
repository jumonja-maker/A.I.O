"""QA-only ownership ablation for Windows handles retained by test references.

Runs two controlled 1000-file Guard cycles in one Python process, without
LibreOffice, comparison workers, downloads or package installation. Samples
before and after dropping only QA references, both before and after gc.collect.
It does not change product cleanup or certify whole-application memory safety.
"""
from __future__ import annotations

import argparse
import gc
import io
import json
import os
from pathlib import Path
import platform
import sys
import time
import weakref

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app_backend.converter_guard import ConverterGuard
from tools.qa.guard_v21_scale_acceptance import ControlledCsvAdapter, resource_sample, require


PRIMARY_SOURCES = [
    'https://github.com/python/cpython/blob/v3.11.9/Include/internal/pycore_condvar.h',
    'https://github.com/python/cpython/blob/v3.11.9/Python/condvar.h',
    'https://github.com/python/cpython/blob/v3.11.9/Python/thread_nt.h',
]


def run_cycle(output, count, until):
    """Keep product owners alive; explicitly remove only inspection aliases."""
    adapter = ControlledCsvAdapter()
    guard = ConverterGuard(adapter=adapter, recovery_parent=output / 'owned')
    result = {'samples': {'before': resource_sample()}, 'files': count}
    try:
        rows = []
        for index in range(count):
            data = ('key,value\n'+str(index)+','+str(100000+index)+'\n').encode()
            rows.append({'id': str(index), 'name': 'scale_'+str(index).zfill(5)+'.csv',
                         'size': len(data)})
        job_id = guard.create(rows, ['csv'], retain_sources=True)['jobId']
        for index, row in enumerate(rows):
            if time.monotonic() >= until:
                raise TimeoutError('Handle ablation exceeded its explicit deadline')
            data = ('key,value\n'+str(index)+','+str(100000+index)+'\n').encode()
            guard.upload(job_id, row['id'], io.BytesIO(data), len(data))
        guard.start(job_id, archive_mode='on_demand')
        while True:
            if time.monotonic() >= until:
                raise TimeoutError('Handle ablation exceeded its explicit deadline')
            with guard._lock:
                job = guard._jobs[job_id]  # Same intentional QA alias as scale acceptance.
                finished = not job.running and job.state in {'ready', 'partial', 'failed', 'cancelled'}
            if finished:
                break
            time.sleep(.02)
        converted = guard.status(job_id)
        require(converted['succeeded'] == count and converted['failed'] == 0
                and adapter.calls == count, 'Controlled conversion did not complete every file')
        result['samples']['converted'] = resource_sample()
        saved_job = guard._jobs[job_id]  # Same second QA alias as scale acceptance.
        require(job is saved_job, 'Polling and inspection aliases should identify one owned job')
        job_ref = weakref.ref(saved_job)
        file_refs = tuple(weakref.ref(item) for item in saved_job.files)
        job_root = saved_job.root
        result['retainedQaAliases'] = ['job', 'saved_job', 'converted', 'rows', 'row', 'data']
        guard.release(job_id, discard=True)
        guard.close()
        require(not guard._jobs and not guard._tickets and guard._reserved == 0,
                'Released Guard registry or reservation survived')
        require(saved_job.io == 0 and not job_root.exists(), 'I/O lease or owned directory survived')
        require(saved_job.store is None or saved_job.store.owner is None, 'OS owner lock remained open')
        require(not any(thread.is_alive() for thread in guard._executor._threads),
                'Guard executor did not terminate')
        result['cleanup'] = {'jobs': 0, 'tickets': 0, 'reservedBytes': 0,
            'ioLeases': 0, 'ownedDirectoryRemoved': True, 'ownerLockClosed': True,
            'ownedExecutorThreadsAlive': 0}
        result['samples']['afterCloseQaReferencesRetained'] = resource_sample()
        result['weakReferencesBeforeDrop'] = {'jobAlive': job_ref() is not None,
            'filesAlive': sum(reference() is not None for reference in file_refs)}
        # No product collection is mutated here. In particular there is no
        # job.files.clear(), Event replacement, native CloseHandle or process exit.
        del saved_job, job, converted, rows, row, data
        result['samples']['afterQaReferencesDroppedBeforeGc'] = resource_sample()
        result['weakReferencesAfterDropBeforeGc'] = {'jobAlive': job_ref() is not None,
            'filesAlive': sum(reference() is not None for reference in file_refs)}
        result['gcCollected'] = gc.collect()
        result['samples']['afterQaReferencesDroppedAndGc'] = resource_sample()
        result['weakReferencesAfterGc'] = {'jobAlive': job_ref() is not None,
            'filesAlive': sum(reference() is not None for reference in file_refs)}
        require(not result['weakReferencesAfterGc']['jobAlive']
                and result['weakReferencesAfterGc']['filesAlive'] == 0,
                'Guard job/file objects remain reachable after removing only QA aliases')
        if os.name == 'nt':
            samples = result['samples']
            before = samples['before']['handles']
            retained = samples['afterCloseQaReferencesRetained']['handles']
            after = samples['afterQaReferencesDroppedAndGc']['handles']
            result['handleAblation'] = {'retainedMinusBefore': retained-before,
                'releasedByDroppingQaReferencesAndGc': retained-after,
                'afterMinusBefore': after-before}
            require(retained-after >= int(count*.8),
                    'QA reference release did not explain the per-file Windows handle increase')
            require(after <= before+32,
                    'Windows handles did not return within the bounded baseline margin')
        result['status'] = 'PASS'
    except Exception as error:
        result['status'] = 'FAIL'
        result['error'] = {'type': type(error).__name__, 'message': str(error)[:1000]}
    finally:
        guard.close()
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--files', type=int, default=1000)
    parser.add_argument('--seconds', type=float, default=180)
    args = parser.parse_args(argv)
    if not 1 <= args.files <= 10000 or not 1 <= args.seconds <= 600:
        parser.error('files must be 1..10000, seconds 1..600')
    args.output.mkdir(parents=True, exist_ok=False)
    begin = time.monotonic()
    # Warm the diagnostic imports before the baseline. No product objects exist yet.
    resource_sample()
    gc.collect()
    evidence = {'schema': 1, 'status': 'FAIL', 'filesPerCycle': args.files,
        'host': {'system': platform.system(), 'python': platform.python_version()},
        'scope': 'two controlled CSV Guard cycles in one process; QA-reference ablation only',
        'nativeLibreOfficeBenchmark': False, 'comparisonWorkersTested': False,
        'wholeApplicationLeakFreedomCertified': False,
        'primarySources': PRIMARY_SOURCES,
        'baseline': resource_sample(), 'cycles': []}
    try:
        for index in range(2):
            result = run_cycle(args.output / ('cycle-'+str(index+1)), args.files, begin+args.seconds)
            result['cycle'] = index+1
            evidence['cycles'].append(result)
            require(result['status'] == 'PASS', 'Handle ablation cycle failed')
        gc.collect()
        evidence['afterBothCycles'] = resource_sample()
        if os.name == 'nt':
            require(evidence['afterBothCycles']['handles'] <= evidence['baseline']['handles']+32,
                    'Two cycles left accumulating Windows handles')
        evidence['status'] = 'PASS'
    except Exception as error:
        evidence['error'] = {'type': type(error).__name__, 'message': str(error)[:1000]}
    evidence['elapsedSeconds'] = round(time.monotonic()-begin, 3)
    (args.output / 'evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
    print('AIO_V21_HANDLE_ABLATION='+json.dumps(evidence, ensure_ascii=True, separators=(',', ':')), flush=True)
    return 0 if evidence['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
