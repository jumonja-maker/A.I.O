"""Primary-route survey of the exact user-supplied 178-source corpus.

Every source is hashed before and after the real adapter. PDF content checks
use independent source XML/plain-text parsing and pypdf readback. These checks
cover literal text, not visual equivalence, fonts, charts or formula semantics.
Unavailable imports/routes remain explicit gaps, never successful conversions.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import platform
import re
import sys
import subprocess
import threading
import time
import unicodedata
from zipfile import ZipFile, is_zipfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
A = '{http://schemas.openxmlformats.org/drawingml/2006/main}'
T = '{urn:oasis:names:tc:opendocument:xmlns:text:1.0}'
O = '{urn:oasis:names:tc:opendocument:xmlns:office:1.0}'


def odf_body_paragraphs(root):
    body = root.find(O+'body')
    return [''.join(p.itertext()) for p in body.iter(T+'p')] if body is not None else []


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def compact(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFC', text))


def expected_text(path):
    """Return independently readable literal paragraphs with their scope."""
    ext = path.suffix.lower()
    if ext == '.txt':
        return path.read_text(encoding='utf-8-sig').splitlines(), 'UTF-8 literal lines'
    if is_zipfile(path):
        with ZipFile(path) as z:
            if 'word/document.xml' in z.namelist():
                root = ET.fromstring(z.read('word/document.xml'))
                return [''.join(p.itertext()) for p in root.iter(W+'p')], 'OOXML body paragraphs'
            slides = sorted((n for n in z.namelist() if re.fullmatch(r'ppt/slides/slide\d+\.xml', n)),
                            key=lambda n: int(re.search(r'(\d+)\.xml$', n)[1]))
            if slides:
                return [ ''.join(t.text or '' for t in p.iter(A+'t'))
                         for n in slides for p in ET.fromstring(z.read(n)).iter(A+'p')], 'OOXML slide paragraphs'
            if 'content.xml' in z.namelist():
                root = ET.fromstring(z.read('content.xml'))
                return odf_body_paragraphs(root), 'ODF body text paragraphs'
    if ext in ('.fodt', '.fodg', '.fodp', '.fods'):
        root = ET.parse(path).getroot()
        return odf_body_paragraphs(root), 'flat ODF body text paragraphs'
    return [], 'no independent literal parser in this survey'


def inspect_pdf(path, paragraphs):
    from pypdf import PdfReader
    from tools.qa.corpus_pdf_text_oracle import inspect_pdf_literals
    reader = PdfReader(path)
    pages = reader.pages
    raw = '\n'.join(p.extract_text() or '' for p in pages)
    text = compact(raw)
    expected = Counter(compact(p) for p in paragraphs if compact(p))
    # Distinct literal paragraphs are checked independently; duplicate counts
    # are recorded but not certified when one paragraph contains another.
    missing = [p for p in expected if p not in text]
    positioned = inspect_pdf_literals(path, paragraphs)
    positioned_status = positioned.get('status')
    return {'pages': len(pages), 'pageSizesPoints': [list(map(float, (p.mediabox.width, p.mediabox.height))) for p in pages],
            'textCharacters': len(raw), 'independentDistinctLiterals': len(expected),
            'missingLiteralCount': len(missing), 'missingLiterals': missing[:50],
            'duplicateOccurrenceCountsCertified': False,
            'visualEquivalenceChecked': False,
            'streamLiteralStatus': 'MISSING' if missing else 'PASS' if expected else 'NOT_CHECKED',
            'positionedLiteralReadback': {k:v for k,v in positioned.items() if k != 'positionedPages'},
            'literalTextStatus': positioned_status}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--provenance', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--executable', type=Path, required=True)
    parser.add_argument('--extensions', default='', help='Optional comma-separated diagnostic subset')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    from PySide6.QtGui import QGuiApplication
    from app_backend.converter_guard import LibreOfficeAdapter
    app = QGuiApplication.instance() or QGuiApplication([])
    manifest = json.loads(args.provenance.read_text(encoding='utf-8'))
    files = manifest['files']
    assert len(files) == 178 and len({row['path'] for row in files}) == 178
    originals = {}
    for row in files:
        rel = Path(row['path'])
        assert not rel.is_absolute() and '..' not in rel.parts
        path = args.input / rel
        assert path.is_file() and not path.is_symlink()
        assert path.stat().st_size == row['bytes'] and digest(path) == row['sha256'], row
        originals[path] = row['sha256']
    selected = {'.'+s.strip().lstrip('.') for s in args.extensions.split(',') if s.strip()}
    report = {'schema': 1, 'sourceSha': __import__('os').environ.get('AIO_QA_SOURCE_SHA'),
              'platform': platform.platform(), 'engine': str(args.executable),
              'archiveSha256': manifest['archiveSha256'], 'corpusFiles': len(files),
              'scope': 'all primary PDF routes; independent literal text subset, no visual certification',
              'selectedExtensions': sorted(selected), 'cases': []}
    adapter = LibreOfficeAdapter(args.executable)
    if not adapter.available:
        raise RuntimeError('Required native engine is unavailable')
    observed_sha = subprocess.run(['git','rev-parse','HEAD'],cwd=ROOT,check=True,capture_output=True,text=True).stdout.strip()
    if report['sourceSha'] is not None and report['sourceSha'] != observed_sha:
        raise RuntimeError('Expected source SHA does not match checkout')
    report['sourceSha'] = observed_sha
    report['trackedTreeDirty'] = bool(subprocess.run(['git','status','--porcelain','--untracked-files=no'],cwd=ROOT,
                                                  check=True,capture_output=True,text=True).stdout.strip())
    expected_cases = sum(not selected or p.suffix.lower() in selected for p in originals)
    if not expected_cases:
        raise ValueError('Selected subset contains no corpus files')
    report['expectedCases'] = expected_cases
    report['completed'] = False
    cancel = threading.Event()
    try:
        with adapter.batch(args.output / 'office'):
            for index, (source, before) in enumerate(originals.items()):
                if selected and source.suffix.lower() not in selected:
                    continue
                tick = time.monotonic()
                row = {'source': source.name, 'sourceSha256': before, 'target': 'pdf'}
                try:
                    proof = adapter.preflight(source, args.output / ('probe-%03d' % index), cancel=cancel, timeout=120)
                    row['proof'] = {k: proof.get(k) for k in ('state','code','basis','modelLoaded','documentServices','selectedImportFilter','diagnosticStage','nativeImportMs','nativePreflightCode','mediaProfile','qtMediaTargets','sourceContract')}
                    row['route'] = adapter.route_decision('pdf', proof)
                    if proof.get('state') != 'detected':
                        row['status'] = 'IMPORT_UNAVAILABLE'
                    elif row['route'] != 'registered':
                        row['status'] = 'ROUTE_UNAVAILABLE'
                    else:
                        adapter.prepare_source(proof)
                        paths = adapter.convert(source, 'pdf', args.output / ('task-%03d' % index), cancel=cancel,
                                                timeout=180, on_started=lambda: None)
                        row['mode'] = adapter.execution_mode()
                        row['exportInfo'] = adapter.export_info()
                        pdfs = [p for p in paths if p.suffix.lower() == '.pdf']
                        assert len(pdfs) == 1, [str(p) for p in paths]
                        paragraphs, scope = expected_text(source)
                        row['sourceLiteralScope'] = scope
                        row['readback'] = inspect_pdf(pdfs[0], paragraphs)
                        row['outputSha256'] = digest(pdfs[0])
                        row['outputBytes'] = pdfs[0].stat().st_size
                        # A PDF text extractor can reorder separate Cyrillic and
                        # Latin font runs. A missing paragraph is uncertainty,
                        # not proof of converter loss, until corroborated.
                        literal_status = row['readback']['literalTextStatus']
                        row['status'] = ('PRINT_LAYOUT_TEXT_OMITTED'
                            if literal_status == 'MISSING' and 'com.sun.star.sheet.SpreadsheetDocument' in proof.get('documentServices', [])
                            else 'TEXT_READBACK_UNCERTAIN' if literal_status in ('MISSING','UNCERTAIN') else 'CONVERTED')
                except Exception as error:
                    row.update(status='FAIL', error=repr(error), code=getattr(error, 'code', None),
                               diagnostic=getattr(error, 'diagnostic', None))
                row['sourceUnchanged'] = digest(source) == before
                row['seconds'] = round(time.monotonic()-tick, 3)
                if not row['sourceUnchanged']:
                    row['status'] = 'FAIL_SOURCE_MUTATED'
                report['cases'].append(row)
                (args.output/'evidence.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
                print('CURRENT178_CASE='+json.dumps(row, ensure_ascii=True), flush=True)
            report['completed'] = len(report['cases']) == expected_cases
    finally:
        report['allSourceHashesUnchanged'] = all(digest(p) == h for p, h in originals.items())
        report['counts'] = dict(Counter(r['status'] for r in report['cases']))
        report['literalReadbackCounts'] = dict(Counter(r.get('readback', {}).get('literalTextStatus','NOT_RUN') for r in report['cases']))
        report['status'] = 'FAIL' if not report['completed'] or any(r['status'].startswith('FAIL') for r in report['cases']) or not report['allSourceHashesUnchanged'] else 'SURVEY_COMPLETE_WITH_GAPS' if any(r['status'] != 'CONVERTED' or r.get('readback',{}).get('literalTextStatus') != 'PASS' for r in report['cases']) else 'PASS'
        (args.output/'evidence.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print('CURRENT178_SUMMARY='+json.dumps({k:v for k,v in report.items() if k!='cases'}, ensure_ascii=True), flush=True)
    return 1 if report['status'] == 'FAIL' else 0


if __name__ == '__main__':
    raise SystemExit(main())
