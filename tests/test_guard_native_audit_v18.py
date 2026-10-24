"""Batch preparation identity, native cancellation and failure-path cleanup."""
import json
from pathlib import Path
import shutil
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile

from app_backend.converter_guard.adapter import LibreOfficeAdapter, _PreparedSourceCache
from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard.office_session import OfficeSession
from app_backend.converter_guard.preflight import inspect_source
from app_backend.converter_guard.uno_session import UnoSession
from app_backend.native_process import ProcessTree


OFFICE = 'urn:oasis:names:tc:opendocument:xmlns:office:1.0'
TABLE = 'urn:oasis:names:tc:opendocument:xmlns:table:1.0'
TEXT = 'urn:oasis:names:tc:opendocument:xmlns:text:1.0'
WRITER = 'com.sun.star.text.TextDocument'


class NativeAuditV18(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cancel = threading.Event()

    def source(self, name='source.odt', marker='A'):
        path = self.root / name
        xml = (f'<o:document-content xmlns:o="{OFFICE}" xmlns:t="{TABLE}" xmlns:p="{TEXT}">'
               '<o:body><o:text><t:table><t:table-row>'
               f'<t:table-cell><p:p>{marker}</p:p></t:table-cell>'
               '<t:table-cell><p:p>B</p:p></t:table-cell>'
               '</t:table-row></t:table></o:text></o:body></o:document-content>')
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('mimetype', 'application/vnd.oasis.opendocument.text')
            archive.writestr('content.xml', xml)
            archive.writestr('Pictures/original.bin', b'original resource')
        return path

    def test_repeated_targets_share_only_private_preparation_and_preserve_original_bytes(self):
        from app_backend.converter_guard.odf_table_import import prepare_odf_table_source
        source = self.source()
        original = source.read_bytes()
        cache = _PreparedSourceCache(self.root / 'cache')
        with patch('app_backend.converter_guard.odf_table_import.prepare_odf_table_source',
                   wraps=prepare_odf_table_source) as prepare:
            prepared, summary = cache.prepare(source, epub=False, cancel=self.cancel)
            before = prepared.read_bytes()
            summary['columns'].clear()
            for _ in range(20):
                repeated, provenance = cache.prepare(source, epub=False, cancel=self.cancel)
                self.assertEqual(repeated, prepared)
                self.assertEqual(provenance['columns'], [2])
                self.assertEqual(repeated.read_bytes(), before)
            self.assertEqual(prepare.call_count, 1)
        self.assertEqual(source.read_bytes(), original)
        self.assertNotEqual(prepared, source)
        with zipfile.ZipFile(prepared) as archive:
            self.assertEqual(archive.read('Pictures/original.bin'), b'original resource')

    def test_source_change_invalidates_preparation_and_old_staging_is_removed(self):
        source = self.source()
        cache = _PreparedSourceCache(self.root / 'cache')
        first, _ = cache.prepare(source, epub=False, cancel=self.cancel)
        self.source(marker='C')
        second, _ = cache.prepare(source, epub=False, cancel=self.cancel)
        self.assertFalse(first.exists())
        self.assertNotEqual(first, second)
        with zipfile.ZipFile(second) as archive:
            self.assertIn(b'>C<', archive.read('content.xml'))
        self.assertEqual(len(list((self.root / 'cache').iterdir())), 1)

    def test_changed_private_copy_is_never_reused_and_cache_keeps_one_source(self):
        cache = _PreparedSourceCache(self.root / 'cache')
        source = self.source()
        first, _ = cache.prepare(source, epub=False, cancel=self.cancel)
        first.write_bytes(b'damaged private artifact')
        second, _ = cache.prepare(source, epub=False, cancel=self.cancel)
        self.assertFalse(first.exists())
        self.assertTrue(zipfile.is_zipfile(second))
        for index in range(12):
            next_source = self.source(str(index) + '.odt')
            next_prepared, _ = cache.prepare(next_source, epub=False, cancel=self.cancel)
            self.assertEqual(len(list((self.root / 'cache').iterdir())), 1)
            if index == 0:
                self.assertFalse(second.exists())
            self.assertTrue(next_prepared.exists())

    def test_source_changed_during_preparation_is_not_committed_and_cancel_is_early(self):
        cache = _PreparedSourceCache(self.root / 'cache')
        source = self.source()
        def changing(path, work, **kwargs):
            path.write_bytes(path.read_bytes() + b'changed')
            return path, None
        with patch('app_backend.converter_guard.odf_table_import.prepare_odf_table_source', side_effect=changing):
            with self.assertRaises(GuardError) as error:
                cache.prepare(source, epub=False, cancel=self.cancel)
        self.assertEqual(error.exception.code, 'SOURCE_CHANGED_DURING_PREPARATION')
        self.assertIsNone(cache.entry)
        self.cancel.set()
        with patch('app_backend.converter_guard.odf_table_import.prepare_odf_table_source') as prepare:
            with self.assertRaises(GuardError) as error:
                cache.prepare(source, epub=False, cancel=self.cancel)
        self.assertEqual(error.exception.code, 'CANCELLED')
        prepare.assert_not_called()

    def test_one_native_preflight_reuses_only_this_calls_source_inspection(self):
        source = self.source()
        executable = self.root / 'soffice'; executable.touch()
        native = SimpleNamespace(valid=True, fingerprint='registry',
            filters={}, import_services={WRITER}, imports={'odt': {WRITER}})
        adapter = LibreOfficeAdapter(executable)
        inspections = []
        from app_backend.converter_guard.source_contract import inspect_source_contract
        def inspect(path, **kwargs):
            inspections.append(path)
            return inspect_source_contract(path, **kwargs)
        class Child:
            returncode = 0
            def poll(self): return 0
        class Tree:
            def __init__(self, command, *, cwd, **kwargs):
                self.process = Child()
                (cwd / 'result.json').write_text(json.dumps({'state': 'detected', 'kind': 0}))
            def __enter__(self): return self
            def __exit__(self, *args): pass
        with patch.object(adapter, '_registry', return_value=native), \
                patch('app_backend.converter_guard.adapter.inspect_source_contract', side_effect=inspect), \
                patch('app_backend.converter_guard.source_contract.inspect_source_contract', side_effect=AssertionError('duplicate contract scan')), \
                patch('app_backend.converter_guard.preflight.ProcessTree', Tree):
            for index in range(2):
                result = adapter._preflight_native(source, self.root / ('work' + str(index)),
                                                  cancel=self.cancel, timeout=None)
                self.assertEqual(result['state'], 'detected')
        self.assertEqual(inspections, [source, source], 'separate calls must not reuse a stale contract')

    def test_cancelled_preflight_and_uno_start_create_no_process_or_workspace(self):
        self.cancel.set()
        work = self.root / 'work'
        session = UnoSession(self.root / 'soffice.exe', work, lambda path: self.fail('profile created'))
        with patch('app_backend.converter_guard.uno_session.ProcessTree') as process:
            with self.assertRaises(GuardError) as error:
                session.ensure_started(self.cancel)
            self.assertEqual(error.exception.code, 'CANCELLED')
        process.assert_not_called()
        self.assertFalse(work.exists())
        with patch('app_backend.converter_guard.preflight.ProcessTree') as process:
            with self.assertRaises(GuardError) as error:
                inspect_source('not used', 'not used', work, cancel=self.cancel, timeout=None,
                               prepare_profile=lambda path: self.fail('profile created'))
            self.assertEqual(error.exception.code, 'CANCELLED')
        process.assert_not_called()
        self.assertFalse(work.exists())

    def test_office_cleanup_finishes_logs_and_removes_profile_after_tree_close_error(self):
        session = OfficeSession('unused', self.root, lambda path: None)
        profile = self.root / 'profile'; profile.mkdir(); (profile / 'private').write_text('x')
        session.profile, session.pipe = profile, 'private pipe'
        session.tree = SimpleNamespace(close=Mock(side_effect=OSError('close failed')))
        logs = session.logs = SimpleNamespace(finish=Mock())
        with self.assertRaisesRegex(OSError, 'close failed'):
            session.recycle()
        logs.finish.assert_called_once()
        self.assertFalse(profile.exists())
        self.assertIsNone(session.tree)
        self.assertIsNone(session.logs)
        self.assertIsNone(session.profile)

    def test_startup_failure_cleans_owned_tree_even_if_launcher_already_exited(self):
        process = SimpleNamespace(kill=Mock(side_effect=ProcessLookupError()), wait=Mock())
        with patch('app_backend.native_process.subprocess.Popen', return_value=process), \
                patch.object(ProcessTree, 'close') as close, \
                patch.object(ProcessTree, '_make_windows_job'), \
                patch('app_backend.native_process.os.name', 'posix'):
            with self.assertRaisesRegex(RuntimeError, 'callback failed'):
                ProcessTree(['unused'], cwd=self.root,
                            on_started=Mock(side_effect=RuntimeError('callback failed')))
        close.assert_called_once()
        process.wait.assert_called_once_with(timeout=3)

    def test_all_rows_are_validated_without_retaining_a_width_list(self):
        from app_backend.converter_guard.odf_table_import import _insertions
        row = b'<t:table-row><t:table-cell/><t:table-cell/></t:table-row>'
        prefix = (f'<o:document-content xmlns:o="{OFFICE}" xmlns:t="{TABLE}">'
                  '<o:body><o:text><t:table>').encode()
        suffix = b'</t:table></o:text></o:body></o:document-content>'
        rows = 20000
        result = _insertions(prefix + row * rows + suffix, self.cancel)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][2:], (2, rows))
        ragged = b'<t:table-row><t:table-cell/></t:table-row>'
        self.assertEqual(_insertions(prefix + row * rows + ragged + suffix, self.cancel), [])

    def test_batch_reuses_source_evidence_for_targets_and_invalidates_for_next_file(self):
        from app_backend.converter_guard import content_invariants
        sources = [self.source('first.odt', 'A'), self.source('second.odt', 'C')]
        original = {path: path.read_bytes() for path in sources}
        executable = self.root / 'soffice'; executable.touch()
        adapter = LibreOfficeAdapter(executable)
        native = SimpleNamespace(valid=True)
        native_calls = []
        def convert(source, target, workspace, **kwargs):
            native_calls.append((source, target))
            output = workspace / 'output'; output.mkdir(parents=True)
            destination = output / (source.stem + '.' + target)
            if target == 'odt':
                shutil.copyfile(source, destination)
            else:
                with zipfile.ZipFile(source) as archive:
                    marker = 'A' if b'>A<' in archive.read('content.xml') else 'C'
                destination.write_text((f'<html><body>{marker} B</body></html>'
                                        if target == 'html' else marker + ' B'), encoding='utf-8')
            adapter._local.export_info = {}
            return [destination]
        batch_work = self.root / 'office'
        from app_backend.converter_guard.odf_table_import import prepare_odf_table_source
        with patch('app_backend.converter_guard.adapter.registry', return_value=native), \
                patch.object(adapter, 'route_decision', return_value='registered'), \
                patch('app_backend.converter_guard.native_metafile.native_metafile_target', return_value=None), \
                patch.object(adapter, '_convert_native', side_effect=convert), \
                patch('app_backend.converter_guard.content_invariants._source_cells',
                      wraps=content_invariants._source_cells) as inspect, \
                patch('app_backend.converter_guard.odf_table_import.prepare_odf_table_source',
                      wraps=prepare_odf_table_source) as prepare:
            with adapter.batch(batch_work):
                for file_index, source in enumerate(sources):
                    adapter._local.proof = {'state': 'detected'}
                    for target in ('txt', 'html', 'odt'):
                        paths = adapter.convert(source, target, self.root / (str(file_index) + target),
                            cancel=self.cancel, timeout=None, on_started=lambda: None)
                        self.assertEqual(len(paths), 1)
                        checks = adapter.export_info()['resultChecks']
                        self.assertTrue(any(c['check'] == 'odf_table_text' and
                                            c['status'] == 'checked_subset' for c in checks))
                self.assertEqual(prepare.call_count, 2)
                self.assertEqual(inspect.call_count, 2)
                self.assertEqual(len(list((batch_work / 'source-preparation').iterdir())), 1)
            self.assertIsNone(adapter._local.source_evidence)
            self.assertIsNone(adapter._local.source_preparation)
        self.assertEqual(len(native_calls), 6, 'every target must still perform its own native export')
        self.assertFalse(batch_work.exists())
        for source in sources:
            self.assertEqual(source.read_bytes(), original[source])

    def test_batch_workspace_removed_even_after_recycle_error(self):
        for factory in (OfficeSession, UnoSession):
            with self.subTest(factory=factory.__name__):
                work = self.root / factory.__name__; work.mkdir()
                (work / 'private-derived-copy').write_bytes(b'private')
                session = factory('unused', work, lambda path: None)
                with patch.object(session, 'recycle', side_effect=OSError('cleanup error')):
                    with self.assertRaises(OSError):
                        session.__exit__(None, None, None)
                self.assertFalse(work.exists())

    def test_preparation_failure_cannot_inherit_previous_tasks_native_provenance(self):
        adapter = LibreOfficeAdapter(self.root / 'soffice')
        adapter._local.execution_mode = 'native_uno'
        adapter._local.metrics = {'source': 'uno_document', 'importMs': 123, 'exportMs': 456}
        adapter._local.export_info = {'filter': 'previous filter', 'warnings': ['previous warning']}
        with patch('app_backend.converter_guard.odf_table_import.prepare_odf_table_source',
                   side_effect=GuardError('CANCELLED', 'cancelled', stage='import')):
            with self.assertRaises(GuardError):
                adapter.convert(self.source(), 'pdf', self.root / 'work', cancel=self.cancel,
                                timeout=None, on_started=lambda: self.fail('native started'))
        self.assertIsNone(adapter.execution_mode())
        self.assertIsNone(adapter.native_timings())
        self.assertEqual(adapter.export_info(), {})

    def test_locked_old_preparation_does_not_poison_the_next_source(self):
        cache = _PreparedSourceCache(self.root / 'batch' / 'private-preparation')
        first_source = self.source('first.odt', 'A')
        first, _ = cache.prepare(first_source, epub=False, cancel=self.cancel)
        locked = cache.current
        other = self.root / 'user-result.txt'; other.write_bytes(b'keep user output')
        second_source = self.source('second.odt', 'C')
        rmtree = shutil.rmtree
        def fail_locked(path, *args, **kwargs):
            if Path(path) == locked:
                raise PermissionError('Windows sharing / antivirus lock')
            return rmtree(path, *args, **kwargs)
        with patch('app_backend.converter_guard.adapter.shutil.rmtree', side_effect=fail_locked) as remove:
            second, _ = cache.prepare(second_source, epub=False, cancel=self.cancel)
            repeated, _ = cache.prepare(second_source, epub=False, cancel=self.cancel)
        self.assertEqual(repeated, second)
        self.assertNotEqual(first, second)
        self.assertTrue(first.exists(), 'locked derived copy stays owned by the batch')
        self.assertTrue(second.exists())
        self.assertEqual(cache.entry[0][0], str(second_source.resolve()))
        remove.assert_called_once_with(locked)
        self.assertEqual(other.read_bytes(), b'keep user output')
        self.assertTrue(first_source.is_file())
        self.assertTrue(second_source.is_file())

    def test_preparation_error_is_preserved_when_private_cleanup_also_fails(self):
        cache = _PreparedSourceCache(self.root / 'batch' / 'private-preparation')
        source = self.source()
        original = source.read_bytes()
        failure = GuardError('CANCELLED', 'Original cancellation', stage='import')
        attempted = []
        def fail_preparation(path, work, **kwargs):
            work.mkdir(parents=True)
            (work / 'partial-derived-copy').write_bytes(b'partial')
            attempted.append(work)
            raise failure
        with patch('app_backend.converter_guard.odf_table_import.prepare_odf_table_source',
                   side_effect=fail_preparation), \
                patch('app_backend.converter_guard.adapter.shutil.rmtree',
                      side_effect=PermissionError('Locked private artifact')) as remove:
            with self.assertRaises(GuardError) as error:
                cache.prepare(source, epub=False, cancel=self.cancel)
        self.assertIs(error.exception, failure)
        self.assertIsNone(cache.entry)
        self.assertIsNone(cache.current)
        remove.assert_called_once_with(attempted[0])
        self.assertEqual(attempted[0].parent, cache.workspace)
        self.assertTrue((attempted[0] / 'partial-derived-copy').is_file())
        self.assertEqual(source.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
