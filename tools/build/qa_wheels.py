"""Install only reviewed wheel bytes; produce source-bound SBOM/evidence.

The supported QA profile is CPython 3.11, x86-64 Windows/Linux. This attests
Python wheels, not bundled Qt/LibreOffice CVEs or a reproducible installer.
"""
import argparse
from email.parser import BytesParser
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
from urllib.parse import urlsplit
from urllib.request import url2pathname
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.build.verify_qa_dependency_lock import profile_lock

WHEEL_LOCK = ROOT / 'tools/build/qa-wheel-hashes.json'


def canonical(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def host_profile():
    if platform.machine().lower() not in ('amd64', 'x86_64'):
        raise ValueError('Unreviewed QA wheel architecture')
    return {'win32': 'windows-x86_64', 'linux': 'linux-x86_64'}[sys.platform]


def selection(phase, *, lock_path=None, profile=None):
    path = Path(lock_path or WHEEL_LOCK)
    lock = json.loads(path.read_text(encoding='utf-8'))
    deps, _ = profile_lock(phase)
    complete, _ = profile_lock('build')
    packages = lock.get('packages', {})
    if lock.get('schema') != 1 or set(packages) != set(complete):
        raise ValueError('Wheel lock package inventory differs from version locks')
    profile = profile or host_profile()
    result = {}
    for name, (display_name, version) in complete.items():
        package = packages[name]
        if package.get('version') != version:
            raise ValueError('Stale wheel lock version: ' + name)
        wheel = package.get('profiles', {}).get(profile)
        if (not isinstance(wheel, dict) or
                not re.fullmatch(r'[A-Za-z0-9_.+-]+\.whl', wheel.get('file', '')) or
                not re.fullmatch(r'[a-f0-9]{64}', wheel.get('sha256', ''))):
            raise ValueError('Missing/invalid reviewed wheel: ' + name)
        if name in deps:
            result[name] = {**wheel, 'name': display_name, 'version': version}
    if len({item['file'] for item in result.values()}) != len(result):
        raise ValueError('Duplicate reviewed wheel filename')
    return result


def requirements(selected):
    return ''.join(item['name'] + '==' + item['version'] +
                   ' --hash=sha256:' + item['sha256'] + '\n'
                   for name, item in sorted(selected.items()))


def verify_wheelhouse(folder, selected):
    folder = Path(folder)
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError('Wheelhouse must be a real directory')
    paths = list(folder.iterdir())
    expected = {item['file'] for item in selected.values()}
    if {path.name for path in paths} != expected:
        raise ValueError('Wheelhouse inventory differs from reviewed wheels')
    evidence = []
    for name, item in sorted(selected.items()):
        path = folder / item['file']
        if path.is_symlink() or not path.is_file() or sha256(path) != item['sha256']:
            raise ValueError('Wheel SHA-256 mismatch: ' + path.name)
        with zipfile.ZipFile(path) as archive:
            metadata = [entry for entry in archive.infolist()
                        if entry.filename.count('/') == 1 and
                        entry.filename.endswith('.dist-info/METADATA')]
            if len(metadata) != 1 or metadata[0].file_size > 2 * 1024 * 1024:
                raise ValueError('Ambiguous/oversized wheel metadata: ' + path.name)
            info = BytesParser().parsebytes(archive.read(metadata[0]))
            if (len(info.get_all('Name', [])) != 1 or
                    len(info.get_all('Version', [])) != 1 or
                    canonical(info['Name']) != name or info['Version'] != item['version']):
                raise ValueError('Wheel metadata differs from locked package: ' + path.name)
        evidence.append({'package': name, 'version': item['version'],
                         'file': path.name, 'sha256': item['sha256'],
                         'bytes': path.stat().st_size})
    return evidence


def verify_install_report(path, folder, selected):
    report = json.loads(Path(path).read_text(encoding='utf-8'))
    installed = {}
    for entry in report.get('install', []):
        info = entry.get('metadata', {})
        name = canonical(info.get('name', ''))
        expected = selected.get(name)
        download = entry.get('download_info', {})
        uri = urlsplit(download.get('url', ''))
        if (expected is None or name in installed or info.get('version') != expected['version'] or
                uri.scheme != 'file' or uri.netloc or uri.query or uri.fragment or
                Path(url2pathname(uri.path)).resolve() != (Path(folder) / expected['file']).resolve() or
                download.get('archive_info', {}).get('hashes', {}).get('sha256') != expected['sha256']):
            raise ValueError('pip did not install the exact verified wheel: ' + name)
        installed[name] = info['version']
    if set(installed) != set(selected):
        raise ValueError('pip install report is incomplete')
    return installed


def sbom(source, evidence, profile):
    root_ref = 'aio-source:' + source
    return {'bomFormat': 'CycloneDX', 'specVersion': '1.6', 'version': 1,
            'metadata': {'component': {'type': 'application', 'name': 'A.I.O',
                         'version': source, 'bom-ref': root_ref},
                         'properties': [{'name': 'aio:profile', 'value': profile},
                                        {'name': 'aio:scope', 'value': 'Python wheel distributions only; vendored/native components not expanded; SCA is a separate report'}]},
            'components': [{'type': 'library', 'name': item['package'], 'version': item['version'],
                            'purl': 'pkg:pypi/' + item['package'] + '@' + item['version'],
                            'bom-ref': 'pkg:pypi/' + item['package'] + '@' + item['version'],
                            'hashes': [{'alg': 'SHA-256', 'content': item['sha256']}],
                            'properties': [{'name': 'aio:wheel', 'value': item['file']}]}
                           for item in evidence],
            'dependencies': [{'ref': root_ref, 'dependsOn': [
                'pkg:pypi/' + item['package'] + '@' + item['version'] for item in evidence]}]}


def verify_receipt(path, phase, source):
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    if (value.get('schema') != 1 or value.get('phase') != phase or
            value.get('sourceRevision') != source or value.get('profile') != host_profile() or
            value.get('scaStatus') != 'not_run' or
            value.get('wheelLockSha256') != sha256(WHEEL_LOCK)):
        raise ValueError('Wheel receipt is stale or belongs to a different build')
    selected = selection(phase)
    evidence = verify_wheelhouse(value['wheelhouse'], selected)
    if value.get('wheels') != evidence:
        raise ValueError('Wheel receipt inventory mismatch')
    report_path = Path(value['pipReport'])
    if value.get('pipReportSha256') != sha256(report_path):
        raise ValueError('pip install report changed')
    verify_install_report(report_path, value['wheelhouse'], selected)
    bom_path = Path(value['sbom'])
    if (value.get('sbomSha256') != sha256(bom_path) or
            json.loads(bom_path.read_text(encoding='utf-8')) != sbom(source, evidence, value['profile'])):
        raise ValueError('SBOM differs from installed wheel evidence')
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase', choices=('runtime', 'build'), required=True)
    parser.add_argument('--wheelhouse', type=Path, required=True)
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 11):
        raise ValueError('QA wheel profile requires CPython 3.11')
    source = os.environ.get('AIO_QA_SOURCE_SHA', '')
    if not re.fullmatch(r'[a-f0-9]{40}', source):
        raise ValueError('Wheel installation requires an exact source SHA')
    selected = selection(args.phase)
    folder = args.wheelhouse.resolve() / args.phase
    folder.mkdir(parents=True, exist_ok=False)
    req = folder.with_suffix('.requirements.txt')
    req.write_text(requirements(selected), encoding='ascii')
    report = folder.with_suffix('.pip-report.json')
    pip = [sys.executable, '-m', 'pip', '--isolated', '--disable-pip-version-check']
    subprocess.run(pip + ['download', '--index-url', 'https://pypi.org/simple',
        '--only-binary=:all:', '--require-hashes', '-r', str(req), '--dest', str(folder)], check=True)
    evidence = verify_wheelhouse(folder, selected)
    subprocess.run(pip + ['install', '--no-index', '--find-links', str(folder),
        '--only-binary=:all:', '--require-hashes', '--force-reinstall',
        '-r', str(req), '--report', str(report)], check=True)
    verify_install_report(report, folder, selected)
    if verify_wheelhouse(folder, selected) != evidence:
        raise ValueError('Wheels changed during installation')
    destination = ROOT / 'build'
    destination.mkdir(exist_ok=True)
    bom_path = destination / ('aio-qa-sbom-' + args.phase + '.cdx.json')
    bom_path.write_text(json.dumps(sbom(source, evidence, host_profile()), sort_keys=True, indent=2) + '\n', encoding='utf-8')
    value = {'schema': 1, 'sourceRevision': source, 'phase': args.phase,
             'profile': host_profile(), 'wheelLockSha256': sha256(WHEEL_LOCK),
             'wheelhouse': str(folder), 'wheels': evidence,
             'pipReport': str(report), 'pipReportSha256': sha256(report),
             'sbom': str(bom_path), 'sbomSha256': sha256(bom_path), 'scaStatus': 'not_run'}
    receipt = destination / ('aio-qa-wheels-' + args.phase + '.json')
    receipt.write_text(json.dumps(value, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    verify_receipt(receipt, args.phase, source)
    print('AIO_QA_WHEEL_HASHES=PASS; SBOM=PASS; SCA=NOT_RUN', flush=True)


if __name__ == '__main__':
    main()
