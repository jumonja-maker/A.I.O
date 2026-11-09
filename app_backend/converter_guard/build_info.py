"""Source-commit provenance for reproducible conversion reports.

A bundled source SHA is embedded by a trusted build; no Git or network lookup
is ever performed by the installed application.
"""
import os
from pathlib import Path
import re
import sys


_SHA = re.compile(r'[0-9a-f]{40}')


def report_build_identity():
    revision = os.environ.get('AIO_BUILD_REVISION', '')
    if _SHA.fullmatch(revision):
        return {'scope': 'report_runtime', 'sourceRevision': revision, 'basis': 'configured'}
    if getattr(sys, 'frozen', False):
        root = getattr(sys, '_MEIPASS', None)
        if isinstance(root, str) and root:
            try:
                value = (Path(root) / 'build-info' /
                         'aio-source-revision.txt').read_text(encoding='ascii').strip()
            except (OSError, UnicodeError):
                value = ''
            if _SHA.fullmatch(value):
                return {'scope': 'report_runtime', 'sourceRevision': value, 'basis': 'bundled'}
    return {'scope': 'report_runtime', 'sourceRevision': None, 'basis': 'unknown'}
