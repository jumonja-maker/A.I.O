"""Opt-in, write-only, best-effort A.I.O -> BLACK.BOX JSONL event producer.

There is intentionally no import of GUARD, Qt, LibreOffice or BLACK.BOX here.
All filesystem I/O happens on the daemon writer, never on a conversion thread.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import queue
import re
import secrets
import tempfile
import threading

FIELDS = frozenset({
    'type', 'seq', 'timestamp', 'request_id', 'file_id', 'target',
    'stage', 'status', 'duration_ms', 'reason_code', 'process_pid',
})
TYPE = re.compile(r'^[a-zA-Z][a-zA-Z0-9_.-]{0,63}$')
TARGET = re.compile(r'^[a-zA-Z0-9_-]{1,24}$')
CODE = re.compile(r'^[a-zA-Z][a-zA-Z0-9_.-]{0,63}$')
# Only known technical stage/status values are transmitted; never pass arbitrary
# exceptions, filenames, paths or document text from native/GUI code.
STAGES = frozenset({
    'upload', 'queue', 'plan', 'preflight', 'route', 'launch', 'engine',
    'import', 'export', 'result', 'packaging', 'bundle', 'save', 'storage',
    'cancel', 'complete', 'idle', 'recovery',
})
STATUSES = frozenset({
    'queued', 'running', 'ready', 'saved', 'partial', 'failed', 'error',
    'cancelled', 'skipped', 'completed', 'started', 'finished',
    'detected', 'undetermined', 'registered', 'unavailable',
})


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


class TelemetryEmitter:
    """Non-blocking bounded producer; single serialized best-effort file writer.

    seq is allocated *before* admission to the queue. Any dropped events
    produce detectable sequence gaps, and the writer emits telemetry_dropped.
    IDs are keyed hashes, stable only inside one A.I.O emitter session.
    """

    def __init__(self, path: Path | str, *, capacity: int = 8192):
        if not isinstance(capacity, int) or isinstance(capacity, bool) or not 1 <= capacity <= 65536:
            raise ValueError('Invalid telemetry capacity')
        requested = Path(path).expanduser()
        if not requested.is_absolute() or requested.suffix.lower() != '.jsonl':
            raise ValueError('Telemetry requires an absolute .jsonl destination')
        destination = requested.resolve()
        tmp = Path(tempfile.gettempdir()).resolve()
        if destination == tmp or destination.is_relative_to(tmp):
            raise ValueError('Telemetry must be outside conversion/temp directories')
        self.path = destination
        self._key = secrets.token_bytes(16)
        self._queue = queue.Queue(maxsize=capacity)
        self._sequence_lock = threading.Lock()
        self._next_seq = 0
        self._lost = 0
        self._closed = False
        self._error = None
        self._stop = threading.Event()
        self._writer_thread = threading.Thread(
            target=self._writer, name='aio-blackbox-jsonl', daemon=True)
        self._writer_thread.start()
        self.emit('telemetry_started', status='started', stage='idle')

    def _opaque(self, value):
        if value is None:
            return None
        # Never store the raw value. This also protects user-supplied file IDs
        # and legacy request identifiers that might contain document names.
        raw = str(value).encode('utf-8', errors='replace')[:2048]
        return hashlib.blake2s(raw, key=self._key, digest_size=16).hexdigest()

    @property
    def dropped(self):
        return self._lost

    @property
    def failed(self):
        return self._error is not None

    def emit(self, kind, *, request_id=None, file_id=None, target=None,
             stage=None, status=None, duration_ms=None, reason_code=None,
             process_pid=None):
        """Safe even when called from under the GUARD RLock: no disk or waits."""
        try:
            if not TYPE.fullmatch(kind):
                return
            event = {'type': kind, 'timestamp': _utc_now()}
            if request_id is not None:
                event['request_id'] = self._opaque(request_id)
            if file_id is not None:
                event['file_id'] = self._opaque(file_id)
            if isinstance(target, str):
                event['target'] = target.lower() if TARGET.fullmatch(target) else 'unknown'
            if stage in STAGES:
                event['stage'] = stage
            if status in STATUSES:
                event['status'] = status
            if isinstance(reason_code, str):
                event['reason_code'] = reason_code if CODE.fullmatch(reason_code) else 'REDACTED'
            if type(duration_ms) in (int, float) and 0 <= duration_ms <= 2 ** 53:
                event['duration_ms'] = int(duration_ms)
            if type(process_pid) is int and process_pid > 0:
                event['process_pid'] = process_pid
            with self._sequence_lock:
                if self._closed or self._error:
                    return
                self._next_seq += 1
                event['seq'] = self._next_seq
                try:
                    self._queue.put_nowait(event)
                except queue.Full:
                    self._lost += 1
        except Exception:
            # Telemetry is not a reason to fail, cancel or slow an export.
            pass

    def _writer(self):
        # The entire lifecycle of opening, mkdir and writing is outside GUARD.
        import json
        expected = 1
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Never append with a seq reset to an earlier producer session.
            with self.path.open('x', encoding='utf-8', buffering=1) as stream:
                while True:
                    try:
                        event = self._queue.get(timeout=0.1)
                    except queue.Empty:
                        if self._stop.is_set() and self._queue.empty():
                            break
                        continue
                    seq = event['seq']
                    if seq > expected:
                        missing = seq - expected
                        stream.write(json.dumps({
                            'type': 'telemetry_dropped', 'seq': expected,
                            'timestamp': _utc_now(), 'stage': 'idle',
                            'status': 'failed', 'reason_code': 'QUEUE_OVERFLOW',
                            # BLACK.BOX v0.1 preserves only known fields.
                            'duration_ms': missing,
                        }, separators=(',', ':')) + '\n')
                    stream.write(json.dumps(event, separators=(',', ':'), ensure_ascii=True) + '\n')
                    expected = seq + 1
                # Also declare loss when the last attempted event was dropped,
                # with no later admitted event to expose the sequence gap.
                with self._sequence_lock:
                    last = self._next_seq
                if last >= expected:
                    stream.write(json.dumps({
                        'type': 'telemetry_dropped', 'seq': last,
                        'timestamp': _utc_now(), 'stage': 'idle',
                        'status': 'failed', 'reason_code': 'QUEUE_OVERFLOW',
                        'duration_ms': last - expected + 1,
                    }, separators=(',', ':')) + '\n')
                stream.flush()
        except Exception as exc:
            # Only the error CLASS is remembered, never filesystem paths.
            self._error = type(exc).__name__
            # Do not drain or block the app if the destination is unavailable.
            return

    def close(self):
        with self._sequence_lock:
            if self._closed:
                return
            self._closed = True
        self._stop.set()
        self._writer_thread.join(timeout=2.0)
        # A daemon writer is allowed to finish asynchronously after timeout.


def from_environment():
    """Off unless user explicitly supplies a diagnostic path for this run.

    Any invalid destination or permission error disables *only* diagnostics.
    BLACK.BOX itself is never imported, contacted or started by A.I.O.
    """
    path = os.environ.get('AIO_BLACKBOX_EVENTS', '').strip()
    if not path:
        return None
    try:
        return TelemetryEmitter(path)
    except (OSError, ValueError, TypeError):
        return None
