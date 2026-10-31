"""Small JSON writes with exclusive staging and a pinned destination directory.

Internal journals may replace their own names. External receipts must use
``overwrite=False``: existing files (including links) are never replaced.
"""
from contextlib import contextmanager, ExitStack
import json
import os
from pathlib import Path
import secrets
import stat


def _ordinary_directory(path):
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or
            getattr(info, 'st_file_attributes', 0) & 0x400):
        raise OSError('JSON destination is not an ordinary directory')
    return info


@contextmanager
def _pinned_parent(parent):
    """Use openat on POSIX; keep no-delete directory handles on Windows.

    Walk from the root without following a symlink/reparse point. A directory
    rename must not redirect a pending metadata write into a different tree.
    """
    parent = Path(os.path.abspath(parent))
    with ExitStack() as stack:
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                wintypes.DWORD, wintypes.HANDLE]
            kernel.CreateFileW.restype = wintypes.HANDLE
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle.restype = wintypes.BOOL
            for component in reversed((parent, *parent.parents)):
                handle = kernel.CreateFileW(str(component), 0x80, 0x3, None,
                                            3, 0x02000000 | 0x00200000, None)
                if handle == wintypes.HANDLE(-1).value:
                    raise ctypes.WinError(ctypes.get_last_error())
                stack.callback(kernel.CloseHandle, handle)
                _ordinary_directory(component)
            yield parent, None
        else:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0)
            descriptor = os.open(parent.anchor, flags)
            try:
                for component in parent.parts[1:]:
                    child = os.open(component, flags, dir_fd=descriptor)
                    os.close(descriptor)
                    descriptor = child
                identity = os.fstat(descriptor)
                yield parent, descriptor
                current = _ordinary_directory(parent)
                if (identity.st_dev, identity.st_ino) != (current.st_dev, current.st_ino):
                    raise OSError('JSON destination changed during publication')
            finally:
                os.close(descriptor)


def atomic_json(path, value, *, overwrite=True):
    """Fsync a unique O_EXCL staging file and publish it atomically.

    The no-replace POSIX operation deliberately fails on a filesystem without
    hard-link support rather than silently falling back to a clobbering rename.
    Windows rename supplies no-replace semantics, including FAT/exFAT volumes.
    Temporary files are removed after failures, including fsync failures.
    """
    path = Path(path)
    if type(overwrite) is not bool or path.name in ('', '.', '..'):
        raise ValueError('Invalid JSON publication request')
    payload = json.dumps(value, ensure_ascii=True, separators=(',', ':')).encode('utf-8')
    with _pinned_parent(path.parent) as (parent, directory):
        name = '.aio-json-' + secrets.token_hex(16) + '.new'
        temporary = parent / name
        descriptor = None
        created = False
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
            descriptor = (os.open(temporary, flags, 0o600) if directory is None else
                          os.open(name, flags, 0o600, dir_fd=directory))
            created = True
            stream = os.fdopen(descriptor, 'wb')
            descriptor = None
            with stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
                owned = os.fstat(stream.fileno())
            observed = (temporary.lstat() if directory is None else
                        os.stat(name, dir_fd=directory, follow_symlinks=False))
            if (not stat.S_ISREG(observed.st_mode) or
                    (owned.st_dev, owned.st_ino, owned.st_size) !=
                    (observed.st_dev, observed.st_ino, observed.st_size)):
                raise OSError('JSON staging file changed')
            if directory is None:
                if overwrite:
                    os.replace(temporary, parent / path.name)
                else:
                    os.rename(temporary, parent / path.name)
            elif overwrite:
                os.replace(name, path.name, src_dir_fd=directory, dst_dir_fd=directory)
            else:
                os.link(name, path.name, src_dir_fd=directory,
                        dst_dir_fd=directory, follow_symlinks=False)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if created:
                try:
                    if directory is None:
                        temporary.unlink()
                    else:
                        os.unlink(name, dir_fd=directory)
                except FileNotFoundError:
                    pass
    return path
