"""Verify exact QA package versions and write a source-bound dependency manifest.

This is a version lock, NOT a hash-locked wheel supply-chain attestation.
Windows LibreOffice's independent MSI SHA-256 is verified by prepare-libreoffice.
"""
import argparse
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / 'tools' / 'build' / 'requirements-qa-locked.txt'
BUILD_LOCK = ROOT / 'tools' / 'build' / 'requirements-qa-build-locked.txt'
PIN = ROOT / 'tools' / 'build' / 'libreoffice-release.json'
MANIFEST = ROOT / 'build' / 'aio-qa-dependencies.json'


def parse_lock(path):
    found = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        value = line.split('#', 1)[0].strip()
        if not value or value.startswith('-r '):
            continue
        if value.count('==') != 1 or any(char in value for char in '<>;'):
            raise ValueError('Unpinned or conditional requirement: ' + value)
        name, version = value.split('==')
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', name) or not re.fullmatch(r'[A-Za-z0-9_.+-]+', version):
            raise ValueError('Invalid exact QA dependency: ' + value)
        canonical = name.lower().replace('_', '-').replace('.', '-')
        if canonical in found and found[canonical][1] != version:
            raise ValueError('Conflicting dependency lock for ' + name)
        found[canonical] = (name, version)
    return found


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--phase', choices=('runtime', 'build'), required=True)
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 11):
        raise RuntimeError('QA requires reviewed Python 3.11')
    deps = parse_lock(LOCK)
    if args.phase == 'build':
        deps.update(parse_lock(BUILD_LOCK))
    observed = {}
    for canonical, (name, expected) in sorted(deps.items()):
        actual = metadata.version(name)
        if actual != expected:
            raise RuntimeError(name + ': expected ' + expected + ', got ' + actual)
        observed[canonical] = actual
    source = os.environ.get('AIO_QA_SOURCE_SHA', '')
    if not re.fullmatch(r'[a-f0-9]{40}', source):
        raise RuntimeError('QA manifest must be bound to a verified source revision')
    pin = json.loads(PIN.read_text(encoding='utf-8'))
    payload = {
        'schema': 1, 'sourceRevision': source,
        'python': platform.python_version(), 'platform': sys.platform,
        'phase': args.phase, 'packages': observed,
        'lockSha256': {item.name: hashlib.sha256(item.read_bytes()).hexdigest()
                       for item in (LOCK, BUILD_LOCK)},
        'libreOfficeVersion': pin['version'],
        'libreOfficeMsiSha256': pin['sha256'],
        'wheelHashesVerified': False,
    }
    if args.phase == 'build':
        MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        MANIFEST.write_text(json.dumps(payload, sort_keys=True, indent=2) + '\n',
                            encoding='utf-8')
    print('AIO_QA_DEPENDENCY_LOCK=PASS')
    print('AIO_QA_DEPENDENCY_MANIFEST=' + json.dumps(payload, sort_keys=True))


if __name__ == '__main__':
    main()
