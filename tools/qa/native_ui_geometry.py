"""Real Qt WebEngine geometry and native-download QA; no external network."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from PySide6.QtWidgets import QApplication, QFileDialog
from app_backend.server import start_server
from app_backend.desktop import MainWindow


def wait(app, page, expression, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = [None]
        page.runJavaScript(expression, lambda answer: value.__setitem__(0, answer))
        inner = time.monotonic() + 1
        while value[0] is None and time.monotonic() < inner:
            app.processEvents()
            time.sleep(.01)
        if value[0]:
            return value[0]
        app.processEvents()
        time.sleep(.03)
    raise AssertionError('Qt UI timed out: ' + expression[:200])


METRICS = r'''(() => {
  const rect=s=>{const e=document.querySelector(s);if(!e)return null;const r=e.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height,right:r.right,bottom:r.bottom};};
  const search=document.querySelector('.aio-cv__search');
  return JSON.stringify({
    shell:rect('.app-shell'),left:rect('.aio-cv__pane--left'),right:rect('.aio-cv__pane--right'),
    search:rect('.aio-cv__search'),first:rect('.aio-cv__file'),run:rect('[data-cv-run]'),
    title:rect('.aio-cv__format-head .aio-cv__pane-title'),issues:rect('.aio-cv__format-head .aio-cv-issues-slot'),
    selectAll:rect('[data-cv-select-all-formats]'),categories:rect('[data-cv-categories]'),
    searchVisible:!!search && !search.hidden && getComputedStyle(search).display!=='none',
    tabs:[...document.querySelectorAll('.aio-cv__category')].map(e=>e.textContent),
    fileCount:document.querySelector('[data-cv-count]')?.textContent,
    filterLabel:(()=>{const e=document.querySelector('[data-filter-label]');return {text:e.textContent,client:e.clientWidth,scroll:e.scrollWidth};})(),
    issueLabel:(()=>{const e=document.querySelector('.aio-cv-issues-button');return e&&e.offsetWidth?{text:e.textContent,client:e.clientWidth,scroll:e.scrollWidth}:null;})(),
    scrollbars:[...document.querySelectorAll('.aio-cv__formats,[data-cv-file-area]')].map(e=>getComputedStyle(e).scrollbarWidth)
  });
})()'''


def add(app, page, files):
    payload = json.dumps([{'name': name, 'data': base64.b64encode(data).decode()}
                          for name, data in files], ensure_ascii=False)
    expression = r'''(() => {
      const dt=new DataTransfer();
      for(const item of DATA){dt.items.add(new File([Uint8Array.from(atob(item.data),x=>x.charCodeAt(0))],item.name));}
      const input=document.querySelector('[data-cv-picker]');input.files=dt.files;
      input.dispatchEvent(new Event('change',{bubbles:true}));return true;
    })()'''.replace('DATA', payload)
    wait(app, page, expression)
    wait(app, page, "!document.querySelector('[data-cv-add]').disabled")


def main():
    destination = Path(os.environ['AIO_UI_QA_OUTPUT']).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    server, token = start_server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    window = MainWindow(server, server.server_port)
    window.resize(1440, 1000)
    window.show()
    evidence = {'scope': 'Linux Qt WebEngine; controlled save destination; Windows NOT_RUN', 'scenarios': []}
    try:
        wait(app, window.page, "!!document.querySelector('.nav-item')")
        wait(app, window.page, """(() => {const n=[...document.querySelectorAll('.nav-item')].find(e=>e.textContent.includes('Конвертер'));n.click();return true;})()""")
        wait(app, window.page, "document.querySelectorAll('.aio-cv__category').length>=3 && !document.querySelector('[data-cv-add]').disabled")
        def capture(label):
            metric = json.loads(wait(app, window.page, METRICS))
            assert metric['searchVisible'] and metric['search']['height'] >= 36, metric
            assert 'Изображения' in metric['tabs'], metric
            assert metric['filterLabel']['scroll'] <= metric['filterLabel']['client'], metric['filterLabel']
            if metric['issueLabel']: assert metric['issueLabel']['scroll'] <= metric['issueLabel']['client'], metric['issueLabel']
            evidence['scenarios'].append({'label': label, 'metrics': metric})
            for _ in range(6):
                app.processEvents()
                time.sleep(.03)
            window.view.grab().save(str(destination / (label + '.png')))
            return metric
        empty = capture('empty')
        add(app, window.page, [('one.csv', b'Name,Qty\nA,1\n')])
        one = capture('one-file')
        add(app, window.page, [('two.bmp', b'BM'), ('three.custom', b'custom')])
        three = capture('three-files')
        assert abs(one['search']['y'] - three['search']['y']) <= 1
        assert abs(one['first']['y'] - three['first']['y']) <= 1
        for name in ('Изображения', 'Таблицы', 'Документы', 'Другие'):
            wait(app, window.page, """(() => {const b=[...document.querySelectorAll('.aio-cv__category')].find(e=>e.textContent===NAME);if(!b)return true;b.click();return true;})()""".replace('NAME', json.dumps(name)))
            metric = json.loads(wait(app, window.page, METRICS))
            assert abs(metric['left']['height'] - three['left']['height']) <= 1
            assert abs(metric['right']['height'] - three['right']['height']) <= 1
        wait(app, window.page, """(() => {const b=document.querySelector('.aio-cv-filter__trigger');b.click();return true;})()""")
        wait(app, window.page, "!!document.querySelector('.aio-cv-filter__choice-main')")
        filter_metric = "JSON.stringify((()=>{const e=document.querySelector('.aio-cv-filter__popover');const r=e.getBoundingClientRect();return {hidden:e.hidden,x:r.x,y:r.y,width:r.width,height:r.height};})())"
        before = json.loads(wait(app, window.page, filter_metric))
        wait(app, window.page, "(()=>{document.querySelectorAll('.aio-cv-filter__choice-main')[0].click();return true;})()")
        after = json.loads(wait(app, window.page, filter_metric))
        assert not after['hidden']
        assert abs(before['width'] - after['width']) <= 1
        capture('filter-open')
        wait(app, window.page, "(()=>{document.querySelector('.aio-cv-filter__trigger').click();return true;})()")
        for zoom in (1.0, 1.5):
            window.view.setZoomFactor(zoom)
            for _ in range(8):
                app.processEvents()
                time.sleep(.03)
            capture('zoom-' + str(zoom).replace('.', '-'))
        window.view.setZoomFactor(1.0)
        window.resize(1160,800)
        for _ in range(8):
            app.processEvents();time.sleep(.03)
        capture('narrow-window')
        window.resize(1440,1000)
        # Real native LO -> HTTP ticket -> Qt download -> completion event.
        wait(app, window.page, """(() => {for(const b of [...document.querySelectorAll('.aio-cv__file [data-op="remove"]')])b.click();return true;})()""")
        wait(app, window.page, "!document.querySelector('[data-cv-add]').disabled")
        # Remove sequentially because a fresh inspection locks the list.
        while wait(app, window.page, "JSON.stringify(document.querySelectorAll('.aio-cv__file').length)") != '0':
            wait(app, window.page, "(()=>{document.querySelector('.aio-cv__file [data-op=\"remove\"]').click();return true;})()")
            wait(app, window.page, "!document.querySelector('[data-cv-add]').disabled")
        width, height = 48, 32
        pixels = b'\x20\x60\xe0' * width * height
        bmp = b'BM' + struct.pack('<IHHI', 54 + len(pixels), 0, 0, 54) + struct.pack(
            '<IiiHHIIiiII', 40, width, height, 1, 24, 0, len(pixels), 3780, 3780, 0, 0) + pixels
        add(app, window.page, [('Native_image.bmp', bmp)])
        wait(app, window.page, """(()=>{[...document.querySelectorAll('.aio-cv__category')].find(e=>e.textContent==='Документы').click();document.querySelector('[aria-label="Выбрать PDF"]').click();return true;})()""")
        wait(app, window.page, "(()=>{document.querySelector('[data-cv-run]').click();return true;})()")
        wait(app, window.page, "!document.querySelector('[data-cv-save]').hidden", timeout=40)
        capture('native-ready')
        wait(app,window.page,"getComputedStyle(document.querySelector('.aio-fp-paint')).display==='none' && !document.querySelector('.aio-fp-check').hidden")
        output = destination / 'Native_image.pdf'
        previous = QFileDialog.getSaveFileName
        QFileDialog.getSaveFileName = staticmethod(lambda *args, **kwargs: (str(output), 'PDF (*.pdf)'))
        try:
            wait(app, window.page, "(()=>{document.querySelector('[data-cv-save]').click();return true;})()")
            wait(app, window.page, "document.querySelector('[data-cv-status]').textContent.includes('Файлы сохранены')", timeout=20)
        finally:
            QFileDialog.getSaveFileName = previous
        assert output.read_bytes().startswith(b'%PDF-')
        assert not server.converter.results
        evidence['nativeDownload'] = {'saved': True, 'bytes': output.stat().st_size,
                                     'actualDialogInteraction': False}
        capture('native-saved')
        wait(app,window.page,"getComputedStyle(document.querySelector('.aio-fp-paint')).display==='none' && document.querySelector('.aio-fp-check').hidden")
        evidence['status'] = 'PASS'
        (destination / 'evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
        print(json.dumps({'status': 'PASS', 'scenarios': len(evidence['scenarios']),
                          'nativeDownload': evidence['nativeDownload']}, ensure_ascii=False), flush=True)
    finally:
        window.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == '__main__':
    main()
