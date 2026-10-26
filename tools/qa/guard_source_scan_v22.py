"""Reproducible structural scan benchmark and differential OPC-path check.

Run from the repository root:
  python tools/qa/guard_source_scan_v22.py --output scan-v22.json
Or supply --baseline-file /path/to/the/exact/old/source_contract.py.

Uses the real scanner and error class in an isolated Python package, avoiding
GUI/native process startup. This measures structural scanning only, with warm
filesystem cache, not end-to-end LibreOffice preflight or conversion.
"""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import random
import statistics
import subprocess
import sys
import tempfile
import time
import types
import zipfile

BASE_REF = '266a33a161fd5348a7d1dcf89bf27ec6d4ef6cee'
BASE_BLOB = '6907d72b77b79dafcffa55e8e45c4624d9f2cf9c'
SCANNER = 'app_backend/converter_guard/source_contract.py'
ROOT = Path(__file__).resolve().parents[2]


def blob_id(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


def load_scanner(name, content):
    # Namespace isolation only; no scanner or error implementation is mocked.
    package = '_aio_scan_benchmark'
    if package not in sys.modules:
        module = types.ModuleType(package)
        module.__path__ = [str(ROOT / 'app_backend/converter_guard')]
        sys.modules[package] = module
    module = types.ModuleType(package + '.' + name)
    module.__package__ = package
    module.__file__ = str(ROOT / SCANNER)
    sys.modules[module.__name__] = module
    exec(compile(content, module.__file__, 'exec'), module.__dict__)
    return module


def differential_paths(before, after):
    rng = random.Random(20261026)
    values = [None, 1, '', '/', '//', '.', '..', 'a..', '/a/./b', '/a/../b',
              '/foo//bar', '/%C3%A9.xml', '/é.xml', '/İ.xml', '/A.xml',
              '/a%20b.xml', '/%C0%AF.xml', '/a:b.xml', 'a:b.xml']
    alphabet = 'abCZ019-._/%?\\#:+;@!()[]\x00\x09\x20\x7f\u00e9\u0130'
    values += [''.join(rng.choices(alphabet, k=rng.randrange(1, 60))) for _ in range(20000)]
    values += ['/' + '/'.join(''.join(rng.choices('abcABC012._-', k=rng.randrange(1, 20)))
                              for _ in range(rng.randrange(1, 8))) for _ in range(10000)]
    def outcome(fn, value, relationship):
        try:
            return ('ok', fn(value, relationship=relationship))
        except (ValueError, UnicodeError, TypeError) as error:
            return ('error', type(error).__name__)
    for relationship in (False, True):
        for value in values:
            old = outcome(before._opc_part_key, value, relationship)
            new = outcome(after._opc_part_key, value, relationship)
            if old != new:
                raise AssertionError((value, relationship, old, new))
    return {'cases': len(values) * 2, 'seed': 20261026, 'mismatches': 0}


def make_cases(folder, scanner):
    cases = []
    def raw(name, data):
        path = folder / name
        path.write_bytes(data)
        cases.append((name, path))
    def packed(name, entries):
        path = folder / name
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for key, value in entries:
                archive.writestr(key, value)
        cases.append((name, path))
    odf = (f'<o:document-content xmlns:o="{scanner.OFFICE}"><o:body><o:text>'
           '<p>Hello</p></o:text></o:body></o:document-content>')
    raw('text-small.txt', b'Plain text\n' * 10)
    raw('text-1MiB.txt', b'x' * (1024 * 1024))
    raw('flat-odf-small.fodt',
        (f'<o:document xmlns:o="{scanner.OFFICE}" o:mimetype="application/vnd.oasis.opendocument.text">'
         '<o:body><o:text><p>Hello</p></o:text></o:body></o:document>').encode())
    packed('odf-small.odt', [('mimetype', 'application/vnd.oasis.opendocument.text'), ('content.xml', odf)])
    large = odf.replace('<p>Hello</p>', '<p>Hello</p>' * 100000)
    packed('odf-large.odt', [('mimetype', 'application/vnd.oasis.opendocument.text'), ('content.xml', large)])
    raw('flat-odf-large.fodt', (folder / 'flat-odf-small.fodt').read_bytes().replace(
        b'<p>Hello</p>', b'<p>Hello</p>' * 100000))
    ct = 'http://schemas.openxmlformats.org/package/2006/content-types'
    rel = 'http://schemas.openxmlformats.org/package/2006/relationships'
    office_rel = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument'
    for name, family, local, kind, member, extra in [
            ('docx-small.docx', 'wordprocessingml', 'document', 'document', 'word/document.xml', 0),
            ('xlsx-small.xlsx', 'spreadsheetml', 'workbook', 'sheet', 'xl/workbook.xml', 0),
            ('pptx-small.pptx', 'presentationml', 'presentation', 'presentation', 'ppt/presentation.xml', 0),
            ('xlsx-2000-parts.xlsx', 'spreadsheetml', 'workbook', 'sheet', 'xl/workbook.xml', 2000),
            ('pptx-3000-parts.pptx', 'presentationml', 'presentation', 'presentation', 'ppt/presentation.xml', 3000)]:
        packed(name, [('[Content_Types].xml', f'<Types xmlns="{ct}"><Override PartName="/{member}" '
                       f'ContentType="application/vnd.openxmlformats-officedocument.{family}.{kind}.main+xml"/></Types>'),
                      ('_rels/.rels', f'<Relationships xmlns="{rel}"><Relationship Id="r1" Type="{office_rel}" '
                       f'Target="{member}"/></Relationships>'),
                      (member, f'<m:{local} xmlns:m="http://schemas.openxmlformats.org/{family}/2006/main"/>'),
                      *[(f'media/item{index}.png', b'not inspected') for index in range(extra)]])
    return cases


def benchmark(before, after, cases, rounds, iterations):
    result = []
    for name, source in cases:
        old, new = before.inspect_source_contract(source), after.inspect_source_contract(source)
        if old != new:
            raise AssertionError(('benchmark must compare equal verdicts', name, old, new))
        # Exclude module imports, ZIP creation and hashing from timed regions.
        samples = [[], []]
        functions = (before.inspect_source_contract, after.inspect_source_contract)
        for fn in functions:
            for _ in range(5):
                fn(source)
        for run in range(rounds):
            for index in ((0, 1) if run % 2 == 0 else (1, 0)):
                fn = functions[index]
                start = time.perf_counter_ns()
                for _ in range(iterations):
                    fn(source)
                samples[index].append((time.perf_counter_ns() - start) / iterations / 1000)
        medians = [statistics.median(values) for values in samples]
        result.append({'case': name, 'source_bytes': source.stat().st_size,
                       'baseline_median_us': round(medians[0], 3),
                       'candidate_median_us': round(medians[1], 3),
                       'delta_percent': round((medians[1] / medians[0] - 1) * 100, 2),
                       'baseline_round_means_us': [round(v, 3) for v in samples[0]],
                       'candidate_round_means_us': [round(v, 3) for v in samples[1]]})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-file', type=Path)
    parser.add_argument('--base-ref', default=BASE_REF)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--rounds', type=int, default=11)
    parser.add_argument('--iterations', type=int, default=40)
    args = parser.parse_args()
    if args.rounds < 3 or args.iterations < 1:
        parser.error('Use at least 3 rounds and 1 iteration.')
    baseline = (args.baseline_file.read_bytes() if args.baseline_file else
                subprocess.check_output(['git', 'show', args.base_ref + ':' + SCANNER], cwd=ROOT))
    if blob_id(baseline) != BASE_BLOB:
        raise SystemExit('Baseline blob mismatch; compare with the reviewed V21 scanner.')
    candidate = (ROOT / SCANNER).read_bytes()
    before, after = load_scanner('baseline', baseline), load_scanner('candidate', candidate)
    report = {'scope': 'structural source scan only; warm filesystem cache; generated fixtures',
              'platform': platform.platform(), 'python': sys.version,
              'baseline_ref': args.base_ref, 'baseline_blob': blob_id(baseline),
              'candidate_blob': blob_id(candidate), 'rounds': args.rounds,
              'iterations_per_round': args.iterations,
              'differential_paths': differential_paths(before, after)}
    with tempfile.TemporaryDirectory(prefix='aio-source-bench-') as folder:
        report['cases'] = benchmark(before, after, make_cases(Path(folder), after),
                                    args.rounds, args.iterations)
    report['equal_weight_corpus_delta_percent'] = round((
        sum(row['candidate_median_us'] for row in report['cases']) /
        sum(row['baseline_median_us'] for row in report['cases']) - 1) * 100, 2)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    for row in report['cases']:
        print(f"{row['case']}: {row['baseline_median_us']:.3f} -> "
              f"{row['candidate_median_us']:.3f} us ({row['delta_percent']:+.2f}%)")
    print('DIFFERENTIAL:', report['differential_paths'])
    print('CORPUS_DELTA:', report['equal_weight_corpus_delta_percent'], '%')


if __name__ == '__main__':
    main()
