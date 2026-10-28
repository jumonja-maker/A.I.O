"""A short, exclusively created native profile with verifiable ownership.

The native engine can add deeply nested registry/cache names. Keep its Windows
profile independent of a retained job's directory depth. No source/output is
moved here, and cleanup never accepts a caller-supplied directory to delete.
"""
import os
from pathlib import Path
import secrets
import shutil
import tempfile
import time

from .result_store import path_is_link


class PrivateNativeProfile:
    def __init__(self, *, parent=None):
        self.path=Path(tempfile.mkdtemp(prefix='aio-uno-',dir=parent))
        self.marker=self.path.with_name(self.path.name+'.owner')
        self.token=secrets.token_hex(32)
        self.identity=self._identity()
        try:
            with self.marker.open('x',encoding='ascii') as stream:stream.write(self.token)
        except BaseException:
            self.path.rmdir()
            raise

    def _identity(self):
        value=self.path.stat()
        return value.st_dev,value.st_ino

    def _owned(self):
        try:
            return (not path_is_link(self.path) and self._identity()==self.identity and
                    not path_is_link(self.marker) and self.marker.stat().st_size==len(self.token) and
                    self.marker.read_text(encoding='ascii')==self.token)
        except (OSError,UnicodeError):return False

    def cleanup(self):
        """Call only after the owned native process tree has stopped.

        Keep both directory and owner record if identity cannot be confirmed.
        A sibling record survives partial removal and bounded lock retries.
        """
        until=time.monotonic()+1
        while self._owned():
            try:
                shutil.rmtree(self.path)
                self.marker.unlink()
                return True
            except OSError:
                if not self.path.exists():
                    try:self.marker.unlink();return True
                    except OSError:return False
                if time.monotonic()>=until:return False
                time.sleep(.025)
        return False


def private_profile_required():
    return os.name=='nt'
