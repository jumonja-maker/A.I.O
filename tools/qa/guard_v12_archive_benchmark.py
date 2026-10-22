"""Compare automatic vs requested-only archives with a synthetic native boundary.

This measures Guard/disk work, never LibreOffice throughput. All output bytes
are generated fixtures. No network, owner runner, sampler or external files.
"""
import argparse
import io
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app_backend.converter_guard import ConverterGuard


class FixtureAdapter:
    available = True
    def __init__(self, size):
        self.size = size

    def convert(self, source, target, workspace, *, cancel, on_started, **_):
        on_started()
        output = workspace/'output';output.mkdir(parents=True)
        path = output/(source.stem+'.'+target)
        remaining = self.size
        block = b'owned-native-boundary-fixture\n'*4096
        with path.open('xb') as stream:
            while remaining:
                if cancel.is_set():
                    raise RuntimeError('Benchmark cancelled')
                count = min(len(block),remaining)
                stream.write(block[:count]);remaining -= count
        return [path]


def case(parent, mode, files, size):
    with tempfile.TemporaryDirectory(prefix='aio-v12-bench-',dir=parent) as directory:
        root = Path(directory)
        guard = ConverterGuard(adapter=FixtureAdapter(size),spool_parent=root)
        try:
            rows = [{'id':str(i),'name':str(i)+'.fixture','size':1} for i in range(files)]
            identity = guard.create(rows,['pdf'])['jobId']
            for row in rows:
                guard.upload(identity,row['id'],io.BytesIO(b'x'),1)
            tick = time.monotonic()
            guard.start(identity,archive_mode=mode)
            until = time.monotonic()+180
            while not (state := guard.status(identity))['finished']:
                if time.monotonic() >= until:
                    raise RuntimeError('Benchmark queue timed out')
                time.sleep(.005)
            elapsed = (time.monotonic()-tick)*1000
            assert state['succeeded']==files and not state['failed']
            job = guard._jobs[identity]
            payload = sum(artifact.size for artifact in job.artifacts)
            archive_bytes = job.bundle.size if job.bundle else 0
            assert payload == files*size
            assert bool(archive_bytes) == (mode=='auto' and files>1)
            return {'mode':mode,'queueMs':round(elapsed,3),'artifactBytes':payload,
                    'archiveBytes':archive_bytes,'retainedBytes':job.reserved,
                    'archiveMs':job.timings.get('archiveMs',0),'succeeded':state['succeeded']}
        finally:
            guard.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--files',type=int,default=128)
    parser.add_argument('--bytes',type=int,default=1048576)
    parser.add_argument('--repeats',type=int,default=3)
    args = parser.parse_args()
    if min(args.files,args.bytes,args.repeats) < 1:
        parser.error('Counts and byte size must be positive')
    args.output = args.output.resolve()
    args.output.mkdir(parents=True,exist_ok=False)
    results = []
    for repeat in range(args.repeats):
        # Alternate order to avoid attributing one cache/order effect to a mode.
        modes = ('auto','on_demand') if repeat%2==0 else ('on_demand','auto')
        for mode in modes:
            row = {'repeat':repeat,**case(args.output,mode,args.files,args.bytes)}
            results.append(row);print(json.dumps(row),flush=True)
    medians = {mode:statistics.median(row['queueMs'] for row in results if row['mode']==mode)
               for mode in ('auto','on_demand')}
    evidence = {'status':'PASS','scope':'synthetic native boundary; archive/disk bookkeeping only',
        'files':args.files,'bytesPerArtifact':args.bytes,'repeats':args.repeats,
        'medianQueueMs':medians,'measurements':results,'nativeConversionSpeedup':'NOT_MEASURED'}
    (args.output/'evidence.json').write_text(json.dumps(evidence,indent=2),encoding='utf-8')
    print(json.dumps(evidence),flush=True)


if __name__=='__main__':
    main()
