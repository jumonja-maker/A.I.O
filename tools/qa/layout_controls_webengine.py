"""Real WebEngine geometry, with the existing synthetic-native HTTP fixture.

This is source GUI QA: not an installer run, native conversion, or OS DPI test.
Browser zoom exercises CSS viewport scaling without claiming Windows DPI coverage.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QApplication
from PySide6.QtWebEngineWidgets import QWebEngineView


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    view = QWebEngineView()
    view.resize(1280, 800)
    view.show()
    evidence = {'status': 'RUNNING', 'scope': 'real Qt WebEngine + production UI/HTTP; synthetic native/save boundaries',
                'installedGUI': 'NOT_RUN', 'windowsDPI': 'NOT_RUN', 'cases': []}

    def pump(seconds=.05):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            app.processEvents()
            time.sleep(.005)

    def js(expression):
        answers = []
        view.page().runJavaScript('JSON.stringify((()=>{try{return (' + expression +
            ');}catch(e){return {qaError:String(e),stack:e.stack};}})())', 0, answers.append)
        end = time.monotonic() + 10
        while not answers and time.monotonic() < end:
            pump(.01)
        if not answers:
            raise AssertionError('WebEngine JS callback timed out')
        if not isinstance(answers[0], str) or not answers[0]:
            raise AssertionError('WebEngine did not return JSON: ' + repr(answers[0]))
        result = json.loads(answers[0])
        if isinstance(result, dict) and 'qaError' in result:
            raise AssertionError(result)
        return result

    def wait(expression, label):
        end = time.monotonic() + 15
        while time.monotonic() < end:
            if js(expression):
                return
            pump(.03)
        raise AssertionError(label + ': ' + str(js("document.querySelector('[data-cv-status]')?.textContent||document.body.innerText")))

    def check(value, message):
        if not value:
            raise AssertionError(message)

    geometry = r'''(()=>{
      const q=s=>document.querySelector(s), rect=e=>{const r=e.getBoundingClientRect();return {left:r.left,top:r.top,right:r.right,bottom:r.bottom,width:r.width,height:r.height};};
      const pane=q('.aio-cv__pane--right'),pr=rect(pane), run=q('[data-cv-run]'), rr=rect(run);
      const violations=[],fits=(a,b)=>a.left>=b.left-2&&a.right<=b.right+2&&a.top>=b.top-2&&a.bottom<=b.bottom+2;
      for(const p of document.querySelectorAll('.aio-cv__pane')){
        if(p.scrollWidth>p.clientWidth+2)violations.push('pane horizontal overflow '+p.className);
      }
      const l=q('.aio-cv__pane--left'),lr=rect(l),head=rect(l.querySelector('.aio-cv__pane-head'));
      const search=rect(l.querySelector('.aio-cv__search')), area=rect(q('[data-cv-file-area]'));
      if(head.bottom>search.top+2||search.bottom>area.top+2)violations.push('header/search/files overlap');
      const controls=['[data-cv-type-filter]','[data-cv-add]','[data-cv-clear-inputs]'].map(s=>rect(q(s)));
      for(const r of controls)if(!fits(r,lr))violations.push('header control outside pane');
      for(let i=0;i<controls.length;i++)for(let j=i+1;j<controls.length;j++){
        const a=controls[i],b=controls[j];if(Math.min(a.right,b.right)>Math.max(a.left,b.left)+2&&Math.min(a.bottom,b.bottom)>Math.max(a.top,b.top)+2)violations.push('header controls overlap');
      }
      for(const s of ['[data-cv-run]','[data-cv-result-actions]','[data-cv-status]'])if(!fits(rect(q(s)),pr))violations.push(s+' outside pane');
      if(lr.right>pr.left+2&&pr.right>lr.left+2&&lr.bottom>pr.top+2&&pr.bottom>lr.top+2)violations.push('panes overlap');
      if(document.documentElement.scrollWidth>innerWidth+2)violations.push('document horizontal overflow');
      const workspace=q('.workspace');if(workspace.scrollWidth>workspace.clientWidth+2)violations.push('workspace horizontal overflow');
      return {violations,viewport:[innerWidth,innerHeight],run:[rr.left-pr.left,rr.top-pr.top,rr.width,rr.height],
        runHidden:run.hidden,runVisibility:getComputedStyle(run).visibility,trashColour:getComputedStyle(q('[data-cv-clear-inputs]')).color,
        pane:pr,footer:q('.aio-cv__footer').innerHTML};
    })()'''

    try:
        loaded = []
        view.loadFinished.connect(loaded.append)
        view.load(QUrl(args.url))
        end = time.monotonic() + 20
        while not loaded and time.monotonic() < end:
            pump(.02)
        check(loaded and loaded[-1], 'application page failed to load')
        wait("!!document.querySelector('.nav-item')", 'navigation')
        js("(()=>{[...document.querySelectorAll('.nav-item')].find(n=>n.textContent.includes('Конвертер')).click();return true;})()")
        wait("!!document.querySelector('[data-cv-picker]')&&!document.querySelector('[data-cv-add]').disabled", 'converter catalog')
        # The actual application theme switch is used; no synthetic CSS theme.
        initial_theme = js("document.documentElement.dataset.theme||'light'")
        for theme in ('light', 'dark'):
            if js("document.documentElement.dataset.theme||'light'") != theme:
                js("(()=>{document.querySelector('#theme-toggle').click();return true;})()")
            wait("(document.documentElement.dataset.theme||'light')===" + json.dumps(theme), 'theme switch')
            check(js("document.querySelector('[data-cv-clear-inputs]').disabled"), 'empty trash must be disabled')
            check(js("!!document.querySelector('[data-cv-clear-inputs] svg[aria-hidden=\"true\"]')"), 'trash must be a monochrome SVG, not emoji')
            muted = js("getComputedStyle(document.querySelector('[data-cv-clear-inputs]')).color")
            js("(()=>{const p=document.querySelector('[data-cv-picker]'),d=new DataTransfer();d.items.add(new File(['BMlayout'],'Контрольный_длинный_исходник.bmp'));p.files=d.files;p.dispatchEvent(new Event('change',{bubbles:true}));return true;})()")
            wait("document.querySelectorAll('.aio-cv__file[data-file-id]').length===1&&!document.querySelector('[data-cv-add]').disabled", 'uploaded input')
            active_colour = js("getComputedStyle(document.querySelector('[data-cv-clear-inputs]')).color")
            check(active_colour != muted, 'active and disabled trash colours must differ')
            check(js(r"(()=>{const c=getComputedStyle(document.querySelector('[data-cv-clear-inputs]')).color.match(/\d+/g).map(Number);return c[0]>c[1]+40&&c[0]>c[2]+40;})()"), 'active trash must be red')
            js("(()=>{document.querySelector('[aria-label=\"Выбрать PDF\"]').click();return true;})()")
            wait("!document.querySelector('[data-cv-run]').disabled", 'PDF selection')
            for width,height,zoom in ((1280,800,1),(1024,640,1),(860,540,1.25),(800,600,1.5),(640,480,1.5)):
                view.resize(width,height)
                view.setZoomFactor(zoom)
                pump(.15)
                # Re-run through the ordinary UI, not direct invocation of handlers.
                js("(()=>{document.querySelector('[data-cv-run]').click();return true;})()")
                wait("!document.querySelector('[data-cv-save]').disabled&&document.querySelector('[data-cv-run]').hidden", 'unsaved completed output')
                before = js(geometry)
                check(not before['violations'], 'before save: ' + repr(before))
                check(before['run'][2] > 0 and before['run'][3] == 48, 'hidden repeat slot must retain its size')
                check(before['runVisibility'] == 'hidden', 'unsaved repeat must remain invisible')
                check(js("(()=>{const b=document.querySelector('[data-cv-run]');b.focus();return document.activeElement!==b;})()"), 'reserved hidden slot cannot take focus')
                # Controlled save boundary, exactly as the existing DOM save tests.
                js("(()=>{window.__qaSaveResolve=null;window.AllInOneBridge.saveExcel=()=>new Promise(r=>window.__qaSaveResolve=r);document.querySelector('[data-cv-save]').click();return true;})()")
                wait("typeof window.__qaSaveResolve==='function'", 'save boundary')
                check(js("document.querySelector('[data-cv-clear-inputs]').disabled"), 'trash disabled while saving')
                check(js("getComputedStyle(document.querySelector('[data-cv-clear-inputs]')).color") == muted, 'busy trash must be neutral')
                js("(()=>{document.querySelector('[data-cv-clear-inputs]').click();window.__qaSaveResolve({cancelled:true});return true;})()")
                wait("!document.querySelector('[data-cv-save]').disabled", 'save retry after cancelled dialog')
                check(js("document.querySelectorAll('.aio-cv__file[data-file-id]').length===1"), 'disabled trash must not remove input')
                cancelled = js(geometry)
                check(all(abs(a-b)<1 for a,b in zip(before['run'],cancelled['run'])), 'cancelled save moved repeat slot')
                js("(()=>{window.__qaSaveResolve=null;document.querySelector('[data-cv-save]').click();return true;})()")
                wait("typeof window.__qaSaveResolve==='function'", 'second save boundary')
                js("(()=>{window.__qaSaveResolve({saved:true});return true;})()")
                wait("document.querySelector('[data-cv-status]').classList.contains('aio-cv__saved')&&!document.querySelector('[data-cv-run]').hidden", 'saved state')
                after = js(geometry)
                check(not after['violations'], 'after save: ' + repr(after))
                check(all(abs(a-b)<1 for a,b in zip(before['run'],after['run'])), 'saving moved repeat slot: '+repr([before['run'],after['run']]))
                check(js("document.querySelector('[data-cv-run]').textContent") == 'Повторить конвертацию', 'repeat label')
                # Long status stays readable by scrolling; must not move buttons.
                js("(()=>{document.querySelector('[data-cv-status]').textContent='Подробное сообщение проверки. '.repeat(100);return true;})()")
                lengthy = js(geometry)
                check(all(abs(a-b)<1 for a,b in zip(after['run'],lengthy['run'])), 'long message moved repeat slot')
                js("(()=>{document.querySelector('[data-cv-run]').scrollIntoView({block:'center'});return true;})()")
                pump(.05)
                check(js("(()=>{const b=document.querySelector('[data-cv-run]'),r=b.getBoundingClientRect(),p=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);return p===b||b.contains(p);})()"), 'repeat inaccessible after scrolling workspace')
                evidence['cases'].append({'theme':theme,'window':[width,height],'zoom':zoom,'cssViewport':after['viewport'],
                    'status':'PASS','noOverlap':True,'repeatSlotUnchanged':True,'cancelledSavePreservedInput':True,
                    'disabledTrashColour':muted,'activeTrashColour':active_colour})
            js("(()=>{document.querySelector('[data-cv-clear-inputs]').click();return true;})()")
            wait("document.querySelectorAll('.aio-cv__file[data-file-id]').length===0&&document.querySelector('[data-cv-clear-inputs]').disabled", 'clear saved input list')
        evidence['status'] = 'PASS'
        evidence['initialTheme'] = initial_theme
    except Exception as exc:
        evidence['status'] = 'FAIL'
        evidence['error'] = str(exc)
        try:
            view.grab().save(str(output / 'failure.png'))
            evidence['page'] = js("document.body.innerText")[0:5000]
        except Exception:
            pass
        raise
    finally:
        (output / 'evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
        print('AIO_LAYOUT_CONTROLS_ACCEPTANCE='+json.dumps(evidence,ensure_ascii=True),flush=True)
        view.close()
        view.deleteLater()
        pump(.1)


if __name__ == '__main__':
    main()
