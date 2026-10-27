import ast
from pathlib import Path
from types import SimpleNamespace
import unittest


class FullscreenOwnership(unittest.TestCase):
    def test_only_app_origin_can_enter_and_prior_window_state_is_restored(self):
        # Exercise the actual method without importing a GUI on source-only
        # hosts. The separate NIKITA QA drives the real native request/window.
        tree=ast.parse((Path(__file__).resolve().parents[1]/'app_backend/desktop.py').read_text())
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='MainWindow')
        method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='_full_screen_requested')
        scope={};exec(compile(ast.Module(body=[method],type_ignores=[]),'actual-fullscreen-handler','exec'),scope)
        changes=[]
        window=SimpleNamespace(_shutdown_started=False,_server=SimpleNamespace(server_port=1234),_fullscreen_restore=None,
            saveGeometry=lambda:b'prior-geometry',isMaximized=lambda:True,
            showFullScreen=lambda:changes.append('fullscreen'),showNormal=lambda:changes.append('normal'),
            restoreGeometry=lambda value:changes.append(value),showMaximized=lambda:changes.append('maximized'))
        def request(host='127.0.0.1',port=1234,on=True):
            return SimpleNamespace(toggleOn=lambda:on,
                origin=lambda:SimpleNamespace(scheme=lambda:'http',host=lambda:host,port=lambda:port),
                accept=lambda:changes.append('accepted'),reject=lambda:changes.append('rejected'))
        handler=scope['_full_screen_requested']
        handler(window,request('untrusted.example'));self.assertEqual(changes,['rejected']);changes.clear()
        handler(window,request(port=9999));self.assertEqual(changes,['rejected']);changes.clear()
        handler(window,request());self.assertEqual(changes,['accepted','fullscreen']);changes.clear()
        handler(window,request(on=False));self.assertEqual(changes,['accepted','normal',b'prior-geometry','maximized'])
        self.assertIsNone(window._fullscreen_restore)
