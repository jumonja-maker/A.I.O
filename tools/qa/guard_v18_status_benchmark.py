"""Measure retained-result status bookkeeping, never LibreOffice conversion.

Synthetic metadata only: no source documents, output bytes or native process
is created. Both wire representations are measured on the same job. The
semantic check reconstructs the negotiated inventory and compares every field
with the legacy response before accepting a timing result.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc

REPOSITORY = Path(__file__).resolve().parents[2]
BASELINE_QA_SHA = 'a6b0861be1c5f9b67130627527dfbafe64fbd0f3'
sys.path.insert(0, str(REPOSITORY))

from app_backend.converter_guard import ConverterGuard
from app_backend.converter_guard.core import _Artifact


class MetadataOnlyAdapter:
    def convert(self, *args, **kwargs):
        raise AssertionError('This benchmark must never run a native export')


def source_identity():
    """Record provenance at start without changing the caller's git index."""
    def git(*args, environment=None):
        return subprocess.run(['git', *args], cwd=REPOSITORY, env=environment,
                              check=True, capture_output=True, timeout=20).stdout

    try:
        head = git('rev-parse', 'HEAD').decode('ascii').strip()
        tree = git('rev-parse', head+'^{tree}').decode('ascii').strip()
        status = git('status', '--porcelain=v1', '-z')
        # Use a disposable index so the hash covers actual dirty source files,
        # not merely the index/HEAD tree. Existing staging is left untouched.
        with tempfile.TemporaryDirectory(prefix='aio-status-benchmark-index-') as directory:
            environment = {**os.environ, 'GIT_INDEX_FILE': str(Path(directory)/'index')}
            git('read-tree', head, environment=environment)
            git('add', '--all', '--', '.', environment=environment)
            working_tree = git('write-tree', environment=environment).decode('ascii').strip()
        return {'status':'AVAILABLE', 'head':head, 'headTree':tree,
                'workingTreeAtStart':working_tree, 'dirtyAtStart':bool(status),
                'statusAtStartSha256':hashlib.sha256(status).hexdigest(),
                'captureScope':'start only; no claim about source changes during/after measurement'}
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:
        return {'status':'NOT_AVAILABLE', 'reason':type(error).__name__,
                'head':None, 'headTree':None, 'workingTreeAtStart':None,
                'dirtyAtStart':None}


def run(files, targets, iterations):
    if min(files, targets, iterations) < 1:
        raise ValueError('files, targets and iterations must be positive')
    with tempfile.TemporaryDirectory(prefix='aio-v18-status-benchmark-') as directory:
        guard = ConverterGuard(adapter=MetadataOnlyAdapter(), spool_parent=directory)
        job = None
        try:
            rows = [{'id':str(i), 'name':str(i)+'.fixture', 'size':0} for i in range(files)]
            identity = guard.create(rows, [f'format{i}' for i in range(targets)])['jobId']
            job = guard._jobs[identity]
            for item in job.files:
                item.state = 'done'
                for task in item.targets:
                    task.state = 'ready'
                    name = 'result.'+task.format
                    artifact = _Artifact(identity,
                        job.root/'work'/str(task.index)/'output'/name, name, 32768,
                        'synthetic-ticket-'+str(task.index).zfill(32), item.id, task.format,
                        str(task.index)+'/'+name, sha256='a'*64)
                    task.artifacts = [artifact]
                    job.artifacts.append(artifact)
            job.state, job.running = 'packaging', True
            job.result = {**job.artifacts[0].metadata(), 'jobId':identity}
            guard._record(job, 'results_available')
            previous = guard.status(identity)
            revision = previous['revision']
            guard._record(job, 'packaging_started')

            def state(negotiated):
                return guard.status(identity, delta=True, result_delta=negotiated,
                                    since_revision=revision)

            def capture(negotiated):
                return json.dumps(state(negotiated), ensure_ascii=False,
                                  separators=(',', ':')).encode('utf-8')

            legacy, negotiated = state(False), state(True)
            if not legacy.get('delta') or not negotiated.get('resultOutputsDelta'):
                raise AssertionError('Benchmark did not exercise negotiated result deltas')
            inventory = {output['ticket']:output for output in previous['result']['outputs']}
            inventory.update({output['ticket']:output for output in negotiated['result']['outputs']})
            reconstructed = {**negotiated, 'result':{**negotiated['result'],
                                                   'outputs':list(inventory.values())}}
            reconstructed.pop('resultOutputsDelta', None)
            if reconstructed != legacy or len(inventory) != files*targets:
                raise AssertionError('Negotiated reconstruction differs from the complete legacy response')
            inventory_sha256 = hashlib.sha256(json.dumps(legacy['result']['outputs'],
                sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()

            # Warm both paths, then alternate order to reduce ordering bias.
            capture(False)
            capture(True)
            elapsed = {False:[], True:[]}
            for iteration in range(iterations):
                for mode in ((False, True) if iteration % 2 == 0 else (True, False)):
                    tick = time.perf_counter_ns()
                    capture(mode)
                    elapsed[mode].append((time.perf_counter_ns()-tick)/1e6)
            modes = {}
            for mode in (False, True):
                tracemalloc.start()
                try:
                    data = capture(mode)
                    _, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
                samples = elapsed[mode]
                modes['negotiated_delta' if mode else 'legacy_delta'] = {
                    'bytes':len(data), 'medianMs':statistics.median(samples),
                    'p95Ms':sorted(samples)[int(.95*(len(samples)-1))],
                    'peakAllocatedBytes':peak, 'samplesMs':samples}
            return {'inputs':files, 'targetsPerInput':targets, 'outputs':files*targets,
                    'iterations':iterations, 'modes':modes,
                    'semanticReconstruction':{'status':'PASS', 'outputsCompared':len(inventory),
                        'outputInventorySha256':inventory_sha256,
                        'scope':'all response fields and complete retained output metadata; no output bytes'},
                    'measurementScope':'packaging-only status response plus UTF-8 JSON serialization; '
                        'peakAllocatedBytes is one request allocation measured by tracemalloc, not process RSS'}
        finally:
            if job is not None:
                job.running = False
            guard.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--files', type=int, default=1000)
    parser.add_argument('--targets', type=int, default=7)
    parser.add_argument('--iterations', type=int, default=12)
    args = parser.parse_args()
    if min(args.files, args.targets, args.iterations) < 1:
        parser.error('--files, --targets and --iterations must be positive')
    provenance = source_identity()
    value = {'schema':1, 'scope':'synthetic Guard status benchmark; no LibreOffice, '
             'conversion, native output bytes, total conversion time or app RSS measured',
             'baselineQaSha':BASELINE_QA_SHA, 'source':provenance,
             'environment':{'python':sys.version, 'platform':platform.platform(),
                            'implementation':platform.python_implementation()},
             **run(args.files, args.targets, args.iterations)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({'status':'PASS', 'scope':value['scope'], 'output':str(args.output),
                      'inputs':args.files, 'outputs':args.files*args.targets,
                      'modes':{name:{key:mode[key] for key in ('medianMs','bytes','peakAllocatedBytes')}
                               for name, mode in value['modes'].items()}}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
