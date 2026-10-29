"""Native multi-page EMZ/WMZ contracts, not the historical FULLTEST corpus."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import sys
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.qa.fulltest_vector_pages_native import fixture, inspect_page, digest
from tools.qa.vector_page_fixture import write_pptx_fixture


def main():
    from app_backend.converter_guard import LibreOfficeAdapter
    from app_backend.converter_guard import adapter as adapter_module
    from PySide6.QtGui import QGuiApplication
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--expect-broken', action='store_true', help='Isolated exact-base reproduction only')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    gui = QGuiApplication.instance() or QGuiApplication([])
    cancel = threading.Event()
    adapter = LibreOfficeAdapter(args.executable)
    original_wrapper = adapter_module.wrap_native_metafiles
    original_plain = {}
    def observe_plain(paths, target, **kwargs):
        # QA records the actual native bytes before wrapping. No source/result
        # repair or alternate exporter is inserted; call the product function.
        plain = {'emz': 'emf', 'wmz': 'wmf'}[target]
        original_plain.clear()
        original_plain.update({p.with_suffix('.' + target).name: digest(p)
                               for p in paths if p.suffix.lower() == '.' + plain})
        return original_wrapper(paths, target, **kwargs)
    counter = 0
    def proof(path):
        nonlocal counter
        counter += 1
        value = adapter.preflight(path, args.output / ('probe-%d' % counter), cancel=cancel, timeout=90)
        assert value.get('state') == 'detected', value
        return value
    def convert(path, target, evidence):
        nonlocal counter
        counter += 1
        adapter.prepare_source(evidence)
        return adapter.convert(path, target, args.output / ('task-%d-%s' % (counter, target)),
                               cancel=cancel, timeout=120, on_started=lambda: None)
    cases, originals = [], {}
    started = time.monotonic()
    try:
        sources = []
        for kind in ('fodp', 'fodg'):
            p = args.output / ('three-pages.' + kind)
            p.write_text(fixture(kind), encoding='utf-8')
            sources.append(p)
        with adapter.batch(args.output / 'office'):
            p = sources[0]
            sources.append(next(x for x in convert(p, 'ppt', proof(p)) if x.suffix == '.ppt'))
            p = args.output / 'independent.pptx'
            write_pptx_fixture(p)
            sources.append(p)
            originals = {p: digest(p) for p in sources}
            with patch.object(adapter_module, 'wrap_native_metafiles', side_effect=observe_plain):
                for source in sources:
                    evidence = proof(source)
                    for target, plain in (('emz', 'emf'), ('wmz', 'wmf')):
                        tick = time.monotonic()
                        paths = convert(source, target, evidence)
                        outputs = [p for p in paths if p.suffix.lower() == '.' + target]
                        assert len(outputs) == 3, [str(p) for p in paths]
                        manifests = [p for p in paths if p.name == 'AIO-pages.json']
                        assert len(manifests) == 1, paths
                        listing = json.loads(manifests[0].read_text(encoding='utf-8'))
                        assert listing['pageCount'] == 3 and len(listing['pages']) == 3, listing
                        assert [r['page'] for r in listing['pages']] == [1, 2, 3], listing
                        by_name = {p.name: p for p in outputs}
                        missing = sum(r['file'] not in by_name for r in listing['pages'])
                        checks = adapter.export_info().get('resultChecks', [])
                        receipts = [r for r in checks if r.get('check') == 'native_preservation_contract']
                        wrappers = [r for r in checks if r.get('check') == 'native_metafile_gzip']
                        assert len(wrappers) == 1 and wrappers[0]['files'] == 3, checks
                        assert wrappers[0]['status'] == 'byte_preserved', wrappers
                        assert adapter.execution_mode() == 'native_uno', adapter.execution_mode()
                        assert set(original_plain) == set(by_name), original_plain
                        markers = []
                        for index, row in enumerate(listing['pages']):
                            # Baseline reproduction may bind only by the old
                            # suffix. Acceptance NEVER repairs missing names.
                            name = (Path(row['file']).with_suffix('.' + target).name
                                    if args.expect_broken else row['file'])
                            assert name in by_name, ('page_manifest_dangling', row)
                            raw = gzip.decompress(by_name[name].read_bytes())
                            assert hashlib.sha256(raw).hexdigest() == original_plain[name], name
                            inspected = args.output / ('inspect-%d-%d.%s' % (counter, index, plain))
                            inspected.write_bytes(raw)  # Independent QA-only decompression.
                            markers.append(inspect_page(inspected, index))
                        if args.expect_broken:
                            assert missing == 3 and not receipts, (missing, receipts)
                        else:
                            assert missing == 0 and len(receipts) == 1, (missing, receipts)
                            r = receipts[0]
                            assert r['expectedUnits'] == r['observedOutputs'] == 3, r
                            assert r['unit'] == 'pages' and r['qualityCompared'] is False, r
                            assert wrappers[0]['pageManifest']['pageCount'] == 3, wrappers
                        assert digest(source) == originals[source], source
                        row = {'sourceType': source.suffix, 'target': target, 'pages': 3,
                               'status': 'REPRODUCED_DEFECT' if args.expect_broken else 'PASS',
                               'danglingManifestReferences': missing, 'pageReceiptRetained': len(receipts) == 1,
                               'nativeBytesPreserved': True, 'markers': markers, 'sourceUnchanged': True,
                               'ms': round((time.monotonic() - tick) * 1000)}
                        cases.append(row)
                        print('COMPRESSED_PAGE_CASE=' + json.dumps(row), flush=True)
        assert len(cases) == 8, cases
        assert all(digest(p) == sha for p, sha in originals.items()), 'source changed'
        result = {'status': 'REPRODUCED_DEFECT' if args.expect_broken else 'PASS',
                  'scope': 'eight synthetic native EMZ/WMZ conversions; not historical FULLTEST or full visual QA',
                  'cases': cases, 'sourceUnchanged': True,
                  'elapsedSeconds': round(time.monotonic() - started, 3)}
    except Exception as error:
        result = {'status': 'FAIL', 'error': str(error), 'code': getattr(error, 'code', None),
                  'diagnostic': getattr(error, 'diagnostic', None), 'cases': cases}
        (args.output / 'evidence.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print('COMPRESSED_PAGES_NATIVE=' + json.dumps(result), flush=True)
        raise
    (args.output / 'evidence.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print('COMPRESSED_PAGES_NATIVE=' + json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
