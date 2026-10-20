"""NIKITA Windows acceptance gate: real import-only LibreOfficeKit preflight.

This gate deliberately needs the *installed* LibreOffice bundle, not test fakes.
A green unit suite does not prove that Windows ships LibreOfficeKit DLLs.
No document export is requested. No install or release takes place.
"""
from __future__ import annotations

import io
import json
from pathlib import Path
import struct
import tempfile
import threading
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app_backend.converter_guard import ConverterGuard, LibreOfficeAdapter
from app_backend.converter_guard.routes import registry
from app_backend.libreoffice_catalog import registry_files


def bmp_fixture():
    width, height = 4, 4
    row = ((width * 3 + 3) // 4) * 4
    body = b''.join(
        b''.join(bytes((x * 37, y * 49, (x + y) * 21)) for x in range(width))
        + b'\x00' * (row - width * 3) for y in range(height)
    )
    info = struct.pack('<IiiHHIIiiII', 40, width, height, 1, 24, 0, len(body),
                       2835, 2835, 0, 0)
    return struct.pack('<2sIHHI', b'BM', 54 + len(body), 0, 0, 54) + info + body


def main():
    adapter = LibreOfficeAdapter()
    if not adapter.available:
        raise RuntimeError('GUARD_V06_PREFLIGHT_NIKITA: LibreOffice executable missing')
    reg = registry(adapter.executable)
    manifest = registry_files(adapter.executable)
    program = adapter.executable.resolve().parent.parent
    print('GUARD_V06_REGISTRY_DIAGNOSTIC ' + json.dumps({
        'engine': str(adapter.executable), 'installRoot': str(program),
        'registryFolder': str(program / 'share' / 'registry'),
        'registryDirectoryExists': (program / 'share' / 'registry').is_dir(),
        'registryFileCount': len(manifest),
        'registryFiles': [Path(x[0]).name for x in manifest][:20],
        'importExtensions': len(reg.imports), 'exportExtensions': len(reg.exports),
        'nativeTypes': len(reg.types), 'externalFilters': reg.external_filters,
        'registryValid': reg.valid,
        'extensionsDirExists': (program / 'share' / 'extensions').is_dir(),
        'extensionXCUFiles': [p.name for p in (program / 'share' / 'extensions').rglob('*.xcu')][:20]
           if (program / 'share' / 'extensions').is_dir() else []
    }, ensure_ascii=False), flush=True)
    if not reg.valid:
        raise RuntimeError('GUARD_V06_PREFLIGHT_NIKITA: installed LibreOffice native registry unavailable')
    with tempfile.TemporaryDirectory(prefix='aio-guard-v06-windows-') as directory:
        root = Path(directory)
        content = bmp_fixture()
        guard = ConverterGuard(adapter=adapter, spool_parent=root)
        try:
            item = {'id': 'source1', 'name': 'photo.bmp', 'size': len(content)}
            made = guard.create([item], prepare_only=True)
            identity = made['jobId']
            guard.upload(identity, 'source1', io.BytesIO(content), len(content))
            state = guard.status(identity)
            if state['attempted'] != 0 or state['total'] != 0:
                raise AssertionError('Preflight created exports before inspecting')
            info = guard.preflight(identity)
            proof = info['files'][0]['preflight']
            if proof.get('state') != 'detected':
                raise AssertionError('Installed Windows LibreOfficeKit cannot detect BMP: ' +
                                     json.dumps(proof, ensure_ascii=False))
            pdf = next((x for x in info['formats'] if x['id'] == 'pdf'), None)
            dbf = next((x for x in info['formats'] if x['id'] == 'dbf'), None)
            if not pdf or 'source1' not in pdf['verifiedFileIds']:
                raise AssertionError('BMP → PDF must have preflight native proof on NIKITA')
            if dbf and 'source1' not in dbf['incompatibleFileIds']:
                raise AssertionError('BMP → DBF must be excluded before export on NIKITA')
            status = guard.status(identity)
            if status['state'] != 'prepared' or status['attempted'] != 0 or status['total'] != 0:
                raise AssertionError('Export attempted during native preflight')
            print('GUARD_V06_NIKITA_PREFLIGHT_PASS ' + json.dumps({
                'engine': str(adapter.executable), 'nativeKind': proof.get('nativeKind'),
                'preflightChecks': info.get('preflightChecks'), 'exportsBeforeStart': status['attempted'],
                'bmpPdfVerified': True, 'bmpDbfExcluded': dbf is not None
            }, ensure_ascii=False), flush=True)
        finally:
            guard.close()


if __name__ == '__main__':
    main()
