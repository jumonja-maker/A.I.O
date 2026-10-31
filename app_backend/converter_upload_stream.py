"""Streaming multipart upload. Only protocol framing is validated here.

Document bytes are never accumulated in RAM or inspected for format/quality.
64 KiB is a transfer buffer, not a file-size limit.
"""
from __future__ import annotations

from email.message import Message
from email.parser import BytesHeaderParser
from email import policy
import errno
import io
import re
from pathlib import Path, PureWindowsPath

from .libreoffice_engine import LibreOfficeError, source_format

CHUNK = 64 * 1024
# Protocol metadata is bounded independently of document bytes. Files continue
# to stream without an implicit size limit through the same parser.
MAX_METADATA_BYTES = 16 * 1024 * 1024


class _MetadataBuffer(io.BytesIO):
    def __init__(self, upload):
        super().__init__()
        self.upload = upload

    def write(self, value):
        if self.upload.metadata_bytes + len(value) > MAX_METADATA_BYTES:
            raise LibreOfficeError('Превышен лимит метаданных запроса.', 'INVALID_REQUEST')
        written = super().write(value)
        self.upload.metadata_bytes += written
        return written


def content_length(headers):
    values = (headers.get_all('Content-Length', []) if hasattr(headers, 'get_all')
              else [headers.get('Content-Length', '')])
    if len(values) != 1 or headers.get('Transfer-Encoding') is not None:
        raise LibreOfficeError('Требуется однозначный размер HTTP-запроса.', 'INVALID_REQUEST')
    value = values[0]
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        raise LibreOfficeError('Не указан размер HTTP-запроса.', 'INVALID_REQUEST')
    try:
        length = int(value)
    except ValueError as error:
        raise LibreOfficeError('Некорректный размер HTTP-запроса.', 'INVALID_REQUEST') from error
    if length <= 0:
        raise LibreOfficeError('Не указан размер HTTP-запроса.', 'INVALID_REQUEST')
    return length


class MultipartUpload:
    def __init__(self, stream, length, content_type, cancel):
        header = Message()
        header['Content-Type'] = content_type
        boundary = header.get_boundary()
        if (header.get_content_type() != 'multipart/form-data' or not boundary or
                len(boundary) > 70 or not boundary.isascii() or '\r' in boundary or '\n' in boundary):
            raise LibreOfficeError('Некорректная граница multipart.', 'INVALID_REQUEST')
        self.stream, self.left, self.cancel = stream, length, cancel
        self.buffer = bytearray()
        self.boundary = boundary.encode('ascii')
        self.metadata_bytes = 0

    def _more(self):
        if self.cancel.is_set():
            raise LibreOfficeError('Загрузка отменена.', 'CANCELLED')
        if not self.left:
            raise LibreOfficeError('Загрузка файла прервана.', 'UPLOAD_INCOMPLETE')
        data = self.stream.read(min(CHUNK, self.left))
        if self.cancel.is_set():
            raise LibreOfficeError('Загрузка отменена.', 'CANCELLED')
        if not data:
            raise LibreOfficeError('Загрузка файла прервана.', 'UPLOAD_INCOMPLETE')
        self.left -= len(data)
        self.buffer.extend(data)

    def _take_until(self, marker, *, header=False):
        while True:
            pos = self.buffer.find(marker)
            if pos >= 0:
                value = bytes(self.buffer[:pos])
                del self.buffer[:pos + len(marker)]
                return value
            # HTTP/MIME header bound, unrelated to file size or document policy.
            if header and len(self.buffer) > CHUNK:
                raise LibreOfficeError('Некорректный заголовок multipart.', 'INVALID_REQUEST')
            self._more()

    def _write_part(self, destination):
        marker = b'\r\n--' + self.boundary
        keep = len(marker) + 2
        while True:
            pos = self.buffer.find(marker)
            if pos >= 0:
                end = pos + len(marker)
                while len(self.buffer) < end + 2:
                    self._more()
                suffix = self.buffer[end:end + 2]
                if suffix in (b'\r\n', b'--'):
                    destination.write(self.buffer[:pos])
                    del self.buffer[:end + 2]
                    return suffix == b'--'
                # A boundary-like byte sequence inside a document is data.
                destination.write(self.buffer[:pos + 2])
                del self.buffer[:pos + 2]
                continue
            flush = len(self.buffer) - keep
            if flush > 0:
                destination.write(self.buffer[:flush])
                del self.buffer[:flush]
            self._more()

    def read(self, directory: Path):
        prefix = b'--' + self.boundary + b'\r\n'
        while len(self.buffer) < len(prefix):
            self._more()
        if not self.buffer.startswith(prefix):
            raise LibreOfficeError('Некорректное начало multipart.', 'INVALID_REQUEST')
        del self.buffer[:len(prefix)]
        files, fields = [], {}
        finished = False
        while not finished:
            raw = self._take_until(b'\r\n\r\n', header=True)
            headers = BytesHeaderParser(policy=policy.default).parsebytes(raw + b'\r\n\r\n')
            if headers.defects or headers.get_content_disposition() != 'form-data':
                raise LibreOfficeError('Некорректное поле multipart.', 'INVALID_REQUEST')
            key = headers.get_param('name', header='content-disposition')
            if key == 'files':
                name = headers.get_filename()
                source_format(name)
                folder = directory / str(len(files))
                folder.mkdir(parents=True)
                # The HTTP filename is for display, never a trusted Windows path.
                # "C:escaped.txt" is drive-relative; "name:stream" is an ADS.
                windows = PureWindowsPath(name)
                stem = name.split('.', 1)[0].rstrip(' ').upper()
                reserved = stem in ('CON', 'PRN', 'AUX', 'NUL') or bool(
                    re.fullmatch(r'(?:COM|LPT)[1-9\u00b9\u00b2\u00b3]', stem))
                unsafe = (bool(windows.drive or windows.root) or
                          bool(re.search(r'[<>:"|?*]', name)) or
                          name.endswith((' ', '.')) or reserved)
                extension = Path(name).suffix
                suffix = extension if re.fullmatch(r'\.[A-Za-z0-9_-]{1,32}', extension) else '.bin'
                path = folder / ('input' + suffix if unsafe else name)
                warnings = ['SOURCE_NAME_NORMALIZED'] if unsafe else []
                try:
                    output = path.open('xb')
                except OSError as exc:
                    if exc.errno not in (errno.ENAMETOOLONG, errno.EINVAL) and getattr(exc, 'winerror', None) not in (123, 206):
                        raise
                    extension = Path(name).suffix
                    safe_suffix = extension if re.fullmatch(r'\.[A-Za-z0-9_-]{1,32}', extension) else '.bin'
                    path = folder / ('input' + safe_suffix)
                    output = path.open('xb')
                    if not warnings:
                        warnings.append('SOURCE_NAME_NORMALIZED')
                with output:
                    finished = self._write_part(output)
                files.append({'path': path, 'name': name, 'size': path.stat().st_size,
                              'warnings': warnings})
            elif key in ('ids', 'format', 'formats') and key not in fields:
                with _MetadataBuffer(self) as value:
                    finished = self._write_part(value)
                    fields[key] = value.getvalue().decode('utf-8', 'strict')
            else:
                raise LibreOfficeError('Некорректное поле запроса.', 'INVALID_REQUEST')
        # Consume the optional MIME epilogue in bounded chunks.
        self.buffer.clear()
        while self.left:
            self._more()
            self.buffer.clear()
        if not files:
            raise LibreOfficeError('Добавьте исходные файлы.', 'INPUT_MISSING')
        return files, fields
