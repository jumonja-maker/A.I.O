"""Own only the process tree started by this adapter, never a user's open office.

POSIX uses a new session. Windows creates the launcher suspended, assigns it to
an owned kill-on-close Job Object, and only then resumes its primary thread.
A Python venv launcher or frozen bootstrap cannot spawn an uncontained child
before assignment. Failure is explicit, never a global taskkill /IM fallback.
The optional process-memory limit applies only to disposable analysis/viewer
workers; ordinary native conversion retains its unrestricted default.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time


class ProcessTree:
    def __init__(self, command: list[str], *, cwd, on_started=lambda: None, stdout=None,
                 stderr=None, env=None, stdin=None, memory_limit_bytes=None):
        if memory_limit_bytes is not None and (
                type(memory_limit_bytes) is not int or
                not 16 * 1024 * 1024 <= memory_limit_bytes <= sys.maxsize):
            raise ValueError('Process memory limit must be an integer byte count of at least 16 MiB')
        self.memory_limit_bytes = memory_limit_bytes
        self.process = None
        self._job = None
        self._kernel = None
        self._closed = False
        self.job_drain_verified = False
        if os.name == 'nt':
            self._make_windows_job()
        try:
            self.process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.DEVNULL if stdin is None else stdin,
                stdout=subprocess.DEVNULL if stdout is None else stdout,
                stderr=subprocess.DEVNULL if stderr is None else stderr, shell=False, env=env,
                start_new_session=os.name != 'nt',
                creationflags=((subprocess.CREATE_NO_WINDOW | 0x00000004) if os.name == 'nt' else 0))
            if self._job:
                if not self._kernel.AssignProcessToJobObject(self._job, int(self.process._handle)):
                    raise OSError('Cannot contain LibreOffice process')
                from .windows_suspended_start import resume_owned_primary
                resume_owned_primary(self.process, self._job)
            on_started()
        except BaseException:
            try:
                if self.process is not None:
                    try:
                        self.process.kill()
                    except ProcessLookupError:
                        pass
                    try:
                        self.process.wait(timeout=3)
                    except (ProcessLookupError, subprocess.TimeoutExpired):
                        pass
            finally:
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
                ('MinimumWorkingSetSize', ctypes.c_size_t), ('MaximumWorkingSetSize', ctypes.c_size_t),
                ('ActiveProcessLimit', w.DWORD), ('Affinity', ctypes.c_size_t),
                ('PriorityClass', w.DWORD), ('SchedulingClass', w.DWORD)]
        class EXTENDED(ctypes.Structure):
            _fields_ = [('BasicLimitInformation', BASIC), ('IoInfo', IO_COUNTERS),
                ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]
        k = ctypes.WinDLL('kernel32', use_last_error=True)
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        k.CreateJobObjectW.restype = w.HANDLE
        k.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        k.SetInformationJobObject.restype = w.BOOL
        k.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        k.AssignProcessToJobObject.restype = w.BOOL
        k.CloseHandle.argtypes = [w.HANDLE]
        k.CloseHandle.restype = w.BOOL
        k.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
        k.TerminateJobObject.restype = w.BOOL
        k.QueryInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p]
        k.QueryInformationJobObject.restype = w.BOOL
        handle = k.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000
        if self.memory_limit_bytes is not None:
            info.BasicLimitInformation.LimitFlags |= 0x100
            info.ProcessMemoryLimit = self.memory_limit_bytes
        if not k.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            error = ctypes.get_last_error()
            k.CloseHandle(handle)
            raise ctypes.WinError(error)
        self._job, self._kernel = handle, k

    def _windows_job_active(self):
        import ctypes
        class Accounting(ctypes.Structure):
            _fields_ = [(name, ctypes.c_longlong) for name in (
                'TotalUserTime', 'TotalKernelTime', 'ThisPeriodTotalUserTime', 'ThisPeriodTotalKernelTime')] + [
                (name, ctypes.c_uint32) for name in ('TotalPageFaultCount', 'TotalProcesses', 'ActiveProcesses', 'TotalTerminatedProcesses')]
        info = Accounting()
        if not self._kernel.QueryInformationJobObject(self._job, 1, ctypes.byref(info), ctypes.sizeof(info), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(info.ActiveProcesses)

    def _owned_process_fence(self):
        from .windows_job_drain import owned_process_fence
        return owned_process_fence(self._job)

    def _drain_windows_job(self):
        import ctypes
        # ActiveProcesses can reach zero before process handles signal. Keep
        # exact owned members alive as handle identities across termination.
        with self._owned_process_fence() as wait_for_exit:
            if not self._kernel.TerminateJobObject(self._job, 1):
                raise ctypes.WinError(ctypes.get_last_error())
            until = time.monotonic() + 3
            while self._windows_job_active():
                if time.monotonic() >= until:
                    raise TimeoutError('Owned native job did not drain')
                time.sleep(.01)
            wait_for_exit(until)
            self.job_drain_verified = True

    def close(self):
        if self._closed:
            return
        self._closed = True
        p = self.process
        if self._job:
            handle = self._job
            try:
                try:
                    self._drain_windows_job()
                finally:
                    self._kernel.CloseHandle(handle)
                    self._job = None
            finally:
                self._reap_parent(p)
            return
        elif p is not None and os.name != 'nt':
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
        self._reap_parent(p)

    @staticmethod
    def _reap_parent(p):
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
