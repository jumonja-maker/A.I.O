"""Guard HTTP port, mounted by AIO on its unbuffered loopback handler."""
from contextlib import contextmanager
from dataclasses import asdict
import json
import re
import secrets
import select
import socket
import threading
from urllib.parse import parse_qs, quote, urlsplit

from ..libreoffice_catalog import mime_for
from .core import CHUNK
from .errors import GuardError

PREFIX = '/api/converter-guard'


class _CancelableInput:
    """Windows-safe short socket reads; wake on Guard cancellation.

    AIO's BaseHTTPRequestHandler has rbufsize=0, so this socket has no
    prefetched HTTP body bytes. No size or elapsed-time conversion limit is
    imposed by this reader.
    """
    def __init__(self, connection, cancel):
        self.connection, self.cancel = connection, cancel

    def read(self, size):
        while not self.cancel.is_set():
            ready, _, _ = select.select([self.connection], [], [], 0.1)
            if ready:
                try:
                    return self.connection.recv(size)
                except OSError:
                    if self.cancel.is_set():
                        raise GuardError('CANCELLED', 'Загрузка отменена.', stage='upload')
                    raise
        raise GuardError('CANCELLED', 'Загрузка отменена.', stage='upload')


class GuardAPI:
    def __init__(self, guard, *, origin, token):
        parsed = urlsplit(origin)
        if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port
                or parsed.path or parsed.query or parsed.fragment or parsed.username
                or parsed.password):
            raise ValueError('Guard API requires an exact HTTP loopback origin')
        if not isinstance(token, str) or not token:
            raise ValueError('Guard API requires a session token')
        self.guard, self.origin, self.host, self.token = guard, origin, parsed.netloc, token
        self._connections = set()
        self._lock = threading.Lock()
        self._closed = False

    def _authorize(self, handler, *, download=False):
        with self._lock:
            if self._closed:
                raise GuardError('GUARD_CLOSED', 'Guard завершает работу.', status=503)
        if (handler.headers.get('Host') != self.host or
                handler.headers.get('Origin', self.origin) != self.origin):
            raise GuardError('ACCESS_DENIED', 'Нет доступа.', status=403)
        if not download and not secrets.compare_digest(
                handler.headers.get('X-AIO-Token', '').encode('utf-8'), self.token.encode('utf-8')):
            raise GuardError('ACCESS_DENIED', 'Нет доступа.', status=403)

    @staticmethod
    def _json(handler, status, body):
        try:
            return handler._json(status, body)
        except OSError:
            return None  # The caller may have closed during cancellation.

    @staticmethod
    def _identity(value):
        if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{32}', value):
            raise GuardError('JOB_ID', 'Неверный идентификатор задания.')
        return value

    @staticmethod
    def _query(handler, key):
        values = parse_qs(urlsplit(handler.path).query, keep_blank_values=True).get(key, [])
        if len(values) != 1:
            raise GuardError('REQUEST_QUERY', 'Неверные параметры запроса.')
        return values[0]

    @staticmethod
    def _length(handler):
        lengths = handler.headers.get_all('Content-Length', [])
        if len(lengths) != 1 or not lengths[0].isdecimal() or handler.headers.get('Transfer-Encoding'):
            handler.close_connection = True
            raise GuardError('REQUEST_SIZE', 'Требуется однозначный размер запроса.')
        try:
            return int(lengths[0])
        except ValueError as error:
            raise GuardError('REQUEST_SIZE', 'Неверный размер запроса.') from error

    def _body(self, handler):
        length = self._length(handler)
        if not 0 < length <= self.guard.limits.max_metadata_bytes:
            handler.close_connection = True
            raise GuardError('REQUEST_SIZE', 'Превышен лимит метаданных Guard.', status=413)
        if handler.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
            raise GuardError('REQUEST_TYPE', 'Требуется JSON-запрос.')
        try:
            # Qt/Windows uses an unbuffered SocketIO reader; read() may be short.
            raw = bytearray()
            while len(raw) < length:
                block = handler.rfile.read(min(CHUNK, length - len(raw)))
                if not block:
                    break
                raw.extend(block)
            if len(raw) != length:
                raise ValueError('incomplete JSON')
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError('not an object')
            return body
        except (ValueError, UnicodeError, RecursionError) as error:
            raise GuardError('REQUEST_JSON', 'Не удалось прочитать запрос Guard.') from error

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

    @staticmethod
    def _shutdown(connection, direction):
        try:
            connection.shutdown(direction)
        except OSError:
            pass

    def close(self):
        with self._lock:
            self._closed = True
            connections = list(self._connections)
        for connection in connections:
            self._shutdown(connection, socket.SHUT_RDWR)

    def get(self, handler, path):
        try:
            self._authorize(handler, download=path == PREFIX + '/result')
            if path == PREFIX + '/result':
                return self._download(handler)
            if path == PREFIX + '/status':
                return self._json(handler,200, {'schemaVersion': 3, 'available': self.guard.adapter.available,
                    'acceptedExtensions': '', 'compatibility': 'native_decision',
                    'routeFiltering': 'native_registry_after_model_confirmation',
                    'preflightAvailable': True, 'selectAllPolicy': 'only_preflight_confirmed',
                    'qualityCompared': False,
                    'limits': asdict(self.guard.limits), 'storage': self.guard.storage_stats()})
            if path == PREFIX + '/jobs':
                trace = parse_qs(urlsplit(handler.path).query).get('trace') == ['1']
                identity = self._identity(self._query(handler, 'job'))
                match = re.fullmatch(r'"([0-9a-f]{32}):([0-9]{1,20})"',
                                     handler.headers.get('If-None-Match', ''))
                since = int(match[2]) if match and match[1] == identity else None
                delta = parse_qs(urlsplit(handler.path).query).get('delta') == ['1']
                state = self.guard.status(identity, include_trace=trace, since_revision=since, delta=delta)
                return self._status_reply(handler, state)
            if path == PREFIX + '/recoverable':
                return self._json(handler,200, self.guard.recoverable())
            if path == PREFIX + '/report':
                return self._json(handler,200, self.guard.report(self._identity(self._query(handler, 'job'))))
            if path == PREFIX + '/retry-plan':
                cancelled = parse_qs(urlsplit(handler.path).query).get('includeCancelled') == ['1']
                return self._json(handler,200, self.guard.retry_plan(
                    self._identity(self._query(handler, 'job')), include_cancelled=cancelled))
            raise GuardError('API_NOT_FOUND', 'Неизвестная операция Guard.', status=404)
        except GuardError as error:
            return self._json(handler,error.status, error.as_dict())
        except OSError:
            return None  # Disconnected result remains retained for a later save.

    def post(self, handler, path):
        try:
            self._authorize(handler)
            handler.connection.settimeout(self.guard.limits.upload_idle_timeout)
            if path == PREFIX + '/input':
                length = self._length(handler)
                if handler.headers.get('Content-Type', '').split(';')[0].strip() != 'application/octet-stream':
                    raise GuardError('REQUEST_TYPE', 'Требуется поток файла.')
                cancelled = threading.Event()
                with self._transfer(handler):
                    self.guard.upload(self._identity(self._query(handler, 'job')),
                        self._query(handler, 'file'),
                        _CancelableInput(handler.connection, cancelled), length,
                        interrupt=cancelled.set)
                return self._json(handler,200, {'ok': True})
            # Track metadata reads too: close() must unblock a stalled JSON body.
            with self._transfer(handler):
                body = self._body(handler)
            if path == PREFIX + '/inspect':
                return self._json(handler,200, self.guard.capabilities(body.get('files')))
            if path == PREFIX + '/prepare':
                return self._json(handler,201, self.guard.create(body.get('files'), prepare_only=True))
            if path == PREFIX + '/jobs':
                if body.get('options'):
                    raise GuardError('OPTIONS_NOT_CONNECTED', 'Параметры экспорта пока не подключены.')
                if sum(key in body for key in ('formats', 'formatId', 'plan')) > 1:
                    raise GuardError('OUTPUT_FORMAT', 'Укажите только один способ выбора форматов.')
                if 'plan' in body and body['plan'] is None:
                    raise GuardError('CONVERSION_PLAN', 'Индивидуальный plan должен быть списком.')
                formats = (None if 'plan' in body else
                           body.get('formats', [body.get('formatId', 'pdf')]))
                return self._json(handler,201, self.guard.create(
                    body.get('files'), formats, plan=body.get('plan')))
            identity = self._identity(body.get('jobId'))
            if path == PREFIX + '/prepare-update':
                return self._json(handler,200, self.guard.update_prepared(identity, body.get('files')))
            if path == PREFIX + '/preflight':
                return self._json(handler,200, self.guard.preflight(identity,
                    recheck=body.get('recheck', False),timeout_seconds=body.get('timeoutSeconds'),
                    only_undetermined=body.get('onlyUndetermined',False)))
            if path == PREFIX + '/start':
                return self._json(handler,202, self.guard.start(identity,
                    formats=body.get('formats'), file_ids=body.get('fileIds'),
                    only_verified=body.get('onlyVerified', False), plan=body.get('plan'),
                    timeout_seconds=body.get('timeoutSeconds')))
            if path == PREFIX + '/cancel':
                return self._json(handler,200, self.guard.cancel(identity))
            if path == PREFIX + '/cancel-file':
                return self._json(handler,200, self.guard.cancel_file(identity, body.get('fileId')))
            if path == PREFIX + '/pause':
                return self._json(handler,200, self.guard.pause(identity, body.get('paused', True)))
            if path == PREFIX + '/bundle':
                return self._json(handler,202, self.guard.rebuild_bundle(identity))
            if path == PREFIX + '/saved':
                return self._json(handler,200, self.guard.confirm_saved(identity, body.get('ticket'),
                    compact=body.get('compact', False)))
            if path == PREFIX + '/release':
                self.guard.release(identity, discard=body.get('discard', False))
                return self._json(handler,200, {'ok': True})
            raise GuardError('API_NOT_FOUND', 'Неизвестная операция Guard.', status=404)
        except GuardError as error:
            handler.close_connection = True
            return self._json(handler,error.status, error.as_dict())
        except (OSError, TimeoutError):
            return self._json(handler,408, {'code': 'UPLOAD_INTERRUPTED', 'error': 'Передача была прервана.'})

    def _download(self, handler):
        ticket = self._query(handler, 'ticket')
        if not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', ticket):
            raise GuardError('RESULT_TICKET', 'Неверный идентификатор результата.', status=404)
        with self._transfer(handler), self.guard.open_result(ticket) as (stream, name, size):
            folder = parse_qs(urlsplit(handler.path).query).get('mode') == ['folder']
            handler.send_response(200)
            handler.send_header('Content-Type', mime_for(name.rsplit('.', 1)[-1].lower()))
            handler.send_header('Content-Length', str(0 if folder else size))
            handler.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + quote(name, safe=''))
            handler.send_header('Cache-Control', 'no-store')
            handler.send_header('Referrer-Policy', 'no-referrer')
            handler.send_header('X-Content-Type-Options', 'nosniff')
            handler.end_headers()
            if folder:
                return  # The native shell handles one directory choice, not a Blob.
            while block := stream.read(CHUNK):
                handler.wfile.write(block)

    @staticmethod
    def _status_reply(handler, state):
        """Unchanged polls bypass both snapshot construction and JSON encoding."""
        unchanged = state.get('unchanged', False)
        data = b'' if unchanged else json.dumps(state, ensure_ascii=False).encode('utf-8')
        try:
            handler.send_response(304 if unchanged else 200)
            handler.send_header('ETag', '"' + state['jobId'] + ':' + str(state['revision']) + '"')
            handler.send_header('Cache-Control', 'no-store')
            handler.send_header('X-Content-Type-Options', 'nosniff')
            handler.send_header('Referrer-Policy', 'no-referrer')
            if not unchanged:
                handler.send_header('Content-Type', 'application/json; charset=utf-8')
                handler.send_header('Content-Length', str(len(data)))
            handler.end_headers()
            if data:
                handler.wfile.write(data)
        except OSError:
            pass
