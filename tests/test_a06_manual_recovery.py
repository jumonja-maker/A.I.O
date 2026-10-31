"""A06 automatic hosted regressions. No user-machine tests or user files."""
import hashlib
import io
import json
import threading
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile
from urllib.request import Request, urlopen

from app_backend.converter_transport import ConverterTransport
from app_backend.libreoffice_engine import LibreOfficeError


class A06ManualRecovery(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.parent = Path(self.folder.name) / 'legacy'

    def make_orphan(self, suffix='a', payload=b'%PDF-1.7\nsurviving-bytes'):
        root = self.parent / ('aio-legacy-' + suffix * 32)
        output = root / 'native' / '0' / '0' / 'output' / 'survivor.pdf'
        output.parent.mkdir(parents=True)
        output.write_bytes(payload)
        (root / 'legacy-result.json').write_bytes(b'{broken')
        return root, output

    def transport(self, **kwargs):
        value = ConverterTransport(recovery_parent=self.parent, lazy_recovery=True, **kwargs)
        self.addCleanup(value.close)
        return value

    def test_manual_first_scan_and_repeat_of_unverified_root(self):
        root, output = self.make_orphan()
        transport = self.transport()
        self.assertFalse(transport._recovery_loaded)
        self.assertEqual(len(transport.recovery_issues), 0)
        one = transport.inventory()
        self.assertEqual(one['total'], 0)
        self.assertEqual(one['recoveryWarnings'][0]['workspaceId'], root.name)
        self.assertEqual(transport.scan_recovery()['state'], 'completed')
        self.assertEqual(transport.inventory()['total'], 0)
        self.assertEqual(len(transport.inventory()['recoveryWarnings']), 1)
        self.assertEqual(output.read_bytes(), b'%PDF-1.7\nsurviving-bytes')

    def test_unverified_zip_is_not_a_conversion_success_and_does_not_delete_source(self):
        root, output = self.make_orphan()
        transport = self.transport()
        self.assertEqual(transport.inventory()['total'], 0)
        response = transport.salvage_workspace(root.name)
        self.assertTrue(response['sourceKept'])
        self.assertFalse(response['conversionSuccess'])
        self.assertTrue(response['result']['salvagedUnverified'])
        self.assertEqual(response['result']['targetReport']['succeeded'], 0)
        self.assertEqual(response['result']['targetReport']['code'], 'RECOVERED_UNVERIFIED')
        ticket = response['result']['ticket']
        with transport.lease(ticket) as result:
            self.assertTrue(result.salvaged)
            with ZipFile(result.path) as archive:
                self.assertIsNone(archive.testzip())
                self.assertEqual(archive.read('native/0/0/output/survivor.pdf'), output.read_bytes())
                report = json.loads(archive.read('AIO-salvage-report.json'))
                self.assertFalse(report['conversionSuccess'])
                self.assertFalse(report['completenessVerified'])
                self.assertEqual(report['files'][0]['sha256'],
                                 hashlib.sha256(output.read_bytes()).hexdigest())
        self.assertTrue(transport.release(ticket))
        self.assertTrue(output.is_file())
        self.assertEqual(transport.inventory()['total'], 0)

    def test_invalid_workspace_cannot_be_salvaged(self):
        root, output = self.make_orphan()
        transport = self.transport()
        transport.inventory()
        for identity in ('../../outside', 'aio-legacy-' + '0' * 32, ''):
            with self.subTest(identity=identity), self.assertRaises(LibreOfficeError):
                transport.salvage_workspace(identity)
        self.assertTrue(output.is_file())

    def test_salvage_limit_does_not_create_false_ticket(self):
        root, output = self.make_orphan()
        transport = self.transport(max_owned_bytes=128)
        transport.inventory()
        with self.assertRaises(LibreOfficeError) as exc:
            transport.salvage_workspace(root.name)
        self.assertEqual(exc.exception.code, 'LEGACY_STORAGE_BYTES')
        self.assertEqual(transport.inventory()['total'], 0)
        self.assertTrue(output.is_file())

    def test_unverified_existing_root_counts_before_manual_scan(self):
        root, output = self.make_orphan()
        transport = self.transport(max_owned_roots=1)
        self.assertFalse(transport._recovery_loaded)
        with self.assertRaises(LibreOfficeError) as exc:
            transport.begin('b' * 32, None)
        self.assertEqual(exc.exception.code, 'LEGACY_STORAGE_LIMIT')
        self.assertFalse(transport._recovery_loaded)
        self.assertTrue(output.is_file())

    def test_retry_after_writer_releases_owner_lock(self):
        first = self.transport()
        root = None
        request = first.begin('c' * 32, None)
        root = request.root
        source = root / 'native/0/0/output/retry.pdf'
        source.parent.mkdir(parents=True)
        source.write_bytes(b'%PDF-1.7\nretry')
        (root / 'legacy-result.json').write_bytes(b'{broken')
        request.temporary.preserve_outputs = True  # Failed publication left native bytes.
        second = self.transport()
        one = second.inventory()
        self.assertEqual(one['recoveryWarnings'][0]['code'], 'LEGACY_RECOVERY_BUSY')
        first.finish(request)
        first.close()
        two = second.scan_recovery()
        self.assertEqual(two['warnings'][0]['code'], 'LEGACY_RECOVERY_UNVERIFIED')
        self.assertTrue(source.exists())

    def test_real_local_http_recover_salvage_and_release(self):
        from app_backend.server import start_server
        root, output = self.make_orphan('e')
        server, token = start_server(
            legacy_recovery_parent=self.parent,
            guard_recovery_parent=Path(self.folder.name) / 'guard',
            guard_history_parent=Path(self.folder.name) / 'history')
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = 'http://127.0.0.1:' + str(server.server_port)
        def http(path, body=None):
            payload = None if body is None else json.dumps(body).encode('utf-8')
            request = Request(base + path, data=payload,
                headers={'X-AIO-Token': token, 'Origin': base,
                         **({'Content-Type': 'application/json'} if payload is not None else {})},
                method='POST' if payload is not None else 'GET')
            with urlopen(request, timeout=15) as response:
                return response.read()
        try:
            self.assertFalse(server.converter._recovery_loaded)
            found = json.loads(http('/api/converter/recover?limit=20&offset=0'))
            self.assertEqual(found['total'], 0)
            self.assertEqual(found['recoveryWarnings'][0]['workspaceId'], root.name)
            salvaged = json.loads(http('/api/converter/salvage', {'workspaceId': root.name}))
            self.assertTrue(salvaged['result']['salvagedUnverified'])
            ticket = salvaged['result']['ticket']
            payload = http(salvaged['result']['downloadUrl'])
            with ZipFile(io.BytesIO(payload)) as archive:
                self.assertEqual(archive.read('native/0/0/output/survivor.pdf'), output.read_bytes())
            done = json.loads(http('/api/converter/release', {'ticket': ticket}))
            self.assertTrue(done['ok'])
            self.assertTrue(output.is_file())
            scan = json.loads(http('/api/converter/rescan', {}))
            self.assertEqual(scan['state'], 'completed')
            self.assertEqual(json.loads(http('/api/converter/recover?offset=0&limit=20'))['total'], 0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)

    def test_guard_history_remains_independent_of_legacy_storage(self):
        from app_backend.server import start_server
        parent = Path(self.folder.name)
        server, _token = start_server(
            guard_recovery_parent=parent / 'jobs',
            guard_history_parent=parent / 'history')
        try:
            self.assertEqual(server.converter.recovery_parent, parent / 'legacy-jobs')
            self.assertFalse(server.converter._recovery_loaded)
            self.assertEqual(server.guard.history_list()['items'], [])
            self.assertEqual(server.guard.recoverable(scan=False)['jobs'], [])
        finally:
            server.server_close()


if __name__ == '__main__':
    unittest.main()
