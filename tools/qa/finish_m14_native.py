"""Operational rare-format native controls, including a genuine Base report.

The ORP is natively generated from LibreOffice's official report-design QA
database; its original XML was neither invented nor repaired. No report SQL is
executed. The check reads the independently exported report control semantics.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys
import threading
from xml.etree import ElementTree as ET
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
FIXTURES = ROOT/'tests'/'fixtures'/'m14'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def report_design(path):
    """Independent serialized control facts; not the worker's native profile."""
    rpt = '{http://openoffice.org/2005/report}'
    office = '{urn:oasis:names:tc:opendocument:xmlns:office:1.0}'
    draw = '{urn:oasis:names:tc:opendocument:xmlns:drawing:1.0}'
    with ZipFile(path) as archive:
        assert archive.testzip() is None
        assert archive.read('mimetype') == b'application/vnd.sun.xml.report'
        xml = ET.fromstring(archive.read('content.xml'))
    report = xml.find(office+'body/'+office+'report')
    assert report is not None
    return {'command':report.get(rpt+'command'),
        'controls':sorted(node.get(draw+'name') for node in report.iter(rpt+'report-component')),
        'labels':sorted(''.join(node.itertext()) for node in report.iter(rpt+'fixed-content')),
        'dataFields':sorted(node.get(rpt+'formula') for node in report.iter(rpt+'formatted-text'))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--report-only', action='store_true')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    from app_backend.converter_guard import LibreOfficeAdapter
    from app_backend.converter_guard.errors import GuardError
    from tools.qa.m14_native_matrix import make_samples, write_odf
    adapter, cancel = LibreOfficeAdapter(args.executable), threading.Event()
    report = {'schema':1, 'status':'INCOMPLETE', 'scope':'actual loaded native rare-format models and operational routes',
              'realBaseReportPositive':'NOT_RUN', 'qualityCompared':False, 'cases':[]}
    original = FIXTURES/'official-roundtrip-report.orp'
    provenance = json.loads((FIXTURES/'provenance.json').read_text())
    checks = report['cases']
    try:
        assert digest(original) == provenance['fixtureSha256']
        before = report_design(original)
        assert before['command'] == 'Table1' and len(before['controls']) == 8
        assert before['labels'] == ['Dog Age', 'Dog Name', 'ID', 'Name']
        with adapter.batch(args.output/'office'):
            proof = adapter.preflight(original, args.output/'report-preflight', cancel=cancel, timeout=60)
            assert proof.get('state') == 'detected', proof
            assert proof.get('modelLoaded') is True and proof.get('documentServices') == ['com.sun.star.report.ReportDefinition'], proof
            assert proof.get('nativeReportStorage', {}).get('controls') == 8, proof
            assert adapter.route_decision('orp', proof) == 'registered'
            assert adapter.route_decision('pdf', proof) == 'unavailable'
            adapter.prepare_source(proof)
            paths = adapter.convert(original, 'orp', args.output/'report-roundtrip',
                                    cancel=cancel, timeout=90, on_started=lambda:None)
            output = next(p for p in paths if p.suffix.lower() == '.orp')
            after = report_design(output)
            assert before == after, {'source':before,'result':after}
            receipts = adapter.export_info().get('resultChecks', [])
            checked = next(row for row in receipts if row.get('check') == 'native_report_definition_roundtrip')
            assert checked['designSha256'] == checked['readbackDesignSha256'] and checked['controls'] == 8
            assert digest(original) == provenance['fixtureSha256']
            report['realBaseReportPositive'] = 'PASS'
            checks.append({'source':'official_report_definition', 'target':'orp', 'status':'PASS',
                           'sourceSha256':digest(original),'outputSha256':digest(output),
                           'nativeServices':proof['documentServices'], 'nativeReceipt':checked,
                           'independentSerializedDesign':after, 'sourceUnchanged':True})
            ordinary = write_odf(args.output/'ordinary.odt', 'text', 'text')
            ordinary_proof = adapter.preflight(ordinary, args.output/'ordinary-preflight', cancel=cancel, timeout=45)
            assert ordinary_proof.get('state') == 'detected', ordinary_proof
            assert adapter.route_decision('orp', ordinary_proof) == 'unavailable'
            checks.append({'source':'ordinary_writer','target':'orp','status':'PASS_UNAVAILABLE',
                           'reason':adapter._registry().route_reason('orp', ordinary_proof)})
            if not args.report_only:
                sources, _negative = make_samples(args.output)
                for suffix, source in sources.items():
                    proof = adapter.preflight(source, args.output/('preflight-'+suffix), cancel=cancel, timeout=60)
                    assert proof.get('state') == 'detected', (suffix, proof)
                    targets = ('pdf','odm','otm') if suffix in ('odm','otm') else ('odc',) if suffix == 'odc' else ('png',)
                    for target in targets:
                        assert adapter.route_decision(target, proof) == 'registered', (suffix,target,proof)
                        adapter.prepare_source(proof)
                        paths = adapter.convert(source,target,args.output/('convert-'+suffix+'-'+target),
                            cancel=cancel,timeout=90,on_started=lambda:None)
                        output = next(p for p in paths if p.suffix.lower()=='.'+target)
                        if target == 'pdf':
                            from pypdf import PdfReader
                            text=''.join(page.extract_text() or '' for page in PdfReader(output).pages)
                            assert 'AIO_M14_REAL_NATIVE_IMPORT_00123' in text,text
                        if suffix == 'psd':
                            from PIL import Image
                            with Image.open(source) as a, Image.open(output) as b:
                                assert a.size == b.size and a.convert('RGB').tobytes() == b.convert('RGB').tobytes()
                        checks.append({'source':suffix,'target':target,'status':'PASS',
                            'documentServices':proof.get('documentServices'),'outputSha256':digest(output)})
        report['status'] = 'PASS'
        return 0
    except BaseException as error:
        report['status'] = 'FAIL'
        report['error'] = {'type':type(error).__name__,'code':getattr(error,'code',None),'message':str(error)[-2500:]}
        return 1
    finally:
        (args.output/'evidence.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        print('AIO_FINISH_M14='+json.dumps(report,ensure_ascii=True),flush=True)


if __name__ == '__main__':
    raise SystemExit(main())
