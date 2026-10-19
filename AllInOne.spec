# -*- mode: python ; coding: utf-8 -*-
"""Clean A.I.O shell + Excel, no legacy converter, no LibreOffice bundle yet.

Build: python -m PyInstaller --noconfirm --clean AllInOne.spec
Do NOT market this as a finished conversion-capable release.
"""
from pathlib import Path

root = Path(SPECPATH)
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
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='A.I.O',
    console=False,
    debug=False,
    strip=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name='A.I.O', strip=False, upx=False)
