"""M29 genuine VBA storage and Cyrillic controls, with macros never executed.

Pinned Apache POI test fixtures (Apache-2.0) are committed with provenance.
A small QA-only MS-OVBA decompressor reads the English test module sources;
the application checks opaque CFB stream bytes. No macro is run.
"""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import threading
from xml.etree import ElementTree as ET
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
REVISION = '519e0a3c3945364e8ea75c172fe1a15fb6d4a202'
PREFIX = 'https://raw.githubusercontent.com/apache/poi/' + REVISION + '/'
FIXTURES = {
    'SimpleMacro.xlsm': ('test-data/spreadsheet/SimpleMacro.xlsm',
        'f76c986f4ebc25c2cc57c088b2511a1269f4bd61d6223a2ab58db351da348ba6'),
    'SimpleMacro.xls': ('test-data/spreadsheet/SimpleMacro.xls',
        '0e92c9bb018abd8a5f9121d65827c9e3bd280777219cb77a2efd70635143c00a'),
    'SimpleMacro.docm': ('test-data/document/SimpleMacro.docm',
        'fd591958fcf5322f72c0a740e9606309c949254bda4c3d9bd966481ddf220563'),
    'SimpleMacro.doc': ('test-data/document/SimpleMacro.doc',
        '39e9608c711d38f298ed20a5bfae12fcdaed6f4be2be4d26db670475216ca393'),
}
MARKER = 'AIO_M29_Кириллица_Семён_Ёж'


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def decompress_vba(raw):
    """MS-OVBA 2.4.1, bounded QA fixture reader; never an execution engine."""
    if not raw or raw[0] != 1:
        raise ValueError('compressed VBA signature')
    out, pos = bytearray(), 1
    while pos < len(raw):
        if pos+2 > len(raw):
            raise ValueError('truncated VBA chunk')
        header = int.from_bytes(raw[pos:pos+2], 'little')
        end = pos + (header & 0xfff) + 3
        if header & 0x7000 != 0x3000 or end > len(raw):
            raise ValueError('invalid VBA chunk')
        pos += 2
        start = len(out)
        if not header & 0x8000:
            if end-pos != 4096:
                raise ValueError('invalid uncompressed VBA chunk')
            out.extend(raw[pos:end]); pos = end
        else:
            while pos < end:
                flags = raw[pos]; pos += 1
                for bit in range(8):
                    if pos >= end:
                        break
                    if flags & (1 << bit):
                        if pos+2 > end:
                            raise ValueError('truncated VBA copy token')
                        token = int.from_bytes(raw[pos:pos+2], 'little'); pos += 2
                        bits = max(4, (len(out)-start-1).bit_length())
                        mask = 0xffff >> bits
                        offset, count = (token >> (16-bits))+1, (token & mask)+3
                        if offset > len(out)-start:
                            raise ValueError('VBA copy before chunk')
                        for _ in range(count):
                            out.append(out[-offset])
                    else:
                        out.append(raw[pos]); pos += 1
                    if len(out)-start > 4096 or len(out) > 1024*1024:
                        raise ValueError('VBA decompression budget')
    return bytes(out)


def static_vba(path):
    import io
    import olefile
    containers = []
    raw = path.read_bytes()
    if raw.startswith(bytes.fromhex('d0cf11e0a1b11ae1')):
        containers.append(raw)
    elif raw.startswith(b'PK'):
        with ZipFile(path) as archive:
            for name in archive.namelist():
                if name.lower().endswith('.bin'):
                    value=archive.read(name)
                    if value.startswith(bytes.fromhex('d0cf11e0a1b11ae1')):containers.append(value)
    modules = []
    for value in containers:
        with olefile.OleFileIO(io.BytesIO(value)) as storage:
            for name in storage.listdir():
                if len(name) < 2 or name[-2].lower() != 'vba' or name[-1].lower() in ('dir','_vba_project') or name[-1].lower().startswith('__srp_'):
                    continue
                blob = storage.openstream(name).read()
                assert len(blob) < 2*1024*1024
                found = []
                for offset in range(len(blob)-3):
                    if blob[offset] != 1 or blob[offset+2] & 0x70 != 0x30:
                        continue
                    try:
                        source = decompress_vba(blob[offset:])
                    except (ValueError, IndexError):
                        continue
                    if source.startswith(b'Attribute VB_Name = '):
                        found.append(source)
                if found:
                    assert len(found) == 1, ('ambiguous fixture module', name)
                    code = found[0].decode('cp1252')
                    modules.append({'name':name[-1], 'sourceSha256':sha(code.encode('utf-8')), 'sourceText':code})
    return {'detected':bool(modules), 'modules':sorted(modules,key=lambda x:x['name']),
            'sourceSignature':sorted((m['name'],m['sourceSha256']) for m in modules)}


def independent(path):
    import io
    import olefile
    result = {'sha256': sha(path.read_bytes()), 'projectParts': [], 'oleStreams': []}
    if path.read_bytes()[:8] == bytes.fromhex('d0cf11e0a1b11ae1'):
        with olefile.OleFileIO(str(path)) as storage:
            result['oleStreams'] = [('/'.join(name), sha(storage.openstream(name).read()))
                                    for name in storage.listdir() if any(p.lower() == 'vba' for p in name)]
    else:
        with ZipFile(path) as archive:
            assert archive.testzip() is None
            for name in archive.namelist():
                if name.lower().endswith('.bin'):
                    raw = archive.read(name)
                    if not raw.startswith(bytes.fromhex('d0cf11e0a1b11ae1')):continue
                    with olefile.OleFileIO(io.BytesIO(raw)) as storage:
                        streams = [('/'.join(item), sha(storage.openstream(item).read())) for item in storage.listdir()]
                    result['projectParts'].append({'name': name, 'sha256': sha(raw), 'streams': streams})
    result['staticVba'] = static_vba(path)
    return result


def add_word_marker(source, destination):
    ns = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
    with ZipFile(source) as original, ZipFile(destination, 'w') as output:
        for entry in original.infolist():
            raw = original.read(entry)
            if entry.filename == 'word/document.xml':
                root = ET.fromstring(raw)
                paragraph = ET.Element('{%s}p' % ns)
                ET.SubElement(ET.SubElement(paragraph, '{%s}r' % ns), '{%s}t' % ns).text = MARKER
                root.find('{%s}body' % ns).insert(0, paragraph)
                raw = ET.tostring(root, encoding='utf-8', xml_declaration=True)
            output.writestr(entry, raw)


def contains_marker(path):
    from openpyxl import load_workbook
    if path.suffix in ('.xlsx', '.xlsm'):
        book = load_workbook(path, read_only=True, data_only=False)
        try:
            assert book.active['D1'].value == MARKER
            assert book.active['D2'].value == '000123'
        finally:
            book.close()
    elif path.suffix in ('.docx', '.docm'):
        with ZipFile(path) as archive:
            root = ET.fromstring(archive.read('word/document.xml'))
            assert MARKER in ''.join(root.itertext())
    else:
        raise AssertionError('unimplemented_marker_consumer')


def rename_word_vba_part(source,destination):
    """A valid alternate OPC Part URI; original program/storage bytes stay exact."""
    with ZipFile(source) as original,ZipFile(destination,'w') as output:
        for entry in original.infolist():
            raw=original.read(entry)
            name=entry.filename
            if name=='word/vbaProject.bin':name='word/alternate-code.bin'
            if name=='[Content_Types].xml' or name.endswith('.rels'):
                raw=raw.replace(b'vbaProject.bin',b'alternate-code.bin')
            output.writestr(name,raw)


def main():
    from openpyxl import load_workbook
    from app_backend.converter_guard import LibreOfficeAdapter
    from app_backend.converter_guard.errors import GuardError
    from app_backend.converter_guard.macro_vba import inspect_vba

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--fixtures', type=Path, default=ROOT/'tests/fixtures/macros')
    parser.add_argument('--require-uno', action='store_true')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    inputs = args.output/'inputs'
    inputs.mkdir()
    originals, provenance = {}, []
    for name, (relative, expected) in FIXTURES.items():
        path = inputs/name
        shutil.copyfile(args.fixtures/name, path)
        assert sha(path.read_bytes()) == expected, ('fixture_sha256', name, sha(path.read_bytes()))
        originals[path] = expected
        provenance.append({'file': name, 'url': PREFIX+relative, 'sha256': expected,
                           'license': 'Apache-2.0', 'facts': independent(path)})
        assert provenance[-1]['facts']['staticVba']['detected']
    license_path = inputs/'POI-LICENSE.txt'
    shutil.copyfile(args.fixtures/'POI-LICENSE.txt', license_path)
    license_sha=sha(license_path.read_bytes())
    assert license_sha == '6e7c918e5a49f677c1b85a9eb2d035cd85c217fc42af6b4a336964a434001a9c', ('upstream_license_sha256',license_sha)
    derived_calc, derived_word = inputs/'cyrillic.xlsm', inputs/'cyrillic.docm'
    book = load_workbook(inputs/'SimpleMacro.xlsm', keep_vba=True)
    book.active['D1'], book.active['D2'] = MARKER, '000123'
    book.save(derived_calc)
    book.close()
    add_word_marker(inputs/'SimpleMacro.docm', derived_word)
    for derived, original in ((derived_calc, inputs/'SimpleMacro.xlsm'), (derived_word, inputs/'SimpleMacro.docm')):
        assert independent(derived)['staticVba']['sourceSignature'] == independent(original)['staticVba']['sourceSignature']
        originals[derived] = sha(derived.read_bytes())
    renamed_word=inputs/'cyrillic-renamed.docm'
    rename_word_vba_part(derived_word,renamed_word)
    assert independent(renamed_word)['staticVba']['sourceSignature']==independent(derived_word)['staticVba']['sourceSignature']
    assert inspect_vba(renamed_word)['state']=='present'
    originals[renamed_word]=sha(renamed_word.read_bytes())
    adapter = LibreOfficeAdapter(args.executable)
    cases, failures, proofs = [], [], {}
    def run(source, target, number):
        before = independent(source)
        try:
            cancel = threading.Event()
            # The production queue prepares one unchanged source once, then
            # applies its registry-bound proof for each selected target.
            key = (str(source.resolve()), sha(source.read_bytes()))
            assert key[1] == originals[source], 'source changed before proof reuse'
            proof = proofs.get(key)
            if proof is None:
                proof = adapter.preflight(source, args.output/('probe-%02d' % number), cancel=cancel, timeout=120)
                proofs[key] = proof
            assert proof.get('state') == 'detected', proof
            adapter.prepare_source(proof)
            outputs = adapter.convert(source, target, args.output/('task-%02d' % number),
                                      cancel=cancel, timeout=180, on_started=lambda: None)
            info, mode = adapter.export_info(), adapter.execution_mode()
            if args.require_uno:
                assert mode == 'native_uno', mode
            primary = next(path for path in outputs if path.suffix == '.'+target)
            after = independent(primary)
            check = next(item for item in info['resultChecks'] if item['check'] == 'vba_storage')
            if target in ('xlsx', 'docx'):
                assert not after['staticVba']['detected']
                assert check['status'] == 'expected_loss'
                assert 'VBA_MACROS_EXPECTED_LOSS' in info['warnings']
            else:
                assert after['staticVba']['detected']
                assert before['staticVba']['sourceSignature'] == after['staticVba']['sourceSignature']
                assert check['status'] in ('streams_preserved','not_checked'), check
                if check['status'] == 'streams_preserved':
                    assert 'VBA_STREAMS_PRESERVED' in info['warnings']
                else:
                    assert check['reason'] == 'vba_stream_bytes_changed'
                    assert 'VBA_PRESERVATION_NOT_CHECKED' in info['warnings']
            if source.name.startswith('cyrillic'):
                contains_marker(primary)
            return {'status': 'PASS', 'nativeOutcome': 'PRESERVED' if target not in ('xlsx','docx') else 'EXPECTED_LOSS',
                    'executionMode': mode, 'output': after, 'exportInfo': info,
                    'sourceUnchanged': sha(source.read_bytes()) == originals[source]}
        except GuardError as error:
            # A build that drops a project must never return success. Keep the
            # refusal visible; Windows acceptance additionally requires actual
            # native positives, and never counts refusals as preservation.
            if error.code == 'ENGINE_OUTPUT_MACROS_LOST' and not args.require_uno:
                return {'status': 'SAFE_REFUSAL', 'code': error.code,
                        'reason': 'native_build_dropped_vba', 'preservationPassed': False}
            raise
    with adapter.batch(args.output/'office'):
        plan = [(inputs/'SimpleMacro.docm','docm'), (derived_word,'docm'), (derived_word,'docx'),
                (inputs/'SimpleMacro.xlsm','xlsm'), (derived_calc,'xlsm'), (derived_calc,'xlsx'),
                (inputs/'SimpleMacro.doc','docm'), (inputs/'SimpleMacro.xls','xlsm'),
                (renamed_word,'docm'), (renamed_word,'docx')]
        for number, (source, target) in enumerate(plan):
            try:
                result = {'source': source.name, 'target': target, **run(source, target, number)}
            except Exception as error:
                failures.append((source.name,target))
                result = {'source': source.name, 'target': target, 'status': 'FAIL', 'error': repr(error),
                          'code': getattr(error,'code',None), 'diagnostic': getattr(error,'diagnostic',None)}
            cases.append(result)
            print('MACRO_COMPLETION_CASE='+json.dumps(result, ensure_ascii=True), flush=True)
    assert all(sha(path.read_bytes()) == expected for path,expected in originals.items())
    profiles = list(args.output.rglob('registrymodifications.xcu'))
    assert profiles and all('DisableMacrosExecution' in path.read_text(encoding='utf-8') for path in profiles)
    evidence = {'schema': 1, 'status': 'PASS' if not failures else 'FAIL',
        'head': subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        'engineVersion': subprocess.check_output([str(args.executable),'--version'],text=True).strip(),
        'platform': platform.platform(), 'macrosExecuted': False, 'sourceBytesUnchanged': True,
        'staticInspection': 'olefile0.47+independent bounded MS-OVBA source decompression, never Office execution',
        'profilesWithMacrosDisabled': len(profiles), 'provenance': provenance, 'cases': cases,
        'counts': {status:sum(row['status']==status for row in cases) for status in ('PASS','SAFE_REFUSAL','FAIL')},
        'limitations': ['Static code/storage equality does not prove macro runtime behavior.',
                        'Cyrillic controls are explicitly derived test documents; downloaded originals stay unchanged.']}
    (args.output/'evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('MACRO_COMPLETION_RESULT='+json.dumps({k:evidence[k] for k in ('status','counts','macrosExecuted','sourceBytesUnchanged')}),flush=True)
    return int(bool(failures))


if __name__ == '__main__':
    raise SystemExit(main())
