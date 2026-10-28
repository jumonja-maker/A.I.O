"""Real Windows startup containment, without replacing original descendant tests."""
import ctypes
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from app_backend.native_process import ProcessTree


@unittest.skipUnless(sys.platform == 'win32', 'Real Windows suspended startup barrier')
class SuspendedStartup(unittest.TestCase):
    def test_venv_launcher_is_suspended_before_assignment_and_child_is_in_owned_job(self):
        from ctypes import wintypes as w
        k = ctypes.WinDLL('kernel32', use_last_error=True)
        k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        k.OpenProcess.restype = w.HANDLE
        k.IsProcessInJob.argtypes = [w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL)]
        k.IsProcessInJob.restype = w.BOOL
        k.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        k.WaitForSingleObject.restype = w.DWORD
        k.CloseHandle.argtypes = [w.HANDLE]
        k.CloseHandle.restype = w.BOOL
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            marker = root / 'executed'
            child_pid = root / 'child.pid'
            script = root / 'immediate.py'
            script.write_text('import pathlib,subprocess,sys,time\n'
                'pathlib.Path(sys.argv[1]).write_text("ran")\n'
                'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"])\n'
                'pathlib.Path(sys.argv[2]).write_text(str(p.pid))\n'
                'time.sleep(60)\n', encoding='utf-8')
            original = ProcessTree._make_windows_job
            assertions = self
            calls = []
            def install(tree):
                original(tree)
                real = tree._kernel
                class SlowAssignment:
                    def __getattr__(self, name):
                        return getattr(real, name)
                    def AssignProcessToJobObject(self, job, process):
                        time.sleep(.25)  # Enlarge the former launch/assign race.
                        assertions.assertFalse(marker.exists(), 'Child executed before containment')
                        calls.append('suspended')
                        return real.AssignProcessToJobObject(job, process)
                tree._kernel = SlowAssignment()
            with patch.object(ProcessTree, '_make_windows_job', install):
                tree = ProcessTree([sys.executable, str(script), str(marker), str(child_pid)], cwd=root)
            child = None
            try:
                deadline = time.monotonic() + 10
                while not child_pid.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(child_pid.exists())
                child = k.OpenProcess(0x100000 | 0x1000, False, int(child_pid.read_text()))
                self.assertTrue(child)
                member = w.BOOL()
                self.assertTrue(k.IsProcessInJob(child, tree._job, ctypes.byref(member)))
                self.assertTrue(member.value, 'Descendant escaped the owned Job Object')
                self.assertEqual(calls, ['suspended'])
                tree.close()
                self.assertTrue(tree.job_drain_verified)
                self.assertEqual(k.WaitForSingleObject(child, 0), 0)
            finally:
                tree.close()
                if child:
                    k.CloseHandle(child)

    def test_failed_resume_reaps_launcher_without_running_its_code(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            marker = root / 'must-not-exist'
            actual_popen = subprocess.Popen
            spawned = []
            def remember(*args, **kwargs):
                p = actual_popen(*args, **kwargs)
                spawned.append(p)
                self.assertTrue(kwargs['creationflags'] & 4)
                return p
            with patch('app_backend.native_process.subprocess.Popen', side_effect=remember), \
                 patch('app_backend.windows_suspended_start.resume_owned_primary', side_effect=OSError('controlled resume failure')):
                with self.assertRaisesRegex(OSError, 'controlled resume failure'):
                    ProcessTree([sys.executable, '-c', 'import pathlib;pathlib.Path(' + repr(str(marker)) + ').touch()'], cwd=root)
            self.assertEqual(len(spawned), 1)
            self.assertIsNotNone(spawned[0].poll())
            self.assertFalse(marker.exists())

    def test_failed_assignment_never_resumes_uncontained_launcher(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            marker = root / 'must-not-exist'
            original = ProcessTree._make_windows_job
            def install(tree):
                original(tree)
                real = tree._kernel
                class FailedAssignment:
                    def __getattr__(self, name):
                        return getattr(real, name)
                    def AssignProcessToJobObject(self, *_):
                        return False
                tree._kernel = FailedAssignment()
            with patch.object(ProcessTree, '_make_windows_job', install), \
                 patch('app_backend.windows_suspended_start.resume_owned_primary') as resume:
                with self.assertRaisesRegex(OSError, 'Cannot contain'):
                    ProcessTree([sys.executable, '-c', 'import pathlib;pathlib.Path(' + repr(str(marker)) + ').touch()'], cwd=root)
                resume.assert_not_called()
            self.assertFalse(marker.exists())
