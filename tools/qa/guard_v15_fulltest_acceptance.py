"""Explicit offline FULLTEST slices. Originals are read only; no GUI certification."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app_backend.converter_guard import ConverterGuard, GuardLimits, LibreOfficeAdapter

SUITES = {
    'html': (set('apng bmp emf emz eps fodg gif jfif jif jpe jpeg jpg odg png svg svgz svm tif tiff webp wmf wmz'.split()), ['html']),
    'tables': ({'csv','dbf','dif'}, ['ods','xlsx','dbf','dif']),
    'writer': ({'odt'}, ['csv','tab','tsv','epub','svg']),
    'web': ({'html','htm','xhtml'}, ['docx','png','htm']),
    'odc': ({'odc'}, ['odc']),
    'epub': ({'epub'}, ['odt']),
}


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(64 * 1024):
            h.update(block)
    return h.hexdigest()


def independent_container_checks(folder):
    checks = []
    for path in folder.rglob('*'):
        if path.suffix.lower() not in ('.ods','.xlsx','.docx','.epub','.png'):
            continue
        if path.suffix.lower() == '.png':
            try:
                from PIL import Image
            except ImportError:
                checks.append({'path':path.relative_to(folder).as_posix(), 'status':'not_run', 'reason':'Pillow_missing'})
                continue
            with Image.open(path) as im:
                im.load()
                details = {'width':im.width, 'height':im.height}
        else:
            with zipfile.ZipFile(path) as z:
                bad = z.testzip()
                if bad:
                    raise ValueError('Native container CRC failed: ' + path.name)
                primary = {'ods':'content.xml', 'xlsx':'xl/workbook.xml',
                           'docx':'word/document.xml', 'epub':'META-INF/container.xml'}[path.suffix[1:]]
                from xml.etree import ElementTree as ET
                ET.fromstring(z.read(primary))
                details = {'zipCRC':'pass', 'primaryXML':primary}
        checks.append({'path':path.relative_to(folder).as_posix(), 'status':'pass', **details})
    return checks


def run_slice(name, corpus, output, executable, seconds):
    extensions, targets = SUITES[name]
    originals = sorted(p for p in corpus.iterdir() if p.is_file() and p.suffix[1:].lower() in extensions)
    if not originals:
        return {'suite':name, 'status':'not_run', 'reason':'no_matching_inputs'}
    output.mkdir()
    before = {p.name:digest(p) for p in originals}
    adapter = LibreOfficeAdapter(executable)
    guard = ConverterGuard(adapter=adapter, limits=GuardLimits(preflight_timeout=seconds, file_timeout=seconds),
                           recovery_parent=output/'owned')
    row = {'suite':name, 'inputs':len(originals), 'unplannedPairs':[], 'qualityCertified':False}
    tick = time.monotonic()
    try:
        files = [{'id':'source'+str(i), 'name':p.name, 'size':p.stat().st_size} for i,p in enumerate(originals)]
        job = guard.create(files, prepare_only=True)['jobId']
        for metadata, original in zip(files, originals):
            with original.open('rb') as stream:
                guard.upload(job, metadata['id'], stream, metadata['size'])
        inspection = guard.preflight(job)
        formats = {f['id']:f for f in inspection['formats']}
        plan = []
        for f in files:
            admitted = []
            for target in targets:
                fmt = formats.get(target, {})
                if f['id'] in fmt.get('selectableFileIds', []):
                    admitted.append(target)
                else:
                    proof = next(p['preflight'] for p in inspection['files'] if p['id'] == f['id'])
                    row['unplannedPairs'].append({'source':f['name'], 'target':target,
                        'reason':fmt.get('unavailableReasons',{}).get(f['id']) or proof.get('code') or fmt.get('reasonCode')})
            if admitted:
                plan.append({'fileId':f['id'], 'formats':admitted})
        if plan:
            guard.start(job, file_ids=[p['fileId'] for p in plan], plan=plan,
                        only_verified=True, archive_mode='on_demand')
            until = time.monotonic() + seconds * sum(len(p['formats']) for p in plan) + 120
            while not guard.status(job)['finished']:
                if time.monotonic() > until:
                    guard.cancel(job)
                    raise RuntimeError('QA supervisor deadline')
                time.sleep(.05)
            row['report'] = guard.report(job)
            state = guard.status(job)
            if state.get('result'):
                row['save'] = guard.save_folder(state['result']['ticket'], output, verify=True)
                saved = next(p for p in output.iterdir() if p.name.startswith('AIO_Results_'))
                row['independentContainerChecks'] = independent_container_checks(saved)
        else:
            row['report'] = {'total':0, 'succeeded':0, 'failed':0, 'cancelled':0}
            row['preflight'] = inspection['files']
        row['originalsUnchanged'] = all(digest(p) == before[p.name] for p in originals)
        row['durationMs'] = round((time.monotonic()-tick)*1000)
        row['status'] = ('native_failures' if row['report'].get('failed') or row['report'].get('cancelled')
                         else 'partial_native_coverage' if row['unplannedPairs'] else 'completed')
        (output/'evidence.json').write_text(json.dumps(row,ensure_ascii=False,indent=2),encoding='utf-8')
        return row
    finally:
        guard.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--corpus',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--executable',type=Path,required=True)
    p.add_argument('--suite',choices=[*SUITES,'all'],default='all')
    p.add_argument('--seconds',type=float,default=20)
    a = p.parse_args()
    if a.seconds <= 0 or not a.corpus.is_dir() or not a.executable.is_file():
        p.error('Specify an existing corpus, native executable and positive timeout')
    a.output.mkdir(parents=True,exist_ok=False)
    version = subprocess.run([str(a.executable),'--version'],capture_output=True,text=True,timeout=20)
    result = {'platform':sys.platform, 'engineVersion':version.stdout.strip(),
              'qualityCertified':False, 'windowsGuiTested':False, 'cases':[]}
    for name in (SUITES if a.suite == 'all' else [a.suite]):
        row = run_slice(name,a.corpus,a.output/name,a.executable,a.seconds)
        result['cases'].append(row)
        (a.output/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({k:row[k] for k in ('suite','status','inputs','durationMs') if k in row}),flush=True)
    return int(any(r['status'] == 'native_failures' or r.get('originalsUnchanged') is False for r in result['cases']))


if __name__ == '__main__':
    sys.exit(main())
