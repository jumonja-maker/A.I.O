"""Build icon resources from the exact owner-approved PNGs restored from AIO.

Build/QA only; Qt is already an application dependency. No network, redesign,
thumbnail upscaling or image processing runs at application startup.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import struct

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QByteArray, QBuffer, QIODevice, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter

ROOT = Path(__file__).resolve().parents[1] / 'assets' / 'brand'
ICO_SIZES = (16, 20, 24, 32, 48, 64, 128, 256)
APPROVED = {
    'aio-new-brand-source.png': (
        '34b6491b56bae5f5eb7c014e0bcd7ede3151c0daff11d6732904ed225a5e0f37',
        '4288071be3e10f33227aacd1f495f1d0d5f03783'),
    'elva-v13-happy-source.png': (
        '87e2b8dc069bee52ec796ea03d31e618789b64ffeb9c8a66ea50f09f49db42c5',
        '6eb9e3b15bf369d298af0b98a64db152bf59618a'),
}


def master(name):
    path = ROOT / name
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != APPROVED[name][0]:
        raise RuntimeError('Не совпал утверждённый оригинал: ' + name)
    image = QImage(str(path))
    if image.isNull() or (image.width(), image.height()) != (1254, 1254) or not image.hasAlphaChannel():
        raise RuntimeError('Нужен оригинальный прозрачный PNG 1254×1254: ' + name)
    return image


def downsample(image, size):
    if size > min(image.width(), image.height()):
        raise ValueError('Refusing to upscale a thumbnail')
    scaled = image.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                          Qt.TransformationMode.SmoothTransformation)
    result = QImage(size, size, QImage.Format.Format_ARGB32)
    result.fill(0)
    painter = QPainter(result)
    try:
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawImage((size-scaled.width())//2, (size-scaled.height())//2, scaled)
    finally:
        painter.end()
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not result.save(buffer, 'PNG'):
        raise RuntimeError('Cannot encode brand PNG')
    buffer.close()
    return bytes(data)


def ico(frames):
    offset = 6 + 16 * len(frames)
    output = bytearray(struct.pack('<HHH', 0, 1, len(frames)))
    for size, data in frames:
        output.extend(struct.pack('<BBBBHHII', size % 256, size % 256,
                                  0, 0, 1, 32, len(data), offset))
        offset += len(data)
    for _, data in frames:
        output.extend(data)
    return bytes(output)


def build():
    app = QGuiApplication.instance() or QGuiApplication(['aio-brand-builder'])
    _ = app
    mark, mascot = master('aio-new-brand-source.png'), master('elva-v13-happy-source.png')
    target = ROOT / 'icons'
    target.mkdir(exist_ok=True)
    generated = {}
    frames = {size: downsample(mark, size) for size in ICO_SIZES}
    generated['icons/aio.ico'] = ico(list(frames.items()))
    for size in (64, 256):
        generated[f'icons/aio-{size}.png'] = frames[size]
    for size in (64, 128, 256, 512):
        generated[f'elva-v13-happy-{size}.png'] = downsample(mascot, size)
    for name, data in generated.items():
        (ROOT / name).write_bytes(data)
    provenance = {
        'sourceRepository': 'jumonja-maker/All-in-One-',
        'sourceCommit': '7856c110b0898c578530bc0c72ae5e065b4f2cfd',
        'originals': {name: {'sha256': sha, 'gitBlob': blob, 'size': [1254, 1254]}
                      for name, (sha, blob) in APPROVED.items()},
        'derivatives': {name: {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
                        for name, data in generated.items()},
        'icoSizes': list(ICO_SIZES),
        'artworkModified': False,
        'thumbnailUpscaled': False,
    }
    (ROOT / 'BRAND_PROVENANCE.json').write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': 'PASS', 'originals': 2, 'generated': len(generated),
                      'icoSizes': list(ICO_SIZES)}, ensure_ascii=False))


if __name__ == '__main__':
    build()
