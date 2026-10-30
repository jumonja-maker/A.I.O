# -*- mode: python ; coding: utf-8 -*-
"""A.I.O Windows QA package with a pinned, complete LibreOffice payload."""
from pathlib import Path
import json
import hashlib
import os
import re
import subprocess
import sys

root = Path(SPECPATH)
app_icon = root / 'assets' / 'brand' / 'icons' / 'aio.ico'
if not app_icon.is_file():
    raise RuntimeError('Approved A.I.O icon missing. Run tools/build_brand_icons.py before packaging.')
assets = []
for folder in ('src', 'addons', 'assets'):
    for path in (root / folder).rglob('*'):
        if path.is_file():
            assets.append((str(path), str(path.parent.relative_to(root))))
assets.append((str(root / 'index.html'), '.'))
engine_pin = root / 'tools' / 'build' / 'libreoffice-release.json'
assets.append((str(engine_pin), 'build-info'))
# Embed the exact checked-out SHA into the installed app, not merely CI logs.
source_revision = os.environ.get('AIO_QA_SOURCE_SHA') or os.environ.get('AIO_BUILD_REVISION', '')
if re.fullmatch(r'[0-9a-f]{40}', source_revision):
    revision_file = root / 'build' / 'aio-source-revision.txt'
    revision_file.parent.mkdir(parents=True, exist_ok=True)
    revision_file.write_text(source_revision + '\n', encoding='ascii')
    assets.append((str(revision_file), 'build-info'))
# The Windows QA package must contain the exact environment used by CI.
qa_manifest = root / 'build' / 'aio-qa-dependencies.json'
if qa_manifest.is_file():
    qa_info = json.loads(qa_manifest.read_text(encoding='utf-8'))
    if (qa_info.get('sourceRevision') != source_revision or
            qa_info.get('phase') != 'build' or
            qa_info.get('wheelHashesVerified') is not True):
        raise RuntimeError('QA dependency manifest does not match reviewed build')
    sbom_file = root / 'build' / 'aio-qa-sbom-build.cdx.json'
    if (not sbom_file.is_file() or
            hashlib.sha256(sbom_file.read_bytes()).hexdigest() != qa_info.get('sbomSha256')):
        raise RuntimeError('Missing or changed source-bound QA SBOM')
    assets.append((str(qa_manifest), 'build-info'))
    assets.append((str(sbom_file), 'build-info'))
    sca_file = root / 'build' / 'aio-qa-sca-build.json'
    if (qa_info.get('scaStatus') != 'pass_python_distribution_scope' or
            not sca_file.is_file() or
            hashlib.sha256(sca_file.read_bytes()).hexdigest() != qa_info.get('scaReportSha256')):
        raise RuntimeError('Missing or changed source-bound Python SCA report')
    assets.append((str(sca_file), 'build-info'))
elif os.environ.get('AIO_QA_REQUIRE_MANIFEST') == '1':
    # Source-contract tests execute the .spec before package preparation;
    # only the actual QA packaging stage requires the generated manifest.
    raise RuntimeError('Missing source-bound QA dependency manifest')


a = Analysis(
    [str(root / 'launcher.py')],
    pathex=[str(root)],
    binaries=[],
    datas=assets,
    hiddenimports=['PySide6.QtWebEngineWidgets', 'PySide6.QtWebEngineCore',
                   'PySide6.QtWidgets', 'PySide6.QtGui', 'PySide6.QtCore', 'PySide6.QtPdf'],
    hookspath=[],
    runtime_hooks=[],
    excludes=['app_backend.converter_universal',
              'app_backend.converter_docx',
              'app_backend.aio_converter_upgrade',
              'app_backend.blackbox_telemetry', 'app_backend.qt_observation',
              'app_backend.converter_guard.comparison_service',
              'app_backend.converter_guard.comparison_worker'],
    noarchive=False,
)
# A stale import must not silently bring a frozen feature back into EXE.
retired_names=('app_backend.blackbox_telemetry','app_backend.qt_observation',
               'app_backend.converter_guard.comparison_',
               'app_backend.converter_guard.preview',
               'app_backend.converter_guard.diagnostic_export')
if any(any(row[0].startswith(name) for name in retired_names) for row in a.pure):
    raise RuntimeError('Retired feature leaked into bundled Python modules')
if sys.platform == 'win32':
    configured = os.environ.get('AIO_LIBREOFFICE_ROOT')
    if not configured:
        raise RuntimeError('AIO_LIBREOFFICE_ROOT is required for Windows LO builds.')
    lo_root = Path(configured).resolve(strict=True)
    for item in ('program/soffice.exe', 'program/soffice.com', 'program/soffice.bin', 'program/python.exe'):
        if not (lo_root / item).is_file():
            raise RuntimeError('Missing complete LibreOffice payload: ' + item)
    if not (lo_root / 'share').is_dir() or not any(lo_root.rglob('LICENSE*')):
        raise RuntimeError('LibreOffice share directory or license text missing')
    pin = json.loads(engine_pin.read_text(encoding='utf-8'))
    version = subprocess.run([str(lo_root / 'program' / 'soffice.com'), '--version'],
                             check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             timeout=30, text=True, encoding='utf-8').stdout.strip()
    if not re.match(r'^LibreOffice\s+' + re.escape(pin['version']) + r'(?:\s|$)', version):
        raise RuntimeError('Bundled LibreOffice version differs from reviewed release pin: ' + version)
    print('AIO_BUNDLED_ENGINE_VERSION=' + version)
    a.datas += Tree(str(lo_root), prefix='libreoffice', typecode='DATA')

pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='A.I.O',
    console=False,
    debug=False,
    strip=False,
    upx=False,
    icon=str(app_icon),
)
coll = COLLECT(exe, a.binaries, a.datas, name='A.I.O', strip=False, upx=False)
