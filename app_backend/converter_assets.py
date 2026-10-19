"""Materialize the owner-approved historical UI; never import converter backend.

Only files physically bundled with All in One are used. No internet or network
requests, no runtime user files and no dynamically evaluated code on the server.
"""
from __future__ import annotations

import base64
from functools import lru_cache
import hashlib
from pathlib import Path
import zlib

EXPECTED = {
    'file-converter.js': ('f652ef7f973bc40624dcb70c4e213b28893498b5ac0fe415084d3f2f0ece7319', 30890),
    'file-converter.css': ('16cccaf1a0ba377780d617cfc969047c4b943ad1c26d770c55de5210c92d1e6b', 12426)
}


def _bounded_decompress(encoded: str, expected_size: int) -> bytes:
    """Reject oversized, incomplete or trailing zlib streams before hashing."""
    compressed = base64.b64decode(encoded, validate=True)
    decoder = zlib.decompressobj()
    try:
        raw = decoder.decompress(compressed, expected_size + 1)
    except zlib.error as exc:
        raise ValueError('Designer addon has invalid compressed data') from exc
    if (len(raw) != expected_size or not decoder.eof or
            decoder.unused_data or decoder.unconsumed_tail):
        raise ValueError('Designer addon has invalid size or zlib framing')
    return raw


@lru_cache(maxsize=2)
def read_approved_designer_asset(root: str, name: str) -> bytes:
    if name not in EXPECTED:
        raise ValueError('Unknown designer asset')
    directory = Path(root) / 'addons' / 'file-converter' / 'payload'
    if name.endswith('.css'):
        chunks = [directory / 'file-converter.css.zlib.b64']
    else:
        chunks = [directory / ('file-converter.js.zlib.part%02d.txt' % i)
                  for i in (1, 2, 3)]
    encoded = ''.join(item.read_text(encoding='ascii').strip() for item in chunks)
    expected_hash, expected_size = EXPECTED[name]
    raw = _bounded_decompress(encoded, expected_size)
    if len(raw) != expected_size or hashlib.sha256(raw).hexdigest() != expected_hash:
        raise ValueError('Designer addon asset failed SHA-256 verification')
    return raw


@lru_cache(maxsize=2)
def read_designer_asset(root: str, name: str) -> bytes:
    raw = read_approved_designer_asset(root, name)
    if name == 'file-converter.js':
        # A drop on the file list bubbles into the surrounding pane; both
        # containers register drop listeners in designer 11's original addon.
        # This made one dragged file appear twice. Apply an exact, fail-closed
        # behavioral fix AFTER checking the original designer asset hash.
        # It changes no DOM/CSS, so the approved design remains untouched.
        target = b'event.preventDefault();appendFiles(event.dataTransfer.files);'
        if raw.count(target) != 1:
            raise ValueError('Unexpected designer addon drag/drop handler')
        raw = raw.replace(target,
                          b'event.preventDefault();event.stopPropagation();'
                          b'appendFiles(event.dataTransfer.files);')
        from .converter_multiselect_patch import apply_multiselect
        raw = apply_multiselect(raw)
        from .converter_ui11_design_patch import apply_ui11_design
        raw = apply_ui11_design(raw)
        from .converter_target_ui_patch import apply_target_recovery
        raw = apply_target_recovery(raw)
        from .converter_issues_ui_patch import apply_issues_ui
        raw = apply_issues_ui(raw)
        from .converter_selection_design_patch import apply_selection_design
        raw = apply_selection_design(raw)
        from .converter_diagnostics_ui_patch import apply_diagnostics_ui
        raw = apply_diagnostics_ui(raw)
        # Dispose Guard observers/results when destroying the mounted view.
        old = b'alive=false;inspectionToken++;closePopover();'
        if raw.count(old) != 1:
            raise ValueError('Unexpected converter disposal boundary')
        raw = raw.replace(old, b'alive=false;inspectionToken++;adapter.resetProgress?.();closePopover();')
        old = b'async function inspect() {'
        if raw.count(old) != 1:
            raise ValueError('Unexpected converter input-change boundary')
        raw = raw.replace(old, old + b'\n      adapter.resetProgress?.();')
        old = (b"formatValues={};currentResult=null;retryResult=null;status='idle';"
               b"statusMessage='';renderFormats();renderOptions();renderSummary();")
        if raw.count(old) != 1:
            raise ValueError('Unexpected converter format-change boundary')
        raw = raw.replace(old, b'adapter.resetProgress?.();' + old)
        # Advanced and the historical conversion backend intentionally stay
        # in the archived repository. The native Guard is connected only through
        # the frontend adapter; no conversion code is materialized here.
    return raw
