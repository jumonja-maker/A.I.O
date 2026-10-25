"""Real isolated comparison of many tiny originals; conversion boundary controlled."""
import argparse
import io
import json
from pathlib import Path
try:
    import resource
except ImportError:
    resource=None
import sys
import tempfile
import time
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from app_backend.converter_guard import ConverterGuard
from app_backend.converter_guard.comparison_service import ComparisonService
from test_comparison_service_v19 import ControlledNativeAdapter
from test_converter_guard_v02 import wait_finished

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--files',type=int,default=1000);a=p.parse_args()
    assert 1<=a.files<=10000
    with tempfile.TemporaryDirectory(prefix='aio-compare-stress-') as folder:
        g=ConverterGuard(adapter=ControlledNativeAdapter(),spool_parent=folder);service=ComparisonService(g)
        try:
            data=b'Original text retained without a native conversion engine in this QA fixture.\n'
            identity=g.create([{'id':str(i),'name':str(i)+'.txt','size':len(data)} for i in range(a.files)],['txt'],retain_sources=True)['jobId']
            for i in range(a.files):g.upload(identity,str(i),io.BytesIO(data),len(data))
            g.start(identity,archive_mode='on_demand');wait_finished(g,identity,seconds=60)
            start=time.monotonic();service.start(identity)
            while True:
                state=service.snapshot(identity)
                if state['state'] in ('completed','failed','cancelled'):break
                if time.monotonic()-start>900:raise TimeoutError('stress deadline')
                time.sleep(.1)
            assert state['state']=='completed' and state['processed']==a.files and state['summary']['checked']==a.files,state
            ids=set()
            for offset in range(0,a.files,50):
                page=service.snapshot(identity,offset=offset);assert len(page['items'])<=50
                ids.update(r['taskId'] for r in page['items'])
            assert len(ids)==a.files
            assert g._jobs[identity].io==0
            report={'status':'PASS','scope':'actual comparison workers + SQLite paging; tiny text files; native conversion fixture controlled','files':a.files,'seconds':time.monotonic()-start,'allReportsReachable':True,'releasedAnalysisLease':True,'summary':state['summary'],'parentMaxRssKiB':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss if resource else None,'maxSingleChildRssKiB':resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss if resource else None,'rssScope':'Linux high-water values; not aggregate RSS or long-term leak proof','real1000OfficeDocuments':'NOT_RUN'}
            a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report))
        finally:service.close();g.close()
if __name__=='__main__':main()
