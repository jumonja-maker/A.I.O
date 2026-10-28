"""1000-file Guard/comparison/cleanup scale probe with a controlled converter.

Conversion is deterministic byte copying with declared injected losses, NOT
LibreOffice throughput or native quality evidence. Guard storage, comparison
workers, report SQL, preview workers and discard are production implementations.
Only generated tiny CSVs are used; no dependencies are installed or downloaded.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import sys
import threading
import time
import tracemalloc

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def require(value, message):
    if not value:
        raise AssertionError(message)


def hash_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while block := stream.read(65536):
            digest.update(block)
    return digest.hexdigest()


def resource_sample():
    """Own process only; no inferred child/GPU memory and no extra dependency."""
    result = {'threads': threading.active_count()}
    try:
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes as w
            class Counters(ctypes.Structure):
                _fields_ = [('cb', w.DWORD), ('PageFaultCount', w.DWORD)] + [
                    (name, ctypes.c_size_t) for name in ('PeakWorkingSetSize', 'WorkingSetSize',
                    'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
                    'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage')]
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.GetCurrentProcess.restype = w.HANDLE
            handle = kernel.GetCurrentProcess()
            counter = w.DWORD()
            kernel.GetProcessHandleCount.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
            if kernel.GetProcessHandleCount(handle, ctypes.byref(counter)):
                result['handles'] = counter.value
            value = Counters(); value.cb = ctypes.sizeof(value)
            psapi = ctypes.WinDLL('psapi', use_last_error=True)
            psapi.GetProcessMemoryInfo.argtypes = [w.HANDLE, ctypes.POINTER(Counters), w.DWORD]
            if psapi.GetProcessMemoryInfo(handle, ctypes.byref(value), value.cb):
                result.update(rssBytes=value.WorkingSetSize, peakRssBytes=value.PeakWorkingSetSize)
        else:
            status = Path('/proc/self/status')
            if status.is_file():
                for line in status.read_text().splitlines():
                    if line.startswith(('VmRSS:', 'VmHWM:')):
                        result['rssBytes' if line.startswith('VmRSS:') else 'peakRssBytes'] = int(line.split()[1]) * 1024
            descriptors = Path('/proc/self/fd')
            if descriptors.is_dir():
                result['fileDescriptors'] = sum(1 for _ in descriptors.iterdir())
    except (OSError, ValueError, AttributeError):
        result['partial'] = True
    return result


class ControlledCsvAdapter:
    available = True

    def __init__(self):
        self.calls = 0

    def capabilities(self, files, proofs=None):
        return {'files': files, 'formats': []}

    def preflight(self, source, workspace, *, cancel, timeout):
        return {'state': 'detected', 'documentServices': ['com.sun.star.sheet.SpreadsheetDocument']}

    def route_decision(self, target, proof):
        return 'registered' if target == 'csv' and proof and proof.get('state') == 'detected' else 'undetermined'

    def convert(self, source, target, workspace, *, cancel, timeout, on_started,
                output_limit, on_document_service=None):
        from app_backend.converter_guard import GuardError
        if cancel.is_set():
            raise GuardError('CANCELLED', 'Synthetic scale operation cancelled')
        on_started()
        if on_document_service:
            on_document_service({'state': 'detected', 'documentServices': ['com.sun.star.sheet.SpreadsheetDocument']})
        data = source.read_bytes()
        index = int(data.splitlines()[1].split(b',')[0])
        if index % 10 == 0:
            data = data.replace(b',' + str(100000 + index).encode() + b'\n', b',0\n')
        output = workspace / 'output'; output.mkdir(parents=True)
        destination = output / (source.stem + '.csv'); destination.write_bytes(data)
        self.calls += 1
        return [destination]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--files', type=int, default=1000)
    parser.add_argument('--seconds', type=float, default=300)
    args = parser.parse_args(argv)
    if not 1 <= args.files <= 10000 or not 1 <= args.seconds <= 1800:
        parser.error('files must be 1..10000 and seconds 1..1800')
    args.output.mkdir(parents=True, exist_ok=False)
    from app_backend.converter_guard import ConverterGuard
    from app_backend.converter_guard import comparison_service as module
    evidence = {'schema': 1, 'status': 'FAIL', 'files': args.files,
        'scope': 'real Guard retention, isolated comparison, disk report, preview, reopen and discard; controlled CSV converter',
        'nativeLibreOfficeBenchmark': False, 'guiBenchmark': False,
        'host': {'system': platform.system(), 'python': platform.python_version()},
        'memoryScope': 'sampled own Python process RSS/handles; tracemalloc only three reopen/preview cycles; not whole-app soak or child peak RSS',
        'phasesSeconds': {}, 'samples': {}, 'limits': {'deadlineSeconds': args.seconds}}
    begin = time.monotonic(); until = begin + args.seconds
    def deadline():
        if time.monotonic() >= until:
            raise TimeoutError('Scale acceptance exceeded its own explicit deadline')
    def record(label):
        evidence['samples'][label] = resource_sample()
    worker_stats = {'started': 0, 'closed': 0, 'active': 0, 'peakActive': 0,
                    'liveAfterClose': 0, 'jobHandleAfterClose': 0}
    worker_lock = threading.Lock()
    original_tree = module.ProcessTree
    class ObservedTree(original_tree):
        def __init__(self, *values, **options):
            self.observed = False
            super().__init__(*values, **options)
            with worker_lock:
                self.observed = True
                worker_stats['started'] += 1; worker_stats['active'] += 1
                worker_stats['peakActive'] = max(worker_stats['peakActive'], worker_stats['active'])
        def close(self):
            super().close()
            with worker_lock:
                if self.observed:
                    self.observed = False
                    worker_stats['closed'] += 1; worker_stats['active'] -= 1
                    worker_stats['liveAfterClose'] += int(self.process is not None and self.process.poll() is None)
                    worker_stats['jobHandleAfterClose'] += int(self._job is not None)
    module.ProcessTree = ObservedTree
    adapter = ControlledCsvAdapter()
    guard = ConverterGuard(adapter=adapter, recovery_parent=args.output/'owned')
    service = module.ComparisonService(guard)
    job_id = None
    record('before')
    try:
        phase = time.monotonic()
        rows = []
        for index in range(args.files):
            data = ('key,value\n'+str(index)+','+str(100000+index)+'\nlabel,SCALE_'+str(index)+'\n').encode()
            rows.append({'id': str(index), 'name': 'scale_'+str(index).zfill(5)+'.csv', 'size': len(data)})
        job_id = guard.create(rows, ['csv'], retain_sources=True)['jobId']
        for index, row in enumerate(rows):
            deadline()
            data = ('key,value\n'+str(index)+','+str(100000+index)+'\nlabel,SCALE_'+str(index)+'\n').encode()
            guard.upload(job_id, row['id'], io.BytesIO(data), len(data))
        evidence['phasesSeconds']['createUpload'] = round(time.monotonic()-phase, 3)
        phase = time.monotonic()
        guard.start(job_id, archive_mode='on_demand')
        while True:
            deadline()
            with guard._lock:
                job = guard._jobs[job_id]
                finished = not job.running and job.state in {'ready', 'partial', 'failed', 'cancelled'}
            if finished: break
            time.sleep(.1)
        converted = guard.status(job_id)
        require(converted['finished'] and converted['succeeded'] == args.files and converted['failed'] == 0,
                'Controlled Guard conversions did not all finish successfully')
        evidence['conversion'] = {key: converted[key] for key in ('state', 'succeeded', 'failed')}
        evidence['phasesSeconds']['controlledConversion'] = round(time.monotonic()-phase, 3)
        record('converted')
        with guard.comparison_inputs(job_id) as records:
            require(len(records) == args.files and all(row['sourceAvailable'] and len(row['paths']) == 1 for row in records),
                    'Retained source/output pair inventory is incomplete')
            fingerprints = {path: hash_file(path) for row in records for path in (row['source'], *row['paths'])}
        del records
        phase = time.monotonic()
        service.start(job_id)
        last_logged = 0
        while True:
            deadline()
            with service.lock:
                item = service.jobs[job_id]
                processed = item.processed
                finished = item.state in module.FINAL and service._settled(item)
            if processed >= last_logged + 100:
                print('AIO_V21_SCALE_PROGRESS='+json.dumps({'processed': processed, 'total': args.files}), flush=True)
                last_logged = processed
            if finished: break
            if 'runningPeak' not in evidence['samples'] or processed % 25 == 0:
                sample = resource_sample()
                peak = evidence['samples'].setdefault('runningPeak', {})
                for key,value in sample.items():
                    if type(value) is int: peak[key] = max(peak.get(key, 0), value)
            time.sleep(.15)
        report = service.snapshot(job_id, limit=100)
        expected_loss = (args.files + 9)//10
        expected = {'checked': args.files-expected_loss, 'confirmed_loss': expected_loss,
                    'format_limitation': 0, 'uncertain': 0, 'not_checked': 0}
        require(report['state'] == 'completed' and report['processed'] == args.files,
                'Isolated comparison did not complete every pair')
        require(report['summary'] == expected, 'Comparison status counts differ from injected-loss ground truth')
        evidence['analysis'] = {'state': report['state'], 'processed': report['processed'],
            'summary': report['summary'], 'expected': expected}
        evidence['phasesSeconds']['isolatedAnalysis'] = round(time.monotonic()-phase, 3)
        analysis_id = report['analysisId']; first_task = None; seen = set(); counts = Counter()
        for offset in range(0,args.files,100):
            deadline(); page = service.snapshot(job_id, offset=offset, limit=100)
            require(len(page['items']) <= 100 and page['totalFiltered'] == args.files, 'Invalid disk pagination')
            for row in page['items']:
                require(row['taskId'] not in seen, 'Duplicate paginated task')
                seen.add(row['taskId']); counts[row['status']] += 1
                index = int(row['fileId'])
                require(row['status'] == ('confirmed_loss' if index % 10 == 0 else 'checked'), 'Wrong per-file classification')
                if index % 10 == 0:
                    loss = next((f for f in row['findings'] if f['code'] == 'CELL_VALUES_CHANGED'), None)
                    require(loss and loss['totalAffected'] == 1, 'Injected single-cell loss not precisely explained')
                first_task = first_task or row['taskId']
        require(len(seen) == args.files, 'Pagination omitted retained tasks')
        for category in ('checked', 'confirmed_loss'):
            require(service.snapshot(job_id, filter=category, limit=1)['totalFiltered'] == expected[category], 'Filter count mismatch')
        evidence['pagination'] = {'uniqueTasks': len(seen), 'pageLimit': 100, 'pages': (args.files+99)//100,
                                  'perTaskTruthVerified': True, 'filteredCountsVerified': True}
        before_workers = worker_stats['started']
        phase = time.monotonic(); tracemalloc.start(1)
        reopened = []
        for cycle in range(3):
            deadline(); service.close(); service = module.ComparisonService(guard)
            recovered = service.snapshot(job_id, limit=1)
            require(recovered['analysisId'] == analysis_id and recovered['summary'] == expected,
                    'Reopened report did not preserve exact session/statuses')
            require(recovered['recovered'] and recovered['state'] == 'completed', 'Reopen did not recover completed disk report')
            for side in ('source', 'result'):
                with service.preview(job_id, first_task, side, expected_analysis_id=analysis_id) as preview:
                    require(preview.get('json', {}).get('kind') == 'text', 'Real isolated CSV text preview unavailable')
            current, peak = tracemalloc.get_traced_memory()
            reopened.append({'cycle': cycle+1, 'currentTracedBytes': current, 'peakTracedBytes': peak})
        tracemalloc.stop()
        require(worker_stats['started'] - before_workers == 6, 'Reopen unexpectedly reran analysis or skipped real previews')
        evidence['reopen'] = {'cycles': 3, 'previewWorkers': 6, 'analysisRerun': False, 'allocationSamples': reopened}
        evidence['phasesSeconds']['reopenPreview'] = round(time.monotonic()-phase, 3)
        require(all(path.is_file() and hash_file(path) == digest for path,digest in fingerprints.items()),
                'Analysis or preview changed a retained source/output')
        evidence['immutability'] = {'filesRehashed': len(fingerprints), 'allUnchanged': True}
        record('beforeDiscard'); phase = time.monotonic()
        saved_item = service.jobs[job_id]; saved_job = guard._jobs[job_id]; job_root = saved_job.root
        require(saved_job.io == 0, 'A completed preview retained an I/O lease')
        guard.release(job_id, discard=True); service.release(job_id)
        service.close(); guard.close()
        require(not guard._jobs and not guard._tickets and guard._reserved == 0, 'Guard retained released jobs/tickets/quota')
        require(not service.jobs and not saved_item.expected_tasks and saved_item.records is None,
                'Comparison retained released session indexes or records')
        require(saved_job.io == 0 and not job_root.exists(), 'Owned job directory or lease survived explicit discard')
        require(saved_job.store is None or saved_job.store.owner is None, 'Recovery owner lock remained open')
        require(worker_stats['started'] == args.files + 6 and worker_stats['closed'] == worker_stats['started'] and
                not worker_stats['active'] and not worker_stats['liveAfterClose'] and not worker_stats['jobHandleAfterClose'],
                'A comparison/preview process tree did not close cleanly')
        require(not any(t.is_alive() for executor in (guard._executor, service.executor, service.retry_executor)
                        for t in executor._threads), 'Owned executor thread remained after close')
        evidence['cleanup'] = {'jobs': 0, 'tickets': 0, 'reservedBytes': 0, 'ioLeases': 0,
            'sessionIndexes': 0, 'ownedDirectoryRemoved': True, 'ownerLockClosed': True,
            'ownedExecutorThreadsAlive': 0}
        evidence['phasesSeconds']['discardClose'] = round(time.monotonic()-phase, 3)
        record('afterClose')
        evidence['status'] = 'PASS'
    except Exception as error:
        evidence['error'] = {'type': type(error).__name__, 'message': str(error)[:1000]}
    finally:
        if tracemalloc.is_tracing(): tracemalloc.stop()
        try:
            if job_id in guard._jobs:
                guard.release(job_id, discard=True); service.release(job_id)
            service.close(); guard.close()
        except Exception as error:
            evidence['status'] = 'FAIL'
            evidence['cleanupError'] = {'type': type(error).__name__, 'message': str(error)[:500]}
        module.ProcessTree = original_tree
        evidence['workers'] = dict(worker_stats)
        evidence['elapsedSeconds'] = round(time.monotonic()-begin, 3)
        (args.output/'evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
        print('AIO_V21_SCALE='+json.dumps(evidence,ensure_ascii=True,separators=(',',':')),flush=True)
    return 0 if evidence['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
