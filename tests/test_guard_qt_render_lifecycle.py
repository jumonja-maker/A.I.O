"""The render QA process must leave its real Qt object graph destroyed."""
import importlib.util
import os
from pathlib import Path
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
try:
    from PySide6.QtCore import QObject, QThread
    from PySide6.QtWidgets import QApplication, QMainWindow, QWidget
except ImportError:
    QApplication = None

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('qt_render_qa',
    ROOT/'tools/qa/guard_v16_qt_render_acceptance.py')
qa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qa)


@unittest.skipIf(QApplication is None, 'Qt Widgets runtime unavailable')
class QtRenderLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(['qt-render-lifecycle-test'])
        cls.app.setQuitOnLastWindowClosed(False)

    def test_checks_run_in_application_event_loop_and_quit_normally(self):
        levels = []
        value = qa._run_in_event_loop(self.app,
            lambda: levels.append(QThread.currentThread().loopLevel()) or 'checked')
        self.assertEqual(value, 'checked')
        self.assertGreaterEqual(levels[0], 1)

    def test_callback_error_remains_a_failure_after_normal_event_loop_exit(self):
        def fail():
            raise qa.RenderFailure('real check failed')
        with self.assertRaisesRegex(qa.RenderFailure, 'real check failed'):
            qa._run_in_event_loop(self.app, fail)

    def test_deferred_window_and_page_destroy_before_retained_profile(self):
        destroyed = []

        class View(QWidget):
            def stop(self):
                destroyed.append('view-stopped')

        window = QMainWindow()
        window.view = View(window)
        window.setCentralWidget(window.view)
        window.page = QObject(window.view)
        window.profile = QObject(window.page)
        window.page.destroyed.connect(lambda: destroyed.append('page'))
        window.profile.destroyed.connect(lambda: destroyed.append('profile'))
        result = qa._run_in_event_loop(self.app,
            lambda: qa._dispose_window(self.app, window))
        self.assertEqual(result, {'windowDestroyed': True, 'pageDestroyed': True,
                                  'profileDestroyed': True})
        self.assertLess(destroyed.index('page'), destroyed.index('profile'))
        self.assertEqual(destroyed[0], 'view-stopped')


if __name__ == '__main__':
    unittest.main()
