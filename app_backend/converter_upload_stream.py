"""Streaming multipart upload. Only protocol framing is validated here.

Document bytes are never accumulated in RAM or inspected for format/quality.
64 KiB is a transfer buffer, not a file-size limit.
"""
from __future__ import annotations

from email.message import Message
from email.parser import BytesHeaderParser
from email import policy
import errno
from pathlib import Path

from .libreoffice_engine import LibreOfficeError, source_format

CHUNK = 64 * 1024


def content_length(headers):
    value = headers.get('Content-Length', '')
    if not value.isdecimal() or int(value) <= 0:
        raise LibreOfficeError('Не указан размер HTTP-запроса.', 'INVALID_REQUEST')
    return int(value)


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
        import io
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
                path = folder / name
                warnings = []
                try:
                    output = path.open('xb')
                except OSError as exc:
                    if exc.errno not in (errno.ENAMETOOLONG, errno.EINVAL) and getattr(exc, 'winerror', None) not in (123, 206):
                        raise
                    extension = Path(name).suffix
                    safe_suffix = extension if len(extension) < 32 and extension.isascii() else ''
                    path = folder / ('input' + safe_suffix)
                    output = path.open('xb')
                    warnings.append('SOURCE_NAME_NORMALIZED')
                with output:
                    finished = self._write_part(output)
                files.append({'path': path, 'name': name, 'size': path.stat().st_size,
                              'warnings': warnings})
            elif key in ('ids', 'format', 'formats') and key not in fields:
                value = io.BytesIO()
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
