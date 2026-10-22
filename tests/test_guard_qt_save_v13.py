"""Actual Qt queued save slots, without requiring WebEngine system libraries.

The four production save methods and their signal wiring are compiled from
desktop.py. The surrounding WebEngine window is deliberately outside scope.
"""
import ast
import importlib.util
import os
import io
from pathlib import Path
import tempfile
import threading
import time
import unittest

from app_backend.converter_guard import ConverterGuard, GuardError
from test_converter_guard_v02 import FakeAdapter, wait_finished


@unittest.skipUnless(os.environ.get('AIO_QT_SAVE_QA')=='1' and importlib.util.find_spec('PySide6'),
                     'Opt in with AIO_QT_SAVE_QA=1 and existing Qt bindings')
class QtSaveBoundary(unittest.TestCase):
    def test_save_signals_are_queued_nonmodal_and_duplicate_picker_is_rejected(self):
        from PySide6.QtCore import Qt, QObject, Signal, Slot, QThread, QTimer
        from PySide6.QtWidgets import QApplication, QMainWindow, QProgressDialog
        app=QApplication.instance() or QApplication([])
        tree=ast.parse((Path(__file__).parents[1]/'app_backend/desktop.py').read_text())
        signals=next(node for node in tree.body if isinstance(node,ast.ClassDef) and node.name=='FolderSaveSignals')
        desktop=next(node for node in tree.body if isinstance(node,ast.ClassDef) and node.name=='MainWindow')
        chosen={'_save_folder','_folder_progress','_folder_finished'}
        methods=[node for node in desktop.body if isinstance(node,ast.FunctionDef) and node.name in chosen]
        initializer=next(node for node in desktop.body if isinstance(node,ast.FunctionDef) and node.name=='__init__')
        wiring=[node for node in initializer.body if '_folder_saves' in ast.unparse(node) or '_folder_signals' in ast.unparse(node)]
        init=ast.parse('def __init__(self):\n QMainWindow.__init__(self)\n').body[0]
        init.body.extend(wiring)
        harness=ast.ClassDef(name='SaveHarness',bases=[ast.Name(id='QMainWindow',ctx=ast.Load())],
                            keywords=[],body=[init,*methods],decorator_list=[])
        calls=[];entered=threading.Event();resume=threading.Event();ticks=[]
        class TrackedDialog(QProgressDialog):
            def setValue(self,value):
                self.assert_gui_thread()
                calls.append(('progress',value))
                super().setValue(value)
            def assert_gui_thread(self):
                if QThread.currentThread()!=app.thread():raise AssertionError('GUI touched by save worker')
        namespace={**globals(),**locals(),'QProgressDialog':TrackedDialog}
        module=ast.fix_missing_locations(ast.Module(body=[signals,harness],type_ignores=[]))
        exec(compile(module,'actual_desktop_save_methods','exec'),namespace)
        window=namespace['SaveHarness']();window.show()
        self.addCleanup(window.close)
        with tempfile.TemporaryDirectory() as tmp:
            guard=ConverterGuard(adapter=FakeAdapter(),recovery_parent=Path(tmp)/'recovery')
            self.addCleanup(guard.close)
            identity=guard.create([{'id':'one','name':'page.docx','size':1}],['html'])['jobId']
            guard.upload(identity,'one',io.BytesIO(b'x'),1);guard.start(identity,archive_mode='on_demand')
            state=wait_finished(guard,identity)
            original=guard.save_folder
            def save(*args,**kwargs):
                entered.set()
                if not resume.wait(3):raise AssertionError('QA barrier not released')
                return original(*args,**kwargs)
            from types import SimpleNamespace
            window._server=SimpleNamespace(guard=SimpleNamespace(save_folder=save))
            notices=[]
            def notify(*args,**kwargs):
                self.assertEqual(QThread.currentThread(),app.thread());notices.append((args,kwargs))
            window._notify_download=notify
            destination=Path(tmp)/'saved';destination.mkdir()
            url='http://127.0.0.1/api/converter-guard/result?ticket='+state['result']['ticket']+'&mode=folder'
            pickers=[]
            def picker(*_):
                pickers.append(True);window._save_folder(url,state['result']['ticket'])
                return str(destination)
            namespace['QFileDialog']=SimpleNamespace(getExistingDirectory=picker)
            timer=QTimer();timer.timeout.connect(lambda:ticks.append(time.monotonic()));timer.start(10)
            try:
                window._save_folder(url,state['result']['ticket'])
                self.assertTrue(entered.wait(1));self.assertEqual(len(pickers),1)
                dialog=window._folder_saves[url][1]
                self.assertEqual(dialog.windowModality(),Qt.WindowModality.NonModal)
                self.assertTrue(window.isEnabled())
                until=time.monotonic()+.2
                while time.monotonic()<until:app.processEvents();time.sleep(.005)
                self.assertGreater(len(ticks),8,'GUI event loop stopped during save')
                resume.set();until=time.monotonic()+3
                while window._folder_saves and time.monotonic()<until:app.processEvents();time.sleep(.005)
                self.assertFalse(window._folder_saves)
                self.assertTrue(notices[0][1]['savedAll'])
                self.assertEqual(notices[0][0][0],'completed')
                self.assertTrue(calls)
                self.assertEqual(guard.status(identity)['files'][0]['targets'][0]['state'],'saved')
                print({'scope':'real Qt Widgets save boundary; WebEngine/Windows NOT_RUN',
                       'status':'PASS','guiTimerTicks':len(ticks),'queuedProgress':len(calls),'duplicatePickerRejected':True})
            finally:resume.set();timer.stop();guard.close()
