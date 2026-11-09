"""Build a source-bound, disposable Inno installer without downloading tools.

The normal Windows mode invokes the supplied preinstalled ISCC 6 compiler.
--validate-only checks source/payload provenance on any platform and explicitly
does not attest a compiled installer, executable behavior, or installed GUI.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
APP_ID = '{C1B5474A-4A1D-42EC-B2CD-A57A2011F34E}'
INSTALLED_MANIFEST = 'AIO-install-source.json'
QA_VERSION = '2026.11.09'
ISS = ROOT / 'installer/windows/AIO-Installed-Acceptance.iss'
REPARSE_POINT = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def checked_path(value, *, must_exist=True):
    """Reject link/junction inputs before resolving their spelling away."""
    path = Path(value).absolute()
    require(not any(c in str(path) for c in ('"', '\r', '\n', '\0')),
            'Path cannot be represented safely in an Inno definition')
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            require(not must_exist or item != path, 'Required path is missing: ' + str(path))
            continue
        require(not stat.S_ISLNK(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & REPARSE_POINT,
                'Symlink/reparse path refused: ' + str(item))
    return path.resolve(strict=must_exist)


def digest(path):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & REPARSE_POINT,
            'Payload item is not a regular file: ' + str(path))
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    after = path.lstat()
    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
    require(identity(info) == identity(after) and stat.S_ISREG(after.st_mode) and
            not getattr(after, 'st_file_attributes', 0) & REPARSE_POINT,
            'File changed while hashing: ' + str(path))
    return value.hexdigest()


def source_identity(source_sha, *, root=ROOT):
    require(re.fullmatch(r'[0-9a-f]{40}', source_sha or ''), 'Expected source SHA must be 40 lowercase hex characters')
    def git(*args):
        return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True,
                              text=True, encoding='utf-8', timeout=30).stdout.strip()
    observed = git('rev-parse', '--verify', 'HEAD')
    require(observed == source_sha, 'Git HEAD differs from expected source SHA')
    require(not git('status', '--porcelain', '--untracked-files=no'), 'Tracked source tree is dirty')
    return {'sourceRevision': observed, 'sourceTree': git('rev-parse', 'HEAD^{tree}'),
            'trackedTreeClean': True}


def inventory(payload):
    payload = checked_path(payload)
    require(payload.is_dir(), 'Payload must be a directory')
    files, names = [], set()
    def visit(folder):
        for child in sorted(folder.iterdir(), key=lambda p: p.name.casefold()):
            info = child.lstat()
            require(not stat.S_ISLNK(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & REPARSE_POINT,
                    'Symlink/reparse payload item refused: ' + str(child))
            require(not any(c in child.name for c in ('\\', ':', '"', '\r', '\n')) and
                    child.name == child.name.rstrip(' .'), 'Ambiguous Windows payload name: ' + child.name)
            relative = child.relative_to(payload).as_posix()
            require(relative.casefold() not in names, 'Case-colliding payload paths: ' + relative)
            names.add(relative.casefold())
            if stat.S_ISDIR(info.st_mode):
                visit(child)
            else:
                require(stat.S_ISREG(info.st_mode), 'Special payload item refused: ' + relative)
                files.append({'path': relative, 'size': info.st_size, 'sha256': digest(child)})
    visit(payload)
    require(files, 'Payload is empty')
    require(not any(row['path'].casefold() == INSTALLED_MANIFEST.casefold() for row in files),
            'Payload contains the reserved installed manifest filename')
    return {'root': str(payload), 'fileCount': len(files), 'files': files}


def read_json(path):
    require(path.stat().st_size <= 32 * 1024 * 1024, 'Oversized provenance JSON: ' + path.name)
    return json.loads(path.read_text(encoding='utf-8-sig'))


def validate_payload(payload, source_sha, *, root=ROOT):
    """Validate the build manifest and every payload byte; never execute it."""
    payload = checked_path(payload)
    value = inventory(payload)
    names = {row['path']: row for row in value['files']}
    required = ('A.I.O.exe', '_internal/build-info/aio-source-revision.txt',
                '_internal/build-info/aio-qa-dependencies.json',
                '_internal/build-info/libreoffice-release.json',
                '_internal/libreoffice/program/soffice.exe',
                '_internal/libreoffice/program/soffice.com',
                '_internal/libreoffice/program/soffice.bin',
                '_internal/libreoffice/program/python.exe')
    require(all(name in names and names[name]['size'] > 0 for name in required),
            'Complete executable/LibreOffice/build-info payload is missing')
    require(any(name.startswith('_internal/libreoffice/share/') for name in names), 'LibreOffice share payload is missing')
    require(any(name.startswith('_internal/libreoffice/') and Path(name).name.upper().startswith('LICENSE')
                for name in names), 'LibreOffice license payload is missing')
    build_info = payload / '_internal/build-info'
    require((build_info / 'aio-source-revision.txt').read_text(encoding='ascii').strip() == source_sha,
            'Embedded source revision differs from expected source SHA')
    pin = root / 'tools/build/libreoffice-release.json'
    require((build_info / pin.name).read_bytes() == pin.read_bytes(), 'Embedded LibreOffice pin differs from reviewed source')
    manifest = read_json(build_info / 'aio-qa-dependencies.json')
    engine_pin = read_json(pin)
    require(manifest.get('schema') == 1 and manifest.get('sourceRevision') == source_sha and
            manifest.get('phase') == 'build' and manifest.get('platform') == 'win32' and
            manifest.get('wheelHashesVerified') is True and
            manifest.get('scaStatus') == 'pass_python_distribution_scope',
            'Embedded dependency manifest is not a verified source-bound Windows build')
    require(manifest.get('libreOfficeVersion') == engine_pin['version'] and
            manifest.get('libreOfficeMsiSha256') == engine_pin['sha256'],
            'Embedded dependency manifest has a different LibreOffice pin')
    from tools.build.verify_qa_dependency_lock import profile_lock
    expected, hashes = profile_lock('build')
    require(manifest.get('packages') == {name: version for name, (_, version) in expected.items()} and
            manifest.get('resolvedLockSha256') == hashes,
            'Embedded dependency manifest differs from reviewed version locks')
    require(manifest.get('wheelLockSha256') == digest(root / 'tools/build/qa-wheel-hashes.json'),
            'Embedded wheel lock differs from reviewed source')
    for key, default in (('sbomFile', 'aio-qa-sbom-build.cdx.json'), ('scaReportFile', 'aio-qa-sca-build.json')):
        filename = manifest.get(key, default)
        require(isinstance(filename, str) and re.fullmatch(r'[A-Za-z0-9_.-]+', filename),
                'Unsafe embedded provenance filename')
        item = build_info / filename
        expected_hash = manifest.get('sbomSha256' if key == 'sbomFile' else 'scaReportSha256')
        require(item.is_file() and re.fullmatch(r'[0-9a-f]{64}', expected_hash or '') and
                digest(item) == expected_hash, 'Embedded SBOM/SCA digest differs from dependency manifest')
    sbom = read_json(build_info / manifest.get('sbomFile', 'aio-qa-sbom-build.cdx.json'))
    require(sbom.get('metadata', {}).get('component', {}).get('version') == source_sha,
            'Embedded SBOM is not bound to expected source')
    from tools.build.qa_sca import verify_report
    verify_report(build_info / manifest.get('scaReportFile', 'aio-qa-sca-build.json'), 'build', source_sha)
    value['dependencyManifestSha256'] = digest(build_info / 'aio-qa-dependencies.json')
    return value


def compiler_banner_major(text):
    """Read the compiler's native banner; ISCC may have no FileVersion."""
    require(isinstance(text, str) and len(text) <= 64 * 1024,
            'Missing or oversized native ISCC help response')
    matches = re.findall(r'^Inno Setup ([0-9]+)(?: \(32-bit\))? Command-Line Compiler\s*$',
                         text, flags=re.MULTILINE)
    require(len(matches) == 1 and int(matches[0]) in (6, 7),
            'Expected a native Inno Setup 6 or 7 command-line compiler banner')
    return int(matches[0])


def compiler_identity(iscc):
    iscc = checked_path(iscc)
    require(iscc.is_file(), 'ISCC compiler is missing')
    result = {'path': str(iscc), 'sha256': digest(iscc), 'size': iscc.stat().st_size,
              'pinStatus': 'observed_preinstalled_compiler_not_repository_hash_pinned',
              'version': 'NOT_OBSERVED', 'signatureStatus': 'NOT_OBSERVED'}
    if sys.platform == 'win32':
        script = r'''$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false)
$p=Get-Item -LiteralPath $env:AIO_ACCEPTANCE_ISCC_PATH
$s=Get-AuthenticodeSignature -LiteralPath $p.FullName
@{version=$p.VersionInfo.FileVersion;productVersion=$p.VersionInfo.ProductVersion;productName=$p.VersionInfo.ProductName;signatureStatus=[string]$s.Status;signerSubject=if($s.SignerCertificate){$s.SignerCertificate.Subject}else{$null};signerThumbprint=if($s.SignerCertificate){$s.SignerCertificate.Thumbprint}else{$null}} | ConvertTo-Json -Compress'''
        environment = dict(os.environ, AIO_ACCEPTANCE_ISCC_PATH=str(iscc))
        observed = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand',
            base64.b64encode(script.encode('utf-16le')).decode('ascii')], env=environment,
            check=True, capture_output=True, text=True, encoding='utf-8-sig', timeout=60)
        result.update(json.loads(observed.stdout))
        help_result = subprocess.run([str(iscc), '/?'], check=False, capture_output=True,
            text=True, encoding='utf-8-sig', errors='replace', timeout=30)
        banner = help_result.stdout
        result.update(nativeHelpExitCode=help_result.returncode, nativeHelpBanner=banner,
                      nativeHelpStderr=help_result.stderr)
        # A missing/empty Windows VERSIONINFO resource is not evidence that a
        # real supplied compiler is unsupported. The native executable reports
        # its own compiler major in /?; retain resource/signature facts as-is.
        try:
            require(help_result.returncode == 0, 'Native ISCC help returned a nonzero exit status')
            result['compilerMajor'] = compiler_banner_major(banner)
        except ValueError as error:
            raise ValueError(str(error) + '; observed compiler identity=' +
                             json.dumps(result, ensure_ascii=True)) from error
        require(digest(iscc) == result['sha256'], 'Compiler changed while observing native identity')
        result['versionBasis'] = 'native_ISCC_help_banner_major; Windows_file_version_recorded_separately'
    return result


def build(*, iscc, payload, output, source_sha, validate_only=False):
    identity = source_identity(source_sha)
    payload = checked_path(payload)
    output = checked_path(output, must_exist=False)
    require(not output.exists(), 'Output directory must be new; overwrite refused')
    require(not output.is_relative_to(payload) and not payload.is_relative_to(output), 'Output and payload must be separate trees')
    require(ISS.is_file(), 'Acceptance Inno source is missing')
    compiler = compiler_identity(iscc)
    contents = validate_payload(payload, source_sha)
    output.mkdir(parents=True, exist_ok=False)
    receipt = {'schema': 1, 'status': 'VALIDATED_NOT_COMPILED', **identity, 'appId': APP_ID,
               'version': QA_VERSION + '+' + source_sha[:12], 'installedManifestFilename': INSTALLED_MANIFEST,
               'scope': 'source-bound disposable packaging; not a release, deterministic build or GUI acceptance',
               'payload': contents, 'compiler': compiler, 'installer': None,
               'installerSource': {'path': str(ISS), 'sha256': digest(ISS)}}
    installed_receipt = output / INSTALLED_MANIFEST
    installed = {key: value for key, value in receipt.items() if key not in ('status', 'installer', 'scope')}
    installed['scope'] = 'expected installed payload from exact reviewed source; installer is tested separately'
    installed_receipt.write_text(json.dumps(installed, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')
    receipt['installedManifestSha256'] = digest(installed_receipt)
    target = output / 'installer-build.json'
    try:
        if not validate_only:
            require(sys.platform == 'win32', 'Compilation requires Windows; use --validate-only for static controls')
            command = [str(checked_path(iscc)), '/DAIOPayload=' + str(payload),
                '/DAIOSourceReceipt=' + str(installed_receipt), '/DAIOOutput=' + str(output),
                '/DAIOSourceSHA=' + source_sha, '/DAIOVersion=' + receipt['version'], str(ISS)]
            receipt['command'] = command
            require(digest(checked_path(iscc)) == compiler['sha256'], 'Compiler changed before compilation')
            require(digest(installed_receipt) == receipt['installedManifestSha256'],
                    'Installed source receipt changed before compilation')
            with (output / 'compiler.log').open('wb') as log:
                subprocess.run(command, check=True, stdout=log, stderr=subprocess.STDOUT, timeout=1200)
            require(source_identity(source_sha) == identity, 'Source identity changed during compilation')
            require(validate_payload(payload, source_sha) == contents, 'Payload changed during compilation')
            require(digest(checked_path(iscc)) == compiler['sha256'], 'Compiler changed during compilation')
            require(digest(installed_receipt) == receipt['installedManifestSha256'],
                    'Installed source receipt changed during compilation')
            require(digest(ISS) == receipt['installerSource']['sha256'], 'Inno source changed during compilation')
            executable = checked_path(output / ('AIO-Installed-Acceptance-' + source_sha[:12] + '.exe'))
            require(executable.stat().st_size > 0, 'ISCC did not create an installer')
            receipt.update(status='PASS', installer={'path': str(executable), 'size': executable.stat().st_size,
                                                     'sha256': digest(executable)})
        return receipt
    except BaseException as error:
        receipt.update(status='FAIL', error={'type': type(error).__name__, 'message': str(error)})
        raise
    finally:
        target.write_text(json.dumps(receipt, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--iscc', type=Path, required=True)
    parser.add_argument('--payload', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--source-sha', required=True)
    parser.add_argument('--validate-only', action='store_true')
    args = parser.parse_args()
    receipt = build(iscc=args.iscc, payload=args.payload, output=args.output,
                    source_sha=args.source_sha, validate_only=args.validate_only)
    print('AIO_INSTALLER_BUILD=' + json.dumps({key: receipt[key] for key in
        ('schema', 'status', 'sourceRevision', 'appId', 'installedManifestFilename', 'installer')}, ensure_ascii=True))


if __name__ == '__main__':
    main()
