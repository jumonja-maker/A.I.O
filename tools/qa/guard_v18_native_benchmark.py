"""Reproduce preparation-only speed and parser allocations against verified v17.

No office conversion is benchmarked here. Use native acceptance separately to
check output content; these short CPU/allocation results are not Windows/RSS or
end-to-end speed claims. Run from a full Git checkout with its base available.
"""
import argparse
import hashlib
import random
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
import zipfile

ROOT = Path(__file__).resolve().parents[2]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--baseline', default='a6b0861be1c5f9b67130627527dfbafe64fbd0f3')
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
BASELINE_COMPONENT = 'app_backend/converter_guard/odf_table_import.py'
EXPECTED_BASE_BLOB = 'f418c0863dfcb4e6489f75a62fc460dcd210235e'
KNOWN_QA_BASE = 'a6b0861be1c5f9b67130627527dfbafe64fbd0f3'
sys.path.insert(0, str(ROOT))
from app_backend.converter_guard.adapter import _PreparedSourceCache
from app_backend.converter_guard import odf_table_import

cancel = threading.Event()
baseline_sha = subprocess.check_output(['git', 'rev-parse', '--verify', args.baseline + '^{commit}'], cwd=ROOT).decode().strip()
baseline_blob = subprocess.check_output(['git', 'rev-parse', baseline_sha + ':' + BASELINE_COMPONENT], cwd=ROOT).decode().strip()
if baseline_blob != EXPECTED_BASE_BLOB:
    raise SystemExit('Requested baseline differs from the verified v17 ODF component: ' + baseline_blob)
before_source = subprocess.check_output(['git', 'show', baseline_sha + ':' + BASELINE_COMPONENT], cwd=ROOT).decode()
baseline = {'__name__': 'odf_table_import_baseline', '__package__': 'app_backend.converter_guard'}
exec(compile(before_source, 'odf_table_import_baseline', 'exec'), baseline)
header = (f'<o:document-content xmlns:o="{odf_table_import.OFFICE}" xmlns:t="{odf_table_import.TABLE}" '
          'xmlns:p="urn:oasis:names:tc:opendocument:xmlns:text:1.0"><o:body><o:text><t:table>').encode()
row = b'<t:table-row><t:table-cell><p:p>A</p:p></t:table-cell><t:table-cell><p:p>B</p:p></t:table-cell></t:table-row>'
tail = b'</t:table></o:text></o:body></o:document-content>'
data = header + row * 60000 + tail
memory = []
for label, function in [('baseline_v17', baseline['_insertions']), ('patched_v18', odf_table_import._insertions)]:
    tracemalloc.start()
    tick = time.perf_counter()
    result = function(data, cancel)
    duration = time.perf_counter() - tick
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert result[0][2:] == (2, 60000)
    memory.append({'label': label, 'seconds': duration, 'peakTracedBytes': peak,
                   'rows': 60000, 'xmlBytes': len(data)})

with tempfile.TemporaryDirectory(prefix='aio-preparation-benchmark-') as name:
    root = Path(name)
    source = root / 'source.odt'
    xml = header + row * 3000 + tail
    # One 2 MiB opaque resource exposes repeated archive recompression. It is
    # copied byte-for-byte and never interpreted as an image in this benchmark.
    resource = random.Random(180024).randbytes(2 * 1024 * 1024)
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('mimetype', 'application/vnd.oasis.opendocument.text')
        archive.writestr('content.xml', xml, compress_type=zipfile.ZIP_DEFLATED)
        archive.writestr('Resources/opaque.bin', resource, compress_type=zipfile.ZIP_DEFLATED)
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    baseline_paths = []
    tick = time.perf_counter()
    for index in range(20):
        path, summary = baseline['prepare_odf_table_source'](source, root / ('cold-' + str(index)), cancel=cancel)
        assert summary['columns'] == [2]
        baseline_paths.append(path)
    cold_seconds = time.perf_counter() - tick
    cache = _PreparedSourceCache(root / 'warm')
    tick = time.perf_counter()
    warm_paths = [cache.prepare(source, epub=False, cancel=cancel)[0] for _ in range(20)]
    warm_seconds = time.perf_counter() - tick
    with zipfile.ZipFile(baseline_paths[0]) as first, zipfile.ZipFile(warm_paths[0]) as cached:
        assert first.namelist() == cached.namelist()
        assert all(first.read(name) == cached.read(name) for name in first.namelist())
        assert cached.read('Resources/opaque.bin') == resource
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_sha
    preparation = {'targets': 20, 'rows': 3000, 'opaqueResourceBytes': len(resource),
                   'sourceBytes': source.stat().st_size, 'sourceSha256': source_sha,
                   'baselineSeconds': cold_seconds, 'cachedSeconds': warm_seconds,
                   'speedupPreparationOnly': cold_seconds / warm_seconds,
                   'sourceUnchanged': True, 'allPreparedMembersByteEqual': True,
                   'distinctPreparedCopiesBefore': len(set(baseline_paths)),
                   'distinctPreparedCopiesAfter': len(set(warm_paths))}

report = {'scope': 'Local Linux microbenchmarks; preparation and parser allocations only, not end-to-end conversion/RSS/Windows.',
          'knownQaBase': KNOWN_QA_BASE, 'baselineRef': args.baseline,
          'resolvedBaselineCommit': baseline_sha, 'baselineComponentBlob': baseline_blob,
          'componentMatchesVerifiedV17': True,
          'measuredHead': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT).decode().strip(),
          'measuredHeadTree': subprocess.check_output(['git', 'rev-parse', 'HEAD^{tree}'], cwd=ROOT).decode().strip(),
          'workingTreeDirty': bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT).strip()), 'python': sys.version,
          'preparation': preparation, 'odfRowInspection': memory}
destination = args.output.resolve()
destination.parent.mkdir(parents=True, exist_ok=True)
destination.write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report, indent=2))
