"""Diagnostic native Calc VBA profile comparison, never a quality gate.

Only isolated QA profiles differ. The application/default profile factory and
all input project bytes stay untouched. Macro execution remains prohibited.
Use actual module-source/storage equality to choose a production follow-up;
diagnostic completion does not certify any variant or application change.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import threading
import traceback
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.qa.completion_macro_native import independent, FIXTURES


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    from app_backend.converter_guard import LibreOfficeAdapter
    source=args.output/'SimpleMacro.xlsm'
    shutil.copyfile(ROOT/'tests/fixtures/macros/SimpleMacro.xlsm',source)
    before=independent(source)
    assert before['sha256']==FIXTURES['SimpleMacro.xlsm'][1] and before['staticVba']['detected']
    report={'schema':1,'status':'DIAGNOSTIC_RUNNING','macrosExecuted':False,
            'applicationDefaultChanged':False,'sourceSha256':before['sha256'],'cases':[]}
    variants=[('default',None),
        ('original_storage_load_false',{'UseExport':False,'Load':False,'Save':True,'Executable':False}),
        ('original_storage_load_true',{'UseExport':False,'Load':True,'Save':True,'Executable':False}),
        ('regenerate_nonexecutable',{'UseExport':True,'Load':True,'Save':True,'Executable':False})]
    for index,(name,properties) in enumerate(variants):
        workspace=args.output/name;workspace.mkdir()
        adapter=LibreOfficeAdapter(args.executable);cancel=threading.Event()
        result={'variant':name,'privateCalcVbaProperties':properties,'macrosExecuted':False}
        try:
            with adapter.batch(workspace/'office') as office:
                baseline=office.prepare_profile
                if properties is not None:
                    def prepare(profile):
                        baseline(profile)
                        path=profile/'user/registrymodifications.xcu'
                        raw=path.read_text(encoding='utf-8')
                        fields=''.join('<prop oor:name="'+key+'" oor:op="fuse"><value>'+str(value).lower()+'</value></prop>'
                                       for key,value in properties.items())
                        raw=raw.replace('</oor:items>',
                            '<item oor:path="/org.openoffice.Office.Calc/Filter/Import/VBA">'+fields+'</item></oor:items>')
                        path.write_text(raw,encoding='utf-8')
                    office.prepare_profile=prepare
                proof=adapter.preflight(source,workspace/'preflight',cancel=cancel,timeout=120)
                result['proof']=proof
                assert proof.get('state')=='detected',proof
                path=Path(office.profile)/'user/registrymodifications.xcu'
                raw=path.read_bytes();root=ET.fromstring(raw)
                disabled=any(p.get('{http://openoffice.org/2001/registry}name')=='DisableMacrosExecution'
                             and p.findtext('value')=='true' for p in root.iter('prop'))
                assert disabled,'QA variant must never enable macros'
                result['profileSecurity']={'DisableMacrosExecution':True,'sha256':hashlib.sha256(raw).hexdigest()}
                adapter.prepare_source(proof)
                try:
                    outputs=adapter.convert(source,'xlsm',workspace/'task',cancel=cancel,timeout=180,on_started=lambda:None)
                    result['guardOutcome']='converted'
                except Exception as error:
                    result.update(guardOutcome='rejected',error=repr(error),code=getattr(error,'code',None))
                    # Private rejected output may be statically inspected, but
                    # is never published as a conversion result.
                    outputs=list((workspace/'task/output').rglob('*.xlsm'))
                result['executionMode']=adapter.execution_mode();result['exportInfo']=adapter.export_info()
                assert result['executionMode']=='native_uno',result['executionMode']
                primaries=[p for p in outputs if p.suffix.lower()=='.xlsm']
                assert len(primaries)==1,[str(p) for p in primaries]
                after=independent(primaries[0]);result['output']=after
                result['exactModuleSourcePreserved']=bool(after['staticVba']['sourceSignature']) and (
                    before['staticVba']['sourceSignature']==after['staticVba']['sourceSignature'])
                result['projectPartBytesPreserved']=[p['sha256'] for p in before['projectParts']]==[p['sha256'] for p in after['projectParts']]
        except Exception as error:
            result.update(diagnosticError=repr(error),traceback=traceback.format_exc()[-6000:])
        result['sourceUnchanged']=hashlib.sha256(source.read_bytes()).hexdigest()==before['sha256']
        assert result['sourceUnchanged']
        report['cases'].append(result)
        (args.output/'evidence.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print('VBA_PROFILE_DIAGNOSTIC='+json.dumps(result,ensure_ascii=True),flush=True)
    report.update(status='DIAGNOSTIC_COMPLETE',sourceBytesUnchanged=True,
        productionDefaultPreserved=report['cases'][0].get('exactModuleSourcePreserved',False),
        preservedVariantNames=[r['variant'] for r in report['cases'] if r.get('exactModuleSourcePreserved')],
        limitations=['QA-only profile variants are not production changes or runtime macro tests.'])
    (args.output/'evidence.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print('VBA_PROFILE_DIAGNOSTIC_SUMMARY='+json.dumps({k:v for k,v in report.items() if k!='cases'},ensure_ascii=True))
    return 0


if __name__=='__main__':raise SystemExit(main())
