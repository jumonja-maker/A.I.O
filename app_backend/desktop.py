"""Standalone A.I.O Qt shell; neither old conversion backend nor Advanced is loaded."""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from PySide6.QtCore import QUrl, Qt, QObject, Signal, Slot
from PySide6.QtGui import QIcon, QDesktopServices
from PySide6.QtWidgets import QApplication, QMainWindow, QFileDialog, QMessageBox, QProgressDialog
from PySide6.QtWebEngineCore import (
    QWebEnginePage, QWebEngineProfile, QWebEngineScript,
    QWebEngineDownloadRequest, QWebEngineUrlRequestInterceptor)
from PySide6.QtWebEngineWidgets import QWebEngineView

from .server import start_server, asset_root
from .downloads import download_filter
from .converter_guard.errors import GuardError


class FolderSaveSignals(QObject):
    progress = Signal(str, int, int)
    finished = Signal(str, object)


def application_icon():
    root = asset_root() / 'assets' / 'brand'
    icon = QIcon(str(root / 'icons' / 'aio.ico'))
    if icon.isNull():
        icon = QIcon(str(root / 'icons' / 'aio-256.png'))
    return icon


class OnlyLocalRequests(QWebEngineUrlRequestInterceptor):
    """No document or third-party network fetches inside WebEngine."""
    def __init__(self, port, parent=None):
        super().__init__(parent)
        self.port = port

    def interceptRequest(self, info):
        url = info.requestUrl()
        if url.scheme() in ('blob', 'data'):
            return
        if url.scheme() == 'http' and url.host() == '127.0.0.1' and url.port() == self.port:
            return
        info.block(True)


class AppPage(QWebEnginePage):
    def __init__(self, profile, parent, port):
        super().__init__(profile, parent)
        self.port = port

    def acceptNavigationRequest(self, url, navigation_type, is_main_frame):
        if not is_main_frame:
            return super().acceptNavigationRequest(url, navigation_type, is_main_frame)
        if url.scheme() == 'http' and url.host() == '127.0.0.1' and url.port() == self.port:
            return True
        if url.scheme() == 'https' and navigation_type == self.NavigationType.NavigationTypeLinkClicked:
            QDesktopServices.openUrl(url)
        return False


class MainWindow(QMainWindow):
    def __init__(self, server, port):
        super().__init__()
        self._downloads = []
        self._folder_saves = {}
        self._folder_signals = FolderSaveSignals(self)
        self._folder_signals.progress.connect(self._folder_progress, Qt.ConnectionType.QueuedConnection)
        self._folder_signals.finished.connect(self._folder_finished, Qt.ConnectionType.QueuedConnection)
        self._server = server
        self._gui_observation = None
        if server.telemetry is not None:
            from .qt_observation import observe_gui
            self._gui_observation = observe_gui(self, server.telemetry)
        self.setWindowTitle('A.I.O — All in One помощник')
        self.setWindowIcon(application_icon())
        self.resize(1280, 800)
        self.setMinimumSize(900, 650)

        self.profile = QWebEngineProfile(self)
        self.profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.MemoryHttpCache)
        self.interceptor = OnlyLocalRequests(port, self.profile)
        self.profile.setUrlRequestInterceptor(self.interceptor)
        self.profile.downloadRequested.connect(self.on_download)
        script = QWebEngineScript()
        script.setName('AIO Native Save')
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        script.setRunsOnSubFrames(False)
        script.setSourceCode('window.AllInOneNativeShell = true;')
        self.profile.scripts().insert(script)

        self.view = QWebEngineView(self)
        self.page = AppPage(self.profile, self.view, port)
        # Page destruction releases its engine adapter before QObject children;
        # owning the profile here keeps it alive until the page is finished.
        self.profile.setParent(self.page)
        self.view.setPage(self.page)
        self.setCentralWidget(self.view)
        self.view.load(QUrl(f'http://127.0.0.1:{port}/'))

    def _notify_download(self, status, name, source_url, **extra):
        data = json.dumps({'status':status, 'name':name, 'sourceUrl':source_url, **extra},
                          ensure_ascii=False)
        self.page.runJavaScript(
            "window.dispatchEvent(new CustomEvent('aio-native-save-result',{detail:" +
            data + "}));")

    def on_download(self, item):
        url = item.url().toString()
        name = item.downloadFileName() or 'Объединённая_таблица.xlsx'
        # Native Save supports only outputs produced by the independent
        # Excel/LibreOffice workflow. Never accept remote download URLs.
        extension = Path(name).suffix.lower().lstrip('.')
        dialog_filter = download_filter(url, name, self._server.server_port)
        if not dialog_filter:
            item.cancel()
            self._notify_download('failed', name, url)
            return
        query = parse_qs(urlsplit(url).query)
        if query.get('mode') == ['folder']:
            item.cancel()
            return self._save_folder(url, query['ticket'][0])
        destination, _ = QFileDialog.getSaveFileName(
            self, 'Сохранить результат', name, dialog_filter)
        if not destination:
            item.cancel()
            self._notify_download('cancelled', name, url)
            return
        path = Path(destination)
        if urlsplit(url).path == '/api/converter-guard/result' and self._server.guard.owns_path(path):
            item.cancel()
            self._notify_download('failed',name,url,error='Выберите папку вне служебного хранилища AIO.')
            return
        if path.suffix.lower() != '.' + extension:
            self._notify_download('cancelled', name, url)
            item.cancel()
            return
        if path.exists():
            # Avoid silent overwrite; the file picker may already confirm this
            # on some platforms, but keep the actual decision explicit.
            answer = QMessageBox.question(self, 'Заменить файл?',
                'Файл уже существует. Заменить его?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                item.cancel()
                self._notify_download('cancelled', name, url)
                return
        item.setDownloadDirectory(str(path.parent))
        item.setDownloadFileName(path.name)
        self._downloads.append(item)
        item.stateChanged.connect(lambda state, d=item, u=url, p=path:
            self._on_download_state(d, state, u, p))
        item.accept()

    def _save_folder(self, url, ticket):
        if url in self._folder_saves:
            return  # A duplicate download signal must not replace a live save.
        self._folder_saves[url] = None  # Reserve before the nested picker loop.
        destination = QFileDialog.getExistingDirectory(self, 'Сохранить все результаты в папку')
        if not destination:
            self._folder_saves.pop(url, None)
            self._notify_download('cancelled', '', url)
            return
        cancelled = threading.Event()
        dialog = QProgressDialog('Сохранение результатов…', 'Отменить сохранение', 0, 0, self)
        dialog.setWindowTitle('AIO — сохранение результатов')
        dialog.setWindowModality(Qt.WindowModality.NonModal)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.canceled.connect(cancelled.set)
        dialog.show()
        self._folder_saves[url] = (cancelled, dialog)

        def work():
            last_progress = 0.0
            def progress(current, total):
                nonlocal last_progress
                now = time.monotonic()
                # Keep Qt's event queue bounded during thousands of small
                # copies. Always deliver the exact final count.
                if current == total or now - last_progress >= .1:
                    last_progress = now
                    self._folder_signals.progress.emit(url, current, total)
            try:
                result = self._server.guard.save_folder(ticket, destination, cancel=cancelled,
                    on_progress=progress)
            except GuardError as error:
                result = {'cancelled':error.code == 'SAVE_CANCELLED', 'error':error.message}
            except Exception:
                result = {'error':'Не удалось сохранить результаты. Готовые файлы остаются в AIO.'}
            self._folder_signals.finished.emit(url, result)
        threading.Thread(target=work, daemon=True, name='aio-folder-save').start()

    @Slot(str, int, int)
    def _folder_progress(self, url, current, total):
        operation = self._folder_saves.get(url)
        if operation:
            dialog = operation[1]
            dialog.setMaximum(total)
            dialog.setValue(current)
            dialog.setLabelText('Сохранено файлов: ' + str(current) + ' из ' + str(total))

    @Slot(str, object)
    def _folder_finished(self, url, result):
        operation = self._folder_saves.pop(url, None)
        if operation:
            operation[1].close()
            operation[1].deleteLater()
        status = 'completed' if result.get('saved') else 'cancelled' if result.get('cancelled') else 'failed'
        self._notify_download(status, result.get('name', ''), url,
                              savedAll=result.get('savedAll',False), error=result.get('error',''),
                              snapshotComplete=result.get('snapshotComplete',False),
                              incompleteRecovery=result.get('incompleteRecovery',False),
                              jobRunning=result.get('jobRunning',False),
                              saveId=result.get('saveId'), outputs=result.get('outputs',0),
                              remainingOutputs=result.get('remainingOutputs',0),
                              bytesWritten=result.get('bytesWritten',0))

    def _on_download_state(self, item, state, source_url, path):
        if state == QWebEngineDownloadRequest.DownloadState.DownloadInProgress:
            return
        if state == QWebEngineDownloadRequest.DownloadState.DownloadCompleted:
            if path.exists() and path.is_file():
                self._notify_download('completed', path.name, source_url)
            else:
                self._notify_download('failed', path.name, source_url)
        elif state == QWebEngineDownloadRequest.DownloadState.DownloadCancelled:
            self._notify_download('cancelled', path.name, source_url)
        else:
            self._notify_download('failed', path.name, source_url)
        try:
            self._downloads.remove(item)
        except ValueError:
            pass

    def closeEvent(self, event):
        if self._gui_observation is not None:
            self._gui_observation.stop()
        for operation in self._folder_saves.values():
            if operation:
                operation[0].set()
        self._server.shutdown()
        self._server.server_close()
        super().closeEvent(event)


def launch_desktop():
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName('A.I.O')
    app.setWindowIcon(application_icon())
    server, _token = start_server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    window = MainWindow(server, server.server_port)
    window.show()
    return app.exec()
