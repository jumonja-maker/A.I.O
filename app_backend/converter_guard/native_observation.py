"""Drain only native stdout into a bounded prefix; never retain raw log paths."""
import os
import threading


class NativeObservation:
    LIMIT = 64 * 1024

    def __enter__(self):
        reader, self.writer = os.pipe()
        self.data = bytearray()
        self.truncated = False
        def drain():
            try:
                while block := os.read(reader, 4096):
                    remaining = self.LIMIT - len(self.data)
                    self.data.extend(block[:remaining])
                    self.truncated |= len(block) > remaining
            except OSError:
                pass
            finally:
                os.close(reader)
        self.thread = threading.Thread(target=drain, name='aio-native-observation', daemon=True)
        try:
            self.thread.start()
        except BaseException:
            os.close(reader); os.close(self.writer)
            raise
        return self

    def __exit__(self, *_):
        os.close(self.writer)
        self.thread.join(2)

    def selected_filter(self):
        if self.thread.is_alive() or self.truncated:
            return None
        names = []
        for line in self.data.decode('utf-8', errors='replace').splitlines():
            if line.startswith('convert ') and ' using filter : ' in line:
                names.append(line.rsplit(' using filter : ', 1)[1].strip())
        return names[0] if len(names) == 1 else None
