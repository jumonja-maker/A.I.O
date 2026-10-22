"""Native timing truth and worker cleanup boundaries, without Windows claims."""
import io
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app_backend.converter_guard.uno_session import UnoSession
from app_backend.converter_guard.native_logs import NativeLogs
from app_backend.converter_guard.native_metrics import clean_timings
from app_backend.libreoffice_engine import convert_path
from app_backend.native_process import ProcessTree
import test_guard_uno_session as worker_contracts


class NativeMetricsQA(unittest.TestCase):
    def test_deep_type_detection_is_not_repeated_for_exports(self):
        calls = []
        def namespace(**kwargs):
            detect = kwargs.get('queryTypeByDescriptor')
            if detect:
                def measured(*args):
                    calls.append(args)
                    return detect(*args)
                kwargs['queryTypeByDescriptor'] = measured
            return SimpleNamespace(**kwargs)
        with patch.object(worker_contracts, 'SimpleNamespace', side_effect=namespace):
            models, _, exports, replies = worker_contracts.UnoWorkerContracts().execute([
                {'operation':'inspect'}, {}, {}])
        self.assertTrue(all(reply['ok'] for reply in replies))
        self.assertEqual(len(exports), 2)
        self.assertEqual(len(models), 3, 'Every export still opens a fresh native model')
        self.assertEqual(len(calls), 0, 'Inspection reads the already loaded native model metadata')

    def test_worker_reports_real_import_export_and_close_durations(self):
        _, _, _, replies = worker_contracts.UnoWorkerContracts().execute([{}, {}])
        exported = [reply for reply in replies if reply.get('id') != 'ready']
        self.assertEqual(len(exported), 2)
        for reply in exported:
            self.assertTrue(reply['ok'])
            timings = reply['timings']
            self.assertEqual(timings['source'], 'uno_document')
            for name in ('importMs', 'exportMs', 'closeMs', 'operationMs'):
                self.assertIsInstance(timings[name], int)
                self.assertGreaterEqual(timings[name], 0)

    def test_failed_import_does_not_invent_export_duration(self):
        _, _, _, replies = worker_contracts.UnoWorkerContracts().execute([{}], fail_first=True)
        result = next(reply for reply in replies if reply.get('id') == '0')
        self.assertFalse(result['ok'])
        self.assertIn('importMs', result['timings'])
        self.assertNotIn('exportMs', result['timings'])

    def test_broken_stdin_cannot_skip_terminating_owned_worker_and_profile_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / 'profile'
            profile.mkdir()
            (profile / 'owned-file').touch()
            session = UnoSession(root / 'soffice.exe', root, lambda path: None)
            process = SimpleNamespace(stdin=SimpleNamespace(close=Mock(side_effect=BrokenPipeError())),
                                      poll=lambda: 0)
            tree = SimpleNamespace(process=process, close=Mock())
            logs = SimpleNamespace(finish=Mock())
            reader = SimpleNamespace(join=Mock())
            session.tree, session.logs, session.reader, session.profile = tree, logs, reader, profile
            session.recycle()
            tree.close.assert_called_once()
            logs.finish.assert_called_once()
            reader.join.assert_called_once()
            self.assertFalse(profile.exists())
            self.assertIsNone(session.tree)

    def test_late_old_reader_cannot_write_into_a_new_session_queue(self):
        session = UnoSession('/unused/office', '/unused/work', lambda path: None)
        old, new = queue.Queue(maxsize=8), queue.Queue(maxsize=8)
        session.responses = new
        source = io.BytesIO(b'AIO_GUARD_RPC {"id":"old","ok":true}\n')
        session._read(source, old)
        self.assertEqual(old.get_nowait()['id'], 'old')
        self.assertTrue(new.empty())

    def test_diagnostic_timings_cannot_contain_paths_nan_or_unbounded_numbers(self):
        value = clean_timings({'source': 'cli_process', 'launchMs': True, 'processMs': float('nan'),
                              'adapterMs': 10 ** 400, 'private': '/private/document', 'exportMs': -1})
        self.assertEqual(value, {'source': 'cli_process'})
        self.assertIsNone(clean_timings({'source': []}))
        self.assertIsNone(clean_timings({'source': 'unknown'}))

    @unittest.skipUnless(os.name == 'posix', 'Executable generated fixture uses a POSIX shebang')
    def test_cli_measures_process_time_without_inventing_import_or_export_times(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            engine = root / 'engine'
            engine.write_text('#!' + sys.executable + '\n'
                              'import sys\nfrom pathlib import Path\n'
                              'out=Path(sys.argv[sys.argv.index("--outdir")+1])\n'
                              '(out/"result.pdf").write_bytes(b"fixture")\n')
            engine.chmod(0o700)
            source = root / 'input.docx'
            source.write_bytes(b'original')
            seen = []
            paths = convert_path(source, 'pdf', root / 'one', executable=engine, metrics_sink=seen.append)
            self.assertEqual(paths[0].read_bytes(), b'fixture')
            self.assertEqual(seen[0]['source'], 'cli_process')
            self.assertGreaterEqual(seen[0]['processMs'], seen[0]['launchMs'])
            self.assertNotIn('importMs', seen[0])
            self.assertNotIn('exportMs', seen[0])
            def unavailable(value):
                raise RuntimeError('Diagnostics unavailable')
            paths = convert_path(source, 'pdf', root / 'two', executable=engine, metrics_sink=unavailable)
            self.assertEqual(paths[0].read_bytes(), b'fixture')
            self.assertEqual(source.read_bytes(), b'original')

    @unittest.skipUnless(os.name == 'posix', 'Owned POSIX worker lifecycle; not Windows Job Object QA')
    def test_twenty_worker_recycles_leave_no_live_readers_profiles_or_pipe_descriptors(self):
        baseline_threads = {t.ident for t in threading.enumerate() if t.name.startswith('aio-')}
        descriptors = Path('/proc/self/fd')
        before = len(list(descriptors.iterdir())) if descriptors.is_dir() else None
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(20):
                session = UnoSession(root / 'soffice', root, lambda path: None)
                profile = root / str(index)
                profile.mkdir()
                tree = ProcessTree([sys.executable, '-c',
                    'import time;print(\'AIO_GUARD_RPC {"id":"ready","ok":true}\',flush=True);time.sleep(30)'],
                    cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                session.tree, session.profile = tree, profile
                session.logs = NativeLogs(tree.process, capture_stdout=False)
                reader = threading.Thread(target=session._read,
                    args=(tree.process.stdout, session.responses), name='aio-uno-response', daemon=True)
                session.reader = reader
                reader.start()
                try:
                    self.assertTrue(session.responses.get(timeout=2)['ok'])
                finally:
                    session.recycle()
                self.assertIsNotNone(tree.process.poll())
                self.assertFalse(reader.is_alive())
                self.assertFalse(profile.exists())
                self.assertTrue(tree.process.stdin.closed)
                self.assertTrue(tree.process.stdout.closed)
                self.assertTrue(tree.process.stderr.closed)
        remaining = {t.ident for t in threading.enumerate() if t.name.startswith('aio-')}
        self.assertTrue(remaining <= baseline_threads)
        if before is not None:
            self.assertLessEqual(len(list(descriptors.iterdir())), before)


if __name__ == '__main__':
    unittest.main()
