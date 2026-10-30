"""A06 published-ticket durability; native conversion is a controlled fixture."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from shutil import copytree
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

from app_backend.converter_transport import ConverterTransport
from app_backend.converter_guard.result_store import ResultStore
from app_backend.libreoffice_engine import LibreOfficeError


ROOT = Path(__file__).resolve().parents[1]


def convert_fixture(source, target, workspace, **kwargs):
    folder = workspace / 'output'
    folder.mkdir(parents=True)
    path = folder / ('Результат.' + target)
    path.write_bytes(b'%PDF-1.7\nlegacy-durable-fixture\n')
    return [path]


def retained(transport, identity='a' * 32):
    request = transport.begin(identity, None)
    with patch('app_backend.converter_transport.convert_path', side_effect=convert_fixture):
        value = transport.execute(request, [{'path': Path('source.txt'), 'warnings': []}], ['file'], ['pdf'])
    transport.finish(request, retained=True)
    return request, value['result']['ticket']


class LegacyDurability(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.parent = Path(self.temporary.name) / 'legacy'

    def transport(self):
        value = ConverterTransport(recovery_parent=self.parent)
        self.addCleanup(value.close)
        return value

    def test_normal_close_preserves_bytes_and_restart_rotates_private_ticket(self):
        first = self.transport()
        request, ticket = retained(first)
        old_path = first.results[ticket].path
        expected = old_path.read_bytes()
        first.close()
        self.assertEqual(old_path.read_bytes(), expected)
        second = self.transport()
        value = second.recover(request.identity)
        self.assertEqual(value['state'], 'ready')
        self.assertNotEqual(value['result']['ticket'], ticket)
        self.assertTrue(value['result']['restoredFromJournal'])
        self.assertFalse(value['result']['conversionQualityCompared'])
        with second.lease(value['result']['ticket']) as result:
            self.assertEqual(result.path.read_bytes(), expected)
        self.assertTrue(second.inventory()['restartRecovery'])
        self.assertNotIn(ticket, (request.root / 'legacy-result.json').read_text())

    def test_real_child_crash_after_publication_survives_without_finish_or_close(self):
        code = '''
import os,sys
from pathlib import Path
from unittest.mock import patch
from app_backend.converter_transport import ConverterTransport
def convert(source,target,workspace,**kwargs):
    out=workspace/'output';out.mkdir(parents=True)
    path=out/'result.pdf';path.write_bytes(b'%PDF-1.7 child crash bytes');return [path]
t=ConverterTransport(recovery_parent=Path(sys.argv[1]))
r=t.begin('b'*32,None)
with patch('app_backend.converter_transport.convert_path',side_effect=convert):
    t.execute(r,[{'path':Path('source.txt'),'warnings':[]}],['file'],['pdf'])
os._exit(23)
'''
        result = subprocess.run([sys.executable, '-c', code, str(self.parent)], cwd=ROOT, timeout=20)
        self.assertEqual(result.returncode, 23)
        transport = self.transport()
        value = transport.recover('b' * 32)
        self.assertEqual(value['state'], 'ready')
        with transport.lease(value['result']['ticket']) as output:
            self.assertEqual(output.path.read_bytes(), b'%PDF-1.7 child crash bytes')

    def test_corrupt_journal_does_not_delete_or_publish_existing_files(self):
        first = self.transport()
        request, ticket = retained(first)
        output = first.results[ticket].path
        expected = output.read_bytes()
        first.close()
        (request.root / 'legacy-result.json').write_bytes(b'{broken')
        second = self.transport()
        self.assertEqual(second.inventory()['total'], 0)
        self.assertEqual(second.inventory()['recoveryWarnings'][0]['code'], 'LEGACY_RECOVERY_UNVERIFIED')
        self.assertEqual(output.read_bytes(), expected)

    def test_changed_output_never_restores_as_ready(self):
        first = self.transport()
        _, ticket = retained(first)
        output = first.results[ticket].path
        first.close()
        output.write_bytes(b'changed payload')
        second = self.transport()
        self.assertEqual(second.recover('a' * 32)['state'], 'not_found')
        self.assertEqual(output.read_bytes(), b'changed payload')

    def test_checksumming_a_traversal_path_does_not_authorize_it(self):
        first = self.transport()
        request, _ = retained(first)
        first.close()
        outside = Path(self.temporary.name) / 'outside.pdf'
        outside.write_bytes(b'outside bytes must remain')
        path = request.root / 'legacy-result.json'
        value = json.loads(path.read_text())
        value.pop('checksum')
        value.update(path='../../outside.pdf', size=outside.stat().st_size,
                     sha256=hashlib.sha256(outside.read_bytes()).hexdigest())
        value['checksum'] = ResultStore._digest(value)
        path.write_text(json.dumps(value))
        second = self.transport()
        self.assertEqual(second.inventory()['total'], 0)
        self.assertEqual(outside.read_bytes(), b'outside bytes must remain')

    def test_competing_instance_cannot_claim_or_remove_an_owned_result(self):
        first = self.transport()
        request, ticket = retained(first)
        second = self.transport()
        self.assertEqual(second.inventory()['total'], 0)
        self.assertEqual(second.inventory()['recoveryWarnings'][0]['code'], 'LEGACY_RECOVERY_BUSY')
        second.close()
        self.assertTrue(first.results[ticket].path.is_file())
        first.close()
        third = self.transport()
        self.assertEqual(third.recover(request.identity)['state'], 'ready')

    def test_explicit_release_remains_deleted_after_restart(self):
        first = self.transport()
        request, ticket = retained(first)
        self.assertTrue(first.release(ticket))
        first.close()
        second = self.transport()
        self.assertEqual(second.inventory()['total'], 0)
        self.assertFalse(request.root.exists())

    def test_duplicate_request_ids_do_not_silently_choose_one_result(self):
        first = self.transport()
        request, _ = retained(first)
        first.close()
        duplicate = self.parent / ('aio-legacy-' + 'f' * 32)
        copytree(request.root, duplicate)
        second = self.transport()
        self.assertEqual(second.inventory()['total'], 0)
        self.assertEqual(len(second.inventory()['recoveryWarnings']), 2)
        self.assertTrue(request.root.is_dir())
        self.assertTrue(duplicate.is_dir())

    def test_failed_explicit_cleanup_has_a_durable_no_restore_barrier(self):
        first = self.transport()
        request, ticket = retained(first)
        owner = request.temporary
        with patch.object(owner.store, 'cleanup_retired', side_effect=PermissionError('locked')), \
                patch.object(first, '_schedule_cleanup'):
            self.assertFalse(first.release(ticket))
            self.assertTrue(owner.store.retirement_pending())
            owner.close()
            second = self.transport()
            self.assertEqual(second.inventory()['total'], 0)
            self.assertTrue(request.root.exists())

    def test_failed_journal_write_preserves_native_bytes_without_false_success(self):
        transport = self.transport()
        request = transport.begin('c' * 32, None)
        with patch('app_backend.converter_transport.convert_path', side_effect=convert_fixture), \
                patch('app_backend.legacy_result_journal.atomic_json', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                transport.execute(request, [{'path': Path('source.txt'), 'warnings': []}], ['file'], ['pdf'])
        transport.finish(request)
        self.assertEqual(transport.inventory()['total'], 0)
        self.assertEqual(len(list(request.root.rglob('*.pdf'))), 1)
        transport.close()
        restarted = self.transport()
        self.assertEqual(restarted.inventory()['total'], 0)
        self.assertTrue(restarted.inventory()['recoveryWarnings'])
        self.assertEqual(len(list(request.root.rglob('*.pdf'))), 1)

    def test_partially_successful_zip_restores_its_original_error_report(self):
        first = self.transport()
        request = first.begin('d' * 32, None)
        def convert(source, target, workspace, **kwargs):
            if target == 'bad':
                raise LibreOfficeError('Fixture refused', 'FIXTURE_UNSUPPORTED')
            return convert_fixture(source, target, workspace, **kwargs)
        with patch('app_backend.converter_transport.convert_path', side_effect=convert):
            original = first.execute(request, [{'path': Path('source.txt'), 'warnings': []}], ['file'], ['pdf', 'bad'])
        first.finish(request, retained=True)
        first.close()
        second = self.transport()
        report = second.recover(request.identity)['result']['targetReport']
        self.assertEqual(report, original['result']['targetReport'])
        self.assertEqual((report['succeeded'], report['failed']), (1, 1))
        self.assertEqual(report['files'][0]['targets'][1]['code'], 'FIXTURE_UNSUPPORTED')

    def test_zero_byte_text_contract_remains_valid_after_restart(self):
        first = self.transport()
        request = first.begin('e' * 32, None)
        def convert(source, target, workspace, **kwargs):
            output = convert_fixture(source, target, workspace, **kwargs)[0]
            output.write_bytes(b'')
            return [output]
        with patch('app_backend.converter_transport.convert_path', side_effect=convert):
            first.execute(request, [{'path': Path('empty.odt'), 'warnings': []}], ['file'], ['txt'])
        first.finish(request, retained=True)
        first.close()
        second = self.transport()
        value = second.recover(request.identity)
        self.assertEqual(value['state'], 'ready')
        self.assertEqual(value['result']['size'], 0)

    def test_final_http_bytes_are_written_only_after_file_lease_is_released(self):
        transport = self.transport()
        _, ticket = retained(transport)
        result = transport.results[ticket]
        result.path.write_bytes(b'a' * (64 * 1024 + 17))
        received, header_sizes = bytearray(), []
        def write(chunk):
            received.extend(chunk)
            if len(received) == 64 * 1024 + 17:
                self.assertEqual(result.leases, 0)
                self.assertTrue(transport.release(ticket))
        transport.stream_result(ticket, lambda result, size: header_sizes.append(size), write)
        self.assertEqual(header_sizes, [64 * 1024 + 17])
        self.assertEqual(received, b'a' * (64 * 1024 + 17))

    def test_empty_http_headers_are_written_after_lease_is_released(self):
        transport = self.transport()
        _, ticket = retained(transport)
        result = transport.results[ticket]
        result.path.write_bytes(b'')
        def headers(result, size):
            self.assertEqual(size, 0)
            self.assertEqual(result.leases, 0)
            self.assertTrue(transport.release(ticket))
        transport.stream_result(ticket, headers, lambda chunk: self.fail('no body expected'))

    def test_real_http_server_restart_rediscovers_only_with_new_session_token(self):
        from app_backend.server import start_server
        parent = Path(self.temporary.name)
        first, _ = start_server(guard_recovery_parent=parent/'guard-one', legacy_recovery_parent=self.parent)
        request, old_ticket = retained(first.converter)
        first.server_close()
        second, token = start_server(guard_recovery_parent=parent/'guard-two', legacy_recovery_parent=self.parent)
        worker = threading.Thread(target=second.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        worker.start()
        try:
            base = 'http://127.0.0.1:' + str(second.server_port)
            def get(path):
                with urlopen(Request(base+path, headers={'X-AIO-Token': token}), timeout=5) as response:
                    return response.read()
            inventory = json.loads(get('/api/converter/recover'))
            self.assertTrue(inventory['restartRecovery'])
            self.assertEqual(inventory['items'][0]['requestId'], request.identity)
            restored = json.loads(get('/api/converter/recover?request=' + request.identity))
            self.assertNotEqual(restored['result']['ticket'], old_ticket)
            self.assertEqual(get(restored['result']['downloadUrl']), b'%PDF-1.7\nlegacy-durable-fixture\n')
        finally:
            second.shutdown()
            second.server_close()
            worker.join(5)


if __name__ == '__main__':
    unittest.main()
