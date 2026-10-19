"""Opt-in native Qt icon + real WebEngine DPI/theme asset acceptance."""
import json
import os
from pathlib import Path
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from PySide6.QtCore import QSize
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication
from app_backend.desktop import MainWindow,application_icon
from app_backend.server import start_server
from native_ui_geometry import wait


def main():
    output=Path(os.environ['AIO_BRAND_QA_OUTPUT']).resolve()
    output.mkdir(parents=True,exist_ok=False)
    app=QApplication.instance() or QApplication([])
    app.setApplicationName('A.I.O')
    app.setWindowIcon(application_icon())
    server,_=start_server()
    worker=threading.Thread(target=server.serve_forever,daemon=True)
    worker.start()
    window=MainWindow(server,server.server_port)
    window.resize(1280,850)
    window.show()
    evidence={'scope':'Linux/source native Qt/WebEngine; Windows installer NOT_RUN','scenarios':[]}
    try:
        sizes=sorted({s.width() for s in window.windowIcon().availableSizes()})
        assert sizes==[16,20,24,32,48,64,128,256],sizes
        native=window.windowIcon().pixmap(QSize(256,256),1.0).toImage().convertToFormat(
            QImage.Format.Format_ARGB32_Premultiplied)
        reference=QImage(str(ROOT/'assets/brand/icons/aio-256.png')).convertToFormat(
            QImage.Format.Format_ARGB32_Premultiplied)
        assert native==reference,'Window icon is not the approved application mark'
        evidence['nativeIcon']={'sizes':sizes,'pixelsMatchApprovedMark':True,
                                'applicationIconNotNull':not app.windowIcon().isNull()}
        wait(app,window.page,"document.readyState==='complete'&&document.querySelectorAll('.nav-item').length>=2")
        wait(app,window.page,"!!document.querySelector('.aio-v13-elva-badge img')?.complete")
        for theme,zoom in [('light',1.0),('dark',1.0),('light',1.5),('dark',2.0)]:
            window.view.setZoomFactor(zoom)
            wait(app,window.page,
                 "(()=>{document.documentElement.dataset.theme='"+theme+
                 "';window.AIOBranding.setTheme('"+theme+"');return true;})()")
            wait(app,window.page,r"""(()=>{
                const e=document.querySelector('.aio-v13-elva-badge img');
                const n=e.currentSrc.match(/elva-v13-happy-(\d+)\.png/);
                return e.complete&&e.naturalWidth>0&&n&&
                    Number(n[1])>=e.getBoundingClientRect().width*devicePixelRatio;
            })()""")
            metric=json.loads(wait(app,window.page,"""JSON.stringify((()=>{
                const e=document.querySelector('.aio-v13-elva-badge img'),r=e.getBoundingClientRect();
                const match=e.currentSrc.match(/elva-v13-happy-(\\d+)\\.png/);
                return {source:e.currentSrc,loaded:e.complete&&e.naturalWidth>0,
                    selectedPixels:match?Number(match[1]):0,cssWidth:r.width,cssHeight:r.height,
                    dpr:devicePixelRatio,theme:document.documentElement.dataset.theme,
                    favicon:document.querySelector('#app-favicon').getAttribute('href')};
            })())"""))
            assert metric['loaded'] and metric['selectedPixels']>=metric['cssWidth']*metric['dpr'],metric
            assert metric['favicon']=='assets/brand/icons/aio-64.png',metric
            metric['source']=metric['source'].split('/assets/')[-1]
            evidence['scenarios'].append(metric)
            # WebEngine load/JS callbacks precede the compositor's paint.
            # Give the actual theme and decoded image time to reach the grab.
            window.view.grab()  # request the first offscreen compositor frame
            for _ in range(25):app.processEvents();time.sleep(.03)
            window.view.grab().save(str(output/(theme+'-zoom-'+str(zoom)+'.png')))
        evidence['status']='PASS'
        (output/'evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps(evidence,ensure_ascii=False),flush=True)
    finally:
        window.close()
        worker.join(3)
        for _ in range(5):app.processEvents()


if __name__=='__main__':
    main()
