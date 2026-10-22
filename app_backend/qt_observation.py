"""Opt-in GUI liveness for BlackBox. No timer or thread when telemetry is off."""
import time
from PySide6.QtCore import QObject, QTimer, Slot


class GuiObservation(QObject):
    def __init__(self, parent, emitter):
        super().__init__(parent)
        self.emitter=emitter
        self.last=time.monotonic()
        self.timer=QTimer(self)
        self.timer.setInterval(5000)
        self.timer.timeout.connect(self.pulse)
        self.timer.start()
        self._emit('gui_heartbeat',0)

    def _emit(self,kind,delay):
        try:
            self.emitter.emit(kind,module_id='app',stage='idle',status='running',duration_ms=delay)
        except Exception:
            pass  # An observer cannot interrupt the GUI or conversion.

    @Slot()
    def pulse(self):
        now=time.monotonic()
        delay=max(0,round((now-self.last)*1000)-self.timer.interval())
        self.last=now
        self._emit('gui_heartbeat',delay)
        if delay>=1000:self._emit('gui_timer_delayed',delay)

    def stop(self):
        self.timer.stop()


def observe_gui(parent,emitter):
    return GuiObservation(parent,emitter) if emitter is not None else None
