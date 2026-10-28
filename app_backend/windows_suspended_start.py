"""Documented Win32 startup barrier for a suspended Popen child.

CPython closes CreateProcess's initial thread handle. Recover that thread by
its exact owner PID while the process has never executed. Do not resume a
running process, another process's thread, or an ambiguous thread set. All
failures are fatal to startup; ProcessTree owns termination/handle cleanup.
"""
from __future__ import annotations


def resume_owned_primary(process, job):
    import ctypes
    from ctypes import wintypes as w

    class ThreadEntry(ctypes.Structure):
        _fields_ = [('dwSize', w.DWORD), ('cntUsage', w.DWORD),
                    ('th32ThreadID', w.DWORD), ('th32OwnerProcessID', w.DWORD),
                    ('tpBasePri', w.LONG), ('tpDeltaPri', w.LONG), ('dwFlags', w.DWORD)]

    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.IsProcessInJob.argtypes = [w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL)]
    k.IsProcessInJob.restype = w.BOOL
    k.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
    k.CreateToolhelp32Snapshot.restype = w.HANDLE
    k.Thread32First.argtypes = [w.HANDLE, ctypes.POINTER(ThreadEntry)]
    k.Thread32First.restype = w.BOOL
    k.Thread32Next.argtypes = [w.HANDLE, ctypes.POINTER(ThreadEntry)]
    k.Thread32Next.restype = w.BOOL
    k.OpenThread.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    k.OpenThread.restype = w.HANDLE
    k.GetProcessIdOfThread.argtypes = [w.HANDLE]
    k.GetProcessIdOfThread.restype = w.DWORD
    k.ResumeThread.argtypes = [w.HANDLE]
    k.ResumeThread.restype = w.DWORD
    k.CloseHandle.argtypes = [w.HANDLE]
    k.CloseHandle.restype = w.BOOL

    member = w.BOOL()
    if not k.IsProcessInJob(int(process._handle), job, ctypes.byref(member)):
        raise ctypes.WinError(ctypes.get_last_error())
    if not member.value or process.poll() is not None:
        raise OSError('Owned suspended process is not in its Job Object')
    snapshot = k.CreateToolhelp32Snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
    if snapshot == ctypes.c_void_p(-1).value or not snapshot:
        raise ctypes.WinError(ctypes.get_last_error())
    thread_ids = []
    try:
        entry = ThreadEntry()
        entry.dwSize = ctypes.sizeof(entry)
        more = k.Thread32First(snapshot, ctypes.byref(entry))
        if not more:
            raise ctypes.WinError(ctypes.get_last_error())
        while more:
            if entry.th32OwnerProcessID == process.pid:
                thread_ids.append(int(entry.th32ThreadID))
            entry.dwSize = ctypes.sizeof(entry)
            more = k.Thread32Next(snapshot, ctypes.byref(entry))
        error = ctypes.get_last_error()
        if error != 18:  # ERROR_NO_MORE_FILES, not a partial failed snapshot
            raise ctypes.WinError(error)
    finally:
        k.CloseHandle(snapshot)
    if len(thread_ids) != 1:
        raise OSError('Cannot identify the single suspended primary thread')
    thread = k.OpenThread(0x0002 | 0x0800, False, thread_ids[0])
    if not thread:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        # PID is pinned by Popen's live process handle, and the thread handle
        # prevents a reused thread id from being acted upon after this check.
        if k.GetProcessIdOfThread(thread) != process.pid:
            raise OSError('Startup thread owner changed')
        previous = k.ResumeThread(thread)
        if previous == 0xffffffff:
            raise ctypes.WinError(ctypes.get_last_error())
        if previous != 1:
            raise OSError('Unexpected primary thread suspend count')
    finally:
        k.CloseHandle(thread)
