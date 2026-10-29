"""SHA-locked API-compatibility follow-up; never updates repository refs."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request
import unittest

REPO='jumonja-maker/A.I.O'
BASE='0a54d5495cf707ef97aa06209dcf3ac937829505'
BASE_TREE='03aaa615fd8c43c91901d9b98d6953559930ab85'
assert os.environ['GITHUB_REPOSITORY']==REPO
assert os.environ['GITHUB_REF']=='refs/heads/work/recovery-save-m02-m30-20261029'
path=Path('app_backend/converter_guard/http_api.py')
data=path.read_bytes()
assert hashlib.sha1(('blob %d\0'%len(data)).encode()+data).hexdigest()=='42e3d09577c9c099ab0c0e37418b2e303421b3f2'
sys.path[:0]=[str(Path.cwd()),str(Path.cwd()/'tests')]
from test_boundaries_v21 import GuardBoundaryLifecycle
baseline=unittest.TestSuite([GuardBoundaryLifecycle('test_release_prunes_comparison_only_after_guard_accepts_release')])
result=unittest.TextTestRunner(verbosity=2).run(baseline)
assert len(result.failures)==1 and not result.errors,'API regression not reproduced'
print('AIO_BASELINE_REPRODUCED=legacy_release_response_shape',flush=True)
text=data.decode('utf-8')
before="""                outcome=self.guard.release(identity, discard=body.get('discard', False),defer_cleanup=True)
                self.comparison.release(identity)
                return self._json(handler,200, {'ok': True, **outcome})"""
after="""                self.guard.release(identity, discard=body.get('discard', False),defer_cleanup=True)
                self.comparison.release(identity)
                # Preserve the legacy acknowledgement. Cleanup progress remains
                # available through close()/storage, not this response shape.
                return self._json(handler,200, {'ok': True})"""
assert text.count(before)==1
text=text.replace(before,after,1)
compile(text,str(path),'exec')
path.write_text(text,encoding='utf-8',newline='\n')
for pattern in ('test_boundaries_v21.py','test_save_snapshot_report.py',
                'test_recovery_cleanup_responsiveness.py','test_guard_save*.py',
                'test_comparison_recovery_v20.py'):
    subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-p',pattern,'-v'],check=True)
print('AIO_RECOVERY_SAVE_COMPATIBILITY=PASS',flush=True)
doc=Path('docs/review/AIO_RECOVERY_SAVE_M02_M30_2026-10-29_RU.md')
content=doc.read_text(encoding='utf-8')
content+='\n## Совместимость HTTP после полного прогона\n\nПервый полный CI `40907768037` выявил несовместимость формы ответа `/release`: прежний тест требует ровно `{ok:true}`. Этот ответ восстановлен в продукте, тест и порядок Guard→comparison не изменены. Дополнительные признаки retained/cleanupPending остаются в прямом core API и close/storage; HTTP `/release` сохраняет старое подтверждение. Отложенная очистка и защита несохранённых результатов сохранены.\n\nРегрессия дополнительно воспроизводится на `0a54d54`, затем проходят неизменённые boundary-тесты и целевой набор сохранения/очистки. Окончательная приёмка относится только к последующему полному CI исправленного SHA.\n'
changes={str(path):text,str(doc):content}

def api(method,path,data=None):
    request=urllib.request.Request('https://api.github.com/repos/'+REPO+path,
        data=None if data is None else json.dumps(data).encode(),method=method,
        headers={'Authorization':'Bearer '+os.environ['GH_TOKEN'],
                 'Accept':'application/vnd.github+json','Content-Type':'application/json'})
    with urllib.request.urlopen(request,timeout=60) as response:return json.load(response)
assert api('GET','/git/commits/'+BASE)['tree']['sha']==BASE_TREE
tree=api('POST','/git/trees',{'base_tree':BASE_TREE,'tree':[
    {'path':p,'mode':'100644','type':'blob','content':c} for p,c in changes.items()]})['sha']
sha=api('POST','/git/commits',{'message':'fix(api): preserve legacy release acknowledgement with deferred cleanup',
    'tree':tree,'parents':[BASE]})['sha']
print('AIO_CANDIDATE_COMMIT='+sha,flush=True)
print('AIO_CANDIDATE_PARENT='+BASE,flush=True)
print('AIO_REFS_UNCHANGED=true',flush=True)
