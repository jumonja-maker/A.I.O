"""Actual QtWebEngine two-pane/4K/frame/filter QA plus native Writer HTML analysis."""
import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[2];sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QImage
from PySide6.QtCore import QPoint,Qt
from PySide6.QtTest import QTest
from app_backend.server import start_server
from app_backend.desktop import MainWindow
from app_backend.converter_guard import ConverterGuard,LibreOfficeAdapter
from app_backend.converter_guard.http_api import GuardAPI
from app_backend.converter_guard.comparison_analysis import analyze_pair
from app_backend.converter_guard.qt_media import _write_apng
from guard_v08_native_acceptance import fixtures

def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()

def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--executable',type=Path,required=True);args=p.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    report={'status':'RUNNING','scope':'source QtWebEngine/real preview codecs + native Writer HTML; not installed EXE or FULLTEST',
            'scaleFactor':os.environ.get('QT_SCALE_FACTOR','1'),'checks':[],'snapshots':[]}
    app=QApplication.instance() or QApplication([]);server=window=thread=None
    try:
        sources=fixtures(args.output/'fixtures');cancel=threading.Event();adapter=LibreOfficeAdapter(args.executable)
        with adapter.batch(args.output/'native-office'):
            source=sources['docx'];before=sha(source)
            proof=adapter.preflight(source,args.output/'writer-inspect',cancel=cancel,timeout=90);assert proof['state']=='detected',proof
            adapter.prepare_source(proof);paths=adapter.convert(source,'html',args.output/'writer-html',cancel=cancel,timeout=120,on_started=lambda:None)
            analysis=analyze_pair(source,paths,'html');assert 'normalized_text' in analysis['checkedFeatures'],analysis
            assert sha(source)==before
            report['checks'].append({'name':'native-writer-html-analysis','status':'PASS','sourceSha256':before,'analysis':analysis})
        server,token=start_server(guard_recovery_parent=args.output/'initial');server.guard_api.close();server.guard.close()
        server.guard=ConverterGuard(adapter=LibreOfficeAdapter(args.executable),recovery_parent=args.output/'owned')
        origin='http://127.0.0.1:'+str(server.server_port);server.guard_api=GuardAPI(server.guard,origin=origin,token=token)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def finished(job):
            deadline=time.monotonic()+120
            while time.monotonic()<deadline:
                state=server.guard.status(job)
                if state['finished']:return state
                time.sleep(.02)
            raise TimeoutError('native conversion queue did not finish')
        def convert(name,data,target):
            job=server.guard.create([{'id':'source','name':name,'size':len(data)}],prepare_only=True,retain_sources=True)['jobId']
            server.guard.upload(job,'source',io.BytesIO(data),len(data));server.guard.preflight(job,timeout_seconds=120)
            server.guard.start(job,formats=[target],file_ids=['source'],only_verified=True,archive_mode='on_demand')
            state=finished(job);assert state['succeeded']==1 and state['failed']==0,state
            return job
        image=QImage(3840,2160,QImage.Format.Format_RGBA8888);image.fill(0x7f20a0f0)
        source=args.output/'native-4k.png';assert image.save(str(source));original=sha(source);job=convert(source.name,source.read_bytes(),'png')
        apng=args.output/'frames.apng';frames=[]
        for colour in (0xffff0000,0xff00ff00,0xff0000ff):
            frame=QImage(320,180,QImage.Format.Format_RGBA8888);frame.fill(colour);frames.append(frame)
        with apng.open('xb') as stream:_write_apng(stream,iter(frames),{'width':320,'height':180,'frames':3,'pages':1,'durationsMs':[100,100,100]},cancel)
        frame_job=convert(apng.name,apng.read_bytes(),'apng')
        window=MainWindow(server,server.server_port);window.resize(1100,800);window.show()
        def js(code):
            values=[];window.page.runJavaScript(code,lambda value:values.append(value));end=time.monotonic()+8
            while not values and time.monotonic()<end:app.processEvents();time.sleep(.005)
            if not values:raise TimeoutError('Qt JavaScript callback')
            return values[0]
        def until(check,seconds=60):
            end=time.monotonic()+seconds
            while time.monotonic()<end:
                app.processEvents()
                if check():return
                time.sleep(.02)
            raise TimeoutError('Qt before/after condition did not become true: '+str(js("document.querySelector('[data-c-feedback]')?.textContent")))
        def click(selector):
            # Native mouse interaction, including the replacement for Qt's
            # unreliable select popup in a modal WebEngine dialog.
            point=json.loads(js("JSON.stringify((()=>{const e=document.querySelector("+json.dumps(selector)+"),r=e.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2;return {x,y,width:r.width,height:r.height,unobscured:e.contains(document.elementFromPoint(x,y))}})())"))
            assert point['width']>0 and point['height']>0 and point['unobscured'],(selector,point)
            receiver=window.view.focusProxy() or window.view
            position=receiver.mapFrom(window.view,QPoint(round(point['x']),round(point['y'])))
            QTest.mouseClick(receiver,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,position)
            app.processEvents()
        def painted_snapshot(name):
            # DOM readiness is not a composited Qt frame. In particular, a
            # fullscreen transition can leave an old backing-store image.
            # Require real visible pixels at both current image coordinates.
            js('window.__aioQAPaint=false;requestAnimationFrame(()=>requestAnimationFrame(()=>window.__aioQAPaint=true));true')
            until(lambda:js('window.__aioQAPaint===true'),seconds=8)
            rgba=image.pixelColor(0,0).getRgb();alpha=rgba[3]/255
            expected=[tuple(round(c*alpha+bg*(1-alpha)) for c in rgba[:3]) for bg in (238,255)]
            end=time.monotonic()+8;last=None
            while time.monotonic()<end:
                app.processEvents()
                points=json.loads(js("JSON.stringify([...document.querySelectorAll('.aio-pair-pane img')].map(i=>{const r=i.getBoundingClientRect(),v=i.closest('.aio-pair-viewport').getBoundingClientRect(),left=Math.max(r.left,v.left,0),right=Math.min(r.right,v.right,innerWidth),top=Math.max(r.top,v.top,0),bottom=Math.min(r.bottom,v.bottom,innerHeight);return {x:(left+right)/2,y:(top+bottom)/2,width:right-left,height:bottom-top}}))"))
                shot=window.grab();pixels=shot.toImage();colours=[]
                for point in points:
                    q=window.view.mapTo(window,QPoint(round(point['x']),round(point['y'])))
                    x=min(pixels.width()-1,max(0,round(q.x()*pixels.width()/window.width())))
                    y=min(pixels.height()-1,max(0,round(q.y()*pixels.height()/window.height())))
                    colours.append(pixels.pixelColor(x,y).getRgb()[:3])
                last={'points':points,'colours':colours,'expected':expected}
                if len(points)==2 and all(p['width']>20 and p['height']>20 for p in points) and all(
                        any(max(abs(a-b) for a,b in zip(colour,wanted))<=4 for wanted in expected) for colour in colours):
                    path=args.output/name;assert shot.save(str(path));raw=path.read_bytes();assert len(raw)<768*1024
                    print('PAIR_QT_SNAPSHOT='+json.dumps({'name':name,'pngBase64':base64.b64encode(raw).decode()}),flush=True)
                    return path,last
                QTest.qWait(50)
            raise AssertionError('Qt frame did not paint the two current images: '+str(last))
        until(lambda:js("!!window.AIOGuardComparison&&!!window.AIOComparisonViewer"))
        js('window.AIOGuardComparison.open('+json.dumps(job)+');true')
        until(lambda:js("document.querySelector('[data-c-progress]')?.textContent.includes('Анализ завершён')"))
        assert js("document.querySelectorAll('.aio-compare-row').length")==0,'checked pair leaked into default problem list'
        assert 'Все результаты' in js("document.querySelector('[data-c-files]').textContent")
        for theme in ('dark','light'):
            js('document.documentElement.setAttribute("data-theme",'+json.dumps(theme)+');true')
            click('[data-c-filter-choice="all"]');until(lambda:js("document.querySelectorAll('.aio-compare-row').length===1"))
            # Focus a real accessible button, then let WebEngine handle a
            # native keyboard event (no programmatic click fallback).
            js("document.querySelector('[data-c-filter-choice=problems]').focus();true")
            QTest.keyClick(window.view.focusProxy() or window.view,Qt.Key.Key_Space)
            until(lambda:js("document.querySelectorAll('.aio-compare-row').length===0"))
            js("document.querySelector('[data-c-filter-choice=all]').focus();true")
            QTest.keyClick(window.view.focusProxy() or window.view,Qt.Key.Key_Space)
            until(lambda:js("document.querySelectorAll('.aio-compare-row').length===1"))
            js("document.querySelector('[data-task]').dispatchEvent(new MouseEvent('dblclick',{bubbles:true}));true")
            until(lambda:js("document.querySelectorAll('.aio-pair-pane img').length===2&&[...document.querySelectorAll('.aio-pair-pane img')].every(i=>i.naturalWidth===3840)"))
            for zoom,width in (('1',3840),('2',7680),('4',15360)):
                click('[data-viewer-zoom="'+zoom+'"]')
                # Native input and runJavaScript use different WebEngine IPC
                # queues. Wait for the exact resulting pixel widths, as for
                # filters/fullscreen; do not replace the native click.
                until(lambda:js("[...document.querySelectorAll('.aio-pair-pane img')].every(i=>i.style.width==="+json.dumps(str(width)+'px')+")"),seconds=8)
            click('[data-viewer-zoom="fit"]');click('[data-viewer-fullscreen]')
            until(lambda:js("document.fullscreenElement===document.querySelector('.aio-pair-viewer')&&document.querySelector('.aio-compare-dialog').classList.contains('aio-compare-maximized')") and window.isFullScreen())
            click('[data-viewer-fullscreen]')
            until(lambda:not js("!!document.fullscreenElement||document.querySelector('.aio-compare-dialog').classList.contains('aio-compare-maximized')") and not window.isFullScreen())
            window.resize(900,650)
            app.processEvents()
            shot,paint=painted_snapshot(theme+'.png')
            geometry=json.loads(js("JSON.stringify((()=>{const d=document.querySelector('.aio-compare-dialog').getBoundingClientRect();return {x:d.x,y:d.y,width:d.width,height:d.height,viewportWidth:innerWidth,viewportHeight:innerHeight,panes:document.querySelectorAll('.aio-pair-pane').length,dpr:devicePixelRatio,previewBottom:document.querySelector('[data-c-preview]').getBoundingClientRect().bottom,reportTop:document.querySelector('[data-c-findings]').getBoundingClientRect().top,reportBottom:document.querySelector('[data-c-findings]').getBoundingClientRect().bottom,actionsTop:document.querySelector('.aio-compare-actions').getBoundingClientRect().top,actionsBottom:document.querySelector('.aio-compare-actions').getBoundingClientRect().bottom}})())"))
            assert geometry['x']>=0 and geometry['y']>=0 and geometry['x']+geometry['width']<=geometry['viewportWidth']+1 and geometry['y']+geometry['height']<=geometry['viewportHeight']+1
            assert geometry['previewBottom']<=geometry['reportTop']+1 and geometry['reportBottom']<=geometry['actionsTop']+1 and geometry['actionsBottom']<=geometry['y']+geometry['height']+1,geometry
            report['snapshots'].append({'name':shot.name,'sha256':sha(shot),'geometry':geometry,'paintedImagePointsVerified':True,'paint':paint})
            report['checks'].append({'name':'qt-two-pane-4k-'+theme,'status':'PASS','nativeMouseFilter':True,'nativeKeyboardFilter':True,'zooms':[100,200,400],'sourcePixels':[3840,2160]})
            window.resize(1100,800)
        js('window.AIOGuardComparison.open('+json.dumps(frame_job)+');true')
        until(lambda:js("document.querySelector('[data-c-progress]')?.textContent.includes('Анализ завершён')"))
        click('[data-c-filter-choice="all"]');until(lambda:js("document.querySelectorAll('.aio-compare-row').length===1"))
        js("document.querySelector('[data-task]').dispatchEvent(new MouseEvent('dblclick',{bubbles:true}));true")
        until(lambda:js("document.querySelectorAll('.aio-pair-pane img').length===2&&[...document.querySelectorAll('.aio-pair-head span')].every(s=>s.textContent==='1 / 3')"))
        click('.aio-pair-pane[data-viewer-side="source"] .aio-pair-head button:last-child')
        until(lambda:js("[...document.querySelectorAll('.aio-pair-head span')].every(s=>s.textContent==='2 / 3')"))
        report['checks'].append({'name':'qt-synchronized-animation-frame-navigation','status':'PASS','frames':3})
        # Actual protected GIF refusal, not a manufactured analysis status.
        # The half-alpha PNG must never become a ready GIF or a fake preview.
        bad=server.guard.create([{'id':'alpha','name':'half-alpha.png','size':source.stat().st_size}],prepare_only=True,retain_sources=True)['jobId']
        server.guard.upload(bad,'alpha',io.BytesIO(source.read_bytes()),source.stat().st_size)
        server.guard.preflight(bad,timeout_seconds=120)
        server.guard.start(bad,formats=['gif'],file_ids=['alpha'],only_verified=True,archive_mode='on_demand');failed=finished(bad)
        assert failed['succeeded']==0 and failed['failed']==1,failed
        target=failed['files'][0]['targets'][0]
        assert target['code']=='MEDIA_GIF_ALPHA_UNREPRESENTABLE',target
        js('window.AIOGuardComparison.open('+json.dumps(bad)+');true')
        until(lambda:js("document.querySelector('[data-c-progress]')?.textContent.includes('Анализ завершён')"))
        click('[data-c-filter-choice="conversion_error"]')
        until(lambda:js("document.querySelectorAll('.aio-compare-row').length===1&&document.querySelector('.aio-compare-kind').textContent==='Ошибка конвертации'"))
        report['checks'].append({'name':'qt-failed-conversion-filter','status':'PASS','code':target['code'],'nativeMouseFilter':True})
        assert sha(source)==original
        report['sourceSha256']=original;report['status']='PASS'
    except Exception as error:
        report.update(status='FAIL',reason=str(error),error=type(error).__name__)
        if window is not None:
            try:
                report['failureView']=json.loads(js("JSON.stringify({images:[...document.querySelectorAll('.aio-pair-pane img')].map(i=>({naturalWidth:i.naturalWidth,width:i.style.width})),zoom:[...document.querySelectorAll('[data-viewer-zoom]')].map(b=>({zoom:b.dataset.viewerZoom,pressed:b.getAttribute('aria-pressed')})),fullscreen:!!document.fullscreenElement})"))
                shot=args.output/'failure.png'
                if window.grab().save(str(shot)) and shot.stat().st_size<768*1024:
                    print('PAIR_QT_SNAPSHOT='+json.dumps({'name':shot.name,'pngBase64':base64.b64encode(shot.read_bytes()).decode()}),flush=True)
            except Exception as capture_error:report['failureCaptureError']=type(capture_error).__name__
        raise
    finally:
        if window is not None:window._shutdown_done=True;window.hide();window.page.deleteLater();app.processEvents()
        if server is not None:
            server.guard_api.close();server.guard.close()
            if thread is not None:server.shutdown();thread.join(3)
            server.server_close()
        (args.output/'evidence.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print('BEFORE_AFTER_SUMMARY='+json.dumps(report,ensure_ascii=True),flush=True)

if __name__=='__main__':main()
