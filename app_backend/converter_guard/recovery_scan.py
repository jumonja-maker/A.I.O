"""One lazy recovery scan, with explicit progress/error and no validation shortcut."""
from concurrent.futures import ThreadPoolExecutor
import threading


class RecoveryScan:
    """Own one scan worker. ``lock`` is the Guard state RLock, never an I/O lock.

    ``plan`` only inspects metadata and returns pending/complete/waiting_for_slot/
    closed. ``load`` performs the existing validated recovery outside that lock.
    A failed scan is retried only by an explicit user request.
    """
    def __init__(self, load, plan, lock):
        self._load, self._plan, self._lock = load, plan, lock
        self.cancel = threading.Event()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='aio-recovery')
        self.future = None
        self._active = False
        self._error = None
        self._attempts = 0

    def request(self, *, retry=False):
        with self._lock:
            if self.cancel.is_set() or self._active:
                return
            planned = self._plan()
            if planned != 'pending':
                if planned == 'complete':
                    self._error = None
                return
            if self._error and not retry:
                return
            self._active, self._error = True, None
            self._attempts += 1
            try:
                self.future = self._executor.submit(self._run)
            except RuntimeError:
                self._active = False
                if not self.cancel.is_set():
                    self._error = 'RECOVERY_SCAN_FAILED'

    def _run(self):
        try:
            if not self.cancel.is_set():
                self._load()
        except Exception:
            # Never make an unreadable disk look like an empty result list.
            # Do not leak filesystem paths or invent corruption diagnoses.
            with self._lock:
                if not self.cancel.is_set():
                    self._error = 'RECOVERY_SCAN_FAILED'
        finally:
            with self._lock:
                self._active = False

    def snapshot(self):
        with self._lock:
            state = ('closed' if self.cancel.is_set() else
                     'scanning' if self._active else
                     'error' if self._error else self._plan())
            return {'state': state, 'running': self._active,
                    'complete': state == 'complete', 'code': self._error,
                    'attempts': self._attempts}

    def close(self):
        self.cancel.set()
        self._executor.shutdown(wait=True)
