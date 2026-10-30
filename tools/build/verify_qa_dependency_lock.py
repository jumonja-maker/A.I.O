"""Verify the complete QA lock graph and exact installed package versions.

Without a verified wheel receipt this remains only a version lock.
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
MAX_LOCK_FILES = 32
MAX_LOCK_BYTES = 64 * 1024


def read_lock_graph(paths, *, allowed_root=None):
    """Resolve reviewed local -r includes; reject cycles and cross-file conflicts.

    The returned hashes cover exactly the bytes parsed, including included
    files. No index switches, URLs, environment substitutions or pip options
    other than a separate '-r relative/path.txt' line are accepted.
    """
    paths = tuple(Path(path) for path in paths)
    if not paths:
        raise ValueError('No QA lock files supplied')
    root = Path(allowed_root or paths[0].parent).resolve()
    found, hashes, visiting, visited = {}, {}, set(), set()

    def visit(path):
        path = path.resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError('QA lock escapes the reviewed directory: ' + str(path))
        if path in visiting:
            raise ValueError('Cyclic QA lock include: ' + str(path))
        if path in visited:
            return
        if len(visited) + len(visiting) >= MAX_LOCK_FILES:
            raise ValueError('Too many QA lock files')
        with path.open('rb') as stream:
            raw = stream.read(MAX_LOCK_BYTES + 1)
        if len(raw) > MAX_LOCK_BYTES:
            raise ValueError('QA lock file is too large: ' + str(path))
        visiting.add(path)
        hashes[path.relative_to(root).as_posix()] = hashlib.sha256(raw).hexdigest()
        for number, line in enumerate(raw.decode('utf-8').splitlines(), 1):
            value = line.split('#', 1)[0].strip()
            if not value:
                continue
            if value.startswith('-r '):
                relative = value[3:].strip()
                # A deliberately small, reproducible subset of pip's syntax.
                if (not re.fullmatch(r'[A-Za-z0-9_./-]+', relative) or
                        relative.startswith('/') or not relative):
                    raise ValueError('Invalid local QA lock include: ' + value)
                visit(path.parent / relative)
                continue
            match = re.fullmatch(r'([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+-]+)', value)
            if not match:
                raise ValueError(f'Unpinned or unsupported requirement at {path.name}:{number}: {value}')
            name, version = match.groups()
            canonical = re.sub(r'[-_.]+', '-', name).lower()
            if canonical in found and found[canonical][1] != version:
                raise ValueError('Conflicting dependency lock for ' + name)
            found.setdefault(canonical, (name, version))
        visiting.remove(path)
        visited.add(path)

    for path in paths:
        visit(path)
    return found, dict(sorted(hashes.items()))


def parse_lock(path):
    # Preserve the public tuple-valued interface used by existing QA tests.
    return read_lock_graph((path,))[0]


def profile_lock(phase):
    if phase not in ('runtime', 'build'):
        raise ValueError('Unknown QA phase: ' + str(phase))
    # Validate both entrypoints together even before a runtime-only install.
    # A build lock may not silently override the reviewed runtime pins.
    complete, hashes = read_lock_graph((LOCK, BUILD_LOCK), allowed_root=LOCK.parent)
    expected = complete if phase == 'build' else parse_lock(LOCK)
    return expected, hashes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--phase', choices=('runtime', 'build'), required=True)
    parser.add_argument('--check-locks', action='store_true',
                        help='Validate lock files before pip; do not attest installed packages')
    parser.add_argument('--wheel-receipt', type=Path,
                        help='Recheck actual wheels, pip install report and source-bound SBOM')
    parser.add_argument('--sca-report', type=Path,
                        help='Require a complete source-bound Python-distribution SCA report')
    args = parser.parse_args()
    deps, hashes = profile_lock(args.phase)
    if args.check_locks:
        print('AIO_QA_LOCK_GRAPH=PASS')
        print('AIO_QA_LOCK_GRAPH_SHA256=' + json.dumps(hashes, sort_keys=True))
        return
    if sys.version_info[:2] != (3, 11):
        raise RuntimeError('QA requires reviewed Python 3.11')
    observed = {}
    for canonical, (name, expected) in sorted(deps.items()):
        actual = metadata.version(name)
        if actual != expected:
            raise RuntimeError(name + ': expected ' + expected + ', got ' + actual)
        observed[canonical] = actual
    source = os.environ.get('AIO_QA_SOURCE_SHA', '')
    if not re.fullmatch(r'[a-f0-9]{40}', source):
        raise RuntimeError('QA manifest must be bound to a verified source revision')
    wheel_evidence = None
    if args.wheel_receipt:
        sys.path.insert(0, str(ROOT))
        from tools.build.qa_wheels import verify_receipt
        wheel_evidence = verify_receipt(args.wheel_receipt, args.phase, source)
    pin = json.loads(PIN.read_text(encoding='utf-8'))
    payload = {
        'schema': 1, 'sourceRevision': source,
        'python': platform.python_version(), 'platform': sys.platform,
        'phase': args.phase, 'packages': observed,
        'lockSha256': {item.name: hashes[item.relative_to(LOCK.parent).as_posix()]
                       for item in (LOCK, BUILD_LOCK)},
        'resolvedLockSha256': hashes,
        'libreOfficeVersion': pin['version'],
        'libreOfficeMsiSha256': pin['sha256'],
        'wheelHashesVerified': wheel_evidence is not None,
    }
    if wheel_evidence is not None:
        payload.update({'wheelLockSha256': wheel_evidence['wheelLockSha256'],
                        'wheels': wheel_evidence['wheels'],
                        'sbomFile': Path(wheel_evidence['sbom']).name,
                        'sbomSha256': wheel_evidence['sbomSha256'],
                        'scaStatus': wheel_evidence['scaStatus']})
    if args.sca_report:
        sys.path.insert(0, str(ROOT))
        from tools.build.qa_sca import verify_report
        sca = verify_report(args.sca_report, args.phase, source)
        payload.update({'scaStatus': 'pass_python_distribution_scope',
                        'scaScope': sca['scope'],
                        'scaReportFile': args.sca_report.name,
                        'scaReportSha256': hashlib.sha256(args.sca_report.read_bytes()).hexdigest()})
    if args.phase == 'build':
        MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        MANIFEST.write_text(json.dumps(payload, sort_keys=True, indent=2) + '\n',
                            encoding='utf-8')
    print('AIO_QA_DEPENDENCY_LOCK=PASS')
    print('AIO_QA_DEPENDENCY_MANIFEST=' + json.dumps(payload, sort_keys=True))


if __name__ == '__main__':
    main()
