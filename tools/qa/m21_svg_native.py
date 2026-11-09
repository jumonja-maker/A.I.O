"""M21 native 4-slide SVG acceptance on the pinned Windows LibreOffice.

Uses synthetic source inspired by original FULLTEST page count, never uploads
user documents or claims original 1079 deviations have been re-audited.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import threading
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture():
    pages = ''.join(
        '<draw:page draw:name="Slide%d"><draw:frame svg:x="1cm" svg:y="1cm" '
        'svg:width="12cm" svg:height="3cm"><draw:text-box>'
        '<text:p>AIO_SVG_PAGE_%d_UNIQUE</text:p></draw:text-box></draw:frame>'
        '</draw:page>' % (n, n) for n in range(1, 5))
    return ('<?xml version="1.0" encoding="UTF-8"?>'
        '<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
        'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" '
        'office:mimetype="application/vnd.oasis.opendocument.presentation" office:version="1.2">'
        '<office:body><office:presentation>' + pages +
        '</office:presentation></office:body></office:document>')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    from PySide6.QtGui import QGuiApplication
    from app_backend.converter_guard import LibreOfficeAdapter
    app = QGuiApplication.instance() or QGuiApplication([])
    source = args.output / 'four-distinct-slides.fodp'
    source.write_text(fixture(), encoding='utf-8')
    original_sha = sha(source)
    report = {'status': 'NOT_RUN', 'scope': 'four synthetic native SVG pages, not original FULLTEST'}
    try:
        adapter = LibreOfficeAdapter(args.executable)
        with adapter.batch(args.output / 'office'):
            proof = adapter.preflight(source, args.output / 'preflight',
                                      cancel=threading.Event(), timeout=90)
            assert proof.get('state') == 'detected', proof
            assert adapter.route_decision('svg', proof) == 'registered', proof
            adapter.prepare_source(proof)
            produced = adapter.convert(source, 'svg', args.output / 'task',
                                       cancel=threading.Event(), timeout=150,
                                       on_started=lambda: None)
            page_files = [p for p in produced if p.suffix.lower() == '.svg']
            manifest_files = [p for p in produced if p.name == 'AIO-pages.json']
            assert len(page_files) == 4 and len(manifest_files) == 1, produced
            listing = json.loads(manifest_files[0].read_text(encoding='utf-8'))
            assert listing['pageCount'] == 4
            assert [row['page'] for row in listing['pages']] == [1, 2, 3, 4]
            mapping = {p.name: p for p in page_files}
            markers = ['AIO_SVG_PAGE_%d_UNIQUE' % n for n in range(1, 5)]
            observations = []
            for index, row in enumerate(listing['pages']):
                path = mapping[row['file']]
                root = ET.parse(path).getroot()
                assert root.tag == '{http://www.w3.org/2000/svg}svg'
                text = ''.join(root.itertext())
                present = [marker in text for marker in markers]
                assert present == [i == index for i in range(4)], (row, present)
                observations.append({'page': index + 1, 'name': path.name,
                                     'markerExclusive': True,
                                     'bytes': path.stat().st_size})
            info = adapter.export_info()
            receipts = [r for r in info.get('resultChecks', ())
                        if r.get('check') == 'svg_page_manifest']
            assert len(receipts) == 1, info
            assert receipts[0]['status'] == 'checked_subset', receipts
            assert receipts[0]['expectedUnits'] == receipts[0]['observedOutputs'] == 4
            assert receipts[0]['qualityCompared'] is False
            native_count = [r for r in info.get('resultChecks', ())
                            if r.get('check') == 'native_preservation_contract']
            assert len(native_count) == 1, native_count
            assert native_count[0]['expectedUnits'] == 4, native_count
            assert sha(source) == original_sha, 'native exporter modified the source'
            report = {'status': 'PASS', 'pages': observations, 'sourceUnchanged': True,
                      'checks': receipts + native_count, 'qualityCompared': False,
                      'scope': 'synthetic Windows LibreOffice native 4-slide SVG; original FULLTEST NOT_RUN'}
    except Exception as exc:
        report = {'status': 'FAIL', 'error': type(exc).__name__,
                  'reason': str(exc), 'scope': 'native four-slide SVG acceptance'}
        raise
    finally:
        (args.output / 'evidence.json').write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print('M21_SVG_NATIVE=' + json.dumps(report, ensure_ascii=True), flush=True)


if __name__ == '__main__':
    main()
