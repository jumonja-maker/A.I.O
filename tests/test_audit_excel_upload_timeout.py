"""A05: stalled authenticated multipart uploads must release Excel merge gate."""
import io
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

from openpyxl import Workbook

import app_backend.server as server_module


def xlsx():
    book = Workbook()
    book.active.append(['Code', 'Value'])
    book.active.append(['00125', 123])
    output = io.BytesIO()
    book.save(output)
    book.close()
    return output.getvalue()


class StalledExcelUpload(unittest.TestCase):
    def test_idle_upload_fails_and_next_complete_merge_succeeds(self):
        with patch.object(server_module, 'EXCEL_UPLOAD_IDLE_SECONDS', 0.35):
            with tempfile.TemporaryDirectory() as temporary:
                server, token = server_module.start_server(
                    guard_recovery_parent=Path(temporary)/'jobs',
                    guard_history_parent=Path(temporary)/'history')
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                base = 'http://127.0.0.1:' + str(server.server_port)
                try:
                    with socket.create_connection(('127.0.0.1', server.server_port), timeout=4) as sock:
                        sock.settimeout(5)
                        headers = (
                            'POST /api/merge HTTP/1.1\r\n'
                            'Host: 127.0.0.1:' + str(server.server_port) + '\r\n'
                            'X-AIO-Token: ' + token + '\r\n'
                            'X-AIO-Job: ' + 'b'*32 + '\r\n'
                            'Content-Type: multipart/form-data; boundary=aio-timeout\r\n'
                            'Content-Length: 500\r\n'
                            'Connection: close\r\n\r\n').encode('ascii')
                        sock.sendall(headers + b'x')
                        response = bytearray()
                        deadline = time.monotonic() + 5
                        while b'EXCEL_UPLOAD_TIMEOUT' not in response and time.monotonic() < deadline:
                            chunk = sock.recv(4096)
                            if not chunk:
                                break
                            response.extend(chunk)
                        self.assertIn(b'408', response.split(b'\r\n', 1)[0])
                        self.assertIn(b'EXCEL_UPLOAD_TIMEOUT', response)

                    boundary = 'aio-followup-boundary'
                    def part(name):
                        return (('--'+boundary+'\r\n'
                            'Content-Disposition: form-data; name="files"; filename="'+name+'"\r\n'
                            'Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n'
                            '\r\n').encode('ascii') + xlsx() + b'\r\n')
                    body = part('a.xlsx')+part('b.xlsx')+('--'+boundary+'--\r\n').encode('ascii')
                    request = Request(base+'/api/merge', data=body, headers={
                        'X-AIO-Token':token, 'X-AIO-Job':'c'*32,
                        'Content-Type':'multipart/form-data; boundary='+boundary})
                    with urlopen(request, timeout=10) as reply:
                        self.assertEqual(reply.status, 200)
                        self.assertGreater(len(reply.read()), 500)
                    self.assertTrue(server.merge_gate.acquire(blocking=False))
                    server.merge_gate.release()
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=5)
