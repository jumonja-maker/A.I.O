"""Inspectible route decisions and safe structured tracing.

Mandatory file safety remains independent from togglable QA rules. This module
does not convert, does not infer output support, and does not import the old
conversion backend. Engine adapters will be registered only when tested.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import re
from time import monotonic
from uuid import uuid4

_FORMAT = re.compile(r'^[a-z0-9]{1,12}$')
_STAGE = frozenset(('input', 'route', 'libreoffice', 'output', 'save', 'compare'))
_STATUS = frozenset(('started', 'allowed', 'blocked', 'failed', 'completed', 'skipped'))


@dataclass(frozen=True)
class GuardEvent:
    stage: str
    status: str
    code: str
    seconds: float


@dataclass
class GuardTrace:
    source_format: str
    target_format: str
    trace_id: str = field(default_factory=lambda: uuid4().hex)
    events: list[GuardEvent] = field(default_factory=list)
    _start: float = field(default_factory=monotonic, repr=False)

    def record(self, stage: str, status: str, code: str):
        if stage not in _STAGE or status not in _STATUS or not re.fullmatch(
                r'[A-Z][A-Z0-9_]{1,60}', code):
            raise ValueError('Invalid Guard event contract')
        self.events.append(GuardEvent(stage, status, code,
                                      round(monotonic() - self._start, 4)))

    def as_dict(self):
        # Never export document contents, filesystem paths, or error stack traces.
        return {
            'schema':'aio.guard.trace.v1',
            'traceId':self.trace_id,
            'sourceFormat':self.source_format,
            'targetFormat':self.target_format,
            'events':[asdict(e) for e in self.events],
            'engineWasLaunched':any(e.stage=='libreoffice' and
                                    e.status=='started' for e in self.events),
        }

    def report(self) -> bytes:
        return json.dumps(self.as_dict(), ensure_ascii=False,
                          indent=2).encode('utf-8')


class ConverterGuard:
    """The sole future UI ↔ LibreOffice conversion boundary.

    Adapter is intentionally NOT installed at migration bootstrap.
    Existing UI never falls back to the archived converter implementation.
    """
    def __init__(self):
        self.adapters = {}

    def begin(self, source_format: str, target_format: str):
        if not all(type(x) is str and _FORMAT.fullmatch(x)
                   for x in (source_format, target_format)):
            raise ValueError('Некорректные расширения исходного и выходного форматов.')
        trace = GuardTrace(source_format, target_format)
        trace.record('input', 'started', 'INPUT_RECEIVED')
        if (source_format, target_format) not in self.adapters:
            trace.record('route', 'blocked', 'NO_VERIFIED_ADAPTER')
        else:
            trace.record('route', 'allowed', 'ADAPTER_AVAILABLE')
        return trace

    def attach_verified_adapter(self, source: str, target: str, adapter):
        """Only explicit qualified LO paths; not an automatic file extension matrix."""
        if not callable(adapter) or not _FORMAT.fullmatch(source) or not _FORMAT.fullmatch(target):
            raise ValueError('Недопустимый адаптер.')
        self.adapters[(source,target)] = adapter
