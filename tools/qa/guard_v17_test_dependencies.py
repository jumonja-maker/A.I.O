"""Materialize pinned, offline TEST dependencies in a new private directory.

The bundle is never part of application packaging. It contains pure Python
test imports and the existing linkedom DOM oracle, with distribution metadata,
licenses and exact hashes. No package manager, network, or global install runs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import zipfile

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / 'tests/data/guard_v17_offline_test_dependencies.zip'
MANIFEST = '__manifest__.json'
MAX_FILES = 8192
MAX_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
BLOCK = 128 * 1024


def safe_name(name):
    if (not isinstance(name, str) or not name or any(c in name for c in '\\:<>"|?*')
            or any(ord(c) < 32 for c in name)):
        raise ValueError('Unsafe dependency bundle path')
    path = PurePosixPath(name)
    if path.is_absolute() or path.as_posix() != name or any(p in ('', '.', '..') for p in path.parts):
        raise ValueError('Unsafe dependency bundle path')
    reserved = {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1, 10)), *(f'lpt{i}' for i in range(1, 10))}
    if any(part.rstrip(' .') != part or part.split('.')[0].casefold() in reserved for part in path.parts):
        raise ValueError('Unsafe dependency bundle path')
    return path


def _stream_hash(stream):
    digest = hashlib.sha256()
    count = 0
    while block := stream.read(BLOCK):
        count += len(block)
        if count > MAX_FILE_BYTES:
            raise ValueError('Dependency file exceeds byte budget')
        digest.update(block)
    return count, digest.hexdigest()


def inspect_bundle(archive):
    entries = archive.infolist()
    names = [entry.filename for entry in entries]
    if len(entries) > MAX_FILES + 1 or len(names) != len(set(name.casefold() for name in names)):
        raise ValueError('Duplicate dependency paths or file budget exceeded')
    if names.count(MANIFEST) != 1:
        raise ValueError('Dependency manifest missing or duplicated')
    manifest_entry = archive.getinfo(MANIFEST)
    if manifest_entry.file_size > MAX_MANIFEST_BYTES:
        raise ValueError('Dependency manifest exceeds byte budget')
    manifest = json.loads(archive.read(MANIFEST).decode('utf-8'))
    if manifest.get('schema') != 1 or manifest.get('scope') != 'offline_test_dependencies_only':
        raise ValueError('Unexpected dependency bundle scope')
    rows = manifest.get('files')
    if not isinstance(rows, list) or not rows or len(rows) > MAX_FILES:
        raise ValueError('Invalid dependency manifest files')
    expected = {}
    total = 0
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Invalid dependency manifest entry')
        name = row.get('name')
        safe_name(name)
        size, digest = row.get('bytes'), row.get('sha256')
        if name == MANIFEST or name.casefold() in expected or type(size) is not int or not 0 <= size <= MAX_FILE_BYTES:
            raise ValueError('Invalid or duplicate dependency manifest entry')
        if not isinstance(digest, str) or re.fullmatch(r'[0-9a-f]{64}', digest) is None:
            raise ValueError('Invalid dependency digest')
        expected[name.casefold()] = row
        total += size
    if total > MAX_EXPANDED_BYTES or set(names) != {MANIFEST, *(row['name'] for row in rows)}:
        raise ValueError('Unlisted dependency entry or expanded-byte budget exceeded')
    for entry in entries:
        safe_name(entry.filename)
        mode = entry.external_attr >> 16
        if entry.is_dir() or stat.S_ISLNK(mode) or stat.S_IFMT(mode) not in (0, stat.S_IFREG):
            raise ValueError('Dependency entries must be regular files')
        if entry.flag_bits & 1 or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise ValueError('Unsupported dependency ZIP entry')
        if entry.filename == MANIFEST:
            continue
        row = expected[entry.filename.casefold()]
        if entry.file_size != row['bytes']:
            raise ValueError('Dependency bytes differ from manifest')
        with archive.open(entry) as stream:
            actual = _stream_hash(stream)
        if actual != (row['bytes'], row['sha256']):
            raise ValueError('Dependency bytes differ from manifest')
    return manifest


def materialize(output, *, bundle=None):
    """Return environment metadata only after all exact bundle bytes verify."""
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise FileExistsError('Dependency destination already exists')
    output = output.resolve()
    if output == ROOT.resolve() or output.is_relative_to(ROOT.resolve()):
        raise ValueError('Dependency destination must be outside the immutable source checkout')
    created = False
    try:
        with zipfile.ZipFile(BUNDLE if bundle is None else bundle) as archive:
            manifest = inspect_bundle(archive)
            output.mkdir(parents=True, mode=0o700, exist_ok=False)
            created = True
            for row in manifest['files']:
                path = output.joinpath(*safe_name(row['name']).parts)
                path.parent.mkdir(parents=True, exist_ok=True)
                digest, count = hashlib.sha256(), 0
                with archive.open(row['name']) as source, path.open('xb') as destination:
                    while block := source.read(BLOCK):
                        count += len(block)
                        if count > row['bytes']:
                            raise ValueError('Dependency bytes changed while extracting')
                        digest.update(block)
                        destination.write(block)
                if (count, digest.hexdigest()) != (row['bytes'], row['sha256']):
                    raise ValueError('Dependency bytes changed while extracting')
        module = output / 'node_modules/linkedom/esm/index.js'
        python = output / 'python'
        if not module.is_file() or not all((python / name / '__init__.py').is_file()
                                         for name in ('openpyxl', 'pypdf', 'et_xmlfile')):
            raise ValueError('Required offline test dependencies missing')
        original_pythonpath = os.environ.get('PYTHONPATH')
        env = {'AIO_DOM_MODULE': str(module),
               'PYTHONPATH': str(python) + (os.pathsep + original_pythonpath if original_pythonpath else '')}
        metadata = {'schema': 1, 'scope': manifest['scope'], 'environment': env,
                    'packages': manifest.get('packages', []), 'filesVerified': len(manifest['files']),
                    'bytesVerified': sum(row['bytes'] for row in manifest['files'])}
        (output / 'environment.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        return metadata
    except BaseException:
        if created:
            shutil.rmtree(output, ignore_errors=True)
        raise


def write_github_env(environment, path):
    # GitHub single-line assignments are safe only when neither names nor
    # values can introduce another assignment or multiline delimiter.
    if set(environment) != {'AIO_DOM_MODULE', 'PYTHONPATH'} or any(
            not isinstance(value, str) or '\n' in value or '\r' in value or '\0' in value
            for value in environment.values()):
        raise ValueError('Unsafe GitHub environment assignment')
    with Path(path).open('a', encoding='utf-8', newline='\n') as stream:
        for name, value in environment.items():
            stream.write(name + '=' + value + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--bundle', type=Path)
    parser.add_argument('--github-env', nargs='?', const='ENV',
                        help='Append assignments to GITHUB_ENV, or an explicit environment-file path')
    args = parser.parse_args()
    target = None
    if args.github_env:
        target = os.environ.get('GITHUB_ENV') if args.github_env == 'ENV' else args.github_env
        if not target:
            parser.error('--github-env requires GITHUB_ENV or an explicit environment-file path')
    result = materialize(args.output, bundle=args.bundle)
    if target:
        write_github_env(result['environment'], target)
    print('AIO_OFFLINE_TEST_DEPENDENCIES=' + json.dumps(result, ensure_ascii=True, separators=(',', ':')))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
