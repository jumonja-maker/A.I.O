"""Minimal authenticated loopback host for A.I.O UI and independent Excel merge.

No legacy converter imports, route catalogues, Advanced hooks or fallback
conversion engines. LibreOffice receives original inputs through a disk-streaming transport.
"""
from __future__ import annotations

from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import io
import select
import zipfile
import mimetypes
import os
import re
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from .converter_assets import read_designer_asset
from .excel import merge_xlsx, MergeError, MAX_TOTAL_BYTES, MAX_FILE_BYTES
from .libreoffice_engine import LibreOfficeError, inspect_metadata
from .libreoffice_catalog import EXTENSION
from .converter_transport import ConverterTransport
from .blackbox_telemetry import from_environment
from .converter_upload_stream import MultipartUpload, content_length, MAX_METADATA_BYTES
from .converter_guard import ConverterGuard, GuardLimits
from .converter_guard.http_api import GuardAPI
from .converter_guard.result_store import data_directory
from urllib.parse import quote


class CancellableSocketUpload:
    """Read only available TCP bytes, without any elapsed-time policy.

    The request headers use an unbuffered rfile, so there are no prefetched
    body bytes bypassed by recv(). Cancellation wakes within 0.1 seconds on
    Linux and Windows without shutting down the socket write half.
    """
    def __init__(self, connection, cancel):
        self.connection = connection
        self.cancel = cancel

    def read(self, size):
        while not self.cancel.is_set():
            readable, _, _ = select.select([self.connection], [], [], 0.1)
            if readable:
                try:
                    return self.connection.recv(size)
                except OSError:
                    if self.cancel.is_set():
                        raise LibreOfficeError('Загрузка отменена.', 'CANCELLED')
                    raise
        raise LibreOfficeError('Загрузка отменена.', 'CANCELLED')


ROOT = Path(__file__).resolve().parent.parent
MAX_UPLOAD = MAX_TOTAL_BYTES + 2 * 1024 * 1024
JOB_PATTERN = re.compile(r'[0-9a-f]{32}\Z')
MAX_PROGRESS_JOBS = 16
MAX_PROGRESS_AGE = 900


def asset_root() -> Path:
    """Supports source launches and frozen PyInstaller launches."""
    import sys
    return Path(getattr(sys, '_MEIPASS', ROOT))


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address, handler):
        super().__init__(address, handler)
        self.merge_gate = threading.BoundedSemaphore(1)
        self.converter_gate = threading.BoundedSemaphore(1)
        self.telemetry = from_environment()
        self.converter = ConverterTransport(telemetry=self.telemetry)
        self.guard = None
        self.guard_api = None
        self.progress = {}
        self.progress_lock = threading.Lock()

    def server_close(self):
        try:
            if self.guard_api is not None:
                self.guard_api.close()
            if self.guard is not None:
                self.guard.close()
        finally:
            try:
                self.converter.close()
            finally:
                try:
                    if self.telemetry is not None:
                        self.telemetry.close()
                finally:
                    super().server_close()


def start_server(port: int = 0, *, guard_recovery_parent=None, guard_history_parent=None):
    token = secrets.token_urlsafe(32)
    root = asset_root().resolve()

    class Handler(BaseHTTPRequestHandler):
        # Use an unbuffered socket reader: buffered rfile.read(64 KiB) can wait
        # forever for a stalled client and cannot be interrupted reliably by
        # shutdown(SHUT_RD) on Windows. Readiness polling below permits cancel.
        rbufsize = 0
        server_version = 'AIO-Clean/0.1'
        sys_version = ''

        def _headers(self, code, content_type, size):
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(size))
            if self.close_connection:
                self.send_header('Connection', 'close')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy',
                "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: blob:; connect-src 'self'; "
                "font-src 'self'; base-uri 'none'; form-action 'none'")
            self.end_headers()

        def _read_exact(self, size):
            # SocketIO.read() may return fewer bytes than requested, unlike the
            # previously buffered reader. Keep Excel and JSON framing exact.
            output = io.BytesIO()
            while size:
                data = self.rfile.read(min(64 * 1024, size))
                if not data:
                    raise ValueError('Загрузка HTTP прервана.')
                output.write(data)
                size -= len(data)
            return output.getvalue()

        def _send(self, code, payload, content_type='application/octet-stream'):
            try:
                self._headers(code, content_type, len(payload))
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # The caller closed its own upload/download.

        def _json(self, code, value):
            data = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
            self._send(code, data, 'application/json; charset=utf-8')

        def _loopback_origin_allowed(self):
            """Reject ambiguous Host before serving the token-bearing UI.

            Binding only to 127.0.0.1 does not establish the HTTP origin:
            a browser can resolve an unrelated hostname to loopback. Require
            exactly the origin used by this application's own WebEngine page.
            An absent Origin is normal for ordinary GET/navigation requests.
            """
            expected_host = '127.0.0.1:' + str(self.server.server_port)
            hosts = self.headers.get_all('Host', [])
            origins = self.headers.get_all('Origin', [])
            expected_origin = 'http://' + expected_host
            return (len(hosts) == 1 and hosts[0] == expected_host and
                    len(origins) <= 1 and
                    (not origins or origins[0] == expected_origin))

        def _authorized(self):
            return secrets.compare_digest(self.headers.get('X-AIO-Token', ''), token)

        def _progress(self, job, stage, current=0, total=0):
            with self.server.progress_lock:
                now = time.monotonic()
                for key, (at, _) in tuple(self.server.progress.items()):
                    if now - at > MAX_PROGRESS_AGE:
                        self.server.progress.pop(key, None)
                if len(self.server.progress) >= MAX_PROGRESS_JOBS and job not in self.server.progress:
                    oldest = min(self.server.progress, key=lambda key:self.server.progress[key][0])
                    self.server.progress.pop(oldest, None)
                self.server.progress[job] = (now, dict(phase=stage, current=current, total=total))

        def _static(self, path):
            if path == '/':
                html = (root / 'index.html').read_text(encoding='utf-8')
                marker = '<body>'
                if html.count(marker) != 1:
                    return self._json(500, {'error':'Не удалось загрузить интерфейс.'})
                html = html.replace(marker, '<body data-aio-token="' + token + '">')
                return self._send(200, html.encode('utf-8'), 'text/html; charset=utf-8')
            if path in ('/addons/file-converter/file-converter.js',
                        '/addons/file-converter/file-converter.css'):
                name = path.rsplit('/', 1)[-1]
                try:
                    blob = read_designer_asset(str(root), name)
                except Exception:
                    return self._json(500, {'error':'Невозможно загрузить утверждённый дизайн.'})
                mime = 'application/javascript' if name.endswith('.js') else 'text/css'
                return self._send(200, blob, mime + '; charset=utf-8')
            if not (path.startswith(('/src/', '/addons/', '/assets/')) and
                    path.endswith(('.js','.css','.svg','.png','.webp','.ico'))):
                return self._json(404, {'error':'Ресурс не найден.'})
            relative = path.lstrip('/')
            try:
                candidate = (root / relative).resolve(strict=True)
                if not candidate.is_relative_to(root) or not candidate.is_file():
                    raise ValueError('Path escapes static root')
                blob = candidate.read_bytes()
            except (OSError, ValueError):
                return self._json(404, {'error':'Ресурс не найден.'})
            mime = mimetypes.guess_type(path)[0] or 'application/octet-stream'
            if path.endswith('.js'):
                mime = 'application/javascript'
            if path.endswith('.svg'):
                mime = 'image/svg+xml'
            return self._send(200, blob, mime)

        def do_GET(self):
            if not self._loopback_origin_allowed():
                self.close_connection = True
                return self._json(403, {'error':'Нет доступа.'})
            path = urlsplit(self.path).path
            if path.startswith('/api/converter-guard'):
                return self.server.guard_api.get(self, path)
            if path == '/api/merge-progress':
                if not self._authorized():
                    return self._json(403, {'error':'Нет доступа.'})
                job = parse_qs(urlsplit(self.path).query).get('job', [''])[0]
                if not JOB_PATTERN.fullmatch(job):
                    return self._json(400, {'error':'Неверный идентификатор задания.'})
                with self.server.progress_lock:
                    row = self.server.progress.get(job)
                return self._json(200, row[1] if row else {'phase':'upload','current':0,'total':0})
            if path == '/api/updates':
                if not self._authorized():
                    return self._json(403, {'error':'Нет доступа.'})
                return self._json(200, {'available':False,
                    'message':'Проверка обновлений для нового репозитория пока не настроена.'})
            if path.startswith('/api/converter'):
                if not self._converter_origin():
                    return self._json(403, {'error': 'Нет доступа.'})
                query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                if path == '/api/converter/result':
                    tickets = query.get('ticket', [])
                    if (len(tickets) != 1 or set(query) - {'ticket', 'aioSave'} or
                            not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', tickets[0])):
                        return self._json(400, {'code': 'INVALID_TICKET', 'error': 'Некорректный адрес результата.'})
                    try:
                        with self.server.converter.lease(tickets[0]) as result:
                            size = result.path.stat().st_size
                            self.send_response(200)
                            self.send_header('Content-Type', result.mime)
                            self.send_header('Content-Length', str(size))
                            self.send_header('Cache-Control', 'no-store')
                            self.send_header('X-Content-Type-Options', 'nosniff')
                            self.send_header('Content-Disposition',
                                "attachment; filename=result" + result.path.suffix +
                                "; filename*=UTF-8''" + quote(result.name))
                            self.end_headers()
                            with result.path.open('rb') as stream:
                                while chunk := stream.read(64 * 1024):
                                    self.wfile.write(chunk)
                        return
                    except LibreOfficeError as exc:
                        return self._json(404, {'code': exc.code, 'error': str(exc)})
                    except (BrokenPipeError, ConnectionResetError):
                        return
                if path == '/api/converter/progress':
                    if not self._authorized():
                        return self._json(403, {'error': 'Нет доступа.'})
                    identity = query.get('request', [''])[0]
                    value = self.server.converter.progress(identity)
                    return self._json(200, value or {'files': [], 'completed': 0, 'total': 0, 'percent': None})
                return self._json(404, {'code':'UNKNOWN_CONVERTER_ENDPOINT',
                    'error':'Неизвестная операция конвертера.'})
            return self._static(path)

        def _read_multipart(self):
            length = self.headers.get('Content-Length', '')
            if not length.isdecimal() or not 0 < int(length) <= MAX_UPLOAD:
                raise MergeError('Недопустимый размер загрузки Excel.')
            content_type = self.headers.get('Content-Type', '')
            if not content_type.lower().startswith('multipart/form-data;'):
                raise MergeError('Требуется загрузка файлов Excel.')
            body = self._read_exact(int(length))
            if len(body) != int(length):
                raise MergeError('Загрузка Excel была прервана.')
            mime = (b'MIME-Version: 1.0\r\nContent-Type: ' +
                    content_type.encode('ascii', 'strict') +
                    b'\r\n\r\n' + body)
            msg = BytesParser(policy=policy.default).parsebytes(mime)
            if not msg.is_multipart() or msg.defects:
                raise MergeError('Ошибка структуры загрузки Excel.')
            files = []
            for part in msg.iter_parts():
                if part.get_content_disposition() != 'form-data' or \
                        part.get_param('name', header='content-disposition') != 'files':
                    raise MergeError('Недопустимое поле формы Excel.')
                name = part.get_filename()
                if not isinstance(name, str) or '/' in name or '\\' in name or \
                        '\0' in name or len(name) > 240 or not name.lower().endswith('.xlsx'):
                    raise MergeError('Разрешены только файлы .xlsx.')
                data = part.get_payload(decode=True)
                if not isinstance(data, bytes) or not 0 < len(data) <= MAX_FILE_BYTES:
                    raise MergeError('Один Excel-файл пустой или больше 20 МБ.')
                files.append((name, data))
                if len(files) > 100:
                    raise MergeError('Допустимо максимум 100 Excel-файлов.')
            if not 2 <= len(files) <= 100 or sum(len(f) for _, f in files) > MAX_TOTAL_BYTES:
                raise MergeError('Выберите от 2 до 100 XLSX общим размером до 50 МБ.')
            return files


        def _converter_origin(self):
            origin = f'http://127.0.0.1:{self.server.server_port}'
            return (self.headers.get('Host') == origin.removeprefix('http://') and
                    self.headers.get('Origin', origin) == origin)

        def _read_converter_json(self):
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                raise LibreOfficeError('Нужен JSON-запрос.', 'INVALID_REQUEST')
            length = content_length(self.headers)
            if length > MAX_METADATA_BYTES:
                # Reject control metadata before reading or allocating its
                # advertised body. This never applies to streamed documents.
                raise LibreOfficeError('Превышен лимит метаданных запроса.', 'INVALID_REQUEST')
            value = json.loads(self._read_exact(length))
            if not isinstance(value, dict):
                raise LibreOfficeError('Некорректные параметры.', 'INVALID_REQUEST')
            return value

        def _convert_request(self, path):
            if not self._converter_origin():
                return self._json(403, {'error': 'Нет доступа.'})
            request = None
            retained = False
            acquired = False
            try:
                if path.endswith('/inspect'):
                    metadata = self._read_converter_json()
                    return self._json(200, inspect_metadata(metadata.get('files')))
                if path.endswith('/release'):
                    value = self._read_converter_json()
                    released = self.server.converter.release(value.get('ticket'))
                    return self._json(200, {'ok': released})
                if path.endswith('/cancel'):
                    value = self._read_converter_json()
                    cancelled = self.server.converter.cancel(value.get('requestId'))
                    return self._json(200, {'ok': True, 'cancelled': cancelled})
                length = content_length(self.headers)
                if not self.server.converter_gate.acquire(blocking=False):
                    return self._json(409, {'code': 'ENGINE_BUSY', 'error': 'Уже выполняется другое задание.'})
                acquired = True
                identity = parse_qs(urlsplit(self.path).query).get('request', [secrets.token_hex(16)])[0]
                if not JOB_PATTERN.fullmatch(identity):
                    raise LibreOfficeError('Некорректный идентификатор задания.', 'INVALID_REQUEST')
                request = self.server.converter.begin(identity, self.connection)
                files, fields = MultipartUpload(
                    CancellableSocketUpload(self.connection, request.cancel),
                    length,
                    self.headers.get('Content-Type', ''),
                    request.cancel).read(request.root / 'inputs')
                try:
                    ids = json.loads(fields['ids'])
                    formats = json.loads(fields['formats']) if 'formats' in fields else [fields.get('format')]
                except (KeyError, ValueError):
                    raise LibreOfficeError('Некорректные параметры задания.', 'INVALID_REQUEST')
                if (not isinstance(ids, list) or len(ids) != len(files) or
                        any(not isinstance(x, str) or not x for x in ids) or len(set(ids)) != len(ids)):
                    raise LibreOfficeError('Некорректные идентификаторы файлов.', 'INVALID_IDS')
                if (not isinstance(formats, list) or not formats or
                        any(not isinstance(x, str) or not EXTENSION.fullmatch(x) for x in formats) or
                        len(set(formats)) != len(formats)):
                    raise LibreOfficeError('Некорректные расширения результата.', 'INVALID_FORMATS')
                outcome = self.server.converter.execute(request, files, ids, formats)
                retained = True
                return self._json(200, outcome)
            except LibreOfficeError as exc:
                if exc.code == 'INVALID_REQUEST':
                    # Invalid framing/oversized metadata can leave an unread
                    # body; it must never be parsed as another HTTP request.
                    self.close_connection = True
                data = {'code': exc.code, 'error': str(exc)}
                if getattr(exc, 'batch_report', None):
                    data['batchReport'] = exc.batch_report
                return self._json(422, data)
            except (UnicodeError, ValueError, TypeError):
                return self._json(400, {'code': 'INVALID_REQUEST', 'error': 'Некорректный запрос конвертации.'})
            except (BrokenPipeError, ConnectionResetError):
                return
            except OSError:
                return self._json(507, {'code': 'STORAGE_ERROR',
                    'error': 'Не удалось прочитать или записать файл. Проверьте свободное место и права доступа.'})
            finally:
                if request is not None:
                    self.server.converter.finish(request, retained)
                if acquired:
                    self.server.converter_gate.release()

        def do_POST(self):
            if not self._loopback_origin_allowed():
                self.close_connection = True
                return self._json(403, {'error':'Нет доступа.'})
            path = urlsplit(self.path).path
            if path.startswith('/api/converter-guard'):
                return self.server.guard_api.post(self, path)
            if not self._authorized():
                return self._json(403, {'error':'Нет доступа.'})
            if path == '/api/icon-theme':
                return self._json(200, {'ok':True})
            if path in ('/api/converter/inspect', '/api/converter/convert',
                        '/api/converter/release', '/api/converter/cancel'):
                return self._convert_request(path)
            if path.startswith('/api/converter'):
                return self._json(404, {'code':'UNKNOWN_CONVERTER_ENDPOINT',
                    'error':'Неизвестная операция конвертера.'})
            if path != '/api/merge':
                return self._json(404, {'error':'Неизвестная операция.'})
            job = self.headers.get('X-AIO-Job','')
            if not JOB_PATTERN.fullmatch(job):
                return self._json(400, {'error':'Неверный идентификатор задания.'})
            if not self.server.merge_gate.acquire(blocking=False):
                return self._json(409, {'error':'Уже выполняется объединение Excel.'})
            try:
                self._progress(job, 'upload')
                files = self._read_multipart()
                output = merge_xlsx(files, progress=lambda phase, current, total:
                                    self._progress(job, phase, current, total))
                return self._send(200, output,
                    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            except (MergeError, ValueError, UnicodeError) as exc:
                self._progress(job, 'error')
                return self._json(400, {'error':str(exc)})
            except Exception:
                self._progress(job, 'error')
                return self._json(500, {'error':'Не удалось объединить Excel-файлы.'})
            finally:
                self.server.merge_gate.release()

        def log_message(self, *_):
            pass  # Never log filenames or contents of personal spreadsheets.

    server = LocalServer(('127.0.0.1', port), Handler)
    try:
        diagnostic_limit = os.environ.get('AIO_GUARD_REJECTED_OUTPUT_BYTES', '0')
        if not re.fullmatch(r'[0-9]{1,8}', diagnostic_limit):
            raise ValueError('AIO_GUARD_REJECTED_OUTPUT_BYTES must be 0..8388608')
        server.guard = ConverterGuard(limits=GuardLimits(rejected_output_bytes=int(diagnostic_limit)), recovery_parent=(data_directory() / 'jobs'
            if guard_recovery_parent is None else guard_recovery_parent),
            telemetry=server.telemetry,
            history_parent=(guard_history_parent if guard_history_parent is not None else
                data_directory() / 'history' if guard_recovery_parent is None else None))
        server.guard_api = GuardAPI(server.guard,
            origin=f'http://127.0.0.1:{server.server_port}', token=token)
    except BaseException:
        server.server_close()
        raise
    return server, token
