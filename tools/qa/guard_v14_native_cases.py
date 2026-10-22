"""Explicit native control vs Guard diagnostics for the unresolved ODC/ODT cases.

Never runs automatically. Caller supplies originals; each native call receives
an owned copy. No conversion resumes, network download or renderer fallback.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import shutil
import sys
import threading
import time
import zipfile
from xml.etree import ElementTree as ET
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from app_backend.converter_guard import ConverterGuard, GuardLimits, LibreOfficeAdapter, GuardError
from app_backend.converter_guard.quality import inspect_isolated
from app_backend.libreoffice_engine import convert_path,LibreOfficeError
from app_backend.converter_guard.adapter import prepare_profile, _RunControl


def package_facts(source):
    with zipfile.ZipFile(source) as archive:
        root=ET.fromstring(archive.read('content.xml'))
        return {'mimetype':archive.read('mimetype').decode('ascii'),
            'hasParentReference':any(v=='..' for e in root.iter() for k,v in e.attrib.items() if k.endswith('}href')),
            'embeddedPackages':sum(n.endswith('/content.xml') for n in archive.namelist())}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--executable',type=Path,required=True)
    parser.add_argument('--seconds',type=float,default=20)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    evidence={'status':'completed','scope':'native control vs Guard; no Windows GUI acceptance',
              'cases':[],'platform':sys.platform,'secondsPerNativeCall':args.seconds}
    for original in sorted(args.input.iterdir()):
        if original.suffix.lower() not in ('.odc','.odt'):continue
        key=original.stem+'-'+original.suffix[1:];case_root=args.output/key;case_root.mkdir()
        source=case_root/original.name;shutil.copyfile(original,source)
        before=hashlib.sha256(original.read_bytes()).hexdigest()
        row={'name':original.name,'sourceSHA256':before,'package':package_facts(original),'control':[]}
        targets=['pdf'] if source.suffix.lower()=='.odc' else ['csv','svg','epub']
        native=LibreOfficeAdapter(args.executable)
        guard=ConverterGuard(adapter=native,limits=GuardLimits(preflight_timeout=args.seconds,
            file_timeout=args.seconds),recovery_parent=case_root/'owned')
        try:
            identity=guard.create([{'id':'source','name':source.name,'size':source.stat().st_size}],prepare_only=True)['jobId']
            with source.open('rb') as stream:guard.upload(identity,'source',stream,source.stat().st_size)
            tick=time.monotonic();inspection=guard.preflight(identity)
            row['preflightMs']=round((time.monotonic()-tick)*1000)
            row['proof']=inspection['files'][0].get('preflight')
            formats={f['id']:f for f in inspection['formats']}
            for target in targets:
                folder=case_root/('direct-'+target);prepare_profile(folder/'profile')
                tick=time.monotonic();control={'target':target,
                    'guardVerified':'source' in formats.get(target,{}).get('verifiedFileIds',[])}
                try:
                    paths=convert_path(source,target,folder,executable=args.executable,
                                       cancel=_RunControl(threading.Event(),folder,args.seconds,None))
                    primary=next(p for p in paths if p.suffix.lower()=='.'+target)
                    control.update(state='ready',size=primary.stat().st_size,
                        outputSHA256=hashlib.sha256(primary.read_bytes()).hexdigest(),
                        structure=inspect_isolated(primary,case_root/('inspection-'+target)))
                except (LibreOfficeError,GuardError) as error:
                    control.update(state='error',code=error.code)
                control['durationMs']=round((time.monotonic()-tick)*1000)
                row['control'].append(control)
            admitted=[r['target'] for r in row['control'] if r['guardVerified']]
            if admitted:
                guard.start(identity,file_ids=['source'],formats=admitted,only_verified=True,
                            archive_mode='on_demand',quality_check=True)
                until=time.monotonic()+args.seconds*len(admitted)+90
                while not guard.status(identity)['finished']:
                    if time.monotonic()>until:raise RuntimeError('QA supervisor deadline')
                    time.sleep(.05)
                row['guardReport']=guard.report(identity)
            else:row['guardReport']={'nativeExportsStarted':0,'reason':'no_confirmed_native_route'}
        finally:guard.close()
        row['originalUnchanged']=hashlib.sha256(original.read_bytes()).hexdigest()==before
        evidence['cases'].append(row)
        (args.output/'evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2))
        print(json.dumps({'name':row['name'],'preflightMs':row['preflightMs'],
                          'proof':row['proof'],'control':[{k:v for k,v in c.items() if k!='structure'} for c in row['control']]},ensure_ascii=False),flush=True)
    (args.output/'evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
