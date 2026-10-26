"""Native fault boundaries: an aborted import is never successful export.

Embedded worker tests exercise the shipped script and controlled UNO boundary;
they are not claims that every real LibreOffice importer triggers these faults.
"""
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app_backend.converter_guard.adapter import LibreOfficeAdapter
from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard.office_session import OfficeSession
from app_backend.converter_guard.preflight_uno import UNO_WORKER_SOURCE
from app_backend.converter_guard.uno_session import UnoSession
import test_guard_uno_session as contracts


class NativeImportV21(unittest.TestCase):
    def execute(self, requests, *, abort_load=None, abort_export=False, fail_export=False):
        loaded = []
        aborts = []
        def abort(handler):
            continuation = SimpleNamespace(queryInterface=lambda _: SimpleNamespace(
                select=lambda: aborts.append(True)))
            handler.handle(SimpleNamespace(getContinuations=lambda: [continuation],
                getRequest=lambda: SimpleNamespace()))
        def namespace(**kwargs):
            load = kwargs.get('loadComponentFromURL')
            if load is not None:
                def wrapped(url, frame, flags, props):
                    result = load(url, frame, flags, props)
                    values = {p.Name: p.Value for p in props}
                    loaded.append(values)
                    if len(loaded) == abort_load:
                        abort(values['InteractionHandler'])
                    return result
                kwargs['loadComponentFromURL'] = wrapped
            return SimpleNamespace(**kwargs)
        def on_export(_):
            if abort_export:
                abort(loaded[-1]['InteractionHandler'])
            if fail_export:
                raise RuntimeError('Lazy import failed while exporting')
        with patch('test_guard_uno_session.SimpleNamespace', side_effect=namespace):
            values = contracts.UnoWorkerContracts().execute(requests, on_export=on_export)
        return (*values, aborts)

    def test_aborted_import_with_nonnull_model_cannot_preflight_or_export(self):
        for operation in ('inspect', 'export'):
            with self.subTest(operation=operation):
                models, _, exports, replies, aborts = self.execute(
                    [{'operation': operation}, {}], abort_load=1)
                self.assertEqual(aborts, [True])
                self.assertFalse(replies[1]['ok'])
                self.assertEqual((replies[1]['code'], replies[1]['stage']),
                                 ('PREFLIGHT_INTERACTION_REQUIRED', 'import'))
                self.assertTrue(replies[2]['ok'], 'An independent next source still runs')
                self.assertEqual(len(exports), 1)
                self.assertTrue(all(model.closed for model in models))

    def test_lazy_import_interaction_during_store_cannot_commit_partial_export(self):
        models, _, exports, replies, aborts = self.execute([{}], abort_export=True)
        self.assertEqual(aborts, [True])
        self.assertEqual(len(exports), 1)
        self.assertFalse(replies[-1]['ok'])
        self.assertEqual(replies[-1]['code'], 'PREFLIGHT_INTERACTION_REQUIRED')
        self.assertTrue(models[0].closed)

    def test_lazy_import_failure_reports_import_cause_for_queue_deduplication(self):
        models, _, exports, replies, aborts = self.execute(
            [{}], abort_export=True, fail_export=True)
        self.assertEqual(aborts, [True])
        self.assertFalse(replies[-1]['ok'])
        self.assertEqual(replies[-1]['code'], 'PREFLIGHT_INTERACTION_REQUIRED')
        self.assertTrue(models[0].closed)

    def test_one_shot_preflight_cannot_admit_aborted_import_with_live_model(self):
        namespace = {'__name__': 'v21_one_shot'}
        exec(compile(UNO_WORKER_SOURCE, 'v21_one_shot', 'exec'), namespace)
        closed, aborted = [], []
        model = SimpleNamespace(supportsService=lambda value: value == contracts.W,
            getArgs=lambda: (SimpleNamespace(Name='FilterName', Value='Writer import'),),
            close=lambda _: closed.append(True))
        def load(_url, _frame, _flags, props):
            handler = next(p.Value for p in props if p.Name == 'InteractionHandler')
            continuation = SimpleNamespace(queryInterface=lambda _: SimpleNamespace(
                select=lambda: aborted.append(True)))
            handler.handle(SimpleNamespace(getContinuations=lambda: [continuation]))
            return model
        desktop = SimpleNamespace(loadComponentFromURL=load)
        resolver = SimpleNamespace(resolve=lambda _: remote)
        remote = SimpleNamespace(ServiceManager=SimpleNamespace(createInstanceWithContext=lambda name, _:
            resolver if name.endswith('UnoUrlResolver') else desktop))
        uno = ModuleType('uno'); uno.getComponentContext = lambda: remote
        uno.createUnoStruct = lambda _: SimpleNamespace(); uno.getTypeByName = lambda value: value
        helper = ModuleType('unohelper'); helper.Base = type('Base', (), {})
        task = ModuleType('com.sun.star.task'); task.XInteractionHandler = type('Handler', (), {})
        class Finished(Exception): pass
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); program = root / 'program'; program.mkdir()
            (program / 'soffice.exe').touch(); (root / 'profile').mkdir()
            (root / 'input.docx').write_bytes(b'original'); (root / 'start.token').touch()
            manifest = root / 'request.json'
            manifest.write_text(json.dumps({'program': str(program), 'profile': str(root / 'profile'),
                'source': str(root / 'input.docx'), 'documentServices': [contracts.W]}))
            with patch.dict(sys.modules, {'uno': uno, 'unohelper': helper, 'com.sun.star.task': task}), \
                    patch('subprocess.Popen'), patch('os._exit', side_effect=Finished):
                with self.assertRaises(Finished): namespace['main'](str(manifest))
            result = json.loads((root / 'result.json').read_text())
        self.assertEqual(aborted, [True]); self.assertEqual(closed, [True])
        self.assertEqual((result['state'], result['code']),
                         ('undetermined', 'PREFLIGHT_INTERACTION_REQUIRED'))


class NativeStartupV21(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cancel = threading.Event()

    def tree(self):
        process = SimpleNamespace(stdin=io.BytesIO(), stdout=io.BytesIO(), stderr=io.BytesIO(),
                                  poll=lambda: None)
        return SimpleNamespace(process=process, close=Mock())

    def session(self, factory=UnoSession):
        return factory(self.root / 'soffice.exe', self.root / 'session',
                       lambda path: path.mkdir(parents=True))

    def assert_released(self, session, tree):
        tree.close.assert_called_once()
        self.assertIsNone(session.tree)
        self.assertIsNone(session.profile)
        self.assertFalse(list(session.workspace.glob('[0-9a-f]' * 16)))
        self.assertTrue(tree.process.stdin.closed)
        self.assertTrue(tree.process.stdout.closed)
        self.assertTrue(tree.process.stderr.closed)

    def test_uno_log_start_failure_releases_owned_process_and_streams_immediately(self):
        session, tree = self.session(), self.tree()
        failure = OSError('Cannot start log reader')
        with patch('app_backend.converter_guard.uno_session.ProcessTree', return_value=tree), \
                patch('app_backend.converter_guard.uno_session.NativeLogs', side_effect=failure):
            with self.assertRaises(OSError) as result: session.ensure_started(self.cancel)
        self.assertIs(result.exception, failure)
        self.assert_released(session, tree)

    def test_uno_thread_start_failure_does_not_join_unstarted_thread(self):
        session, tree = self.session(), self.tree()
        failure = RuntimeError('Cannot start response thread')
        reader = SimpleNamespace(start=Mock(side_effect=failure), ident=None,
                                 join=Mock(side_effect=AssertionError('Unstarted thread joined')))
        logs = SimpleNamespace(finish=Mock())
        with patch('app_backend.converter_guard.uno_session.ProcessTree', return_value=tree), \
                patch('app_backend.converter_guard.uno_session.NativeLogs', return_value=logs), \
                patch('app_backend.converter_guard.uno_session.threading.Thread', return_value=reader):
            with self.assertRaises(RuntimeError) as result: session.ensure_started(self.cancel)
        self.assertIs(result.exception, failure)
        self.assert_released(session, tree)
        reader.join.assert_not_called(); logs.finish.assert_called_once()

    def test_uno_negative_ready_releases_process_without_outer_batch_exit(self):
        session, tree = self.session(), self.tree()
        reader = SimpleNamespace(start=Mock(), ident=123, join=Mock())
        logs = SimpleNamespace(finish=Mock())
        with patch('app_backend.converter_guard.uno_session.ProcessTree', return_value=tree), \
                patch('app_backend.converter_guard.uno_session.NativeLogs', return_value=logs), \
                patch('app_backend.converter_guard.uno_session.threading.Thread', return_value=reader), \
                patch.object(session, '_receive', return_value={'id': 'ready', 'ok': False}):
            with self.assertRaises(GuardError) as result: session.ensure_started(self.cancel)
        self.assertEqual(result.exception.code, 'UNO_SESSION_UNAVAILABLE')
        self.assert_released(session, tree)

    def test_fallback_office_log_failure_releases_process_and_can_start_again(self):
        session, tree = self.session(OfficeSession), self.tree()
        with patch('app_backend.converter_guard.office_session.ProcessTree', return_value=tree), \
                patch('app_backend.converter_guard.office_session.NativeLogs', side_effect=OSError('log reader failed')):
            with self.assertRaises(OSError): session.ensure_started()
        self.assert_released(session, tree)
        replacement = self.tree()
        logs = SimpleNamespace(finish=Mock())
        with patch('app_backend.converter_guard.office_session.ProcessTree', return_value=replacement), \
                patch('app_backend.converter_guard.office_session.NativeLogs', return_value=logs):
            session.ensure_started()
        self.assertIs(session.tree, replacement)
        session.recycle()
        self.assert_released(session, replacement)

    def test_real_child_is_reaped_after_uno_log_initialization_failure(self):
        # Exercise real OS process ownership; no LibreOffice fixture is used.
        from app_backend.native_process import ProcessTree
        session = self.session()
        owned = []
        def launch(_command, **kwargs):
            tree = ProcessTree([sys.executable, '-c', 'import time; time.sleep(30)'], **kwargs)
            owned.append(tree)
            return tree
        with patch('app_backend.converter_guard.uno_session.ProcessTree', side_effect=launch), \
                patch('app_backend.converter_guard.uno_session.NativeLogs', side_effect=OSError('Cannot start reader')):
            with self.assertRaises(OSError): session.ensure_started(self.cancel)
        self.assertEqual(len(owned), 1)
        self.assertIsNotNone(owned[0].process.poll())
        for name in ('stdin', 'stdout', 'stderr'):
            self.assertTrue(getattr(owned[0].process, name).closed)
        self.assertIsNone(session.tree)
        self.assertIsNone(session.profile)

    def test_nested_batch_does_not_inherit_or_leak_retry_recipe(self):
        binary = self.root / 'soffice'; binary.touch()
        adapter = LibreOfficeAdapter(binary)
        native = SimpleNamespace(valid=True)
        with patch('app_backend.converter_guard.adapter.registry', return_value=native):
            with adapter.batch(self.root / 'outer'):
                self.assertIsNone(getattr(adapter._local, 'retry_recipe', None))
                adapter.prepare_retry_recipe('pdf_preserve_images')
                with adapter.batch(self.root / 'inner'):
                    self.assertIsNone(getattr(adapter._local, 'retry_recipe', None))
                    adapter.prepare_retry_recipe('csv_preserve_values')
                self.assertEqual(adapter._local.retry_recipe, 'pdf_preserve_images')
            self.assertIsNone(adapter._local.retry_recipe)


if __name__ == '__main__': unittest.main()
