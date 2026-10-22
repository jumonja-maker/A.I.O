"""Private short-lived structural inspection worker, including frozen builds."""
import json
import os
from pathlib import Path
import sys


def main(manifest):
    request=Path(manifest).resolve(strict=True)
    value=json.loads(request.read_text(encoding='utf-8'))
    # Bound Python/reader allocations on POSIX. Windows process containment is
    # owned by the parent's Job Object; the parent enforces the wall deadline.
    if os.name=='posix':
        import resource
        ceiling=768*1024*1024
        resource.setrlimit(resource.RLIMIT_AS,(ceiling,ceiling))
    from .quality import inspect_document
    try:
        result=inspect_document(Path(value['source']),seconds=value.get('seconds',8))
    except Exception:
        result={'schema':1,'status':'not_checked','complete':False,'kind':'unknown',
                'counts':{},'reason':'inspection_failed_or_memory_budget'}
    part=request.parent/'quality-result.part'
    part.write_text(json.dumps(result,ensure_ascii=True),encoding='ascii')
    part.replace(request.parent/'quality-result.json')


if __name__=='__main__':
    main(sys.argv[1])
