import ctypes
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import nullcontext
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from app_backend.converter_guard.native_profile import PrivateNativeProfile
from app_backend.converter_guard.uno_session import UnoSession
from app_backend.native_process import ProcessTree


class PrivateProfileContracts(unittest.TestCase):
    def test_private_profile_factory_receives_a_new_path_and_failure_reaps_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            work=Path(tmp)/'work';seen=[]
            def prepare(path):
                self.assertFalse(path.exists());seen.append(path);path.mkdir(parents=True)
            office=UnoSession('/unused/soffice.exe',work,prepare)
            with patch('app_backend.converter_guard.uno_session.private_profile_required',return_value=True), \
                    patch('app_backend.converter_guard.uno_session.ProcessTree',side_effect=RuntimeError('controlled startup stop')):
                with self.assertRaisesRegex(RuntimeError,'controlled startup stop'):office.ensure_started(threading.Event())
            self.assertEqual(len(seen),1);self.assertFalse(seen[0].is_relative_to(work))
            self.assertFalse(seen[0].parent.exists());self.assertFalse(seen[0].parent.with_name(seen[0].parent.name+'.owner').exists())

    def test_cleanup_removes_only_exclusively_created_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent=Path(tmp);source=parent/'original.docx';source.write_bytes(b'original 001')
            saved=parent/'saved.pdf';saved.write_bytes(b'already saved')
            before={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (source,saved)}
            owner=PrivateNativeProfile(parent=parent);(owner.path/'user').mkdir();(owner.path/'user/cache').write_bytes(b'cache')
            self.assertTrue(owner.cleanup());self.assertFalse(owner.path.exists());self.assertFalse(owner.marker.exists())
            self.assertEqual(before,{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (source,saved)})

    def test_replaced_marker_and_reparse_root_are_not_owned(self):
        with tempfile.TemporaryDirectory() as tmp:
            owner=PrivateNativeProfile(parent=tmp);sentinel=owner.path/'keep';sentinel.write_bytes(b'keep')
            owner.marker.write_text('foreign',encoding='ascii');self.assertFalse(owner.cleanup());self.assertEqual(sentinel.read_bytes(),b'keep')
            owner.marker.write_text(owner.token,encoding='ascii')
            with patch('app_backend.converter_guard.native_profile.path_is_link',return_value=True):
                self.assertFalse(owner.cleanup());self.assertEqual(sentinel.read_bytes(),b'keep')
            self.assertTrue(owner.cleanup())

    def test_directory_replacement_cannot_be_deleted_with_an_old_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            owner=PrivateNativeProfile(parent=tmp);original=owner.path.with_name(owner.path.name+'-retained')
            owner.path.rename(original);owner.path.mkdir();(owner.path/'foreign').write_bytes(b'never delete')
            self.assertFalse(owner.cleanup());self.assertEqual((owner.path/'foreign').read_bytes(),b'never delete')

    def test_unconfirmed_native_stop_keeps_the_private_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            owner=PrivateNativeProfile(parent=tmp);office=UnoSession('/unused/soffice.exe',Path(tmp)/'work',lambda _:None)
            office.profile=owner.path;office._profile_owner=owner;office.tree=SimpleNamespace(close=Mock(side_effect=OSError('stop failed')),process=SimpleNamespace(stdin=None))
            with self.assertRaisesRegex(OSError,'stop failed'):office.recycle()
            self.assertTrue(owner.path.is_dir());self.assertTrue(owner.marker.is_file());self.assertTrue(owner.cleanup())


class WindowsDrainContracts(unittest.TestCase):
    def tree(self,active):
        tree=ProcessTree.__new__(ProcessTree);tree._closed=False;tree._job=123;tree.process=SimpleNamespace(poll=lambda:0)
        tree.job_drain_verified=False;calls=[]
        tree._kernel=SimpleNamespace(TerminateJobObject=lambda *_:calls.append('terminate') or 1,
                                     CloseHandle=lambda *_:calls.append('close') or 1)
        tree._owned_process_fence=lambda:nullcontext(lambda _:None)
        def count():
            result=next(active);calls.append(result)
            if isinstance(result,Exception):raise result
            return result
        tree._windows_job_active=count
        return tree,calls

    def test_parent_exit_is_insufficient_until_every_owned_process_exits(self):
        tree,calls=self.tree(iter([2,1,0]))
        with patch('app_backend.native_process.time.sleep'):tree.close()
        self.assertEqual(calls,['terminate',2,1,0,'close']);self.assertTrue(tree.job_drain_verified)
        tree.close();self.assertEqual(calls,['terminate',2,1,0,'close'])

    def test_job_handle_closes_but_failed_drain_is_not_claimed_verified(self):
        tree,calls=self.tree(iter([OSError('query unavailable')]))
        with self.assertRaisesRegex(OSError,'query unavailable'):tree.close()
        self.assertEqual(calls[-1],'close');self.assertFalse(tree.job_drain_verified);self.assertIsNone(tree._job)

    def test_zero_accounting_does_not_bypass_unsignaled_process_handle(self):
        tree,calls=self.tree(iter([0]))
        wait=Mock(side_effect=TimeoutError('owned handle still alive'))
        tree._owned_process_fence=lambda:nullcontext(wait)
        with self.assertRaisesRegex(TimeoutError,'owned handle still alive'):tree.close()
        wait.assert_called_once();self.assertFalse(tree.job_drain_verified)
        self.assertEqual(calls,['terminate',0,'close'])

    def test_process_handles_are_waited_after_accounting_reaches_zero(self):
        tree,calls=self.tree(iter([2,0]))
        tree._owned_process_fence=lambda:nullcontext(lambda _:calls.append('handles-signaled'))
        with patch('app_backend.native_process.time.sleep'):tree.close()
        self.assertEqual(calls,['terminate',2,0,'handles-signaled','close'])
        self.assertTrue(tree.job_drain_verified)


@unittest.skipUnless(sys.platform=='win32','Real Windows Job Object control')
class RealWindowsOwnedTree(unittest.TestCase):
    def test_descendant_exit_and_unrelated_owned_process_survival(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);start=root/'start';pidfile=root/'child.pid';script=root/'spawn.py'
            script.write_text("import pathlib,subprocess,sys,time\nstart=pathlib.Path(sys.argv[1])\nwhile not start.exists():time.sleep(.01)\np=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\npathlib.Path(sys.argv[2]).write_text(str(p.pid))\ntime.sleep(60)\n")
            kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            from ctypes import wintypes as w
            kernel.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];kernel.OpenProcess.restype=w.HANDLE
            kernel.WaitForSingleObject.argtypes=[w.HANDLE,w.DWORD];kernel.WaitForSingleObject.restype=w.DWORD
            kernel.CloseHandle.argtypes=[w.HANDLE];kernel.CloseHandle.restype=w.BOOL
            with ProcessTree([sys.executable,'-c','import time;time.sleep(60)'],cwd=root) as sentinel:
                tree=ProcessTree([sys.executable,str(script),str(start),str(pidfile)],cwd=root,on_started=start.touch)
                child=None
                try:
                    until=time.monotonic()+10
                    while not pidfile.exists() and time.monotonic()<until:time.sleep(.01)
                    self.assertTrue(pidfile.is_file());pid=int(pidfile.read_text())
                    child=kernel.OpenProcess(0x100000|0x1000,False,pid);self.assertTrue(child)
                    self.assertEqual(kernel.WaitForSingleObject(child,0),258)
                    tree.close();self.assertTrue(tree.job_drain_verified)
                    self.assertEqual(kernel.WaitForSingleObject(child,0),0)
                    self.assertIsNotNone(tree.process.poll());self.assertIsNone(sentinel.process.poll())
                finally:
                    tree.close()
                    if child:kernel.CloseHandle(child)
