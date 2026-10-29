"""Publish tested source blobs, never branch refs, installers or releases."""
import hashlib
import json
import os
from pathlib import Path
import urllib.request

for name in ('app_backend/converter_guard/native_metafile.py',
             'app_backend/converter_guard/adapter.py', '.github/workflows/aio-hosted-qa.yml'):
    raw = Path(name).read_bytes()
    expected = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
    request = urllib.request.Request('https://api.github.com/repos/' + os.environ['GITHUB_REPOSITORY'] + '/git/blobs',
        data=json.dumps({'content': raw.decode('utf-8'), 'encoding': 'utf-8'}).encode(),
        headers={'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
                 'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(request, timeout=60) as response:
        result = json.load(response)
    assert result['sha'] == expected, name
    print('TESTED_METAFILE_BLOB=' + json.dumps({'path': name, 'sha': expected, 'bytes': len(raw)}), flush=True)
