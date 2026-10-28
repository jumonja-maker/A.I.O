"""Wait for actual owned process termination, not only Job accounting updates.

Job ActiveProcesses may reach zero before a held process handle is signaled.
Capture current owned members before TerminateJobObject, then use the retained
handles as a lifetime fence. No process-name search or foreign termination.
"""
from contextlib import contextmanager
import time


@contextmanager
def owned_process_fence(job):
    import ctypes
    from ctypes import wintypes as w
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.QueryInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p]
    k.QueryInformationJobObject.restype = w.BOOL
    k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    k.OpenProcess.restype = w.HANDLE
    k.IsProcessInJob.argtypes = [w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL)]
    k.IsProcessInJob.restype = w.BOOL
    k.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    k.WaitForSingleObject.restype = w.DWORD
    k.CloseHandle.argtypes = [w.HANDLE]
    k.CloseHandle.restype = w.BOOL

    capacity = 32
    while True:
        class ProcessIds(ctypes.Structure):
            _fields_ = [('assigned', w.DWORD), ('listed', w.DWORD),
                        ('ids', ctypes.c_size_t * capacity)]
        values = ProcessIds()
        ok = k.QueryInformationJobObject(job, 3, ctypes.byref(values), ctypes.sizeof(values), None)
        if ok and values.listed <= capacity:
            pids = [int(pid) for pid in values.ids[:values.listed]]
            break
        error = ctypes.get_last_error()
        if error != 234 or capacity >= 65536:  # ERROR_MORE_DATA
            raise OSError(error, 'Cannot enumerate owned Job Object members')
        capacity *= 2
    handles = []
    try:
        for pid in pids:
            if not pid:
                raise OSError('Invalid owned process identity')
            handle = k.OpenProcess(0x100000 | 0x1000, False, pid)
            if not handle:
                error = ctypes.get_last_error()
                if error == 87:  # Already exited; no process has this PID.
                    continue
                raise ctypes.WinError(error)
            member = w.BOOL()
            if not k.IsProcessInJob(handle, job, ctypes.byref(member)):
                error = ctypes.get_last_error()
                k.CloseHandle(handle)
                raise ctypes.WinError(error)
            if not member.value:
                # PID reuse or membership uncertainty: never wait on or act
                # upon a foreign process, and do not claim verified drainage.
                k.CloseHandle(handle)
                raise OSError('Owned process membership changed')
            handles.append(handle)
        def wait(deadline):
            for handle in handles:
                milliseconds = max(0, min(3000, int((deadline - time.monotonic()) * 1000)))
                result = k.WaitForSingleObject(handle, milliseconds)
                if result == 258:
                    raise TimeoutError('Owned process handle has not signaled termination')
                if result != 0:
                    raise ctypes.WinError(ctypes.get_last_error())
        yield wait
    finally:
        for handle in handles:
            k.CloseHandle(handle)
