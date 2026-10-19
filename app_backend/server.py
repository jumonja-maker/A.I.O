"""Minimal authenticated loopback host for A.I.O UI and independent Excel merge.

No legacy converter imports, route catalogues, Advanced hooks or fallback
conversion engines. The converter is not wired until its new architecture is supplied.
"""
from __future__ import annotations

from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import io
import zipfile
import mimetypes
import re
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from .converter_assets import read_designer_asset
from .excel import merge_xlsx, MergeError, MAX_TOTAL_BYTES, MAX_FILE_BYTES
from .libreoffice_engine import (
    LibreOfficeError, MAX_SOURCE as LO_MAX_SOURCE, MAX_BATCH as LO_MAX_BATCH,
    MAX_FILES as LO_MAX_FILES, MAX_RESULT as LO_MAX_RESULT,
    inspect as inspect_libreoffice, convert as run_libreoffice,
    OUTPUT_META, source_format, compatible,
)

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


        def _read_converter_form(self):
            """Strict 50MB multipart transport; never interpret uploads as filesystem paths."""
            length = self.headers.get('Content-Length', '')
            if not length.isdecimal() or not 0 < int(length) <= LO_MAX_BATCH + 1024 * 1024:
                raise LibreOfficeError('Слишком большой запрос конвертации.', 'REQUEST_TOO_LARGE')
            content_type = self.headers.get('Content-Type', '')
            if not content_type.lower().startswith('multipart/form-data;'):
                raise LibreOfficeError('Нужна форма с файлами.', 'INVALID_REQUEST')
            raw = self.rfile.read(int(length))
            if len(raw) != int(length):
                raise LibreOfficeError('Загрузка файла прервана.', 'UPLOAD_INCOMPLETE')
            wrapped = (b'MIME-Version: 1.0\r\nContent-Type: ' +
                       content_type.encode('ascii', 'strict') + b'\r\n\r\n' + raw)
            msg = BytesParser(policy=policy.default).parsebytes(wrapped)
            if not msg.is_multipart() or msg.defects:
                raise LibreOfficeError('Некорректная форма загрузки.', 'INVALID_REQUEST')
            files = []
            fields = {}
            for part in msg.iter_parts():
                if part.get_content_disposition() != 'form-data':
                    raise LibreOfficeError('Некорректное поле.', 'INVALID_REQUEST')
                key = part.get_param('name', header='content-disposition')
                if key == 'files':
                    name = part.get_filename()
                    source_format(name)
                    blob = part.get_payload(decode=True)
                    if not isinstance(blob, bytes) or not 0 < len(blob) <= LO_MAX_SOURCE:
                        raise LibreOfficeError('Файл пустой или превышает 20 МБ.', 'INPUT_TOO_LARGE')
                    files.append((name, blob))
                    if len(files) > LO_MAX_FILES:
                        raise LibreOfficeError('Выбрано более 100 файлов.', 'TOO_MANY_FILES')
                elif key in ('ids', 'format', 'formats') and key not in fields:
                    value = part.get_payload(decode=True)
                    if type(value) is not bytes or len(value) > 4000:
                        raise LibreOfficeError('Некорректные параметры.', 'INVALID_REQUEST')
                    fields[key] = value.decode('utf-8', 'strict')
                else:
                    raise LibreOfficeError('Неподдерживаемое поле запроса.', 'INVALID_REQUEST')
            if not files or sum(len(blob) for _, blob in files) > LO_MAX_BATCH:
                raise LibreOfficeError('Допустимо 1–100 файлов общим размером до 50 МБ.',
                                       'INVALID_BATCH')
            try:
                ids = json.loads(fields['ids'])
            except (KeyError, ValueError):
                raise LibreOfficeError('Отсутствуют идентификаторы файлов.', 'INVALID_IDS')
            if (not isinstance(ids, list) or len(ids) != len(files) or
                any(not isinstance(x, str) or not 1 <= len(x) <= 128 for x in ids) or
                len(set(ids)) != len(ids)):
                raise LibreOfficeError('Недопустимые идентификаторы файлов.', 'INVALID_IDS')
            return files, ids, fields

        def _convert_request(self, path):
            if not self.server.converter_gate.acquire(blocking=False):
                return self._json(409, {'code':'ENGINE_BUSY',
                    'error':'LibreOffice уже выполняет другое задание.'})
            try:
                files, ids, fields = self._read_converter_form()
                if path.endswith('/inspect'):
                    return self._json(200, inspect_libreoffice(files, ids))
                if 'formats' in fields:
                    try:
                        formats = json.loads(fields['formats'])
                    except ValueError:
                        raise LibreOfficeError('Некорректный список форматов.', 'INVALID_FORMATS')
                else:
                    formats = [fields.get('format')]
                if (not isinstance(formats, list) or not 1 <= len(formats) <= 12 or
                    any(not isinstance(fmt, str) or fmt not in OUTPUT_META for fmt in formats) or
                    len(set(formats)) != len(formats)):
                    raise LibreOfficeError('Некорректный выбор выходных форматов.', 'INVALID_FORMATS')
                if not all(compatible(source_format(name), fmt)
                           for name, _ in files for fmt in formats):
                    raise LibreOfficeError('Выбрано неподдерживаемое направление LibreOffice.',
                                           'UNSUPPORTED_DIRECTION')
                outputs = []
                total = 0
                for i, (name, blob) in enumerate(files):
                    for fmt in formats:
                        result = run_libreoffice(name, blob, fmt)
                        total += len(result)
                        if total > LO_MAX_RESULT:
                            raise LibreOfficeError('Результат превышает 100 МБ.', 'RESULT_TOO_LARGE')
                        clean = re.sub(r'[^a-zA-Z0-9а-яА-ЯёЁ._ -]', '_', Path(name).stem)[:80] or 'file'
                        outputs.append((f'{i + 1:03d}_{clean}.{fmt}', fmt, result))
                if len(outputs) == 1:
                    _, fmt, blob = outputs[0]
                    return self._send(200, blob, OUTPUT_META[fmt][2])
                out = io.BytesIO()
                with zipfile.ZipFile(out, 'w', compression=zipfile.ZIP_DEFLATED,
                                     compresslevel=6) as archive:
                    for filename, _fmt, blob in outputs:
                        archive.writestr(filename, blob)
                if out.tell() > LO_MAX_RESULT:
                    raise LibreOfficeError('Архив превышает 100 МБ.', 'RESULT_TOO_LARGE')
                return self._send(200, out.getvalue(), 'application/zip')
            except LibreOfficeError as exc:
                return self._json(422, {'code': exc.code, 'error':str(exc)})
            except (UnicodeError, ValueError) as exc:
                return self._json(400, {'code':'INVALID_REQUEST',
                    'error':'Некорректный запрос конвертации.'})
            finally:
                self.server.converter_gate.release()

        def do_POST(self):
            path = urlsplit(self.path).path
            if not self._authorized():
                return self._json(403, {'error':'Нет доступа.'})
            if path == '/api/icon-theme':
                return self._json(200, {'ok':True})
            if path in ('/api/converter/inspect', '/api/converter/convert'):
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
    return server, token
