"""Production Qt/WebEngine + HTTP + native conversion completion acceptance.

Run with --output; optionally --xhtml-zip to exercise 32 original-source copies
through the actual bulk UI. --record-only captures pre-fix geometry without
claiming acceptance. Browser zoom and Qt devicePixelRatio are reported separately.
This source-shell driver does not claim an installed Windows GUI run.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import struct
import sys
import threading
import time
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from PySide6.QtWidgets import QApplication, QFileDialog
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from app_backend.server import start_server
from app_backend.desktop import MainWindow

GEOMETRY = r'''(()=>{
 const q=s=>document.querySelector(s),r=e=>{const b=e.getBoundingClientRect();return {left:b.left,top:b.top,right:b.right,bottom:b.bottom,width:b.width,height:b.height};};
 const fits=(a,b)=>a.left>=b.left-2&&a.right<=b.right+2&&a.top>=b.top-2&&a.bottom<=b.bottom+2;
 const visible=e=>e&&!e.hidden&&getComputedStyle(e).display!=='none'&&getComputedStyle(e).visibility!=='hidden';
 const ws=q('.workspace'),left=q('.aio-cv__pane--left'),right=q('.aio-cv__pane--right'),lr=r(left),rr=r(right),head=r(left.querySelector('.aio-cv__pane-head')),search=r(q('.aio-cv__search')),area=r(q('[data-cv-file-area]'));
 const counter=r(q('[data-cv-count]')),counterBox=r(left.querySelector('.aio-cv__pane-title'));
 const font=e=>{const s=getComputedStyle(e);return {family:s.fontFamily,size:s.fontSize,weight:s.fontWeight,numeric:s.fontVariantNumeric};};
 const counterFont=font(q('[data-cv-count]')),headFont=font(left.querySelector('.aio-cv__pane-head'));
 const violations=[],controlElements=['[data-cv-type-filter]','[data-cv-add]','[data-cv-clear-inputs]'].map(q).filter(visible),controls=controlElements.map(r);
 for(const a of controls)if(!fits(a,head)||!fits(a,lr))violations.push('header control outside header/pane');
 for(const a of controls)if(Math.abs((a.top+a.bottom-counter.top-counter.bottom)/2)>1)violations.push('header control below/above file counter');
 if(!fits(counter,counterBox))violations.push('file count outside reserved slot');
 if(JSON.stringify(counterFont)!==JSON.stringify(headFont))violations.push('counter slot ch uses a different font from the number');
 for(const e of controlElements)if(!e.getAttribute('aria-label'))violations.push('header control missing accessible label');
 const labelMetrics=[...left.querySelectorAll('[data-filter-label],.aio-cv__add-label')].map(e=>({text:e.textContent,visible:visible(e),client:e.clientWidth,scroll:e.scrollWidth}));
 for(const e of labelMetrics)if(innerWidth>=1280&&e.visible&&e.scroll>e.client+1)violations.push('desktop header label clipped: '+e.text);
 for(let i=0;i<controls.length;i++)for(let j=i+1;j<controls.length;j++){const a=controls[i],b=controls[j];if(Math.min(a.right,b.right)>Math.max(a.left,b.left)+2&&Math.min(a.bottom,b.bottom)>Math.max(a.top,b.top)+2)violations.push('header controls overlap');}
 if(head.bottom>search.top+2||search.bottom>area.top+2)violations.push('header/search/files overlap');
 if(Math.min(lr.right,rr.right)>Math.max(lr.left,rr.left)+2&&Math.min(lr.bottom,rr.bottom)>Math.max(lr.top,rr.top)+2)violations.push('panes overlap');
 for(const e of [left,right,ws])if(e.scrollWidth>e.clientWidth+2)violations.push('horizontal overflow '+e.className);
 if(document.documentElement.scrollHeight>document.documentElement.clientHeight+2)violations.push('document vertical overflow');
 if(document.body.scrollHeight>innerHeight+2&&!['hidden','clip'].includes(getComputedStyle(document.body).overflowY))violations.push('body has an external scrollbar');
 if(document.body.scrollTop!==0)violations.push('focus scrolled the outer body');
 if(document.documentElement.scrollWidth>innerWidth+2)violations.push('document horizontal overflow');
 for(const e of document.querySelectorAll('.aio-cv__category'))if(e.scrollHeight>e.clientHeight+2)violations.push('category label clipped vertically: '+e.textContent);
 const footer=r(q('.aio-cv__footer'));
 for(const s of ['[data-cv-run]','[data-cv-result-actions]','[data-cv-summary]','[data-cv-status]'])if(!fits(r(q(s)),footer)||!fits(r(q(s)),rr))violations.push(s+' outside footer/pane');
 return {violations,viewport:[innerWidth,innerHeight],devicePixelRatio,workspace:{client:ws.clientHeight,scroll:ws.scrollHeight,excess:ws.scrollHeight-ws.clientHeight},document:{client:document.documentElement.clientHeight,scroll:document.documentElement.scrollHeight,bodyScroll:document.body.scrollHeight,bodyScrollTop:document.body.scrollTop,bodyOverflow:getComputedStyle(document.body).overflowY,bodyChildren:[...document.body.children].filter(visible).map(e=>({tag:e.tagName,cls:e.className,rect:r(e),position:getComputedStyle(e).position,client:e.clientHeight,scroll:e.scrollHeight}))},shell:r(q('.app-shell')),appHeader:r(q('.app-header')),appLayout:r(q('.app-layout')),sidebar:r(q('.sidebar')),left:lr,right:rr,header:head,counter,counterBox,counterFont,headFont,controls,labelMetrics,run:r(q('[data-cv-run]')),runVisibility:getComputedStyle(q('[data-cv-run]')).visibility,fileArea:area,fileCount:q('[data-cv-count]').textContent};
})()'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--xhtml-zip', type=Path)
    parser.add_argument('--bulk-count', type=int, default=32)
    parser.add_argument('--record-only', action='store_true')
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    server, _ = start_server(guard_recovery_parent=output/'runtime'/'jobs',
        legacy_recovery_parent=output/'runtime'/'legacy', guard_history_parent=output/'runtime'/'history')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    window = MainWindow(server, server.server_port)
    window.resize(1280, 800)
    window.show()
    evidence = {'status':'RUNNING', 'scope':'production source Qt WebEngine + HTTP + native LibreOffice; controlled save destination',
        'installedWindowsGUI':'NOT_RUN', 'cases':[], 'bulkXhtml':'NOT_REQUESTED'}
    header_references={}

    def pump(seconds=.05):
        end = time.monotonic()+seconds
        while time.monotonic()<end:
            app.processEvents()
            time.sleep(.005)

    def js(expression):
        answers=[]
        window.page.runJavaScript('JSON.stringify((()=>{try{return ('+expression+
            ');}catch(e){return {qaError:String(e),stack:e.stack};}})())',0,answers.append)
        end=time.monotonic()+10
        while not answers and time.monotonic()<end:
            pump(.01)
        if not answers or not isinstance(answers[0],str):
            raise AssertionError('No JSON from WebEngine: '+repr(answers))
        value=json.loads(answers[0])
        if isinstance(value,dict) and 'qaError' in value:
            raise AssertionError(value)
        return value

    def wait(expression, label, timeout=60):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            if js(expression):return
            pump(.05)
        raise AssertionError(label+': '+str(js('document.body.innerText'))[-2500:])

    def click(selector):
        js('(()=>{const e=document.querySelector('+json.dumps(selector)+');if(!e||e.disabled)throw Error("Unavailable control: "+'+json.dumps(selector)+');e.click();return true;})()')

    def add(files, checking_label=None):
        payload=json.dumps([{'name':name,'data':base64.b64encode(data).decode()} for name,data in files],ensure_ascii=False)
        js('(()=>{const dt=new DataTransfer();for(const f of '+payload+')dt.items.add(new File([Uint8Array.from(atob(f.data),c=>c.charCodeAt(0))],f.name));const p=document.querySelector("[data-cv-picker]");p.files=dt.files;p.dispatchEvent(new Event("change",{bubbles:true}));return true;})()')
        if checking_label:
            wait('document.querySelector("[data-cv-add]").disabled','preflight running')
            capture(checking_label,roomy=True)
        wait('!document.querySelector("[data-cv-add]").disabled','native preflight',timeout=480)

    def capture(label, check=True, roomy=False):
        js('(()=>{document.querySelector(".workspace").scrollTop=0;window.scrollTo(0,0);return true;})()')
        pump(.15)
        metric=js(GEOMETRY)
        evidence['cases'].append({'label':label,'metrics':metric})
        window.view.grab().save(str(output/(label+'.png')))
        if check and not args.record_only:
            assert not metric['violations'],metric
            if roomy:assert metric['workspace']['excess']<=2,metric
            key=tuple(metric['viewport'])+(metric['devicePixelRatio'],)
            previous=header_references.get(key)
            if previous:
                for name in ('header','counterBox'):
                    assert all(abs(metric[name][k]-previous['metrics'][name][k])<=1 for k in ('left','top','width','height')),(label,name,previous['label'],metric)
                for current,original in zip(metric['controls'],previous['metrics']['controls']):
                    assert all(abs(current[k]-original[k])<=1 for k in ('left','top','width','height')),(label,'header control moved',previous['label'],metric)
                metric['headerStableTo']=previous['label']
            else:header_references[key]={'label':label,'metrics':metric}
            metric['reachability']={}
            for selector in ('[data-cv-type-filter]','[data-cv-add]','[data-cv-clear-inputs]','[data-cv-run]'):
                hit=js('(()=>{const e=document.querySelector('+json.dumps(selector)+');if(e.hidden||getComputedStyle(e).visibility==="hidden")return {hidden:true};e.scrollIntoView({block:"center",behavior:"instant"});const r=e.getBoundingClientRect(),h=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);return {reachable:h===e||e.contains(h),hit:h?.tagName,rect:{top:r.top,bottom:r.bottom}};})()')
                metric['reachability'][selector]=hit
                assert hit.get('hidden') or hit.get('reachable'),(label,selector,hit)
                if selector=='[data-cv-clear-inputs]':
                    pump(.1)
                    window.view.grab().save(str(output/(label+'-header.png')))
            pump(.1)
            window.view.grab().save(str(output/(label+'-actions.png')))
            js('(()=>{document.querySelector(".workspace").scrollTop=0;window.scrollTo(0,0);return true;})()')
        return metric

    def sizes(prefix):
        for width,height,zoom in [(1440,1000,1),(1280,800,1),(1024,640,1),(800,600,1.5),(640,480,1.5)]:
            window.resize(width,height)
            window.view.setZoomFactor(zoom)
            pump(.25)
            case_metric=capture(prefix+f'-{width}x{height}-zoom{zoom}',roomy=width>=1280 and zoom==1)
            if prefix=='empty' and not args.record_only:
                original=js('document.querySelector("[data-cv-count]").textContent')
                for maximum in ('4096/4096','10000/10000'):
                    js('(()=>{document.querySelector("[data-cv-count]").textContent='+json.dumps(maximum)+';return true;})()')
                    capture(f'max-counter-{maximum.split("/")[0]}-{width}x{height}-zoom{zoom}',roomy=width>=1280 and zoom==1)
                js('(()=>{document.querySelector("[data-cv-count]").textContent='+json.dumps(original)+';return true;})()')
            # The real body portal must remain reachable after body containment.
            js('(()=>{document.querySelector("[data-cv-type-filter]").scrollIntoView({block:"center",behavior:"instant"});return true;})()')
            click('[data-cv-type-filter]')
            wait('document.querySelector("[data-cv-type-filter]").getAttribute("aria-expanded")==="true"','filter open')
            pump(.15)
            popover=js('(()=>{const p=document.querySelector(".aio-cv-filter__popover:not(.aio-cv-flyout--closing)"),b=p.querySelector(".aio-cv-filter__choice-main"),r=p.getBoundingClientRect(),br=b.getBoundingClientRect(),h=document.elementFromPoint(br.left+br.width/2,br.top+br.height/2);return {withinViewport:r.left>=0&&r.top>=0&&r.right<=innerWidth+1&&r.bottom<=innerHeight+1,choiceReachable:h===b||b.contains(h),bodyPortal:p.parentElement===document.body};})()')
            assert all(popover.values()),popover
            click('.aio-cv-filter__popover:not(.aio-cv-flyout--closing) .aio-cv-filter__choice-main')
            window.view.grab().save(str(output/(prefix+f'-filter-{width}x{height}-zoom{zoom}.png')))
            click('[data-cv-type-filter]')
            wait('document.querySelector("[data-cv-type-filter]").getAttribute("aria-expanded")==="false"','filter close')
            wait('!document.querySelector(".aio-cv-filter__popover")','filter portal disposal')
            case_metric['filterPopover']=popover
            if prefix=='one-file':
                # A real Qt Tab key reaches Add from Filter even inside the
                # short, bounded workspace; browser focus scrolls it into view.
                window.view.setFocus()
                pump(.1)
                js('(()=>{document.querySelector("[data-cv-type-filter]").focus();return true;})()')
                QTest.keyClick(window.view.focusProxy() or window.view,Qt.Key.Key_Tab)
                wait('document.activeElement===document.querySelector("[data-cv-add]")','keyboard Tab to Add',timeout=5)
                hit=js('(()=>{const e=document.activeElement,r=e.getBoundingClientRect(),h=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);return {reachable:h===e||e.contains(h)};})()')
                assert hit['reachable'],hit
                case_metric['keyboardTabToAdd']=True
        window.resize(1280,800)
        window.view.setZoomFactor(1)
        pump(.2)

    try:
        wait('!!document.querySelector(".nav-item")','navigation')
        js('(()=>{[...document.querySelectorAll(".nav-item")].find(e=>e.textContent.includes("Конвертер")).click();window.__qaGuardState=null;window.addEventListener("aio-guard-state",e=>window.__qaGuardState=e.detail);return true;})()')
        wait('!!document.querySelector("[data-cv-picker]")&&!document.querySelector("[data-cv-add]").disabled','catalog',timeout=90)
        sizes('empty')
        if args.record_only:
            evidence['status']='RECORDED'
            return
        # Valid native image with Cyrillic filename and 100 DPI, not header-only data.
        width,height=48,32
        pixels=b'\x20\x60\xe0'*width*height
        bmp=b'BM'+struct.pack('<IHHI',54+len(pixels),0,0,54)+struct.pack('<IiiHHIIiiII',40,width,height,1,24,0,len(pixels),3937,3937,0,0)+pixels
        add([('Контрольный_исходник_100dpi.bmp',bmp)])
        sizes('one-file')
        click('#theme-toggle')
        sizes('dark-one-file')
        click('#theme-toggle')
        click('.aio-guard-state-button')
        wait('document.querySelector(".aio-guard-popup").open','State dialog')
        click('[data-g-quality]')
        assert js('window.AllInOneBridge.converter.getQualityCheck()') is True
        click('[data-g-close]')
        js('(()=>{[...document.querySelectorAll(".aio-cv__category")].find(e=>e.textContent==="Изображения").click();return true;})()')
        click('[aria-label="Выбрать PNG"]')
        wait('!document.querySelector("[data-cv-run]").disabled','PNG selectable')
        click('[data-cv-run]')
        wait('window.__qaGuardState?.finished===true&&window.__qaGuardState.succeeded===1','native image export',timeout=180)
        wait('!document.querySelector("[data-cv-save]").disabled&&document.querySelector("[data-cv-run]").hidden','completed UI')
        before=capture('native-ready',roomy=True)
        assert before['runVisibility']=='hidden'
        click('.aio-guard-state-button')
        wait('!!document.querySelector(".aio-guard-task small")','quality evidence')
        report=server.guard.report(js('window.__qaGuardState.jobId'))
        target=report['files'][0]['targets'][0]
        detail=js('document.querySelector(".aio-guard-task small").textContent')
        assert 'Проверки результата:' in detail and target['checkedFeatures'],(detail,target)
        assert 'Границы проверки:' in detail and (target['unknownReasons'] or target['quality'].get('unknown')),(detail,target)
        assert '100 × 100 DPI' in detail,detail
        assert 'Копия на диске: не сохранена' in detail and 'Внешний вид, шрифты и расположение объектов не сравнивались.' in detail,detail
        evidence['quality']={'task':target,'visibleText':detail}
        window.view.grab().save(str(output/'actual-quality-scope.png'))
        click('[data-g-close]')
        destination=output/'saved'/'native-result.png'
        destination.parent.mkdir(exist_ok=True)
        previous=QFileDialog.getSaveFileName
        QFileDialog.getSaveFileName=staticmethod(lambda *a,**k:(str(destination),'PNG (*.png)'))
        try:
            click('[data-cv-save]')
            wait('document.querySelector("[data-cv-status]").classList.contains("aio-cv__saved")','native save acknowledgement',timeout=60)
        finally:QFileDialog.getSaveFileName=previous
        assert destination.read_bytes().startswith(b'\x89PNG\r\n\x1a\n')
        after=capture('native-saved',roomy=True)
        assert all(abs(before['run'][k]-after['run'][k])<=1 for k in ('left','top','width','height')),(before,after)
        evidence['nativeSave']={'bytes':destination.stat().st_size,'sha256':hashlib.sha256(destination.read_bytes()).hexdigest(),
            'realDialogInteraction':False,'runSlotUnchanged':True}
        if args.xhtml_zip:
            assert hashlib.sha256(args.xhtml_zip.read_bytes()).hexdigest()=='fc3fedff2160b65c0b463826e317e284e700d1c45994e809a370bbd7d134988f','Original XHTML corpus ZIP SHA-256 changed'
            click('[data-cv-clear-inputs]')
            wait('document.querySelectorAll(".aio-cv__file[data-file-id]").length===0','clear saved list')
            click('.aio-guard-state-button')
            click('[data-g-quality]')
            assert js('window.AllInOneBridge.converter.getQualityCheck()') is False
            click('[data-g-close]')
            with zipfile.ZipFile(args.xhtml_zip) as archive:
                originals=[archive.read(f'NEW_0{level}_LEVEL_{level}.xhtml') for level in (1,2)]
            bulk=[(f'bulk_{index:03d}_level_{index%2+1}.xhtml',originals[index%2]) for index in range(args.bulk_count)]
            add(bulk,checking_label='bulk-xhtml-checking')
            js('(()=>{[...document.querySelectorAll(".aio-cv__category")].find(e=>e.textContent==="Документы").click();return true;})()')
            prepared=server.guard.report(js('window.__qaGuardState.jobId'))
            pdf=js('(()=>{const b=document.querySelector("[aria-label=\\"Выбрать PDF\\"]");return !!b&&!b.disabled;})()')
            evidence['bulkXhtml']={'count':len(bulk),'originalZipSha256':hashlib.sha256(args.xhtml_zip.read_bytes()).hexdigest(),
                'preparedAll':False,'pdfSelectable':pdf,'report':prepared}
            assert pdf,prepared
            for row in prepared['files']:
                assert (row.get('nativeRoute') or {}).get('state')=='detected',row
            evidence['bulkXhtml']['preparedAll']=True
            capture('bulk-xhtml-prepared',roomy=True)
            click('[aria-label="Выбрать PDF"]')
            click('[data-cv-run]')
            wait('window.__qaGuardState?.finished===true&&window.__qaGuardState.total==='+str(args.bulk_count),'bulk XHTML export',timeout=900)
            state=js('window.__qaGuardState')
            assert state['succeeded']==args.bulk_count and state['failed']==0,state
            evidence['bulkXhtml']['result']={k:state[k] for k in ('state','total','succeeded','failed','completed')}
            capture('bulk-xhtml-ready',roomy=True)
            wait('!document.querySelector("[data-cv-save]").disabled','bulk save ready')
            destination=output/'bulk-saved'
            destination.mkdir(exist_ok=True)
            previous=QFileDialog.getExistingDirectory
            QFileDialog.getExistingDirectory=staticmethod(lambda *a,**k:str(destination))
            try:
                click('[data-cv-save]')
                wait('document.querySelector("[data-cv-status]").classList.contains("aio-cv__saved")','bulk native folder save',timeout=180)
            finally:QFileDialog.getExistingDirectory=previous
            from pypdf import PdfReader
            pdfs=sorted(destination.rglob('*.pdf'))
            assert len(pdfs)==args.bulk_count,(len(pdfs),args.bulk_count)
            outputs=[]
            for pdf in pdfs:
                index=int(pdf.stem.split('bulk_')[1][:3])
                text='\n'.join(page.extract_text() or '' for page in PdfReader(pdf).pages)
                expected=[f'AIO-NEW-20261108-L{index%2+1}','00123','12345678901234567','Москва']
                assert all(marker in text for marker in expected),(pdf.name,expected,text[:1200])
                outputs.append({'name':pdf.name,'bytes':pdf.stat().st_size,'sha256':hashlib.sha256(pdf.read_bytes()).hexdigest(),'markersPreserved':True})
            evidence['bulkXhtml']['savedOutputs']=outputs
            capture('bulk-xhtml-saved',roomy=True)
        evidence['status']='PASS'
    except BaseException as exc:
        evidence['status']='FAIL'
        evidence['error']={'type':type(exc).__name__,'message':str(exc)}
        try:
            evidence['page']=js('document.body.innerText')
            window.view.grab().save(str(output/'failure.png'))
        except Exception:pass
        raise
    finally:
        (output/'evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
        summary={k:v for k,v in evidence.items() if k not in ('quality','cases','page','bulkXhtml')}
        summary['geometryCases']=len(evidence['cases'])
        bulk=evidence['bulkXhtml']
        summary['bulkXhtml']={k:v for k,v in bulk.items() if k not in ('report','savedOutputs')} if isinstance(bulk,dict) else bulk
        print('AIO_COMPLETION_UI='+json.dumps(summary,ensure_ascii=True,default=str),flush=True)
        window.close()
        deadline=time.monotonic()+30
        while not window._shutdown_done and time.monotonic()<deadline:pump(.02)
        assert window._shutdown_done,'Native shutdown did not complete'
        thread.join(timeout=5)
        window.deleteLater()
        pump(.1)
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        pump(.1)


if __name__=='__main__':main()
