"""Explicit local native QA; does not start GitHub workflows or install anything.

Generated fixtures only. Checks exact records, PDF markers/geometry and archive
ownership; it is not a full visual comparison or the user's 255-file corpus.
"""
import argparse
import csv
from decimal import Decimal
import hashlib
import json
import platform
from pathlib import Path
import subprocess
import sys
import threading
import time
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app_backend.converter_guard import ConverterGuard, GuardLimits, LibreOfficeAdapter
from app_backend.converter_guard.adapter import prepare_profile
from app_backend.libreoffice_engine import convert_path
from guard_v08_native_acceptance import fixtures, zip_text
from guard_v09_save_queue_acceptance import verify_snapshot


def digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def finished(guard, identity, seconds=240):
    until = time.monotonic() + seconds
    revision = -1
    while True:
        state = guard.status(identity)
        if state['revision'] != revision:
            revision = state['revision']
            print(json.dumps({'completed':state['completed'], 'total':state['total'],
                              'currentTask':state['currentTask']}, ensure_ascii=True), flush=True)
        if state['finished']:
            return state
        if time.monotonic() >= until:
            raise AssertionError('Native QA deadline expired')
        time.sleep(.02)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--executable', type=Path)
    parser.add_argument('--quality-check',action='store_true')
    parser.add_argument('--session-max-tasks', type=int, default=2)
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    sources = fixtures(args.output/'fixtures')
    original_hashes = {key:digest(path) for key,path in sources.items()}
    adapter = LibreOfficeAdapter(args.executable)
    assert adapter.available, 'A real local LibreOffice is required'
    version = subprocess.run([str(adapter.executable),'--version'], capture_output=True,
                             text=True, check=True, timeout=15).stdout.strip()
    guard = ConverterGuard(adapter=adapter,
        limits=GuardLimits(file_timeout=90,preflight_timeout=90,session_max_tasks=args.session_max_tasks),
        recovery_parent=args.output/'owned-jobs')
    evidence = {'status':'FAIL','platform':platform.system(),'engineVersion':version,
                'scope':'owned generated 4-source / 8-task native pipeline',
                'visibleQt':'NOT_RUN','fullVisualFidelity':'NOT_RUN','userCorpus':'NOT_RUN'}
    tick = time.monotonic()
    try:
        identity = guard.create([{'id':key,'name':path.name,'size':path.stat().st_size}
                                 for key,path in sources.items()],prepare_only=True)['jobId']
        for key,path in sources.items():
            with path.open('rb') as stream:
                guard.upload(identity,key,stream,path.stat().st_size)
        start = time.monotonic()
        inspection = guard.preflight(identity)
        evidence['preflightMs'] = round((time.monotonic()-start)*1000)
        requested = {'docx':['pdf','html','odt'],'xlsx':['csv','tsv'],
                     'csv':['pdf'],'bmp':['pdf','png']}
        eligible = {item['id']:set(item['verifiedFileIds']) for item in inspection['formats']}
        for key,targets in requested.items():
            for target in targets:
                assert key in eligible.get(target,set()), 'Unconfirmed route: '+key+' -> '+target
        assert 'bmp' not in eligible.get('dbf',set())
        assert 'csv' not in eligible.get('docx',set())
        start = time.monotonic()
        guard.start(identity,file_ids=list(sources),only_verified=True,archive_mode='on_demand',
                    quality_check=args.quality_check,
                    plan=[{'fileId':key,'formats':targets} for key,targets in requested.items()])
        done = finished(guard,identity)
        evidence['queueMs'] = round((time.monotonic()-start)*1000)
        assert (done['state'],done['succeeded'],done['failed']) == ('ready',8,0), guard.report(identity)
        assert done['bundleDeferred'] and done['result']['folderOnly']
        assert done['packagingError'] is None
        assert not list(guard._jobs[identity].root.glob('*.zip'))
        destination = args.output/'saved';destination.mkdir()
        start = time.monotonic()
        saved = guard.save_folder(done['result']['ticket'],destination)
        evidence['folderSaveMs'] = round((time.monotonic()-start)*1000)
        assert saved['savedAll'] and saved['outputs'] >= 8
        folder, manifest = verify_snapshot(destination,saved)
        outputs = {(item['fileId'],item['format']):folder/item['archiveName']
                   for item in done['result']['outputs'] if item['name'].endswith('.'+item['format'])}
        for target in ('csv','tsv'):
            with outputs['xlsx',target].open(encoding='utf-8-sig',newline='') as stream:
                records = list(csv.reader(stream,delimiter=',' if target == 'csv' else '\t'))
            assert len(records) == 2 and all(len(row)==2 for row in records)
            assert records[0][0]=='AIO_SHEET_MARKER' and records[1][0]=='Проверка'
            assert Decimal(records[0][1].replace(',','.')) == Decimal('1234.5')
            assert Decimal(records[1][1].replace(',','.')) == Decimal('1244.5')
        for marker in ('AIO_DOCUMENT_MARKER','AIO_TABLE_LEFT','AIO_TABLE_RIGHT'):
            assert marker in outputs['docx','html'].read_text(encoding='utf-8')
            assert marker in zip_text(outputs['docx','odt'])
        assert outputs['bmp','png'].read_bytes().startswith(b'\x89PNG\r\n\x1a\n')
        checks = {'textCellsAndFormulaValue':'PASS','officeContainerAndTableMarkers':'PASS',
                  'independentSavedByteReadback':'PASS','pngHeader':'PASS','pdfReadback':'NOT_RUN'}
        try:
            from pypdf import PdfReader
            reader = PdfReader(outputs['docx','pdf'])
            text = '\n'.join(page.extract_text() or '' for page in reader.pages)
            for marker in ('AIO_DOCUMENT_MARKER','AIO_TABLE_LEFT','AIO_TABLE_RIGHT'):
                assert marker in text
            control = args.output/'direct-pdf'
            prepare_profile(control/'profile')
            direct = convert_path(sources['docx'],'pdf',control,executable=adapter.executable,
                                  cancel=threading.Event(),convert_spec='pdf:writer_pdf_Export')
            before = PdfReader(next(p for p in direct if p.suffix=='.pdf'))
            assert len(before.pages)==len(reader.pages)
            assert [tuple(p.mediabox) for p in before.pages]==[tuple(p.mediabox) for p in reader.pages]
            checks['pdfReadback']='PASS'
            checks['pdfPageGeometryVsDirectCLI']='PASS'
        except ImportError:
            pass
        original_state = guard.status(identity)['state']
        start = time.monotonic()
        guard.rebuild_bundle(identity)
        bundled = finished(guard,identity)
        evidence['explicitArchiveMs'] = round((time.monotonic()-start)*1000)
        assert bundled['state'] == original_state
        assert not bundled['bundleDeferred'] and bundled['packagingError'] is None
        assert bundled['result']['name'].endswith('.zip')
        with zipfile.ZipFile(guard._jobs[identity].bundle.path) as archive:
            expected = {item['path']:item['sha256'] for item in manifest['outputs']}
            members = set(archive.namelist())-{'AIO-conversion-report.json'}
            assert members == set(expected)
            for name,sha in expected.items():
                assert hashlib.sha256(archive.read(name)).hexdigest()==sha
            assert archive.testzip() is None
        assert original_hashes == {key:digest(path) for key,path in sources.items()}
        checks['explicitArchiveContainsEverySavedByte']='PASS'
        report = guard.report(identity)
        evidence.update(status='PASS',succeeded=done['succeeded'],outputArtifacts=saved['outputs'],
            automaticArchiveBytes=0,sourceBytesUnchanged=True,checks=checks,
            officeStarts=bundled['officeStarts'],sessionMaxTasks=args.session_max_tasks,
            nativeModes=sorted({target['executionMode'] for f in report['files']
                                for target in f['targets'] if target['executionMode']}),
            report=report,sourceSHA256=original_hashes)
        guard.release(identity)
    finally:
        guard.close()
        evidence['elapsedMs'] = round((time.monotonic()-tick)*1000)
        (args.output/'evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({k:v for k,v in evidence.items() if k!='report'},ensure_ascii=False),flush=True)


if __name__ == '__main__':
    main()
