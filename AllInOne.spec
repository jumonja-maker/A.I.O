# -*- mode: python ; coding: utf-8 -*-
"""A.I.O Windows QA package with a pinned, complete LibreOffice payload."""
from pathlib import Path
import json
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
              'app_backend.aio_converter_upgrade'],
    noarchive=False,
)
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
