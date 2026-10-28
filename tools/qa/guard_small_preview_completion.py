"""Supplementary real Qt small-original viewer gate; no product modification."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QImage, QColor, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from app_backend.server import start_server
from app_backend.desktop import MainWindow
from app_backend.converter_guard import ConverterGuard, LibreOfficeAdapter
from app_backend.converter_guard.http_api import GuardAPI


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--executable', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    result = {'status': 'RUNNING', 'scope': 'real source QtWebEngine small original, not installed EXE',
              'scaleFactor': os.environ.get('QT_SCALE_FACTOR', '1'), 'checks': []}
    app = QApplication.instance() or QApplication([])
    server = window = thread = None
    try:
        image = QImage(140, 105, QImage.Format.Format_RGBA8888)
        image.fill(QColor(25, 180, 60))
        painter = QPainter(image)
        painter.fillRect(0, 0, 28, 21, QColor(230, 30, 40))
        painter.fillRect(112, 84, 28, 21, QColor(30, 70, 230))
        painter.end()
        source = args.output / 'small-original-140x105.png'
        assert image.save(str(source))
        original_sha = digest(source)
        server, token = start_server(guard_recovery_parent=args.output / 'initial')
        server.guard_api.close(); server.guard.close()
        server.guard = ConverterGuard(adapter=LibreOfficeAdapter(args.executable),
                                      recovery_parent=args.output / 'owned')
        origin = 'http://127.0.0.1:' + str(server.server_port)
        server.guard_api = GuardAPI(server.guard, origin=origin, token=token)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        payload = source.read_bytes()
        job = server.guard.create([{'id': 'source', 'name': source.name, 'size': len(payload)}],
                                  prepare_only=True, retain_sources=True)['jobId']
        server.guard.upload(job, 'source', io.BytesIO(payload), len(payload))
        server.guard.preflight(job, timeout_seconds=120)
        server.guard.start(job, formats=['png'], file_ids=['source'], only_verified=True, archive_mode='on_demand')
        deadline = time.monotonic() + 120
        while True:
            state = server.guard.status(job)
            if state['finished']: break
            if time.monotonic() >= deadline: raise TimeoutError('small PNG native queue')
            time.sleep(.02)
        assert state['succeeded'] == 1 and state['failed'] == 0, state
        window = MainWindow(server, server.server_port); window.resize(1100, 800); window.show()

        def js(code):
            values = []; window.page.runJavaScript(code, lambda value: values.append(value))
            end = time.monotonic() + 8
            while not values and time.monotonic() < end:
                app.processEvents(); time.sleep(.005)
            if not values: raise TimeoutError('Qt Javascript callback')
            return values[0]

        def until(check, seconds=60):
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                app.processEvents()
                if check(): return
                time.sleep(.02)
            raise TimeoutError('Small viewer condition: ' + str(js("document.querySelector('[data-c-feedback]')?.textContent")))

        def click(selector, double=False):
            value = js("JSON.stringify((()=>{const e=document.querySelector(" + json.dumps(selector) + "),r=e.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2;return{x,y,w:r.width,h:r.height,visible:e.contains(document.elementFromPoint(x,y))}})())")
            point = json.loads(value)
            assert point['w'] > 0 and point['h'] > 0 and point['visible'], (selector, point)
            receiver = window.view.focusProxy() or window.view
            position = receiver.mapFrom(window.view, QPoint(round(point['x']), round(point['y'])))
            method = QTest.mouseDClick if double else QTest.mouseClick
            method(receiver, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, position)
            app.processEvents()

        until(lambda: js('!!window.AIOGuardComparison&&!!window.AIOComparisonViewer'))
        js('window.AIOGuardComparison.open(' + json.dumps(job) + ');true')
        until(lambda: js("document.querySelector('[data-c-progress]')?.textContent.includes('Анализ завершён')"))
        click('[data-c-filter-choice="all"]')
        until(lambda: js("document.querySelectorAll('.aio-compare-row').length===1"))
        click('[data-task]', double=True)
        until(lambda: js("document.querySelectorAll('.aio-pair-pane img').length===2&&[...document.querySelectorAll('.aio-pair-pane img')].every(i=>i.naturalWidth===140&&i.naturalHeight===105)"))
        for theme in ('dark', 'light'):
            js('document.documentElement.setAttribute("data-theme",' + json.dumps(theme) + ');true')
            for scale, width in (('1', 140), ('2', 280), ('4', 560)):
                click('[data-viewer-zoom="' + scale + '"]')
                until(lambda: js("[...document.querySelectorAll('.aio-pair-pane img')].every(i=>i.style.width===" + json.dumps(str(width) + 'px') + ')'), seconds=8)
            click('[data-viewer-zoom="fit"]')
            until(lambda: js("[...document.querySelectorAll('.aio-pair-pane img')].every(i=>parseFloat(i.style.width)>140)"), seconds=8)
            geometry = json.loads(js("JSON.stringify([...document.querySelectorAll('.aio-pair-pane img')].map(i=>{const r=i.getBoundingClientRect(),v=i.closest('.aio-pair-viewport').getBoundingClientRect();return{x:r.x,y:r.y,w:r.width,h:r.height,vx:v.x,vy:v.y,vw:v.width,vh:v.height,nw:i.naturalWidth,nh:i.naturalHeight}}))"))
            assert len(geometry) == 2
            assert abs(geometry[0]['w'] - geometry[1]['w']) <= 1, geometry
            for box in geometry:
                assert box['w'] > 140 and box['h'] > 105 and box['w'] <= 1120, box
                assert box['x'] >= box['vx'] - 1 and box['y'] >= box['vy'] - 1, box
                assert box['x'] + box['w'] <= box['vx'] + box['vw'] + 1, box
                assert box['y'] + box['h'] <= box['vy'] + box['vh'] + 1, box
                assert abs(box['w'] / box['h'] - 140 / 105) < .02, box
            expected = ((230, 30, 40), (25, 180, 60), (30, 70, 230))
            last = []
            end = time.monotonic() + 8
            while time.monotonic() < end:
                app.processEvents(); pixels = window.grab().toImage(); last = []
                for box in geometry:
                    for fx, fy in ((.1, .1), (.5, .5), (.9, .9)):
                        point = window.view.mapTo(window, QPoint(round(box['x'] + box['w'] * fx), round(box['y'] + box['h'] * fy)))
                        x = max(0, min(pixels.width() - 1, round(point.x() * pixels.width() / window.width())))
                        y = max(0, min(pixels.height() - 1, round(point.y() * pixels.height() / window.height())))
                        last.append(pixels.pixelColor(x, y).getRgb()[:3])
                if all(max(abs(a - b) for a, b in zip(got, wanted)) <= 4 for got, wanted in zip(last, expected * 2)):
                    break
                QTest.qWait(50)
            else: raise AssertionError(('uncropped painted corners not confirmed', last, expected))
            result['checks'].append({'theme': theme, 'status': 'PASS', 'sourcePixels': [140, 105],
                'fit': geometry, 'actualPaintedRGB': last, 'zoomWidths': [140, 280, 560],
                'nativeMouse': True, 'bothCornersAndCentreVisible': True})
        assert digest(source) == original_sha
        result.update(status='PASS', sourceSHA256=original_sha, sourceUnchanged=True)
    except BaseException as error:
        result.update(status='FAIL', error=type(error).__name__, reason=str(error)); raise
    finally:
        (args.output / 'evidence.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print('SMALL_VIEWER_RESULT=' + json.dumps(result, ensure_ascii=True), flush=True)
        if window is not None: window.close(); app.processEvents()
        if server is not None:
            if thread is not None: server.shutdown(); thread.join(timeout=5)
            server.server_close(); server.guard_api.close(); server.guard.close()


if __name__ == '__main__': main()
