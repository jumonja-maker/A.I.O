"""A06: refusal is a durable, user-confirmed retirement, never an unsolicited popup."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from app_backend.converter_transport import ConverterTransport
from app_backend.converter_guard.result_store import ResultStore
from app_backend.libreoffice_engine import LibreOfficeError


def fixture_convert(source, target, workspace, **kwargs):
    output = workspace / 'output'
    output.mkdir(parents=True)
    item = output / ('result.' + target)
    item.write_bytes(b'%PDF-1.7\nvalid bytes')
    return [item]


class DeclineRecovery(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.parent = self.base / 'legacy-jobs'

    def transport(self, **kwargs):
        instance = ConverterTransport(recovery_parent=self.parent, lazy_recovery=True, **kwargs)
        self.addCleanup(instance.close)
        return instance

    def make_verified(self):
        transport = self.transport()
        task = transport.begin('a' * 32, None)
        with patch('app_backend.converter_transport.convert_path', side_effect=fixture_convert):
            result = transport.execute(task, [{'path':self.base/'source.txt','warnings':[]}], ['one'], ['pdf'])
        transport.finish(task, retained=True)
        root = task.root
        transport.close()
        return root, result['result']['ticket']

    def make_corrupt(self, character='f'):
        root = self.parent / ('aio-legacy-' + character * 32)
        output = root / 'native' / '0' / '0' / 'output' / 'orphan.pdf'
        output.parent.mkdir(parents=True)
        output.write_bytes(b'%PDF-1.7\nold orphan')
        (root / 'legacy-result.json').write_bytes(b'{bad journal')
        return root, output

    def test_confirmed_single_decline_retired_after_reboot(self):
        root, old_ticket = self.make_verified()
        current = self.transport()
        items = current.inventory()
        self.assertEqual(items['total'], 1)
        self.assertEqual(items['restoredCount'], 1)
        identity = items['items'][0]['requestId']
        self.assertTrue(items['items'][0]['restoredFromJournal'])
        self.assertNotEqual(current.recover(identity)['result']['ticket'], old_ticket)
        outcome = current.decline_recovery(request_id=identity)
        self.assertTrue(outcome['declined'])
        self.assertFalse(outcome['willReappear'])
        self.assertEqual(current.inventory()['total'], 0)
        current.close()
        after_restart = self.transport()
        inventory = after_restart.inventory()
        self.assertEqual(inventory['total'], 0)
        self.assertEqual(inventory['restoredCount'], 0)
        self.assertEqual(inventory['recoveryWarnings'], [])
        self.assertFalse(root.exists())

    def test_corrupt_root_can_be_declined_without_salvage(self):
        root, output = self.make_corrupt()
        current = self.transport()
        self.assertEqual(current.inventory()['recoveryWarnings'][0]['workspaceId'], root.name)
        result = current.decline_recovery(workspace_id=root.name)
        self.assertTrue(result['declined'])
        self.assertEqual(current.inventory()['recoveryWarnings'], [])
        current.close()
        restarted = self.transport()
        self.assertEqual(restarted.inventory()['recoveryWarnings'], [])
        self.assertFalse(output.exists())
        self.assertFalse(root.exists())

    def test_declined_pending_deletion_does_not_reappear(self):
        root, output = self.make_corrupt()
        current = self.transport()
        current.inventory()
        with patch.object(ResultStore, 'cleanup_retired', side_effect=PermissionError('locked')):
            outcome = current.decline_recovery(workspace_id=root.name)
        self.assertTrue(outcome['declined'])
        self.assertTrue(outcome['cleanupPending'])
        self.assertTrue(root.exists())
        self.assertTrue(ResultStore(root).retirement_pending())
        current.close()
        restarted = self.transport()
        snapshot = restarted.inventory()
        self.assertEqual(snapshot['total'], 0)
        self.assertEqual(snapshot['recoveryWarnings'], [])
        self.assertFalse(root.exists())

    def test_bulk_decline_only_previous_and_not_current(self):
        verified, _ = self.make_verified()
        orphan, _ = self.make_corrupt('e')
        current = self.transport()
        self.assertEqual(current.inventory()['restoredCount'], 1)
        task = current.begin('b' * 32, None)
        with patch('app_backend.converter_transport.convert_path', side_effect=fixture_convert):
            value = current.execute(task, [{'path':self.base/'source.txt','warnings':[]}], ['two'], ['pdf'])
        current.finish(task, retained=True)
        current_ticket = value['result']['ticket']
        result = current.decline_all_recovery()
        self.assertEqual(result['declined'], 2)
        self.assertEqual(result['notDeclined'], 0)
        snapshot = current.inventory()
        self.assertEqual(snapshot['total'], 1)
        self.assertEqual(snapshot['restoredCount'], 0)
        self.assertEqual(snapshot['recoveryWarnings'], [])
        self.assertIsNotNone(current.results.get(current_ticket))
        self.assertFalse(verified.exists())
        self.assertFalse(orphan.exists())

    def test_path_traversal_and_current_results_rejected(self):
        current = self.transport()
        task = current.begin('b' * 32, None)
        with patch('app_backend.converter_transport.convert_path', side_effect=fixture_convert):
            result = current.execute(task, [{'path':self.base/'source.txt','warnings':[]}], ['one'], ['pdf'])
        current.finish(task, retained=True)
        for kwargs in ({'request_id':'b'*32}, {'workspace_id':'../../outside'},
                       {'workspace_id':'aio-legacy-'+'f'*32}, {}):
            with self.subTest(kwargs=kwargs), self.assertRaises(LibreOfficeError):
                current.decline_recovery(**kwargs)
        self.assertEqual(current.inventory()['total'], 1)
        self.assertIn(result['result']['ticket'], current.results)

    def test_http_requires_confirmation_and_does_not_affect_history(self):
        from app_backend.server import start_server
        root, output = self.make_corrupt()
        server, token = start_server(legacy_recovery_parent=self.parent,
                                     guard_recovery_parent=self.base/'guard-jobs',
                                     guard_history_parent=self.base/'report-history')
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def post(value, *, token_ok=True):
            conn = http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=10)
            body = json.dumps(value).encode()
            try:
                conn.request('POST','/api/converter/decline',body=body,
                             headers={'Content-Type':'application/json',
                                      'X-AIO-Token':token if token_ok else 'invalid'})
                response=conn.getresponse()
                return response.status, json.loads(response.read())
            finally:
                conn.close()
        try:
            self.assertEqual(post({'workspaceId':root.name,'confirm':True},token_ok=False)[0],403)
            self.assertNotEqual(post({'workspaceId':root.name})[0],200)
            self.assertNotEqual(post({'workspaceId':root.name,'confirm':False})[0],200)
            self.assertEqual(output.read_bytes(),b'%PDF-1.7\nold orphan')
            self.assertEqual(server.guard.history_list()['items'],[])
            status,result=post({'scope':'all_previous','confirm':True})
            self.assertEqual(status,200)
            self.assertEqual(result['declined'],1)
            self.assertEqual(server.guard.history_list()['items'],[])
            self.assertEqual(server.guard.recoverable(scan=False)['jobs'],[])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)
        new = self.transport()
        self.assertEqual(new.inventory()['recoveryWarnings'],[])
        self.assertFalse(root.exists())


if __name__ == '__main__':
    unittest.main()
