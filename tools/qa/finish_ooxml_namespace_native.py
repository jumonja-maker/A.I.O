"""Require actual original->native-failure->private-CT retry on ten inputs."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import threading
from zipfile import ZipFile
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--executable',type=Path,required=True)
    parser.add_argument('--require-uno',action='store_true')
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    from PySide6.QtGui import QGuiApplication
    from app_backend.converter_guard import LibreOfficeAdapter
    from app_backend.converter_guard.ooxml_content_types import KIND, NATIVE_FAILURES
    from tools.qa.completion_current_corpus_native import expected_text
    from tools.qa.corpus_pdf_text_oracle import inspect_pdf_literals
    app=QGuiApplication.instance() or QGuiApplication([])
    provenance=json.loads((args.input.parent/'provenance.json').read_text(encoding='utf-8'))
    assert provenance['archiveSha256']=='8ca1f19a0d72405dd6a4df289bab68a393be71b090517683e392e665936c137d'
    expected={row['path']:row['sha256'] for row in provenance['files']}
    sources=sorted(p for p in args.input.iterdir() if p.suffix.lower() in ('.docm','.dotm','.pptm','.potm','.ppsx'))
    assert len(sources)==10
    report={'schema':1,'status':'INCOMPLETE','scope':'ten exact current178 originals; actual native import retry and independent PDF literal readback',
            'archiveSha256':provenance['archiveSha256'],'macrosExecuted':False,'cases':[]}
    cancel=threading.Event();adapter=LibreOfficeAdapter(args.executable)
    try:
        with adapter.batch(args.output/'office'):
            for index,source in enumerate(sources):
                row={'source':source.name,'sourceSha256':sha(source)}
                try:
                    assert sha(source)==expected[source.name]
                    proof=adapter.preflight(source,args.output/('probe-%02d'%index),cancel=cancel,timeout=120)
                    row['proof']={k:proof.get(k) for k in ('state','code','basis','modelLoaded','documentServices','selectedImportFilter','registryFingerprint','sourceContract','sourcePreparation')}
                    assert proof.get('state')=='detected',proof
                    prep=proof.get('sourcePreparation') or {}
                    assert prep.get('kind')==KIND and prep.get('originalNativeCode') in NATIVE_FAILURES,proof
                    assert prep['originalSha256']==expected[source.name]
                    if args.require_uno:
                        assert proof.get('basis')=='native_preflight_uno_document' and proof.get('modelLoaded') is True,proof
                    assert adapter.route_decision('pdf',proof)=='registered'
                    adapter.prepare_source(proof)
                    outputs=adapter.convert(source,'pdf',args.output/('task-%02d'%index),cancel=cancel,timeout=180,on_started=lambda:None)
                    if args.require_uno:assert adapter.execution_mode()=='native_uno',adapter.execution_mode()
                    pdf=next(p for p in outputs if p.suffix=='.pdf')
                    paragraphs,_scope=expected_text(source)
                    readback=inspect_pdf_literals(pdf,paragraphs)
                    assert readback['status']=='PASS',readback
                    row['readback']={k:v for k,v in readback.items() if k!='positionedPages'}
                    row['readback']['pages']=len(readback['positionedPages'])
                    row['executionMode']=adapter.execution_mode()
                    row['outputSha256']=sha(pdf)
                    row['exportInfo']=adapter.export_info()
                    assert KIND==row['exportInfo']['sourcePreparation']['kind']
                    assert 'OOXML_PRIVATE_CONTENT_TYPES_NAMESPACE' in row['exportInfo']['warnings']
                    # Independent byte comparison against the original ZIP.
                    prepared=args.output/('task-%02d'%index)/'ooxml-ct-compat'/source.name
                    with ZipFile(source) as original,ZipFile(prepared) as copy:
                        assert original.namelist()==copy.namelist()
                        changed=[n for n in original.namelist() if original.read(n)!=copy.read(n)]
                        assert changed==['[Content_Types].xml']
                    row['changedParts']=changed
                    row['status']='PASS'
                except Exception as error:
                    row.update(status='FAIL',error=repr(error),code=getattr(error,'code',None),diagnostic=getattr(error,'diagnostic',None))
                row['sourceUnchanged']=sha(source)==expected[source.name]
                if not row['sourceUnchanged']:row['status']='FAIL'
                report['cases'].append(row)
                (args.output/'evidence.json').write_text(json.dumps(report,ensure_ascii=True,indent=2),encoding='utf-8')
                print('OOXML_NAMESPACE_CASE='+json.dumps(row,ensure_ascii=True),flush=True)
            # A genuine licensed VBA project's opaque parts must also survive
            # this metadata-only operation, not merely suffix-only fixtures.
            from app_backend.converter_guard.macro_vba import inspect_vba
            genuine=ROOT/'tests/fixtures/macros/SimpleMacro.docm'
            genuine_sha=sha(genuine)
            derivative=args.output/'genuine-prefixed.docm'
            with ZipFile(genuine) as original,ZipFile(derivative,'w') as derived:
                ET.register_namespace('qa_ct','http://schemas.openxmlformats.org/package/2006/content-types')
                for info in original.infolist():
                    data=original.read(info)
                    if info.filename=='[Content_Types].xml':data=ET.tostring(ET.fromstring(data),encoding='utf-8',xml_declaration=True)
                    derived.writestr(info,data)
            row={'source':'genuine_POI_Word_VBA_prefixed_CT','genuineFixtureSha256':genuine_sha,
                 'derivedSourceSha256':sha(derivative)}
            try:
                before_vba=inspect_vba(derivative,cancel=cancel)
                assert before_vba['state']=='present',before_vba
                proof=adapter.preflight(derivative,args.output/'vba-preflight',cancel=cancel,timeout=120)
                assert proof.get('state')=='detected' and proof.get('sourcePreparation',{}).get('kind')==KIND,proof
                adapter.prepare_source(proof)
                outputs=adapter.convert(derivative,'docm',args.output/'vba-output',cancel=cancel,timeout=180,on_started=lambda:None)
                result=next(p for p in outputs if p.suffix=='.docm')
                after_vba=inspect_vba(result,cancel=cancel)
                assert before_vba['projects']==after_vba['projects'] and after_vba['state']=='present',(before_vba,after_vba)
                row.update(status='PASS',vbaStreamsByteEqual=True,macrosExecuted=False,
                           sourcePreparation=proof['sourcePreparation'],exportInfo=adapter.export_info())
            except Exception as error:
                row.update(status='FAIL',error=repr(error),code=getattr(error,'code',None),diagnostic=getattr(error,'diagnostic',None))
            row['sourceUnchanged']=sha(derivative)==row['derivedSourceSha256'] and sha(genuine)==genuine_sha
            report['cases'].append(row)
            print('OOXML_NAMESPACE_CASE='+json.dumps(row,ensure_ascii=True),flush=True)
            # Signature marker guard, not a claim of cryptographic signature
            # validation: such packages must never be reserialized by us.
            signed=args.output/'signature-marker.docm'
            with ZipFile(sources[0]) as original,ZipFile(signed,'w') as marked:
                for info in original.infolist():marked.writestr(info,original.read(info))
                marked.writestr('_xmlsignatures/sig1.xml',b'<Signature xmlns="http://www.w3.org/2000/09/xmldsig#"/>')
            signed_sha=sha(signed)
            row={'source':'signature_marker_private_copy_refusal','sourceSha256':signed_sha}
            try:
                proof=adapter.preflight(signed,args.output/'signature-preflight',cancel=cancel,timeout=120)
                assert proof.get('state')!='detected' and 'sourcePreparation' not in proof,proof
                assert not (args.output/'signature-preflight/ooxml-ct-compat').exists()
                row.update(status='PASS',compatibilityCopyNotCreated=True,cryptographicSignatureValidation=False)
            except Exception as error:row.update(status='FAIL',error=repr(error),code=getattr(error,'code',None))
            row['sourceUnchanged']=sha(signed)==signed_sha
            report['cases'].append(row)
            print('OOXML_NAMESPACE_CASE='+json.dumps(row,ensure_ascii=True),flush=True)
            # A tiny explicit spool allowance must stop private staging before
            # any native exporter is launched, while retaining the original.
            from app_backend.converter_guard.errors import GuardError
            row={'source':'private_copy_obeys_explicit_spool_budget'}
            try:
                proof=adapter.preflight(sources[0],args.output/'quota-preflight',cancel=cancel,timeout=120)
                assert proof.get('state')=='detected',proof
                adapter.prepare_source(proof)
                started=[]
                try:
                    adapter.convert(sources[0],'pdf',args.output/'quota-output',cancel=cancel,timeout=120,
                                    on_started=lambda:started.append(True),output_limit=lambda:1)
                    raise AssertionError('Tiny quota incorrectly allowed native conversion')
                except GuardError as error:
                    assert error.code=='SPOOL_QUOTA',error.code
                assert not started and not (args.output/'quota-output/output').exists()
                row.update(status='PASS',code='SPOOL_QUOTA',nativeExporterNotStarted=True)
            except Exception as error:row.update(status='FAIL',error=repr(error),code=getattr(error,'code',None))
            row['sourceUnchanged']=sha(sources[0])==expected[sources[0].name]
            report['cases'].append(row)
            print('OOXML_NAMESPACE_CASE='+json.dumps(row,ensure_ascii=True),flush=True)
        report['status']='PASS' if len(report['cases'])==13 and all(r['status']=='PASS' and r['sourceUnchanged'] for r in report['cases']) else 'FAIL'
    finally:
        report['allSourceHashesUnchanged']=all(sha(p)==expected[p.name] for p in sources)
        (args.output/'evidence.json').write_text(json.dumps(report,ensure_ascii=True,indent=2),encoding='utf-8')
        print('OOXML_NAMESPACE_SUMMARY='+json.dumps({k:v for k,v in report.items() if k!='cases'},ensure_ascii=True),flush=True)
    return 0 if report['status']=='PASS' and report['allSourceHashesUnchanged'] else 1


if __name__=='__main__':raise SystemExit(main())
