"""Publish tested git objects only; no ref/branch/PR updates and no artifacts."""
import base64
import hashlib
import json
import os
from pathlib import Path
import urllib.request
ROOT=Path(__file__).resolve().parents[2]
paths=['app_backend/converter_guard/uno_session.py','app_backend/converter_guard/adapter.py',
       'tools/qa/fulltest_vector_pages_native.py','.github/workflows/aio-hosted-qa.yml']
for name in paths:
    data=(ROOT/name).read_bytes()
    expected=hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
    body=json.dumps({'content':base64.b64encode(data).decode('ascii'),'encoding':'base64'}).encode()
    request=urllib.request.Request('https://api.github.com/repos/'+os.environ['GITHUB_REPOSITORY']+'/git/blobs',
        data=body,headers={'Authorization':'Bearer '+os.environ['GH_TOKEN'],
        'Accept':'application/vnd.github+json','Content-Type':'application/json','User-Agent':'aio-qa-blobs'},method='POST')
    with urllib.request.urlopen(request,timeout=30) as response:result=json.load(response)
    assert result['sha']==expected,(name,result['sha'],expected)
    print('TESTED_COLOUR_BLOB='+json.dumps({'path':name,'sha':expected,'bytes':len(data)}),flush=True)
