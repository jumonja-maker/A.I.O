"""Controlled Guard status benchmark, without native exports or user files.

Run this identical script on each compared checkout. This measures only
status/progress bookkeeping, never LibreOffice speed or total app RSS.
"""
import argparse
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app_backend.converter_guard import ConverterGuard


def run(files, targets, frames):
    with tempfile.TemporaryDirectory() as folder:
        guard = ConverterGuard(spool_parent=folder)
        try:
            rows = [{'id': str(i), 'name': f'fixture-{i}.docx', 'size': 0} for i in range(files)]
            identity = guard.create(rows, [f'format{i}' for i in range(targets)])['jobId']
            job = guard._jobs[identity]
            item = job.files[0]
            elapsed = []
            for _ in range(frames):
                revision = job.revision
                tick = time.perf_counter_ns()
                # Native stage updates do not change completed counters.
                with guard._lock:
                    guard._record(job, 'native_stage', file_id=item.id, target=item.targets[0].format)
                state = guard.status(identity, delta=True, since_revision=revision)
                elapsed.append((time.perf_counter_ns() - tick) / 1e6)
                assert state['delta'] and len(state['files']) == 1
                assert state['total'] == state['pending'] == files * targets
            return {'files': files, 'targetsPerFile': targets, 'tasks': files * targets,
                    'frames': frames, 'totalMs': round(sum(elapsed), 3),
                    'medianMs': round(statistics.median(elapsed), 3),
                    'p95Ms': round(sorted(elapsed)[int(.95 * (len(elapsed) - 1))], 3)}
        finally:
            guard.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--frames', type=int, default=100)
    args = parser.parse_args()
    assert args.frames > 0
    value = {'scope': 'synthetic Guard status bookkeeping; no native conversion',
             'runs': [run(count, 32, args.frames) for count in (100, 1000)]}
    args.output.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(value), flush=True)


if __name__ == '__main__':
    main()
