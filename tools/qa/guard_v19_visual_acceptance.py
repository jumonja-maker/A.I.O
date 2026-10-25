"""Real QtWebEngine layout smoke; only native conversion is a controlled fixture."""
import argparse
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from PySide6.QtWidgets import QApplication
from app_backend.server import start_server
from app_backend.desktop import MainWindow
from app_backend.converter_guard import ConverterGuard
from app_backend.converter_guard.http_api import GuardAPI
from test_comparison_service_v19 import ControlledNativeAdapter
from test_converter_guard_v02 import wait_finished
from openpyxl import Workbook


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    app=QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory(prefix='aio-v19-visual-') as folder:
        server,token=start_server(guard_recovery_parent=Path(folder)/'initial')
        server.guard_api.close();server.guard.close()
        class Adapter(ControlledNativeAdapter):
            def convert(self,source,target,workspace,**kwargs):
                kwargs['on_started']();directory=workspace/'output';directory.mkdir(parents=True)
                path=directory/(source.stem+'.csv')
                path.write_text('Название,Значение\nТовар,'+('0' if 'Договор' in source.name else '42')+'\n',encoding='utf-8')
                return [path]
        server.guard=ConverterGuard(adapter=Adapter(),spool_parent=folder)
        origin='http://127.0.0.1:'+str(server.server_port)
        server.guard_api=GuardAPI(server.guard,origin=origin,token=token)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        wb=Workbook();wb.active.append(['Название','Значение']);wb.active.append(['Товар',42]);wb.active['B2'].number_format='0.00';data=io.BytesIO();wb.save(data);wb.close();raw=data.getvalue()
        identity=server.guard.create([{'id':str(i),'name':name,'size':len(raw)} for i,name in enumerate(['Договор поставки.xlsx','Сводная таблица.xlsx'])],['csv'],retain_sources=True)['jobId']
        for i in range(2):server.guard.upload(identity,str(i),io.BytesIO(raw),len(raw))
        server.guard.start(identity,archive_mode='on_demand');wait_finished(server.guard,identity)
        window=MainWindow(server,server.server_port);window.show()
        def javascript(code):
            result=[];window.page.runJavaScript(code,lambda value:result.append(value))
            until=time.monotonic()+5
            while not result and time.monotonic()<until:app.processEvents();time.sleep(.01)
            if not result:raise TimeoutError('JavaScript callback')
            return result[0]
        def until(predicate,seconds=20):
            end=time.monotonic()+seconds
            while time.monotonic()<end:
                app.processEvents()
                if predicate():return
                time.sleep(.03)
            raise TimeoutError('Qt comparison UI did not become ready')
        try:
            until(lambda:javascript("typeof window.AIOGuardComparison==='object'"))
            javascript("window.AIOGuardComparison.open("+json.dumps(identity)+");true")
            until(lambda:javascript("document.querySelector('[data-c-progress]')?.textContent.includes('Анализ завершён')"))
            javascript("document.querySelector('.aio-compare-row[data-status=confirmed_loss] button[data-task]')?.dispatchEvent(new MouseEvent('dblclick',{bubbles:true}));true")
            until(lambda:javascript("!!document.querySelector('[data-c-preview] pre')"))
            scenes=[]
            for name,theme,width,height in [('dark','dark',1280,800),('light','light',1280,800),('narrow','dark',900,650)]:
                window.resize(width,height);javascript("document.documentElement.setAttribute('data-theme',"+json.dumps(theme)+");true")
                end=time.monotonic()+.4
                while time.monotonic()<end:app.processEvents();time.sleep(.02)
                path=args.output/(name+'.png');assert window.grab().save(str(path))
                geometry=json.loads(javascript("JSON.stringify((()=>{let d=document.querySelector('.aio-compare-dialog'),b=d.getBoundingClientRect();return {width:innerWidth,height:innerHeight,dialog:{x:b.x,y:b.y,width:b.width,height:b.height},rows:document.querySelectorAll('.aio-compare-row').length,red:document.querySelectorAll('[data-status=confirmed_loss]').length,yellow:document.querySelectorAll('[data-status=format_limitation]').length,preview:document.querySelector('[data-c-preview] pre')?.textContent}})())"))
                assert geometry['dialog']['x']>=0 and geometry['dialog']['y']>=0
                assert geometry['dialog']['x']+geometry['dialog']['width']<=geometry['width']+1
                assert geometry['dialog']['y']+geometry['dialog']['height']<=geometry['height']+1
                print(json.dumps(geometry,ensure_ascii=False),flush=True)
                assert geometry['red'] and geometry['yellow'] and geometry['preview']
                scenes.append({'name':name,'geometry':geometry,'screenshot':path.name})
            from reportlab.pdfgen import canvas
            stream=io.BytesIO();writer=canvas.Canvas(stream,pagesize=(400,300));writer.setFont('Helvetica',24);writer.drawString(30,250,'PDF BEFORE / AFTER');writer.setFillColorRGB(.1,.3,.8);writer.rect(30,40,200,100,fill=1);writer.showPage();writer.drawString(30,200,'PAGE TWO');writer.save();pdf=stream.getvalue()
            original_convert=server.guard.adapter.convert
            def copy_pdf(source,target,workspace,**kwargs):
                kwargs['on_started']();directory=workspace/'output';directory.mkdir(parents=True);path=directory/'sample.pdf';path.write_bytes(source.read_bytes());return[path]
            server.guard.adapter.convert=copy_pdf
            pdf_id=server.guard.create([{'id':'pdf','name':'sample.pdf','size':len(pdf)}],['pdf'],retain_sources=True)['jobId']
            server.guard.upload(pdf_id,'pdf',io.BytesIO(pdf),len(pdf));server.guard.start(pdf_id,archive_mode='on_demand');wait_finished(server.guard,pdf_id)
            window.resize(1280,800);javascript('window.AIOGuardComparison.open('+json.dumps(pdf_id)+');true')
            until(lambda:javascript("document.querySelector('[data-c-progress]')?.textContent.includes('Анализ завершён')"))
            javascript("document.querySelector('.aio-compare-row button[data-task]')?.dispatchEvent(new MouseEvent('dblclick',{bubbles:true}));true")
            until(lambda:javascript("!!document.querySelector('[data-c-preview] img')?.naturalWidth"))
            end=time.monotonic()+2
            while time.monotonic()<end:app.processEvents();time.sleep(.03)
            assert window.grab().save(str(args.output/'pdf.png'))
            pdf_meta=json.loads(javascript("JSON.stringify({naturalWidth:document.querySelector('[data-c-preview] img')?.naturalWidth,iframes:document.querySelectorAll('[data-c-preview] iframe').length})"))
            assert pdf_meta['naturalWidth']>0 and pdf_meta['iframes']==0
            scenes.append({'name':'pdf','metadata':pdf_meta,'screenshot':'pdf.png'})
            evidence={'status':'PASS','scope':'real QtWebEngine local UI and HTTP/isolated analysis; native output fixture controlled','windowsGUI':'NOT_RUN','scenes':scenes}
            (args.output/'evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n');print(json.dumps(evidence,ensure_ascii=False))
        finally:
            window._shutdown_done=True;window.hide();window.page.deleteLater();app.processEvents();server.guard_api.close();server.guard.close();server.shutdown();server.server_close();thread.join(3)


if __name__=='__main__':main()
