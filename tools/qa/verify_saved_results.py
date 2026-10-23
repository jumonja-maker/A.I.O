"""Read-only verification of every SHA-256 in an AIO folder/ZIP manifest."""
import argparse
import json
from pathlib import Path
import sys
import zipfile
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app_backend.converter_guard.save_verification import verify_folder, sha256_stream
from app_backend.converter_guard.html_resources import resource_path


def verify(path):
    if path.is_dir():
        manifest = json.loads((path/'AIO-results-manifest.json').read_text(encoding='utf-8'))
        result = verify_folder(path,manifest['outputs'])
    else:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            if len(names) != len(set(names)):
                raise ValueError('Duplicate ZIP entries')
            manifest = json.loads(z.read('AIO-results-manifest.json'))
            seen = set()
            for entry in manifest['outputs']:
                name = resource_path(entry['path'])
                if name.casefold() in seen:
                    raise ValueError('Duplicate manifest path')
                seen.add(name.casefold())
                with z.open(name) as stream:
                    size, digest = sha256_stream(stream)
                if size != entry['size'] or digest != entry['sha256']:
                    raise ValueError('Saved bytes differ: ' + name)
            result = {'readbackVerified':True, 'outputsVerified':len(seen)}
    return {'status':'PASS', **result, 'qualityCertified':False}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('path',type=Path)
    p.add_argument('--output',type=Path)
    a = p.parse_args()
    try:
        result = verify(a.path)
    except Exception as error:
        result = {'status':'FAIL', 'error':str(error), 'qualityCertified':False}
    text = json.dumps(result,ensure_ascii=False,indent=2)
    if a.output:
        with a.output.open('x',encoding='utf-8') as stream:
            stream.write(text+'\n')
    print(text)
    return int(result['status'] != 'PASS')


if __name__ == '__main__':
    sys.exit(main())
