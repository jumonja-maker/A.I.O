"""UI status-delta and native download lifetime regression boundaries."""
import ast
from enum import Enum
import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).parents[1]


@unittest.skipUnless(shutil.which('node'), 'Node is required to execute the production JS bridge')
class GuardUIAuditTests(unittest.TestCase):
    def test_outputs_delta_and_queue_view(self):
        run = subprocess.run(['node', str(Path(__file__).with_name('guard_audit_v18.mjs'))],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stdout+'\n'+run.stderr)
        print(run.stdout.strip())


@unittest.skipUnless(importlib.util.find_spec('PySide6'), 'Qt bindings required for QObject lifetime proof')
class NativeDownloadLifetime(unittest.TestCase):
    def test_rejected_requests_and_folder_redirects_leave_no_profile_children(self):
        from PySide6.QtCore import QObject, Signal, QCoreApplication, QEvent
        app = QCoreApplication.instance() or QCoreApplication([])
        source = ast.parse((ROOT/'app_backend/desktop.py').read_text())
        desktop = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == 'MainWindow')
        method = next(node for node in desktop.body if isinstance(node, ast.FunctionDef) and node.name == 'on_download')
        profile = QObject()
        mode = [0]
        notices, folders = [], []
        class Download(QObject):
            stateChanged = Signal(object)
            def url(self):
                path = 'http://127.0.0.1/api/converter-guard/result?ticket='+'b'*43
                if mode[0] == 1:
                    path += '&mode=folder'
                return SimpleNamespace(toString=lambda:path)
            def downloadFileName(self):
                return 'result.pdf'
            def cancel(self):
                self.cancelled = True
            def accept(self):
                raise AssertionError('Early rejection/redirect must not accept the WebEngine request')
        with tempfile.TemporaryDirectory() as temp:
            existing = Path(temp)/'existing.pdf'
            existing.write_bytes(b'existing')
            def picker(*args):
                return ({2:'',3:str(Path(temp)/'service.pdf'),4:str(Path(temp)/'wrong.docx'),
                         5:str(existing)}.get(mode[0],''), '')
            def notify(*args, **kwargs):
                notices.append((args,kwargs))
                if mode[0] == 6:
                    raise RuntimeError('page was destroyed')
            scope = {'Path':Path,'parse_qs':parse_qs,'urlsplit':urlsplit,
                     'download_filter':lambda *args:'' if mode[0] in (0,6) else 'PDF',
                     'QFileDialog':SimpleNamespace(getSaveFileName=picker),
                     'QMessageBox':SimpleNamespace(StandardButton=SimpleNamespace(Yes=1,No=2),question=lambda *args:2)}
            exec(compile(ast.fix_missing_locations(ast.Module(body=[method],type_ignores=[])),
                         'actual_native_download_early_paths','exec'),scope)
            window=SimpleNamespace(_downloads=[],_server=SimpleNamespace(server_port=1234,
                guard=SimpleNamespace(owns_path=lambda path:mode[0]==3)),_notify_download=notify,
                _save_folder=lambda *args,**kwargs:folders.append((args,kwargs)))
            for index in range(1000):
                mode[0]=index%7
                item=Download(profile)
                if mode[0]==6:
                    with self.assertRaisesRegex(RuntimeError,'page was destroyed'):
                        scope['on_download'](window,item)
                else:
                    scope['on_download'](window,item)
                self.assertTrue(item.cancelled)
            self.assertFalse(window._downloads)
            QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
            app.processEvents()
            self.assertFalse(profile.children(),'Cancelled/redirected requests require deletion before terminal handlers are registered')
            self.assertGreater(len(folders),100)
            self.assertTrue(all(args[0].startswith('http://127.0.0.1/') for args,kwargs in folders))
            self.assertTrue(all(args[2].startswith('http://127.0.0.1/') for args,kwargs in notices))
        profile.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)

    def test_terminal_requests_and_callbacks_leave_profile_ownership(self):
        from PySide6.QtCore import QObject, Signal, QCoreApplication, QEvent
        app = QCoreApplication.instance() or QCoreApplication([])
        source = ast.parse((ROOT/'app_backend/desktop.py').read_text())
        desktop = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == 'MainWindow')
        method = next(node for node in desktop.body if isinstance(node, ast.FunctionDef) and node.name == '_on_download_state')
        module = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
        class DownloadState(Enum):
            DownloadRequested = 0
            DownloadInProgress = 1
            DownloadCompleted = 2
            DownloadCancelled = 3
            DownloadInterrupted = 4
        scope = {'QWebEngineDownloadRequest':SimpleNamespace(DownloadState=DownloadState), 'urlsplit':urlsplit}
        exec(compile(module, 'actual_native_download_lifecycle', 'exec'), scope)
        profile = QObject()
        class Download(QObject):
            stateChanged = Signal(object)
        notices = []
        window = SimpleNamespace(_downloads=[], _notify_download=lambda *args:notices.append(args))
        callback = scope['_on_download_state']
        for index in range(1000):
            item = Download(profile)
            window._downloads.append(item)
            item.stateChanged.connect(lambda state, d=item: callback(window,d,state,'http://127.0.0.1/api/converter/result',Path('absent.pdf')))
            item.stateChanged.emit(DownloadState.DownloadInProgress)
            self.assertIn(item, window._downloads)
            item.stateChanged.emit(DownloadState.DownloadCancelled if index%2 else DownloadState.DownloadInterrupted)
        self.assertFalse(window._downloads)
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        self.assertFalse(profile.children(), 'Terminal QWebEngine requests/callbacks must not accumulate under the profile')
        self.assertEqual(len(notices), 1000)
        profile.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_terminal_request_is_released_even_when_notification_raises(self):
        source = ast.parse((ROOT/'app_backend/desktop.py').read_text())
        desktop = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == 'MainWindow')
        method = next(node for node in desktop.body if isinstance(node, ast.FunctionDef) and node.name == '_on_download_state')
        scope = {'QWebEngineDownloadRequest':SimpleNamespace(DownloadState=SimpleNamespace(
            DownloadCompleted=2, DownloadCancelled=3, DownloadInterrupted=4)), 'urlsplit':urlsplit}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), 'actual_native_download_lifecycle', 'exec'), scope)
        released=[]
        item=SimpleNamespace(stateChanged=SimpleNamespace(disconnect=lambda:released.append('disconnect')),deleteLater=lambda:released.append('delete'))
        def fail(*args):
            raise RuntimeError('page was destroyed')
        window=SimpleNamespace(_downloads=[item],_notify_download=fail)
        with self.assertRaises(RuntimeError):
            scope['_on_download_state'](window,item,3,'http://127.0.0.1/api/converter/result',Path('absent.pdf'))
        self.assertFalse(window._downloads)
        self.assertEqual(released,['disconnect','delete'])
