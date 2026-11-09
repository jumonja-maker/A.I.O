"""Matched legacy quota A/B and optional real CLI controls.

Only the quota scanner is exchanged: all other transport/engine code, full-scan
boundaries, inputs, output checks and quota are identical. Alternating A/B order
limits warm-cache/order bias. This is a generated Linux/Windows control, not the
historical FULLTEST, installed GUI acceptance or a claim about persistent UNO.
"""
from __future__ import annotations

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
import threading
import time
import tracemalloc
from unittest.mock import patch
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[2]
COMPONENT = 'app_backend/legacy_storage_budget.py'
sys.path.insert(0, str(ROOT))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def history(parent, count):
    old = parent / 'foreign-unverified'
    old.mkdir(parents=True)
    for number in range(count):
        (old / ('%05d.bin' % number)).write_bytes(b'x')


def summary(rows, key):
    result = {}
    for mode in ('baseline', 'patched'):
        values = [row[key] for row in rows if row['mode'] == mode]
        result[mode] = {'median': statistics.median(values), 'min': min(values),
                        'max': max(values), 'samples': len(values)}
    result['baselineOverPatchedMedian'] = result['baseline']['median'] / result['patched']['median']
    return result


def scanner_cases(functions, output, files, repeats, iterations, *, directory_inputs=0, cancellable=False):
    rows, memory = [], {}
    with tempfile.TemporaryDirectory(prefix='scan-', dir=output) as folder:
        parent = Path(folder)
        history(parent, files)
        # Mirror the directories produced by two targets per synthetic input.
        # A flat foreign-history scan alone misses directory-descent overhead.
        for number in range(directory_inputs):
            for target in ('0', '1'):
                destination = parent / 'active' / 'native' / str(number) / target / 'output'
                destination.mkdir(parents=True)
                (destination / 'result').write_bytes(b'x')
        expected = files + directory_inputs * 2
        kwargs = {'cancel': threading.Event()} if cancellable else {}
        for function in functions.values():
            assert function([parent], **kwargs) == expected  # Both start from warm directory metadata.
        for repeat in range(repeats):
            order = ('baseline', 'patched') if repeat % 2 == 0 else ('patched', 'baseline')
            for mode in order:
                wall, cpu = time.perf_counter(), time.process_time()
                for _ in range(iterations):
                    assert functions[mode]([parent], **kwargs) == expected
                rows.append({'repeat': repeat, 'mode': mode,
                             'wallMsPerScan': (time.perf_counter()-wall)*1000/iterations,
                             'parentCpuMsPerScan': (time.process_time()-cpu)*1000/iterations})
        for mode, function in functions.items():
            tracemalloc.start()
            assert function([parent], **kwargs) == expected
            _, memory[mode] = tracemalloc.get_traced_memory()
            tracemalloc.stop()
    return {'measurements': rows, 'cancellable': cancellable,
            'directoryInputs': directory_inputs, 'expectedBytes': expected,
            'wallMsPerScan': summary(rows, 'wallMsPerScan'),
            'peakPythonTracedBytesOneScan': memory}


def transport_case(scanner, output, existing, inputs, executable):
    from app_backend import converter_transport as module
    from app_backend.libreoffice_engine import convert_path
    with tempfile.TemporaryDirectory(prefix='native-' if executable else 'fixture-', dir=output) as folder:
        work = Path(folder)
        parent = work / 'legacy'
        history(parent, existing)
        sources = []
        for number in range(inputs):
            source = work / ('source-%03d.txt' % number)
            source.write_text(''.join('COMPLETION_G05_%03d line %02d exact text\n' % (number, line)
                                      for line in range(12)), encoding='utf-8')
            sources.append(source)
        original_hashes = [sha(source.read_bytes()) for source in sources]
        scans, native, process_metrics = [], [], []

        def measured(roots, **kwargs):
            tick = time.perf_counter()
            try:
                return scanner(roots, **kwargs)
            finally:
                scans.append({'ms': (time.perf_counter()-tick)*1000,
                              'cancellable': kwargs.get('cancel') is not None})

        def convert(source, target, workspace, **kwargs):
            tick = time.perf_counter()
            try:
                if executable:
                    return convert_path(source, target, workspace, executable=executable,
                                        metrics_sink=process_metrics.append, **kwargs)
                output = workspace / 'output'
                output.mkdir(parents=True)
                result = output / ('result.' + target)
                result.write_bytes(b'fixture exact output bytes\n')
                os.utime(result, (1_700_000_000, 1_700_000_000))
                return [result]
            finally:
                native.append((time.perf_counter()-tick)*1000)

        transport = module.ConverterTransport(recovery_parent=parent, max_owned_bytes=1024**3)
        request = None
        try:
            with patch.object(module, 'measured_bytes', measured), patch.object(module, 'convert_path', convert):
                wall, cpu = time.perf_counter(), time.process_time()
                request = transport.begin('a'*32, None)
                result = transport.execute(request,
                    [{'path': source, 'warnings': []} for source in sources],
                    [str(index) for index in range(inputs)], ['pdf', 'txt'])
                elapsed = (time.perf_counter()-wall)*1000
                cpu_ms = (time.process_time()-cpu)*1000
            report = result['result']['targetReport']
            assert report['succeeded'] == inputs*2 and report['failed'] == 0, report
            saved = transport.results[result['result']['ticket']]
            with ZipFile(saved.path) as archive:
                members = [(name, len(archive.read(name)), sha(archive.read(name)))
                           for name in archive.namelist()]
            content = []
            if executable:
                from pypdf import PdfReader
                for index, source in enumerate(sources):
                    marker = 'COMPLETION_G05_%03d' % index
                    for target in ('pdf', 'txt'):
                        path = next((request.root/'native'/str(index)/str(('pdf','txt').index(target))/'output').glob('*.'+target))
                        if target == 'pdf':
                            reader = PdfReader(path)
                            try:
                                pages = len(reader.pages)
                                text = ' '.join(page.extract_text() or '' for page in reader.pages)
                            finally:
                                reader.close()
                            assert pages == 1, pages
                        else:
                            text, pages = path.read_text(encoding='utf-8-sig'), None
                        normalized = ' '.join(text.split())
                        assert normalized.count(marker) == 12, normalized
                        assert normalized == ' '.join(source.read_text().split()), normalized
                        content.append((index, target, pages, sha(normalized.encode())))
            assert [sha(source.read_bytes()) for source in sources] == original_hashes
            transport.finish(request, retained=True)
            return {'wallMs': elapsed, 'parentCpuMs': cpu_ms, 'conversions': inputs*2,
                    'jobsPerSecond': inputs*2/(elapsed/1000), 'fullScans': len(scans),
                    'fullScanMs': sum(row['ms'] for row in scans),
                    'scanBoundaries': {label: {
                        'count': sum(row['cancellable'] == cancellable for row in scans),
                        'ms': sum(row['ms'] for row in scans if row['cancellable'] == cancellable)}
                        for label, cancellable in (('explicit', False), ('cancellable', True))},
                    'nativeBoundaryMs': sum(native),
                    'cliProcessMs': sum(row['processMs'] for row in process_metrics),
                    'cliLaunchMs': sum(row['launchMs'] for row in process_metrics),
                    'cliProcesses': len(process_metrics), 'archiveBytes': saved.path.stat().st_size,
                    'memberContentSha256': sha(json.dumps(members).encode()),
                    'nativeSemanticSha256': sha(json.dumps(content).encode()) if executable else None,
                    'sourceUnchanged': True, 'succeeded': report['succeeded']}
        finally:
            if request is not None and not request.done.is_set():
                transport.finish(request)
            transport.close()


def matched_transport(functions, output, existing, inputs, repeats, executable=None):
    rows = []
    for repeat in range(repeats):
        order = ('baseline', 'patched') if repeat % 2 == 0 else ('patched', 'baseline')
        for mode in order:
            row = {'repeat': repeat, 'mode': mode,
                   **transport_case(functions[mode], output, existing, inputs, executable)}
            rows.append(row)
            print(json.dumps({'realNative': bool(executable), **row}), flush=True)
    if executable:
        assert len({row['nativeSemanticSha256'] for row in rows}) == 1
    else:
        assert len({row['memberContentSha256'] for row in rows}) == 1
        assert len({row['fullScans'] for row in rows}) == 1
    return {'measurements': rows, 'wallMs': summary(rows, 'wallMs'),
            'fullScanMs': summary(rows, 'fullScanMs')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', default='b7216d1210489a5fd1eadb21daf2e3b3849e30f4')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--executable', type=Path)
    parser.add_argument('--existing-files', type=int, default=4000)
    parser.add_argument('--fixture-inputs', type=int, default=100)
    parser.add_argument('--repeats', type=int, default=5)
    parser.add_argument('--native-repeats', type=int, default=3)
    parser.add_argument('--native-inputs', type=int, default=2)
    args = parser.parse_args()
    if min(args.existing_files, args.fixture_inputs, args.repeats, args.native_repeats, args.native_inputs) < 1:
        parser.error('All counts must be positive')
    if args.executable and not args.executable.is_file():
        parser.error('Executable does not exist')
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    base = subprocess.check_output(['git', 'rev-parse', '--verify', args.baseline+'^{commit}'], cwd=ROOT).decode().strip()
    source = subprocess.check_output(['git', 'show', base+':'+COMPONENT], cwd=ROOT)
    baseline = {'__package__': 'app_backend', '__name__': 'quota_baseline'}
    exec(compile(source, 'quota_baseline', 'exec'), baseline)
    from app_backend.legacy_storage_budget import measured_bytes
    functions = {'baseline': baseline['measured_bytes'], 'patched': measured_bytes}
    report = {'schema': 1, 'status': 'FAIL', 'baselineCommit': base,
              'exchangedComponent': COMPONENT, 'baselineComponentSha256': sha(source),
              'patchedComponentSha256': sha((ROOT/COMPONENT).read_bytes()),
              'head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT).decode().strip(),
              'platform': platform.platform(), 'python': sys.version,
              'scope': 'Isolated scanner differential; identical remaining checkout and full quota scan boundaries. Warm filesystem metadata; fresh private office profile for every native conversion.',
              'limitations': ['Generated controls, not historical FULLTEST or installed Windows GUI.',
                              'No persistent-UNO change or FT4-07 CLI scheduling speedup measured.',
                              'No cold-disk, SMB, native peak RSS or multi-process contention measurement.'],
              'existingFiles': args.existing_files}
    try:
        report['scanner'] = scanner_cases(functions, args.output, args.existing_files, args.repeats, 10)
        report['scannerWithCancellation'] = scanner_cases(functions, args.output, args.existing_files,
                                                          args.repeats, 10, cancellable=True)
        report['directoryHeavyScanner'] = {
            label: scanner_cases(functions, args.output, args.existing_files, args.repeats, 10,
                                 directory_inputs=args.fixture_inputs, cancellable=cancellable)
            for label, cancellable in (('explicit', False), ('cancellable', True))}
        report['syntheticTransport'] = matched_transport(functions, args.output, args.existing_files,
                                                         args.fixture_inputs, args.repeats)
        if args.executable:
            report['engineVersion'] = subprocess.check_output([str(args.executable), '--version'], text=True).strip()
            report['engineLauncherSha256'] = sha(args.executable.read_bytes())
            report['realCliTransport'] = matched_transport(functions, args.output, args.existing_files,
                args.native_inputs, args.native_repeats, args.executable.resolve())
        report['status'] = 'PASS'
    finally:
        (args.output/'evidence.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({'status': report['status'], 'evidence': str(args.output/'evidence.json')}), flush=True)


if __name__ == '__main__':
    main()
