"""Controlled CLI processes, not a real LibreOffice or Windows qualification."""
import io
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest

from app_backend.converter_guard import ConverterGuard, GuardLimits, LibreOfficeAdapter
from test_converter_guard_v02 import wait_finished


CLI = """#!/usr/bin/env python3
from pathlib import Path
import subprocess
import sys
import time
out = Path(sys.argv[sys.argv.index('--outdir') + 1])
source = Path(sys.argv[-1])
target = sys.argv[sys.argv.index('--convert-to') + 1]
if source.stem == 'hold':
    heartbeat = str(Path(__file__).parent / 'descendant-heartbeat')
    child_code = ('from pathlib import Path; import time; p=Path(' + repr(heartbeat) + ');'
                  '[(p.write_text(str(time.monotonic())),time.sleep(.02)) for _ in range(150)]')
    subprocess.Popen([sys.executable, '-c', child_code])
    time.sleep(60)
if source.stem == 'grow':
    (out / ('grow.' + target)).write_bytes(b'x' * (2 * 1024 * 1024))
    time.sleep(60)
(out / (source.stem + '.' + target)).write_bytes(b'native-fixture')
"""


@unittest.skipIf(os.name == 'nt', 'POSIX test fixture; real Windows Job Object needs Windows acceptance')
class ControlledProcessTests(unittest.TestCase):
    def test_deadline_kills_own_process_tree_and_next_file_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            executable=root/'fake-soffice'
            executable.write_text(CLI)
            executable.chmod(0o700)
            guard=ConverterGuard(adapter=LibreOfficeAdapter(executable),
                limits=GuardLimits(file_timeout=.4),spool_parent=directory)
            try:
                identity=guard.create([{'id':'a','name':'hold.bmp','size':1},
                                       {'id':'b','name':'good.bmp','size':1}])['jobId']
                for key in ('a','b'): guard.upload(identity,key,io.BytesIO(b'x'),1)
                guard.start(identity)
                heartbeat=root/'descendant-heartbeat'
                deadline=time.monotonic()+3
                while not heartbeat.exists() and time.monotonic()<deadline: time.sleep(.005)
                self.assertTrue(heartbeat.exists())
                state=wait_finished(guard,identity)
                self.assertEqual(state['files'][0]['targets'][0]['code'],'ENGINE_TIMEOUT')
                self.assertEqual(state['files'][1]['state'],'ready')
                self.assertFalse(guard._jobs[identity].cancel.is_set())
                # Heartbeat works with nested PID namespaces too; /proc/PID
                # can refer to another namespace in a container QA runtime.
                before=heartbeat.read_text()
                time.sleep(.15)
                self.assertEqual(heartbeat.read_text(),before)
            finally:
                guard.close()
            self.assertEqual(guard.storage_stats()['reservedBytes'],0)

    def test_live_output_quota_stops_only_the_growing_process(self):
        with tempfile.TemporaryDirectory() as directory:
            executable=Path(directory)/'fake-soffice'
            executable.write_text(CLI)
            executable.chmod(0o700)
            guard=ConverterGuard(adapter=LibreOfficeAdapter(executable),
                limits=GuardLimits(max_spool_bytes=128*1024),spool_parent=directory)
            try:
                identity=guard.create([{'id':'a','name':'grow.bmp','size':1},
                                       {'id':'b','name':'good.bmp','size':1}])['jobId']
                for key in ('a','b'): guard.upload(identity,key,io.BytesIO(b'x'),1)
                guard.start(identity)
                state=wait_finished(guard,identity)
                self.assertEqual(state['files'][0]['targets'][0]['code'],'SPOOL_QUOTA')
                self.assertEqual(state['files'][1]['state'],'ready')
                self.assertFalse(guard._jobs[identity].cancel.is_set())
            finally:
                guard.close()
            self.assertEqual(guard.storage_stats()['reservedBytes'],0)

    def test_slow_cleanup_does_not_hold_the_global_metadata_lock(self):
        from unittest.mock import patch
        import shutil
        from test_converter_guard_v02 import FakeAdapter
        with tempfile.TemporaryDirectory() as directory:
            guard=ConverterGuard(adapter=FakeAdapter(),spool_parent=directory)
            identity=guard.create([{'id':'a','name':'good.bmp','size':1}])['jobId']
            root=guard._jobs[identity].root
            entered,unblock=threading.Event(),threading.Event()
            original=shutil.rmtree
            def slow(path,*args,**kwargs):
                if Path(path) == root:
                    entered.set()
                    unblock.wait(2)
                return original(path,*args,**kwargs)
            try:
                with patch('app_backend.converter_guard.core.shutil.rmtree',slow):
                    worker=threading.Thread(target=guard.release,args=(identity,))
                    worker.start()
                    self.assertTrue(entered.wait(1))
                    started=time.monotonic()
                    guard.storage_stats()
                    self.assertLess(time.monotonic()-started,.5)
                    unblock.set()
                    worker.join(2)
                    self.assertFalse(worker.is_alive())
            finally:
                unblock.set()
                guard.close()


if __name__=='__main__':
    unittest.main()
