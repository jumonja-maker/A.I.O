import importlib.util
import os
import unittest
from unittest.mock import patch


@unittest.skipUnless(os.environ.get('AIO_QT_SAVE_QA')=='1' and importlib.util.find_spec('PySide6'),
                     'Opt in with AIO_QT_SAVE_QA=1 and existing Qt bindings')
class QtObservation(unittest.TestCase):
    def test_no_timer_without_opt_in_and_delay_measurement_does_not_throw(self):
        from PySide6.QtCore import QObject,QTimer
        from PySide6.QtWidgets import QApplication
        from app_backend.qt_observation import observe_gui
        app=QApplication.instance() or QApplication([])
        parent=QObject()
        self.assertIsNone(observe_gui(parent,None))
        self.assertFalse(parent.findChildren(QTimer))
        events=[]
        class Emitter:
            def emit(self,kind,**fields):events.append((kind,fields))
        with patch('app_backend.qt_observation.time.monotonic',side_effect=[1,8.2,13.2]):
            observer=observe_gui(parent,Emitter())
            observer.pulse();observer.pulse()
        self.assertEqual([kind for kind,_ in events],['gui_heartbeat','gui_heartbeat','gui_timer_delayed','gui_heartbeat'])
        self.assertEqual(events[2][1]['duration_ms'],2200)
        self.assertEqual(events[-1][1]['duration_ms'],0)
        observer.emitter=type('Broken',(),{'emit':lambda *a,**k:(_ for _ in ()).throw(OSError())})()
        observer.pulse();observer.stop()
        self.assertFalse(observer.timer.isActive())
