"""Minimal authenticated loopback host for A.I.O UI and independent Excel merge.

No legacy converter imports, route catalogues, Advanced hooks or fallback
conversion engines. Converter Guard has a separate, intentionally empty port.
"""
from __future__ import annotations

from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import re
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from .converter_assets import read_designer_asset
from .excel import merge_xlsx, MergeError, MAX_TOTAL_BYTES, MAX_FILE_BYTES

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
        self.progress = {}
        self.progress_lock = threading.Lock()


def start_server(port: int = 0):
    token = secrets.token_urlsafe(32)
    root = asset_root().resolve()

    class Handler(BaseHTTPRequestHandler):
        server_version = 'AIO-Clean/0.1'
        sys_version = ''

        def _headers(self, code, content_type, size):
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(size))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy',
                "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: blob:; connect-src 'self'; "
                "font-src 'self'; base-uri 'none'; form-action 'none'")
            self.end_headers()

        def _send(self, code, payload, content_type='application/octet-stream'):
            self._headers(code, content_type, len(payload))
            self.wfile.write(payload)

        def _json(self, code, value):
            data = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
            self._send(code, data, 'application/json; charset=utf-8')

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
            path = urlsplit(self.path).path
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
                return self._json(501, {'code':'GUARD_NOT_READY',
                    'error':'Новый Converter Guard ещё не подключён.'})
            return self._static(path)

        def _read_multipart(self):
            length = self.headers.get('Content-Length', '')
            if not length.isdecimal() or not 0 < int(length) <= MAX_UPLOAD:
                raise MergeError('Недопустимый размер загрузки Excel.')
            content_type = self.headers.get('Content-Type', '')
            if not content_type.lower().startswith('multipart/form-data;'):
                raise MergeError('Требуется загрузка файлов Excel.')
            body = self.rfile.read(int(length))
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

        def do_POST(self):
            path = urlsplit(self.path).path
            if not self._authorized():
                return self._json(403, {'error':'Нет доступа.'})
            if path == '/api/icon-theme':
                return self._json(200, {'ok':True})
            if path.startswith('/api/converter'):
                return self._json(501, {'code':'GUARD_NOT_READY',
                    'error':'Новый Converter Guard ещё не подключён.'})
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
    return server, token
