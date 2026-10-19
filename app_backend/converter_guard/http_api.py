"""Authenticated loopback port; raw uploads/downloads stay in bounded chunks."""
from __future__ import annotations

import json
import re
import socket
import threading
from contextlib import contextmanager
from urllib.parse import parse_qs, quote, urlsplit

from .catalog import INPUTS
from .core import CHUNK
from .errors import GuardError


class GuardAPI:
    def __init__(self, guard):
        self.guard = guard
        self._connections = set()
        self._lock = threading.Lock()
        self._closed = False

    @contextmanager
    def _transfer(self, handler):
        with self._lock:
            if self._closed:
                raise GuardError('GUARD_CLOSED', 'Guard завершает работу.', status=503)
            self._connections.add(handler.connection)
        try:
            yield
        finally:
            with self._lock:
                self._connections.discard(handler.connection)

    def close(self):
        with self._lock:
            self._closed = True
            connections = list(self._connections)
        # Unblock a socket read/write before the owner waits for its I/O leases.
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    @staticmethod
    def _job(value):
        if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{32}', value):
            raise GuardError('JOB_ID', 'Неверный идентификатор задания.')
        return value

    @staticmethod
    def _json_body(handler):
        length = handler.headers.get('Content-Length', '')
        if not length.isdecimal() or not 0 < int(length) <= 64 * 1024:
            raise GuardError('REQUEST_SIZE', 'Неверный размер запроса Guard.')
        if handler.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
            raise GuardError('REQUEST_TYPE', 'Требуется JSON-запрос.')
        try:
            raw = handler.rfile.read(int(length))
            if len(raw) != int(length):
                raise ValueError('Truncated JSON')
            body = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise GuardError('REQUEST_JSON', 'Не удалось прочитать запрос Guard.') from exc
        if not isinstance(body, dict):
            raise GuardError('REQUEST_JSON', 'Ожидается объект запроса Guard.')
        return body

    def get(self, handler, path):
        try:
            if path == '/api/converter/result':
                return self._download(handler)
            if not handler._authorized():
                return handler._json(403, {'code': 'ACCESS_DENIED', 'error': 'Нет доступа.'})
            if path == '/api/converter/status':
                limits = self.guard.limits
                return handler._json(200, {'available': self.guard.adapter.available,
                    'inputs': list(INPUTS), 'outputs': ['pdf'],
                    'limits': {'maxFiles': limits.max_files, 'maxFileBytes': limits.max_file_bytes,
                        'maxSpoolBytes': limits.max_spool_bytes, 'fileTimeoutSeconds': limits.file_timeout},
                    'storage': self.guard.storage_stats()})
            if path == '/api/converter/jobs':
                query = parse_qs(urlsplit(handler.path).query)
                return handler._json(200, self.guard.status(self._job(query.get('job', [''])[0]),
                    include_trace=query.get('trace') == ['1']))
            raise GuardError('API_NOT_FOUND', 'Неизвестная операция Guard.', status=404)
        except GuardError as exc:
            return handler._json(exc.status, exc.as_dict())

    def post(self, handler, path):
        try:
            if not handler._authorized():
                return handler._json(403, {'code': 'ACCESS_DENIED', 'error': 'Нет доступа.'})
            if path == '/api/converter/input':
                query = parse_qs(urlsplit(handler.path).query)
                length = handler.headers.get('Content-Length', '')
                if not length.isdecimal():
                    raise GuardError('UPLOAD_LENGTH', 'Не указан размер загрузки.')
                if handler.headers.get('Content-Type', '').split(';')[0] != 'application/octet-stream':
                    raise GuardError('REQUEST_TYPE', 'Требуется поток файла.')
                handler.connection.settimeout(30)
                with self._transfer(handler):
                    self.guard.upload(self._job(query.get('job', [''])[0]),
                        query.get('file', [''])[0], handler.rfile, int(length))
                return handler._json(200, {'ok': True})
            body = self._json_body(handler)
            if path == '/api/converter/inspect':
                return handler._json(200, self.guard.capabilities(body.get('files')))
            if path == '/api/converter/jobs':
                if body.get('formatId', 'pdf') != 'pdf' or body.get('options'):
                    raise GuardError('OUTPUT_NOT_CONNECTED', 'В первой версии Guard подключён прямой экспорт в PDF.')
                return handler._json(201, self.guard.create(body.get('files')))
            id = self._job(body.get('jobId'))
            if path == '/api/converter/start':
                return handler._json(202, self.guard.start(id))
            if path == '/api/converter/cancel':
                return handler._json(200, self.guard.cancel(id))
            if path == '/api/converter/release':
                self.guard.release(id)
                return handler._json(200, {'ok': True})
            raise GuardError('API_NOT_FOUND', 'Неизвестная операция Guard.', status=404)
        except GuardError as exc:
            return handler._json(exc.status, exc.as_dict())
        except (OSError, TimeoutError):
            return handler._json(408, {'code': 'UPLOAD_INTERRUPTED', 'error': 'Передача файла была прервана.'})

    def _download(self, handler):
        query = parse_qs(urlsplit(handler.path).query)
        ticket = query.get('ticket', [''])[0]
        if not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', ticket):
            raise GuardError('RESULT_TICKET', 'Неверный идентификатор результата.', status=404)
        # A random, session-owned bearer ticket authorizes an anchor/Qt download,
        # which cannot attach X-AIO-Token. It never resolves a client-supplied path.
        with self._transfer(handler), self.guard.open_result(ticket) as (stream, name, size):
            handler.send_response(200)
            handler.send_header('Content-Type', 'application/zip' if name.endswith('.zip') else 'application/pdf')
            handler.send_header('Content-Length', str(size))
            handler.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + quote(name, safe=''))
            handler.send_header('Cache-Control', 'no-store')
            handler.send_header('Referrer-Policy', 'no-referrer')
            handler.send_header('X-Content-Type-Options', 'nosniff')
            handler.end_headers()
            try:
                while block := stream.read(CHUNK):
                    handler.wfile.write(block)
            except (BrokenPipeError, ConnectionResetError):
                pass  # Keep the result for a later save; the lease is released.
