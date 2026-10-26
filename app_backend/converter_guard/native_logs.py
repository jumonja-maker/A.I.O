"""Drain native pipes continuously; retain a bounded diagnostic tail in RAM."""
from collections import deque
import threading


class NativeLogs:
    def __init__(self, process, limit=32768, *, capture_stdout=True):
        self._parts, self._size = deque(), 0
        self._lock = threading.Lock()
        self.limit = limit
        self._threads = []
        for pipe in ((getattr(process,'stdout',None) if capture_stdout else None), getattr(process,'stderr',None)):
            if pipe is not None:
                thread = threading.Thread(target=self._drain, args=(pipe,), daemon=True,
                                          name='aio-native-log')
                thread.start()
                self._threads.append(thread)

    def _drain(self, pipe):
        try:
            read = getattr(pipe, 'read1', pipe.read)
            while block := read(4096):
                with self._lock:
                    self._parts.append(block)
                    self._size += len(block)
                    while self._size > self.limit and self._parts:
                        excess = self._size - self.limit
                        first = self._parts.popleft()
                        if len(first) > excess:
                            self._parts.appendleft(first[excess:])
                            self._size -= excess
                        else:
                            self._size -= len(first)
        except (OSError, ValueError):
            pass
        finally:
            pipe.close()

    def text(self):
        with self._lock:
            return b''.join(self._parts).decode('utf-8', errors='replace')

    def clear(self):
        with self._lock:
            self._parts.clear()
            self._size = 0

    def finish(self):
        for thread in self._threads:
            thread.join(timeout=1)
        return self.text()


def close_native_pipes(process):
    """Close orphaned pipe ends only after their owned process tree stopped.

    Normally response/log drainers close their streams. Startup failures may
    occur before either reader starts, so no thread owns those handles yet.
    Never use this to interrupt a live reader or an unrelated office process.
    """
    for name in ('stdin', 'stdout', 'stderr'):
        stream = getattr(process, name, None)
        if stream is not None:
            try:
                stream.close()
            except (OSError, ValueError):
                pass
