"""Own only the process tree started by this adapter, never a user's open office.

POSIX uses a new session. Windows uses a kill-on-close Job Object; if attaching
the job fails, conversion fails explicitly instead of using taskkill /IM.
"""
from __future__ import annotations

import os
import signal
import subprocess


class ProcessTree:
    def __init__(self, command: list[str], *, cwd, on_started=lambda: None, stdout=None,
                 stderr=None, env=None, stdin=None):
        self.process = None
        self._job = None
        self._kernel = None
        self._closed = False
        if os.name == 'nt':
            self._make_windows_job()
        try:
            self.process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.DEVNULL if stdin is None else stdin,
                stdout=subprocess.DEVNULL if stdout is None else stdout,
                stderr=subprocess.DEVNULL if stderr is None else stderr, shell=False, env=env,
                start_new_session=os.name != 'nt',
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0))
            if self._job and not self._kernel.AssignProcessToJobObject(
                    self._job, int(self.process._handle)):
                raise OSError('Cannot contain LibreOffice process')
            on_started()
        except BaseException:
            if self.process is not None:
                self.process.kill()
                self.process.wait()
            self.close()
            raise

    def _make_windows_job(self):
        import ctypes
        from ctypes import wintypes as w

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                'ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
                'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

        class BASIC(ctypes.Structure):
            _fields_ = [('PerProcessUserTimeLimit', ctypes.c_longlong),
                ('PerJobUserTimeLimit', ctypes.c_longlong), ('LimitFlags', w.DWORD),
                ('MinimumWorkingSetSize', ctypes.c_size_t),
                ('MaximumWorkingSetSize', ctypes.c_size_t),
                ('ActiveProcessLimit', w.DWORD), ('Affinity', ctypes.c_size_t),
                ('PriorityClass', w.DWORD), ('SchedulingClass', w.DWORD)]

        class EXTENDED(ctypes.Structure):
            _fields_ = [('BasicLimitInformation', BASIC), ('IoInfo', IO_COUNTERS),
                ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                ('PeakProcessMemoryUsed', ctypes.c_size_t),
                ('PeakJobMemoryUsed', ctypes.c_size_t)]

        k = ctypes.WinDLL('kernel32', use_last_error=True)
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        k.CreateJobObjectW.restype = w.HANDLE
        k.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        k.SetInformationJobObject.restype = w.BOOL
        k.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        k.AssignProcessToJobObject.restype = w.BOOL
        k.CloseHandle.argtypes = [w.HANDLE]
        k.CloseHandle.restype = w.BOOL
        handle = k.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            error = ctypes.get_last_error()
            k.CloseHandle(handle)
            raise ctypes.WinError(error)
        self._job, self._kernel = handle, k

    def close(self):
        if self._closed:
            return
        self._closed = True
        p = self.process
        if self._job:
            self._kernel.CloseHandle(self._job)
            self._job = None
        elif p is not None:
            # Also reap surviving children when a launcher exited before them.
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            if p.poll() is None:
                try:
                    p.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if p is not None and p.poll() is None:
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=3)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
