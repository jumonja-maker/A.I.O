"""Explicit, bounded Guard v16 acceptance and native positive-fixture generation.

Run manually with an installed LibreOffice executable. Generated OOXML and ODF
positives come from native exports and a second, direct native PDF import. User
fixtures stay unchanged; malformed fixtures are evidence, never repaired in place.
This command does not certify Windows GUI, macros or a complete FULLTEST.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app_backend.converter_guard import ConverterGuard, GuardError, GuardLimits, LibreOfficeAdapter
from app_backend.converter_guard.adapter import _RunControl, prepare_profile
from app_backend.converter_guard.routes import registry
from app_backend.libreoffice_engine import LibreOfficeError, convert_path

OFFICE = 'urn:oasis:names:tc:opendocument:xmlns:office:1.0'
TABLE = 'urn:oasis:names:tc:opendocument:xmlns:table:1.0'
TEXT = 'urn:oasis:names:tc:opendocument:xmlns:text:1.0'
DRAW = 'urn:oasis:names:tc:opendocument:xmlns:drawing:1.0'
WORD = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
MARKERS = tuple('AIO_V16_R%dC%d' % (r, c) for r in range(12) for c in range(4))
SLIDE_MARKER = 'AIO_V16_PRESENTATION'
WRITER = 'com.sun.star.text.TextDocument'
IMPRESS = 'com.sun.star.presentation.PresentationDocument'


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while block := stream.read(64 * 1024):
            h.update(block)
    return h.hexdigest()


def writer_seed():
    rows = ''.join('<table:table-row>' + ''.join(
        '<table:table-cell office:value-type="string"><text:p>%s</text:p></table:table-cell>'
        % MARKERS[r * 4 + c] for c in range(4)) + '</table:table-row>' for r in range(12))
    return ('<?xml version="1.0" encoding="UTF-8"?>'
        '<office:document xmlns:office="%s" xmlns:table="%s" xmlns:text="%s" '
        'office:version="1.3" office:mimetype="application/vnd.oasis.opendocument.text">'
        '<office:body><office:text><text:p>AIO v16 explicit four-column control</text:p>'
        '<table:table table:name="Control"><table:table-column table:number-columns-repeated="4"/>'
        '%s</table:table></office:text></office:body></office:document>' % (OFFICE, TABLE, TEXT, rows))


def presentation_seed():
    return ('<?xml version="1.0" encoding="UTF-8"?>'
        '<office:document xmlns:office="%s" xmlns:draw="%s" xmlns:text="%s" '
        'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" '
        'office:version="1.3" office:mimetype="application/vnd.oasis.opendocument.presentation">'
        '<office:body><office:presentation><draw:page draw:name="Control">'
        '<draw:frame svg:x="1cm" svg:y="1cm" svg:width="20cm" svg:height="3cm">'
        '<draw:text-box><text:p>%s</text:p></draw:text-box></draw:frame>'
        '</draw:page></office:presentation></office:body></office:document>'
        % (OFFICE, DRAW, TEXT, SLIDE_MARKER))


def fixture_facts(path):
    """Independent facts only; do not infer macro retention from a file suffix."""
    path = Path(path)
    facts = {'name': path.name, 'size': path.stat().st_size, 'sha256': digest(path)}
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            bad = archive.testzip()
            if bad:
                raise ValueError('ZIP CRC failed: ' + bad)
            facts['zipCRC'] = 'pass'
            names = archive.namelist()
            facts['hasVbaProject'] = any(n.lower().endswith('/vbaproject.bin') for n in names)
            if 'mimetype' in names:
                facts['mimetype'] = archive.read('mimetype').decode('ascii')
            if 'content.xml' in names:
                root = ET.fromstring(archive.read('content.xml'))
                facts['contentNamespace'] = root.tag.split('}')[0].lstrip('{')
                facts['tableCells'] = sum(1 for _ in root.iter('{%s}table-cell' % TABLE))
                facts['tableColumnDeclarations'] = sum(1 for _ in root.iter('{%s}table-column' % TABLE))
            if 'word/document.xml' in names:
                root = ET.fromstring(archive.read('word/document.xml'))
                facts['tableCells'] = sum(1 for _ in root.iter('{%s}tc' % WORD))
            if '[Content_Types].xml' in names:
                root = ET.fromstring(archive.read('[Content_Types].xml'))
                facts['mainContentTypes'] = {e.get('PartName'): e.get('ContentType')
                    for e in root if e.get('PartName') in
                    ('/word/document.xml', '/ppt/presentation.xml', '/xl/workbook.xml')}
    elif path.suffix.lower() in ('.fodt', '.fodp'):
        root = ET.parse(path).getroot()
        facts['mimetype'] = root.get('{%s}mimetype' % OFFICE)
        facts['bodyKinds'] = [e.tag.rsplit('}', 1)[-1] for b in root.findall('{%s}body' % OFFICE) for e in b]
    return facts


def output_text(path):
    path = Path(path)
    if path.suffix.lower() == '.pdf':
        from pypdf import PdfReader
        return '\n'.join(p.extract_text() or '' for p in PdfReader(path).pages)
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            member = 'word/document.xml' if 'word/document.xml' in archive.namelist() else 'content.xml'
            if member in archive.namelist():
                return ''.join(ET.fromstring(archive.read(member)).itertext())
        return ''
    return path.read_text(encoding='utf-8-sig', errors='strict')


def check_markers(path, markers):
    text = output_text(path)
    missing = [m for m in markers if m not in text]
    return {'status': 'pass' if not missing else 'fail', 'expectedMarkers': len(markers),
            'missingMarkers': missing, 'method': 'independent_content_extraction'}


def native_export(source, target, work, executable, seconds, filter_name=None):
    prepare_profile(work / 'profile')
    return convert_path(source, target, work, executable=executable,
        cancel=_RunControl(threading.Event(), work, seconds, None),
        convert_spec=(target + ':' + filter_name) if filter_name else None)


def fixture_export_filter(native, target, service):
    name = native.export_filter(target, [service])
    if name:
        return name
    # A fixture producer may explicitly choose between two installed OOXML
    # dialects. This does not change Guard routing or infer a filter by suffix.
    standard = {'dotx': 'Office Open XML Text Template',
                'potx': 'Impress MS PowerPoint 2007 XML Template',
                'ppsx': 'Impress MS PowerPoint 2007 XML AutoPlay'}.get(target)
    item = native.filters.get(standard)
    return standard if item and item[0] == service and target in item[1] and 'EXPORT' in item[2] else None


def positive_container_check(path, facts):
    expected_mime = {'odt': 'application/vnd.oasis.opendocument.text',
        'fodt': 'application/vnd.oasis.opendocument.text',
        'odp': 'application/vnd.oasis.opendocument.presentation',
        'fodp': 'application/vnd.oasis.opendocument.presentation'}
    expected_main = {
        'docx': ('/word/document.xml', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml'),
        'dotx': ('/word/document.xml', 'application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml'),
        'pptx': ('/ppt/presentation.xml', 'application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml'),
        'potx': ('/ppt/presentation.xml', 'application/vnd.openxmlformats-officedocument.presentationml.template.main+xml'),
        'ppsx': ('/ppt/presentation.xml', 'application/vnd.openxmlformats-officedocument.presentationml.slideshow.main+xml')}
    suffix = Path(path).suffix.lower().lstrip('.')
    if suffix in expected_mime and facts.get('mimetype') != expected_mime[suffix]:
        raise ValueError('Native fixture MIME does not match requested format')
    if suffix in expected_main:
        part, content_type = expected_main[suffix]
        if facts.get('mainContentTypes', {}).get(part) != content_type:
            raise ValueError('Native fixture main Content-Type does not match requested format')


def generate_positive_fixtures(root, executable, seconds):
    """Return positives only after native export and direct native re-import."""
    seeds, positives = root / 'seeds', root / 'positive'
    seeds.mkdir(); positives.mkdir()
    writer = seeds / 'writer.fodt'; writer.write_text(writer_seed(), encoding='utf-8')
    slides = seeds / 'slides.fodp'; slides.write_text(presentation_seed(), encoding='utf-8')
    native = registry(executable)
    specs = [(writer, WRITER, target, MARKERS) for target in ('odt', 'docx', 'dotx', 'fodt', 'sxw')]
    specs += [(slides, IMPRESS, target, (SLIDE_MARKER,)) for target in ('odp', 'pptx', 'potx', 'ppsx', 'fodp')]
    rows = []
    for source, service, target, markers in specs:
        row = {'target': target, 'service': service, 'producer': 'native_LibreOffice_export'}
        candidate = None
        filter_name = fixture_export_filter(native, target, service)
        row['filter'] = filter_name
        if not filter_name:
            row.update(status='not_run', reason='installed_native_export_filter_unavailable')
            rows.append(row); continue
        try:
            outputs = native_export(source, target, root / ('produce-' + target), executable, seconds, filter_name)
            generated = next(p for p in outputs if p.suffix.lower() == '.' + target)
            candidate = positives / (source.stem + '.' + target)
            shutil.copyfile(generated, candidate)
            row['fixture'] = fixture_facts(candidate)
            positive_container_check(candidate, row['fixture'])
            pdf_filter = native.export_filter('pdf', [service])
            proof_outputs = native_export(candidate, 'pdf', root / ('direct-reopen-' + target),
                                          executable, seconds, pdf_filter)
            pdf = next(p for p in proof_outputs if p.suffix.lower() == '.pdf')
            row['directNativeReimport'] = check_markers(pdf, markers)
            row['status'] = 'pass' if row['directNativeReimport']['status'] == 'pass' else 'fail'
        except (LibreOfficeError, GuardError, ValueError, OSError, StopIteration) as error:
            row.update(status='fail', code=getattr(error, 'code', type(error).__name__))
        # Container or roundtrip failures cannot enter the positive corpus.
        if candidate is not None and candidate.is_file() and row['status'] != 'pass':
            rejected = root / 'rejected-generated'; rejected.mkdir(exist_ok=True)
            candidate.rename(rejected / candidate.name)
        rows.append(row)
    return rows


def classify_originals(corpus):
    """Inventory originals; suspicious ODF tables stay diagnostic, not invalid."""
    rows = []
    if corpus is None:
        return rows
    for path in sorted(corpus.iterdir()):
        if not path.is_file() or path.suffix.lower() not in ('.fodt', '.fodp', '.sxw', '.odt', '.dotm'):
            continue
        facts = fixture_facts(path)
        row = {'source': facts, 'role': 'diagnostic_original', 'originalUnchanged': True}
        if path.suffix.lower() in ('.fodt', '.fodp'):
            expected = 'application/vnd.oasis.opendocument.' + ('text' if path.suffix.lower() == '.fodt' else 'presentation')
            if facts.get('mimetype') != expected:
                row.update(role='negative_inconsistent_fixture', reason='flat_xml_mimetype_inconsistent')
        if path.suffix.lower() == '.sxw' and facts.get('contentNamespace') == OFFICE:
            row.update(role='negative_inconsistent_fixture', reason='staroffice_mime_with_odf_namespaces')
        if path.suffix.lower() == '.odt' and facts.get('tableCells', 0) and not facts.get('tableColumnDeclarations'):
            row['warning'] = 'table_columns_undeclared_direct_native_loss_requires_content_check'
        if path.suffix.lower() == '.dotm' and not facts.get('hasVbaProject'):
            row['warning'] = 'cannot_test_VBA_retention_without_actual_VBA'
        rows.append(row)
    return rows


def guard_acceptance(root, executable, seconds, corpus=None):
    wanted = [('writer.odt', ['docx', 'txt'], MARKERS), ('slides.fodp', ['pdf'], (SLIDE_MARKER,))]
    selected = [(root / 'positive' / name, targets, markers) for name, targets, markers in wanted
                if (root / 'positive' / name).is_file()]
    if corpus is not None:
        # These are original FULLTEST samples, not synthetic replacements. The
        # identity path must preserve the complete admitted file byte for byte.
        for name, target in [('05_LEVEL_2.png', 'png'), ('06_LEVEL_3.gif', 'gif'),
                             ('06_LEVEL_3.apng', 'apng'), ('06_LEVEL_3.tiff', 'tiff'),
                             ('05_LEVEL_2.odc', 'odc')]:
            if (corpus / name).is_file():
                selected.append((corpus / name, [target], ()))
    if not selected:
        return {'status': 'not_run', 'reason': 'no_native_validated_positive_inputs'}
    source_hashes = {path: digest(path) for path, targets, markers in selected}
    guard = ConverterGuard(adapter=LibreOfficeAdapter(executable),
        limits=GuardLimits(preflight_timeout=seconds, file_timeout=seconds), recovery_parent=root / 'owned')
    try:
        files = [{'id': 'source%d' % i, 'name': p.name, 'size': p.stat().st_size}
                 for i, (p, targets, markers) in enumerate(selected)]
        job = guard.create(files, prepare_only=True)['jobId']
        for metadata, (path, targets, markers) in zip(files, selected):
            with path.open('rb') as stream:
                guard.upload(job, metadata['id'], stream, metadata['size'])
        inspection = guard.preflight(job)
        formats = {f['id']: f for f in inspection['formats']}
        plan, rejected = [], []
        for metadata, (path, targets, markers) in zip(files, selected):
            admitted = [t for t in targets if metadata['id'] in formats.get(t, {}).get('selectableFileIds', [])]
            source_proof = next(f['preflight'] for f in inspection['files'] if f['id'] == metadata['id'])
            rejected.extend({'source': path.name, 'target': t,
                'reason': source_proof.get('code') or
                formats.get(t, {}).get('unavailableReasons', {}).get(metadata['id']) or
                formats.get(t, {}).get('reasonCode')} for t in targets if t not in admitted)
            if admitted:
                plan.append({'fileId': metadata['id'], 'formats': admitted})
        row = {'preflight': inspection['files'], 'unplannedPairs': rejected,
               'contentChecks': [], 'identityChecks': []}
        if not plan:
            return {**row, 'status': 'not_run', 'reason': 'native_preflight_did_not_admit_routes'}
        guard.start(job, file_ids=[p['fileId'] for p in plan], plan=plan,
                    only_verified=True, archive_mode='on_demand')
        until = time.monotonic() + seconds * sum(len(p['formats']) for p in plan) + 60
        while not guard.status(job)['finished']:
            if time.monotonic() > until:
                guard.cancel(job); raise RuntimeError('QA supervisor deadline')
            time.sleep(.05)
        row['report'] = guard.report(job)
        state = guard.status(job)
        if state.get('result'):
            row['save'] = guard.save_folder(state['result']['ticket'], root, verify=True)
            saved = root / row['save']['name']
            for path in saved.rglob('*'):
                if path.is_file() and path.suffix.lower() in ('.pdf', '.docx', '.txt'):
                    markers = MARKERS if path.stem == 'writer' else (SLIDE_MARKER,)
                    row['contentChecks'].append({'path': path.relative_to(saved).as_posix(), **check_markers(path, markers)})
            manifest = json.loads((saved / 'AIO-results-manifest.json').read_text(encoding='utf-8'))
            originals = {metadata['id']: path for metadata, (path, targets, markers) in zip(files, selected)
                         if not markers}
            for item in manifest['outputs']:
                source = originals.get(item.get('fileId'))
                if source is not None:
                    result = saved / item['path']
                    actual, expected = digest(result), digest(source)
                    row['identityChecks'].append({'source': source.name, 'path': item['path'],
                        'status': 'pass' if actual == expected else 'fail',
                        'sourceSha256': expected, 'resultSha256': actual,
                        'method': 'independent_whole_file_sha256'})
        row['originalsUnchanged'] = all(digest(path) == expected for path, expected in source_hashes.items())
        row['status'] = ('fail' if row['originalsUnchanged'] is False or row['report'].get('failed') or
            any(c['status'] != 'pass' for c in row['contentChecks'] + row['identityChecks']) else
            'partial_native_coverage' if rejected else 'pass')
        return row
    finally:
        guard.close()


def negative_controls(corpus, *, work=None, executable=None, seconds=30):
    from app_backend.converter_guard.source_contract import inspect_source_contract
    from app_backend.converter_guard.postconditions import check_native_output
    rows = []
    if corpus is None:
        return rows
    for name in ('05_LEVEL_2.fodt', '05_LEVEL_2.fodp', '05_LEVEL_2.sxw'):
        path = corpus / name
        if not path.is_file():
            continue
        proof = inspect_source_contract(path)
        rows.append({'source': name, 'check': 'source_contract',
                     'status': 'pass' if proof.get('state') == 'invalid' else 'fail', 'proof': proof})
    for name, target in (('14404-emz-06_LEVEL_3.emz', 'emz'), ('14415-wmz-06_LEVEL_3.wmz', 'wmz')):
        path = corpus / name
        if not path.is_file():
            continue
        try:
            check_native_output([path], target, corpus / '06_LEVEL_3.odt', cancel=threading.Event())
            code = None
        except GuardError as error:
            code = error.code
        rows.append({'source': name, 'check': 'corrupt_gzip_rejected', 'code': code,
                     'status': 'pass' if code == 'ENGINE_OUTPUT_CONTAINER_INVALID' else 'fail'})
    original = corpus / '05_LEVEL_2.odt'
    if original.is_file() and work is not None and executable is not None:
        row = {'source': original.name, 'check': 'undeclared_odf_columns_direct_native_control',
               'expectedOutcome': 'preserve cell text or reject content loss'}
        try:
            native = registry(executable)
            outputs = native_export(original, 'docx', work, executable, seconds,
                                    native.export_filter('docx', [WRITER]))
            primary = next(p for p in outputs if p.suffix.lower() == '.docx')
            row['sourceFacts'], row['outputFacts'] = fixture_facts(original), fixture_facts(primary)
            try:
                row['postcondition'] = check_native_output(outputs, 'docx', original, cancel=threading.Event())
                row['status'] = 'pass' if row['postcondition'] is not None else 'fail'
            except GuardError as error:
                row['postconditionCode'] = error.code
                row['postconditionDiagnostic'] = error.diagnostic
                row['status'] = 'pass' if error.code == 'ENGINE_OUTPUT_CONTENT_LOSS' else 'fail'
        except (GuardError, LibreOfficeError, OSError, ValueError, StopIteration) as error:
            row.update(status='not_run', reason=getattr(error, 'code', type(error).__name__))
        rows.append(row)
    return rows


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--executable', type=Path, required=True)
    p.add_argument('--corpus', type=Path)
    p.add_argument('--seconds', type=float, default=30)
    p.add_argument('--generate-only', action='store_true')
    a = p.parse_args()
    if a.seconds <= 0 or not a.executable.is_file() or a.corpus and not a.corpus.is_dir():
        p.error('Existing native executable/corpus and positive timeout required')
    a.executable = a.executable.resolve()
    if not registry(a.executable).valid:
        p.error('Use the actual LibreOffice program executable with its installed Filter/Types registry, not a wrapper')
    a.output.mkdir(parents=True, exist_ok=False)
    version = subprocess.run([str(a.executable), '--version'], capture_output=True, text=True, timeout=20)
    originals = classify_originals(a.corpus)
    evidence = {'schema': 1, 'platform': sys.platform, 'engineVersion': version.stdout.strip(),
        'qualityCertified': False, 'windowsGuiTested': False, 'fulltest510': 'NOT_RUN',
        'macroRetention': {'status': 'NOT_RUN', 'reason': 'requires_a_fixture_with_actual_VBA'},
        'originalFixtures': originals, 'generatedFixtures': generate_positive_fixtures(a.output, a.executable, a.seconds)}
    evidence['guardAcceptance'] = ({'status': 'NOT_RUN', 'reason': 'generate_only'} if a.generate_only else
                                   guard_acceptance(a.output, a.executable, a.seconds, a.corpus))
    evidence['negativeControls'] = ([] if a.generate_only else negative_controls(a.corpus,
        work=a.output / 'negative-native-odt-control', executable=a.executable, seconds=a.seconds))
    for row in originals:
        path = a.corpus / row['source']['name']
        row['originalUnchanged'] = digest(path) == row['source']['sha256']
    (a.output / 'evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'generated': {s: sum(r['status'] == s for r in evidence['generatedFixtures'])
        for s in ('pass', 'fail', 'not_run')}, 'guard': evidence['guardAcceptance']['status'],
        'originalsUnchanged': all(r['originalUnchanged'] for r in originals)}, ensure_ascii=False), flush=True)
    return int(any(r['status'] == 'fail' for r in evidence['generatedFixtures']) or
               evidence['guardAcceptance']['status'] == 'fail' or
               any(r['status'] == 'fail' for r in evidence['negativeControls']) or
               any(not r['originalUnchanged'] for r in originals))


if __name__ == '__main__':
    sys.exit(main())
