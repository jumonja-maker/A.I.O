# -*- mode: python ; coding: utf-8 -*-
"""A.I.O UI + Excel + direct LibreOffice QA (no Converter Guard).

Windows distributable requires a complete verified LibreOffice directory.
All source and profile safety checks belong to the app, not Guard.
"""
from pathlib import Path
import os
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

a = Analysis(
    [str(root / 'launcher.py')],
    pathex=[str(root)],
    binaries=[],
    datas=assets,
    hiddenimports=['PySide6.QtWebEngineWidgets', 'PySide6.QtWebEngineCore',
                   'PySide6.QtWidgets', 'PySide6.QtGui', 'PySide6.QtCore'],
    hookspath=[],
    runtime_hooks=[],
    excludes=['app_backend.converter_universal',
              'app_backend.converter_docx',
              'app_backend.aio_converter_upgrade'],
    noarchive=False,
)
# A private complete, unmodified LibreOffice tree is bundled for Windows QA.
# Do not produce a misleading converter-ready setup without the actual engine.
if sys.platform == 'win32':
    configured = os.environ.get('AIO_LIBREOFFICE_ROOT')
    if not configured:
        raise RuntimeError('AIO_LIBREOFFICE_ROOT is required for Windows LO builds.')
    lo_root = Path(configured).resolve(strict=True)
    for item in ('program/soffice.exe', 'program/soffice.bin'):
        if not (lo_root / item).is_file():
            raise RuntimeError('Missing complete LibreOffice payload: ' + item)
    if not (lo_root / 'share').is_dir():
        raise RuntimeError('Missing LibreOffice share directory')
    if not any(lo_root.rglob('LICENSE*')):
        raise RuntimeError('LibreOffice license text missing')
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
