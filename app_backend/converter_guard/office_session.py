"""Private recyclable office for the one-shot Windows preflight fallback.

The fast Windows path uses UnoSession. Linux keeps its existing LOK preflight
and CLI exports; the measured warm-CLI experiment is not enabled or shipped.
"""
from pathlib import Path
import secrets
import shutil
import subprocess

from ..native_process import ProcessTree
from .native_logs import NativeLogs


class OfficeSession:
    def __init__(self, executable, workspace, prepare_profile, max_tasks=128):
        self.executable, self.workspace = Path(executable), Path(workspace)
        self.prepare_profile = prepare_profile
        self.max_tasks = max_tasks
        self.tree = self.logs = None
        self.completed, self.starts = 0, 0
        self.pipe = None
        self.profile = None

    def ensure_started(self):
        if self.tree and (self.tree.process.poll() is not None or self.completed >= self.max_tasks):
            self.recycle()
        if self.tree:
            return
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.profile = self.workspace / secrets.token_hex(8)
        self.prepare_profile(self.profile)
        self.pipe = 'aio_guard_' + secrets.token_hex(16)
        command = [str(self.executable), '-env:UserInstallation=' + self.profile.resolve().as_uri(),
                   '--headless', '--nologo', '--nodefault', '--norestore',
                   '--accept=pipe,name=' + self.pipe + ';urp;StarOffice.ServiceManager']
        self.tree = ProcessTree(command, cwd=self.workspace,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.logs = NativeLogs(self.tree.process)
        self.completed = 0
        self.starts += 1

    def recycle(self):
        tree, logs, profile = self.tree, self.logs, self.profile
        self.tree = self.logs = self.profile = self.pipe = None
        try:
            if tree:
                tree.close()
        finally:
            try:
                if logs:
                    logs.finish()
            finally:
                if profile:
                    shutil.rmtree(profile, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        try:
            self.recycle()
        finally:
            shutil.rmtree(self.workspace, ignore_errors=True)
