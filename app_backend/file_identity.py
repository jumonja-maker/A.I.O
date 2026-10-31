"""Cheap retained-file identity, without following links or reading payloads."""
from pathlib import Path
import stat


def file_identity(path):
    try:
        info = Path(path).lstat()
        if (not stat.S_ISREG(info.st_mode) or
                getattr(info, 'st_file_attributes', 0) & 0x400):
            return None
        return (info.st_dev, info.st_ino, info.st_size,
                info.st_mtime_ns, info.st_ctime_ns)
    except OSError:
        return None


def owned_file_identity(path, root):
    """Accept only a regular file in the lexical, non-linked owned tree."""
    path, root = Path(path).absolute(), Path(root).absolute()
    try:
        relative = path.relative_to(root)
        if not relative.parts or any(part in ('', '.', '..') for part in relative.parts):
            return None
        for part in (root, *[root.joinpath(*relative.parts[:i]) for i in range(1, len(relative.parts))]):
            info = part.lstat()
            if not stat.S_ISDIR(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                return None
        return file_identity(path)
    except (OSError, ValueError):
        return None
