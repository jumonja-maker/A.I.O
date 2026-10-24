"""Reassemble and verify the private, immutable FULLTEST fixture transfer.

This QA-only tool reads a pinned data checkout. It performs no download and
never replaces an existing destination or modifies an original fixture.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import stat
import zipfile

ORIGINAL_ZIP_SHA256 = 'f3294f15d6e188209bb7e4897e503f2464419df032712b093d39e15900147253'
DOM_ZIP_SHA256 = 'd9ad7038af496b846a1d1cfe8d05a6dd305c86dd90a90f0154217b80aa78462e'
PILLOW_WINDOWS_SHA256 = '1a992e86b0dd7aeb1f053cd506508c0999d710a8f07b4c791c63843fc6a807ac'
PILLOW_WINDOWS_WHEEL = 'pillow-11.3.0-cp311-cp311-win_amd64.whl'


def _assemble(parts, destination, expected_sha256):
    parts, destination = Path(parts), Path(destination)
    if destination.exists() or destination.is_symlink():
        raise ValueError('QA destination already exists; refusing overwrite')
    if not re.fullmatch('[0-9a-f]{64}', expected_sha256):
        raise ValueError('Invalid expected archive fingerprint')
    metadata = parts/'manifest.json'
    if metadata.is_symlink() or metadata.stat().st_size > 1024*1024:
        raise ValueError('Invalid fixture transfer manifest')
    manifest = json.loads(metadata.read_text(encoding='utf-8'))
    if manifest.get('schema') != 1 or manifest.get('zipSha256') != expected_sha256:
        raise ValueError('Fixture transfer fingerprint is not the requested snapshot')
    rows = manifest.get('parts')
    if not isinstance(rows, list) or not 0 < len(rows) <= 128:
        raise ValueError('Invalid fixture transfer part list')
    names = [row.get('path') for row in rows]
    if (len(set(names)) != len(names) or any(not isinstance(name,str) or
            not re.fullmatch(r'part-\d{3}\.bin',name) for name in names)):
        raise ValueError('Unsafe or repeated fixture part')
    destination.mkdir()
    archive = destination/'original-FULLTEST.zip'
    original = destination/'original'
    digest, size = hashlib.sha256(), 0
    with archive.open('xb') as output:
        for row in rows:
            path = parts/row['path']
            if path.is_symlink() or not path.is_file():
                raise ValueError('Missing or linked fixture transfer part')
            before = path.stat()
            git_hash = hashlib.sha1(b'blob '+str(before.st_size).encode()+b'\0')
            with path.open('rb') as stream:
                while block := stream.read(65536):
                    output.write(block);digest.update(block);git_hash.update(block);size += len(block)
            after = path.stat()
            if (before.st_size,before.st_mtime_ns,before.st_ino) != (after.st_size,after.st_mtime_ns,after.st_ino):
                raise ValueError('Fixture transfer part changed during reading')
            if git_hash.hexdigest() != row.get('gitBlobSha'):
                raise ValueError('Fixture transfer part differs from Git object')
    if size != manifest.get('zipBytes') or digest.hexdigest() != expected_sha256:
        raise ValueError('Reassembled corpus differs from original ZIP')
    return manifest, archive


def materialize(parts, destination, *, expected_sha256=ORIGINAL_ZIP_SHA256):
    destination=Path(destination)
    manifest,archive=_assemble(parts,destination,expected_sha256)
    original=destination/'original'
    extracted, total, inventory = set(), 0, []
    with zipfile.ZipFile(archive) as package:
        entries = package.infolist()
        if len(entries) != manifest.get('files') or len(entries) > 10000:
            raise ValueError('Unexpected corpus file count')
        if sum(entry.file_size for entry in entries) != manifest.get('sourceBytes'):
            raise ValueError('Unexpected corpus expanded size')
        for entry in entries:
            name = entry.filename
            if (not name or '/' in name or '\\' in name or name in ('.','..') or
                    '\0' in name or name.casefold() in extracted or entry.is_dir() or
                    stat.S_ISLNK(entry.external_attr >> 16)):
                raise ValueError('Unsafe or repeated corpus archive name')
            extracted.add(name.casefold())
        original.mkdir()
        for entry in entries:
            fingerprint, written = hashlib.sha256(), 0
            with package.open(entry) as source, (original/entry.filename).open('xb') as target:
                while block := source.read(65536):
                    target.write(block);fingerprint.update(block);written += len(block)
            if written != entry.file_size:
                raise ValueError('Truncated fixture extraction')
            total += written
            inventory.append({'name':entry.filename,'size':written,'sha256':fingerprint.hexdigest()})
    if total != manifest['sourceBytes']:
        raise ValueError('Extracted corpus size differs')
    proof = {'schema':1,'zipSha256':expected_sha256,'sourceBytes':total,
             'files':inventory,'originalsModified':False,
             'missingHtmlCompanions':manifest.get('missingHtmlCompanions',[])}
    (destination/'corpus-manifest.json').write_text(json.dumps(proof,ensure_ascii=True,indent=2)+'\n')
    return original, proof


def materialize_dom(parts,destination):
    """Unpack a hash-pinned existing DOM test dependency without installing it."""
    destination=Path(destination)
    manifest,archive=_assemble(parts,destination,DOM_ZIP_SHA256)
    if manifest.get('kind') != 'offline_existing_test_dependency':
        raise ValueError('Unexpected dependency archive role')
    names=set()
    with zipfile.ZipFile(archive) as package:
        entries=package.infolist()
        if len(entries)>5000 or sum(e.file_size for e in entries)>32*1024*1024:
            raise ValueError('Unexpected DOM dependency size')
        for entry in entries:
            path=Path(entry.filename)
            if (path.is_absolute() or '..' in path.parts or '\\' in entry.filename or
                    not entry.filename.startswith('node_modules/') or
                    any(':' in part or part in ('','.','..') for part in path.parts) or
                    entry.filename.casefold() in names or stat.S_ISLNK(entry.external_attr >> 16)):
                raise ValueError('Unsafe DOM dependency archive entry')
            names.add(entry.filename.casefold())
        for entry in entries:
            target=destination/entry.filename
            if entry.is_dir():
                target.mkdir(parents=True,exist_ok=True)
                continue
            target.parent.mkdir(parents=True,exist_ok=True)
            with package.open(entry) as source,target.open('xb') as output:
                while block:=source.read(65536):output.write(block)
    entry=destination/'node_modules/linkedom/cjs/index.js'
    if not entry.is_file():raise ValueError('Expected DOM entry point missing')
    return entry,{'schema':1,'zipSha256':DOM_ZIP_SHA256,'files':len(entries),
                  'entry':str(entry),'dependencyInstallation':False}


def materialize_pillow_windows(parts, destination):
    """Expose the pinned Windows QA wheel as a directory, without installing it.

    No import or executable from the wheel runs during extraction. The caller
    may prepend this isolated directory to the QA process's PYTHONPATH.
    """
    destination = Path(destination)
    manifest, archive = _assemble(parts, destination, PILLOW_WINDOWS_SHA256)
    if (manifest.get('kind') != 'offline_windows_pillow_qa_only' or
            manifest.get('wheel') != PILLOW_WINDOWS_WHEEL):
        raise ValueError('Unexpected Windows Pillow QA archive role')
    names, inventory, total = set(), [], 0
    required = {'PIL/__init__.py', 'PIL/_imaging.cp311-win_amd64.pyd',
                'pillow-11.3.0.dist-info/licenses/LICENSE',
                'pillow-11.3.0.dist-info/METADATA',
                'pillow-11.3.0.dist-info/WHEEL',
                'pillow-11.3.0.dist-info/RECORD'}
    with zipfile.ZipFile(archive) as package:
        entries = package.infolist()
        if not 0 < len(entries) <= 1024 or sum(e.file_size for e in entries) > 64*1024*1024:
            raise ValueError('Unexpected Windows Pillow QA expanded size')
        for entry in entries:
            name = entry.filename
            segments = name.rstrip('/').split('/')
            if (not name or '\\' in name or '\0' in name or name.startswith('/') or
                    any(part in ('', '.', '..') or ':' in part or part != part.rstrip(' .')
                        for part in segments) or
                    segments[0] not in ('PIL', 'pillow.libs', 'pillow-11.3.0.dist-info') or
                    name.casefold() in names or segments[-1].casefold().endswith('.pth') or
                    stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1):
                raise ValueError('Unsafe or repeated Windows Pillow QA archive entry')
            names.add(name.casefold())
        if not required.issubset({entry.filename for entry in entries if not entry.is_dir()}):
            raise ValueError('Pinned Windows Pillow core or license metadata missing')
        for entry in entries:
            target = destination/entry.filename
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            fingerprint, written = hashlib.sha256(), 0
            with package.open(entry) as source, target.open('xb') as output:
                while block := source.read(65536):
                    output.write(block); fingerprint.update(block); written += len(block)
            if written != entry.file_size:
                raise ValueError('Truncated Windows Pillow QA extraction')
            total += written
            inventory.append({'name': entry.filename, 'size': written,
                              'sha256': fingerprint.hexdigest()})
    proof = {'schema': 1, 'zipSha256': PILLOW_WINDOWS_SHA256,
             'wheel': PILLOW_WINDOWS_WHEEL, 'files': len(inventory), 'bytes': total,
             'pythonPath': str(destination), 'version': '11.3.0',
             'scope': 'Independent Windows cp311 win_amd64 regression QA only',
             'dependencyInstallation': False, 'productionDependency': False,
             'licensesPreserved': True}
    (destination/'pillow-qa-manifest.json').write_text(
        json.dumps({**proof, 'inventory': inventory}, indent=2)+'\n', encoding='utf-8')
    return destination, proof


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--parts',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--kind',choices=('fulltest','dom','pillow-windows'),default='fulltest')
    args=parser.parse_args()
    if args.kind=='dom':
        entry,proof=materialize_dom(args.parts,args.output)
        print(json.dumps({'status':'PASS',**proof}),flush=True)
        return
    if args.kind=='pillow-windows':
        entry,proof=materialize_pillow_windows(args.parts,args.output)
        print(json.dumps({'status':'PASS',**proof}),flush=True)
        return
    folder,proof=materialize(args.parts,args.output)
    print(json.dumps({'status':'PASS','corpus':str(folder),'files':len(proof['files']),
                      'bytes':proof['sourceBytes'],'zipSha256':proof['zipSha256']}),flush=True)


if __name__=='__main__':
    main()
